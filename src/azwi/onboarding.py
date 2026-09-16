from __future__ import annotations

import argparse
from contextlib import closing
import json
import re
import time
from pathlib import Path
from copy import deepcopy
from urllib.parse import unquote, urlsplit

from azwi.auth import (Credential, ORG_HELP, PAT_EXPIRATION, PAT_HELP_URL,
                       credential_entry, credential_lock, save_entry, credentials_path,
                       missing_pat_message, resolve_credential, save_credential)
from azwi.auth_flow import Authentication, METHODS, auth_settings
from azwi.azure_cli import choose, interactive_terminal, tenant_id
from azwi.config import default_config_path, load_config, resolve_config, save_config, set_defaults
from azwi.errors import AuthError, AzureCLIUnavailable, AzwiError, ChooseAuthentication, ConfigError, UsageError
from azwi.skill import install_skill, skill_status


def read_masked_pat(stdin, stderr) -> str:
    # Import only for interactive setup. Keep normal agent commands lightweight.
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import DummyHistory
    from prompt_toolkit.input import create_input
    from prompt_toolkit.output import create_output

    if not stdin.isatty() or not stderr.isatty():
        raise OSError("Masked input requires a terminal")
    with closing(create_input(stdin=stdin)) as terminal_input:
        session = PromptSession(
            input=terminal_input, output=create_output(stdout=stderr),
            history=DummyHistory(), is_password=True, enable_suspend=False,
        )
        return session.prompt("PAT: ")


