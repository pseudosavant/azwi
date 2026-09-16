"""Credential selection, identity binding, and managed PAT renewal."""

from __future__ import annotations

from dataclasses import replace
import time

from azwi.auth import (Credential, credential_entry, credential_lock, credentials_path,
                       missing_pat_message, resolve_credential, save_entry)
from azwi.azure_cli import AzureCLI, choose, tenant_id
from azwi.config import default_config_path
from azwi.errors import AuthError, AzwiError, ConfigError, MissingCredential
from azwi.pat_lifecycle import PatLifecycle, discover_tenant

METHODS = ("managed-pat", "entra", "manual-pat", "environment")


def auth_settings(raw, org):
    orgs = raw.get("orgs", {})
    if not isinstance(orgs, dict):
        raise ConfigError("Organization profiles must be a TOML table.")
    profile = orgs.get(org, {}) if org else {}
    value = profile.get("auth", {}) if isinstance(profile, dict) else {}
    if not isinstance(value, dict) or set(value) - {"method", "tenant", "identity", "login_method", "pat_lifetime_days", "renew_before_days"}:
        raise ConfigError("Invalid authentication configuration. Run azwi setup for this organization.")
    result = dict(value)
    if result.get("method", "manual-pat") not in METHODS:
        raise ConfigError("Unknown authentication method. Run azwi setup for this organization.")
    for key in ("tenant", "identity"):
        if key in result:
            try:
                result[key] = tenant_id(result[key])
            except AzwiError:
                raise ConfigError("Invalid authentication identity or tenant in config.") from None
    lifetime = result.get("pat_lifetime_days", 30)
    window = result.get("renew_before_days", min(7, max(0, lifetime - 1)) if type(lifetime) is int else 7)
    if type(lifetime) is not int or not 1 <= lifetime <= 365 or type(window) is not int or not 0 <= window < lifetime:
        raise ConfigError("PAT lifetime must be 1 to 365 days. Renewal window must be shorter than the lifetime.")
    if result.get("login_method", "auto") not in ("auto", "device-code"):
        raise ConfigError("Invalid login method in config.")
    return result


