from __future__ import annotations

import argparse
from contextlib import closing
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from azwi.auth import Credential, ORG_HELP, PAT_EXPIRATION, PAT_HELP_URL, credentials_path, missing_pat_message, resolve_credential, save_credential
from azwi.config import default_config_path, load_config, resolve_config, save_config, set_defaults
from azwi.errors import AuthError, AzwiError, ConfigError, UsageError
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
        credential = resolve_credential(env, org, credentials_path(path))
        if not credential.pat:
            issues.append(missing_pat_message())
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
    if org and credential.pat and item_id:
        try:
            verification = verify(org, item_id, credential.pat)
        except AzwiError as exc:
            verification = {"status": "failed"}
            issues.append(str(exc))
            code = exc.exit_code
    report = {
        "ready": code == 0, "org": org, "org_source": source, "config_path": str(path),
        "credential_source": credential.source,
        "credentials_path": str(credentials_path(path)),
        "skill": skill, "verification": verification, "next_steps": issues,
    }
    write_report(report, stdout, namespace.format)
    for issue in issues:
        stderr.write(f"ERROR: {issue}\n")
    return code


def setup(argv, *, stdout, stderr, stdin, env, config_path, verify, program) -> int:
    parser = argparse.ArgumentParser(prog=f"{program} setup", description="Save an organization and PAT, verify a work item URL, and install $azure-workitem.")
    parser.add_argument("url", nargs="?", help="Azure DevOps Cloud work item URL")
    parser.add_argument("--org", help="organization name instead of a work item URL")
    parser.add_argument("--skills-dir", type=Path, help="skills root (default: ~/.agents/skills)")
    parser.add_argument("--format", choices=["json", "plain"], default="json", help="result format (default: json)")
    parser.add_argument("--non-interactive", action="store_true", help="never prompt. Use AZWI_PAT or a saved PAT")
    parser.add_argument("--replace-pat", action="store_true", help="prompt for a replacement saved PAT (unset AZWI_PAT first)")
    namespace = parser.parse_args(argv)
    interactive = not namespace.non_interactive and bool(getattr(stdin, "isatty", lambda: False)())
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
    try:
        credential = resolve_credential(env, org, credential_path)
    except AuthError as exc:
        credential = Credential(None, "unavailable")
        issues.append(str(exc))
    entered_pat = None
    if not issues and interactive and (not credential.pat or namespace.replace_pat):
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
        verification = verify(org, item_id, credential.pat)
    if entered_pat:
        try:
            save_credential(org, entered_pat, credential_path)
            credential = Credential(entered_pat, "file")
        except AuthError as exc:
            issues.append(str(exc))
    save_config(set_defaults(raw, org=org), path)
    try:
        skill = install_skill(namespace.skills_dir)
    except AzwiError as exc:
        raise UsageError(f"Organization saved to {path}. Skill installation failed: {exc}") from None
    report = {
        "ready": not issues, "org": org, "config_path": str(path),
        "credential_source": credential.source,
        "credentials_path": str(credential_path),
        "skill": skill, "verification": verification, "next_steps": issues,
    }
    write_report(report, stdout, namespace.format)
    for issue in issues:
        stderr.write(f"ERROR: {issue}\n")
    return 4 if issues else 0
