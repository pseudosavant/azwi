"""Azure CLI token acquisition and the interactive login handoff."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from uuid import UUID

from azwi.auth import Credential
from azwi.errors import AuthError, AzureCLIUnavailable, ChooseAuthentication, LoginRequired, UsageError
from azwi._windows_job import contain_child_processes

RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
SCOPE = RESOURCE + "/.default"


def tenant_id(value: str) -> str:
    try:
        parsed = UUID(value)
        if not parsed.int:
            raise ValueError
        return str(parsed)
    except (ValueError, TypeError, AttributeError):
        raise UsageError("Tenant must be a nonzero Microsoft Entra tenant ID (UUID).") from None


def interactive_terminal(stdin, stderr) -> bool:
    return all(bool(getattr(stream, "isatty", lambda: False)()) for stream in (stdin, stderr))


def choose(stdin, stderr, text: str, choices: set[str], default: str | None = None) -> str:
    stderr.write(text)
    stderr.flush()
    for _ in range(3):
        line = stdin.readline()
        if not line:
            raise AuthError("Setup cancelled. No input was received.")
        value = line.strip() or default
        if value in choices:
            return value
        stderr.write("Enter one of the listed choices: ")
        stderr.flush()
    raise UsageError("No valid choice was entered. Run setup again.")


class AzureCLI:
    def __init__(self, *, env, stdin=None, stderr=None):
        self.env = dict(env)
        self.env.pop("AZWI_PAT", None)
        self.stdin, self.stderr = stdin, stderr

    def executable(self):
        executable = shutil.which("az", path=self.env.get("PATH", os.defpath))
        if not executable:
            raise AzureCLIUnavailable(
                "Azure CLI is unavailable. Install it from https://aka.ms/installazurecli, "
                "or run setup with --auth manual-pat or --auth environment."
            )
        return executable

    def _capture(self, args):
        process = None
        try:
            contain_child_processes()
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            process = subprocess.Popen(
                [self.executable(), *args, "--output", "json", "--only-show-errors"],
                env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", **options,
            )
            output, diagnostic = process.communicate(timeout=45)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            if process is not None:
                self._stop(process)
            raise AuthError("Azure CLI token acquisition was cancelled or timed out. Retry or run azwi setup in your terminal.") from None
        except OSError:
            raise AuthError("Cannot execute Azure CLI. Check its installation and access in this execution environment.") from None
        if process.returncode:
            # Never forward captured output. It can contain credentials or account data.
            error = diagnostic.lower()
            if any(word in error for word in ("connectionerror", "connection refused", "name resolution", "timed out", "certificate verify failed", "permissionerror")):
                raise AuthError("Azure CLI cannot access its cache or the network. Check this execution environment and retry.")
            if any(word in error for word in ("interactionrequired", "interaction_required", "need user interaction",
                                              "az login", "aadsts50076", "aadsts50079", "aadsts50173",
                                              "aadsts700082", "aadsts70043")):
                raise LoginRequired("Microsoft sign-in is required. Run uvx azwi setup in your terminal.")
            raise AuthError("Azure CLI could not acquire credentials. Check network access, tenant permissions, and Azure CLI configuration.")
        try:
            payload = json.loads(output)
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, TypeError):
            raise AuthError("Azure CLI returned an invalid response. Update Azure CLI and retry setup.") from None

    def current_tenant(self):
        try:
            value = self._capture(["account", "show"]).get("tenantId")
            return tenant_id(value) if value else None
        except LoginRequired:
            return None

    def token(self, tenant: str, *, interactive=False, login_method="auto") -> Credential:
        tenant = tenant_id(tenant)
        try:
            payload = self._capture(["account", "get-access-token", "--tenant", tenant, "--resource", RESOURCE])
        except LoginRequired:
            if not interactive or not interactive_terminal(self.stdin, self.stderr):
                raise
            selected = choose(self.stdin, self.stderr,
                "\nMicrosoft sign-in is required. This also updates your Azure CLI sign-in session.\n"
                "Setup will continue automatically afterward.\n"
                "1. Continue with Microsoft sign-in\n2. Sign in using a device code\n"
                "3. Choose another authentication method\n4. Cancel\nChoice [1]: ",
                {"1", "2", "3", "4"}, "1")
            if selected == "3":
                raise ChooseAuthentication("Choose another authentication method.")
            if selected == "4":
                raise AuthError("Sign-in cancelled. Existing azwi credentials were preserved.")
            self.login(tenant, "device-code" if selected == "2" else login_method)
            # A successful login is not proof that a usable token was returned.
            payload = self._capture(["account", "get-access-token", "--tenant", tenant, "--resource", RESOURCE])
        secret = payload.get("accessToken")
        try:
            expiry = int(payload["expires_on"])
            if (not isinstance(secret, str) or not secret or any(c.isspace() for c in secret)
                    or expiry <= time.time() + 60 or tenant_id(payload.get("tenant")) != tenant
                    or payload.get("tokenType", "Bearer").lower() != "bearer"):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError, UsageError):
            raise AuthError("Azure CLI returned an expired token or an unexpected tenant. Run setup for the intended tenant.") from None
        return Credential(secret, "azure-cli", "Bearer", expiry, tenant)

    def login(self, tenant, method):
        args = [self.executable(), "login", "--tenant", tenant_id(tenant), "--scope", SCOPE,
                "--allow-no-subscriptions", "--output", "none"]
        if method == "device-code":
            args.append("--use-device-code")
        environment = {**self.env, "AZURE_CORE_LOGIN_EXPERIENCE_V2": "off"}
        # Do not suppress warnings. Device code instructions are emitted there.
        environment.pop("AZURE_CORE_ONLY_SHOW_ERRORS", None)
        environment["AZURE_CORE_ONLY_SHOW_ERRORS"] = "false"
        self.stderr.write("\nStarting Microsoft sign-in. Complete the account window or device-code instructions.\nWaiting for Azure CLI...\n")
        self.stderr.flush()
        process = None
        try:
            contain_child_processes()
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            process = subprocess.Popen(args, env=environment, stdin=self.stdin,
                                       stdout=self.stderr, stderr=self.stderr, **options)
            code = self._wait_for_exit(process, timeout=300)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            if process is not None:
                self._stop(process)
            raise AuthError("Microsoft sign-in cancelled or timed out. Run setup again. Existing azwi credentials were preserved.") from None
        except (OSError, ValueError):
            if process is not None:
                self._stop(process)
            raise AuthError("Cannot hand the terminal to Azure CLI. Run azwi setup in an interactive terminal.") from None
        if code:
            raise AuthError("Microsoft sign-in did not complete. Run setup again or choose another authentication method.")
        self.stderr.write("Sign-in completed. Checking Azure DevOps access...\n")

    @staticmethod
    def _wait_for_exit(process, *, timeout):
        # Windows process waits can defer Python's Ctrl+C handler until they end.
        # Return to Python regularly so cancellation can stop the child tree.
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            try:
                return process.wait(timeout=min(0.2, remaining))
            except subprocess.TimeoutExpired:
                continue

    @staticmethod
    def _stop(process):
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=5)