def normalize_org(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", value):
        raise UsageError("Enter an organization name such as contoso, or supply a supported work item URL.")
    return value.lower()


def parse_work_item_url(value: str) -> tuple[str, int]:
    error = "Expected an HTTPS Azure DevOps work item URL such as https://dev.azure.com/contoso/Payments/_workitems/edit/2195."
    try:
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError
        url = urlsplit(value)
        if url.scheme != "https" or url.username is not None or url.password is not None or url.port not in (None, 443):
            raise ValueError
        parts = [unquote(part) for part in url.path.rstrip("/").split("/")[1:]]
        if len(parts) < 3 or parts[-3:-1] != ["_workitems", "edit"]:
            raise ValueError
        if not re.fullmatch(r"[0-9]+", parts[-1]) or int(parts[-1]) <= 0:
            raise ValueError
        if url.hostname == "dev.azure.com" and len(parts) >= 4:
            org = parts[0]
        elif url.hostname and re.fullmatch(r"[A-Za-z0-9-]+\.visualstudio\.com", url.hostname):
            org = url.hostname.split(".")[0]
        else:
            raise ValueError
        return normalize_org(org), int(parts[-1])
    except (ValueError, UsageError):
        raise UsageError(error) from None


def target(url: str | None, org: str | None) -> tuple[str | None, int | None]:
    explicit = normalize_org(org) if org else None
    if not url:
        return explicit, None
    parsed_org, item_id = parse_work_item_url(url)
    if explicit and explicit != parsed_org:
        raise UsageError("The work item URL organization conflicts with --org. Supply matching values or omit --org.")
    return parsed_org, item_id


def write_report(report: dict, stdout, output_format: str) -> None:
    if output_format == "json":
        stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        for key, value in report.items():
            stdout.write(f"{key}: {json.dumps(value, ensure_ascii=False)}\n")


def check(namespace, *, stdout, stderr, env, config_path, verify) -> int:
    org, item_id = target(namespace.url, namespace.org)
    path = config_path or default_config_path()
    raw = load_config(path)
    resolved = resolve_config(raw, env=env, cli_org=org)
    org = normalize_org(resolved.org) if resolved.org else None
    defaults = raw.get("defaults", {})
    default_org = defaults.get("org") if isinstance(defaults, dict) else None
    source = "argument" if namespace.org or namespace.url else (
        "config" if isinstance(default_org, str) and default_org else "environment" if org else "missing"
    )
    issues = []
    code = 0
    if not org:
        issues.append(ORG_HELP)
        code = 3
    try:
        flow = Authentication(org, raw, env=env, config_path=path, stderr=stderr)
        credential = flow.local()
        if credential.source == "azure-cli":
            flow.cli.executable()
            if not flow.settings.get("tenant") or not flow.settings.get("identity"):
                raise AuthError("Microsoft Entra authentication is incomplete. Run uvx azwi setup --auth entra.")
        elif not credential.pat:
            issues.append(missing_pat_message())
            code = code or 4
        elif credential.expires_on is not None and credential.expires_on <= time.time():
            issues.append(f"The managed PAT has expired. Run uvx azwi setup --org {org} in your terminal.")
            code = code or 4
    except AuthError as exc:
        credential = Credential(None, "unavailable")
        issues.append(str(exc))
        code = code or 4
    try:
        skill = skill_status(namespace.skills_dir)
        skill_ready = skill["installed"] and skill["managed"]
    except AzwiError:
        skill = {"installed": False, "managed": False}
        skill_ready = False
    if not skill_ready:
        issues.append("Managed agent skill is missing or unreadable. Run uvx azwi skill install.")
        code = code or 3
    verification = {"status": "not_checked"}
    if org and credential.source not in ("missing", "unavailable") and item_id:
        try:
            credential = flow.acquire(renew=False)
            verification = verify(org, item_id, credential.client_value())
        except AzwiError as exc:
            verification = {"status": "failed"}
            issues.append(str(exc))
            code = exc.exit_code
    report = {
        "ready": code == 0, "org": org, "org_source": source, "config_path": str(path),
        "credential_source": credential.source,
        "auth_method": "environment" if credential.source == "environment" else auth_settings(raw, org).get("method", "manual-pat"),
        "expires_on": credential.expires_on,
        "credentials_path": str(credentials_path(path)),
        "skill": skill, "verification": verification, "next_steps": issues,
    }
    write_report(report, stdout, namespace.format)
    for issue in issues:
        stderr.write(f"ERROR: {issue}\n")
    return code


def setup(argv, *, stdout, stderr, stdin, env, config_path, verify, program, fallback_work_item_id=None) -> int:
    parser = argparse.ArgumentParser(prog=f"{program} setup", description="Configure authentication, verify access, and install $azure-workitem.")
    parser.add_argument("url", nargs="?", help="Azure DevOps Cloud work item URL")
    parser.add_argument("--org", help="organization name instead of a work item URL")
    parser.add_argument("--skills-dir", type=Path, help="skills root (default: ~/.agents/skills)")
    parser.add_argument("--format", choices=["json", "plain"], default="json", help="result format (default: json)")
    parser.add_argument("--non-interactive", action="store_true", help="never prompt or launch Microsoft sign-in")
    parser.add_argument("--replace-pat", action="store_true", help="prompt for a replacement saved PAT (unset AZWI_PAT first)")
    parser.add_argument("--auth", choices=METHODS, help="authentication method for this organization")
    parser.add_argument("--tenant", type=tenant_id, help="Microsoft Entra tenant ID")
    parser.add_argument("--login-method", choices=["auto", "device-code"], help="Microsoft sign-in method")
    parser.add_argument("--pat-lifetime-days", type=int, help="managed PAT lifetime (default: 30, range: 1..365)")
    parser.add_argument("--renew-before-days", type=int, help="managed PAT renewal window (default: 7, shorter than lifetime)")
    namespace = parser.parse_args(argv)
    interactive = not namespace.non_interactive and interactive_terminal(stdin, stderr)
    if env.get("AZWI_PAT", "").strip() and (namespace.auth not in (None, "environment") or namespace.tenant or namespace.login_method or namespace.pat_lifetime_days is not None or namespace.renew_before_days is not None):
        raise UsageError("Unset AZWI_PAT before configuring another authentication method. AZWI_PAT overrides saved authentication.")
    if not namespace.url and not namespace.org and interactive:
        stderr.write("Paste a work item URL or enter your organization name: ")
        stderr.flush()
        entered = stdin.readline().strip()
        if not entered:
            raise UsageError("Setup cancelled. Supply a work item URL or --org YOUR_ORG.")
        if "://" in entered:
            namespace.url = entered
        else:
            namespace.org = entered
    org, item_id = target(namespace.url, namespace.org)
    item_id = item_id or fallback_work_item_id
    path = config_path or default_config_path()
    raw = load_config(path)
    resolved = resolve_config(raw, env=env, cli_org=org)
    if not resolved.org:
        raise ConfigError(ORG_HELP)
    org = normalize_org(resolved.org)
    issues = []
    credential_path = credentials_path(path)
    if namespace.replace_pat and (not interactive or env.get("AZWI_PAT", "").strip()):
        raise UsageError("To replace a saved PAT, unset AZWI_PAT and run uvx azwi setup --replace-pat in an interactive terminal.")
    flow = Authentication(org, raw, env=env, config_path=path, stdin=stdin, stderr=stderr)
    try:
        credential = Credential(None, "missing") if namespace.auth in ("entra", "environment") else flow.local()
    except AuthError as exc:
        credential = Credential(None, "unavailable")
        issues.append(str(exc))
    method = namespace.auth or ("manual-pat" if namespace.replace_pat else flow.settings.get("method"))
    if credential.source == "environment":
        method = "environment"
    if method is None and credential.pat:
        method = "manual-pat"
    if method is None and interactive and not issues:
        method = choose_method(stdin, stderr)
    method = method or "manual-pat"
    if namespace.replace_pat and namespace.auth not in (None, "manual-pat"):
        raise UsageError("--replace-pat is for a manually supplied PAT. Use setup --auth managed-pat for automatic management.")
    if method not in ("managed-pat", "entra") and (namespace.tenant or namespace.login_method):
        raise UsageError("--tenant and --login-method require managed-pat or entra authentication.")
    if method != "managed-pat" and (namespace.pat_lifetime_days is not None or namespace.renew_before_days is not None):
        raise UsageError("PAT lifetime and renewal options require --auth managed-pat.")

    while method in ("managed-pat", "entra") and not issues:
        try:
            return setup_azure(namespace, method, flow, org, item_id, raw, path,
                               interactive=interactive, stdout=stdout, stderr=stderr, verify=verify)
        except ChooseAuthentication:
            method = choose_method(stdin, stderr)
        except AzureCLIUnavailable as exc:
            if not interactive:
                raise
            stderr.write(str(exc) + "\n")
            method = choose_method(stdin, stderr)

    entered_pat = None
    if method == "environment":
        credential = resolve_credential(env)
    elif method == "manual-pat" and credential.source == "azure-cli":
        credential = resolve_credential(env, org, credential_path)
    if not issues and method == "manual-pat" and interactive and (not credential.pat or namespace.replace_pat):
        stderr.write(
            "\nCreate a PAT with these permissions:\n"
            "  - Work Items: Read\n  - Code: Read\n\n"
            + PAT_EXPIRATION.replace(". ", ".\n") + "\n\n"
            f"PAT instructions:\n  {PAT_HELP_URL}\n\n"
            "The PAT will be saved as plain text with normal inherited permissions:\n"
            f"  {credential_path}\n\n"
            "Paste your PAT, then press Enter. Input is masked with asterisks.\n"
        )
        stderr.flush()
        try:
            entered_pat = read_masked_pat(stdin, stderr).strip()
        except (EOFError, OSError):
            raise AuthError("Masked PAT input is unavailable. " + missing_pat_message()) from None
        if not entered_pat:
            raise UsageError("Setup cancelled. No PAT was entered.")
        credential = Credential(entered_pat, "input")
    if not credential.pat and not issues:
        issues.append(missing_pat_message())
    verification = {"status": "not_checked"}
    # Verify before changing the default org.
    if credential.pat and item_id:
        verification = verify(org, item_id, credential.client_value())
    if entered_pat:
        try:
            save_credential(org, entered_pat, credential_path)
            credential = Credential(entered_pat, "file")
        except AuthError as exc:
            issues.append(str(exc))
    if namespace.auth or flow.settings or method == "environment" and credential.source != "environment":
        # A temporary AZWI_PAT override must not change the saved provider.
        if namespace.auth or credential.source != "environment":
            if method == "manual-pat" and credential.source == "managed-pat" and not entered_pat:
                # Explicitly stop managing this PAT while preserving its secret.
                save_credential(org, credential.pat, credential_path)
                credential = Credential(credential.pat, "file")
            raw.setdefault("orgs", {}).setdefault(org, {})["auth"] = {"method": method}
    save_config(set_defaults(raw, org=org), path)
    try:
        skill = install_skill(namespace.skills_dir)
    except AzwiError as exc:
        raise UsageError(f"Organization saved to {path}. Skill installation failed: {exc}") from None
    report = {
        "ready": not issues, "org": org, "config_path": str(path),
        "credential_source": credential.source,
        "auth_method": method,
        "credentials_path": str(credential_path),
        "skill": skill, "verification": verification, "next_steps": issues,
    }
    write_report(report, stdout, namespace.format)
    for issue in issues:
        stderr.write(f"ERROR: {issue}\n")
    return 4 if issues else 0


def choose_method(stdin, stderr):
    choice = choose(stdin, stderr,
        "\nHow would you like to connect?\n"
        "1. Automatically manage a read-only personal access token\n"
        "   Fast requests. Azure CLI handles creation and renewal.\n"
        "2. Use temporary Microsoft Entra tokens through Azure CLI\n"
        "   Each invocation acquires a token through Azure CLI.\n"
        "3. Enter an existing personal access token\n"
        "4. Use AZWI_PAT from your environment\n"
        "5. Cancel\nChoice [1]: ", {"1", "2", "3", "4", "5"}, "1")
    if choice == "5":
        raise AuthError("Setup cancelled.")
    return {"1": "managed-pat", "2": "entra", "3": "manual-pat", "4": "environment"}[choice]


def setup_azure(namespace, method, flow, org, item_id, raw, path, *, interactive, stdout, stderr, verify):
    settings = dict(flow.settings)
    settings["method"] = method
    if namespace.login_method:
        settings["login_method"] = namespace.login_method
    if namespace.pat_lifetime_days is not None:
        settings["pat_lifetime_days"] = namespace.pat_lifetime_days
    if namespace.renew_before_days is not None:
        settings["renew_before_days"] = namespace.renew_before_days
    if method == "managed-pat":
        settings.setdefault("pat_lifetime_days", 30)
        settings.setdefault("renew_before_days", min(7, max(0, settings["pat_lifetime_days"] - 1)))
    else:
        settings.pop("pat_lifetime_days", None)
        settings.pop("renew_before_days", None)
    pending_config = deepcopy(raw)
    pending_config.setdefault("orgs", {}).setdefault(org, {})["auth"] = settings
    auth_settings(pending_config, org)
    if method == "managed-pat":
        stderr.write(
            f"\nThe read-only PAT is saved as plain text with normal inherited permissions at {flow.path}.\n"
            f"azwi will renew it when used within {settings['renew_before_days']} days of expiration.\n"
            f"Requested lifetime: {settings['pat_lifetime_days']} days, subject to organization policy.\n"
        )
    verification = {"status": "not_checked"}
    def verify_candidate(candidate):
        nonlocal verification
        stderr.write("Verifying the supplied work item...\n" if item_id else "Verifying organization access...\n")
        verification = verify(org, item_id, candidate.client_value())
        if verification.get("identity") and verification["identity"] != settings.get("identity"):
            raise AuthError("The credential belongs to a different Azure DevOps identity.")

    previous = credential_entry(org, flow.path) if method == "managed-pat" else {}
    reusable = (method == "managed-pat" and previous.get("managed") and flow.settings.get("method") == method
                and (namespace.tenant is None or namespace.tenant == previous["tenant"]))
    if reusable:
        flow.settings = settings
        credential = flow.acquire(interactive=interactive, verify=verify_candidate, repair=True)
        if verification["status"] == "not_checked":
            verify_candidate(credential)
        save_config(set_defaults(pending_config, org=org), path)
    else:
        tenant = flow.select_tenant(namespace.tenant, interactive=interactive)
        token, service, identity, name = flow.connect(tenant, interactive=interactive,
            login_method=settings.get("login_method", "auto"), expected_identity=flow.settings.get("identity"))
        stderr.write(f"Signed in as {name}. Checking access to {org}...\n")
        settings.update(tenant=tenant, identity=identity)
        if method == "entra":
            credential = token
            verify_candidate(credential)
            save_config(set_defaults(pending_config, org=org), path)
        else:
            with credential_lock(flow.path):
                if credential_entry(org, flow.path) != previous:
                    raise AuthError("Credentials changed during setup. Retry setup.")
                stderr.write("Creating a read-only token...\n")
                entry = service.create(settings["pat_lifetime_days"])
                entry["identity"] = identity
                credential = Credential(entry["pat"], "managed-pat", expires_on=entry["expires_on"], tenant=tenant)
                saved = False
                try:
                    verify_candidate(credential)
                    save_entry(org, entry, flow.path)
                    saved = True
                    save_config(set_defaults(pending_config, org=org), path)
                except BaseException:
                    if saved:
                        try:
                            save_entry(org, previous, flow.path)
                        except AzwiError:
                            raise AuthError("The new PAT was saved but configuration could not be completed or rolled back. Run setup again. The saved PAT was not revoked.") from None
                    flow.cleanup(service, entry["authorization_id"])
                    raise
                if previous.get("managed") and previous.get("identity") == identity:
                    flow.cleanup(service, previous["authorization_id"])
    try:
        skill = install_skill(namespace.skills_dir)
    except AzwiError as exc:
        raise UsageError(f"Organization and authentication saved. Skill installation failed: {exc}") from None
    write_report({"ready": True, "org": org, "config_path": str(path), "auth_method": method,
                  "credential_source": credential.source, "expires_on": credential.expires_on,
                  "credentials_path": str(flow.path), "skill": skill, "verification": verification,
                  "next_steps": []}, stdout, namespace.format)
    return 0