class Authentication:
    def __init__(self, org, raw, *, env, config_path=None, stdin=None, stderr=None):
        self.org, self.raw, self.env = org, raw, env
        self.path = credentials_path(config_path or default_config_path())
        self.stdin, self.stderr = stdin, stderr
        self.cli = AzureCLI(env=env, stdin=stdin, stderr=stderr)
        self.settings = {} if env.get("AZWI_PAT", "").strip() else auth_settings(raw, org)

    def notice(self, text):
        if self.stderr is not None:
            self.stderr.write(text + "\n")

    def local(self):
        """No subprocess, sign-in, network call, or mutation."""
        if self.env.get("AZWI_PAT", "").strip():
            return resolve_credential(self.env)
        method = self.settings.get("method", "manual-pat")
        if method == "environment":
            return Credential(None, "missing")
        if method == "entra":
            return Credential(None, "azure-cli", "Bearer", tenant=self.settings.get("tenant"))
        credential = resolve_credential(self.env, self.org, self.path)
        if method == "managed-pat" and credential.pat:
            entry = credential_entry(self.org, self.path)
            if not entry.get("managed"):
                raise AuthError("The saved PAT is not managed by azwi. Run setup with --auth managed-pat.")
            if any(entry[key] != self.settings.get(key) for key in ("tenant", "identity")):
                raise AuthError("Managed PAT identity does not match configuration. Run setup before continuing.")
            return replace(credential, source="managed-pat", expires_on=entry["expires_on"], tenant=entry["tenant"])
        return credential

    def select_tenant(self, explicit=None, *, interactive=False):
        selected = explicit or self.settings.get("tenant")
        if selected:
            return tenant_id(selected)
        selected = discover_tenant(self.org)
        if selected:
            return tenant_id(selected)
        if not interactive:
            raise AuthError("Could not discover a Microsoft Entra tenant. Run setup with --tenant TENANT_ID.")
        current = self.cli.current_tenant()
        if current:
            answer = choose(self.stdin, self.stderr,
                f"Could not discover the organization's tenant. Use the current Azure CLI tenant {current}? [y/N]: ",
                {"y", "Y", "n", "N"}, "n")
            if answer.lower() == "y":
                return current
        self.notice("Enter the Microsoft Entra tenant ID for this organization:")
        return tenant_id(self.stdin.readline().strip())

    def connect(self, tenant, *, interactive=False, login_method=None, expected_identity=None):
        credential = self.cli.token(tenant, interactive=interactive,
                                    login_method=login_method or self.settings.get("login_method", "auto"))
        service = PatLifecycle(self.org, credential)
        identity, name = service.identity()
        if expected_identity and identity != expected_identity:
            raise AuthError("Azure CLI is signed in as a different Azure DevOps account. Sign in with the original account, then rerun setup. No PAT was changed.")
        return credential, service, identity, name

    def acquire(self, *, interactive=False, verify=None, renew=True, repair=False):
        local = self.local()
        if local.source == "environment":
            return local
        method = self.settings.get("method", "manual-pat")
        if method == "entra":
            if not self.settings.get("tenant") or not self.settings.get("identity"):
                raise MissingCredential("Microsoft Entra authentication is not configured. Run uvx azwi setup --auth entra.")
            token, _, _, _ = self.connect(self.settings["tenant"], interactive=interactive,
                                          expected_identity=self.settings["identity"])
            return replace(token, refresh=lambda: self.acquire(interactive=False, renew=False))
        if not local.pat:
            raise MissingCredential(missing_pat_message())
        if method != "managed-pat":
            return local
        entry = credential_entry(self.org, self.path)
        now = time.time()
        window = self.settings.get("renew_before_days", min(7, self.settings.get("pat_lifetime_days", 30) - 1)) * 86400
        if not renew:
            if entry["expires_on"] <= now:
                raise AuthError("The managed PAT has expired. Run uvx azwi setup in your terminal.")
            return local
        if entry["expires_on"] > now + window:
            return local
        if not repair and entry.get("retry_after", 0) > now and entry["expires_on"] > now:
            return local
        try:
            # A still-valid PAT must not interrupt a fetch with sign-in UI.
            _, service, _, _ = self.connect(entry["tenant"],
                interactive=interactive and (repair or entry["expires_on"] <= now), expected_identity=entry["identity"])
            with credential_lock(self.path):
                current = credential_entry(self.org, self.path)
                if current != entry:
                    if current.get("managed") and current.get("expires_on", 0) > time.time() and all(current[k] == entry[k] for k in ("tenant", "identity")):
                        return Credential(current["pat"], "managed-pat", expires_on=current["expires_on"])
                    raise AuthError("Credentials changed during renewal. Retry the command.")
                lifetime = self.settings.get("pat_lifetime_days", 30)
                if entry["expires_on"] > time.time():
                    renewed = {**entry, "expires_on": service.extend(entry["authorization_id"], lifetime)}
                    renewed.pop("retry_after", None)
                    save_entry(self.org, renewed, self.path)
                    return Credential(renewed["pat"], "managed-pat", expires_on=renewed["expires_on"])
                if verify is None:
                    raise AuthError("The managed PAT has expired. Run setup with a work item URL to verify its replacement.")
                replacement = service.create(lifetime)
                replacement["identity"] = entry["identity"]
                try:
                    candidate = Credential(replacement["pat"], "managed-pat", expires_on=replacement["expires_on"])
                    verify(candidate)
                    save_entry(self.org, replacement, self.path)
                except BaseException:
                    self.cleanup(service, replacement["authorization_id"])
                    raise
                self.cleanup(service, entry["authorization_id"])
                return candidate
        except AzwiError:
            if repair or entry["expires_on"] <= time.time():
                raise
            try:
                with credential_lock(self.path):
                    if credential_entry(self.org, self.path) == entry:
                        save_entry(self.org, {**entry, "retry_after": int(time.time()) + 3600}, self.path)
            except AzwiError:
                pass
            self.notice(f"NOTICE: PAT renewal could not complete. Using the still-valid PAT. Run uvx azwi setup --org {self.org} in your terminal. Renewal retries after one hour.")
            return local

    def cleanup(self, service, identifier):
        try:
            service.revoke(identifier)
        except AzwiError:
            self.notice(f"NOTICE: Could not revoke azwi PAT {identifier}. Remove it on your Azure DevOps Personal access tokens page.")
