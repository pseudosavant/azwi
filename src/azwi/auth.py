from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tomllib
from typing import Mapping
from uuid import uuid4

from azwi.config import default_config_path
from azwi.errors import AuthError


PAT_SCOPES = "Work Items: Read and Code: Read"
PAT_EXPIRATION = (
    "Choose an expiration that fits your organization's policy. "
    "A longer lifetime reduces renewal interruptions. "
    "Use a shorter lifetime when the information or environment calls for it."
)
PAT_HELP_URL = "https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate"
ORG_HELP = (
    "Organization is required. Run uvx azwi setup with a work item URL, "
    "or save a default with uvx azwi config set-defaults --org YOUR_ORG. "
    "For https://dev.azure.com/contoso/..., use contoso. "
    "Use --org YOUR_ORG for this request only."
)
ENV_HELP = (
    'PowerShell:\n  $env:AZWI_PAT = "<your-pat>"\n'
    'Bash or zsh:\n  export AZWI_PAT="<your-pat>"\n'
    "Set it in the environment that runs azwi or your agent, then rerun setup. "
    "These commands apply to the current shell and its child processes. "
    "azwi cannot change the parent shell's environment."
)


@dataclass(frozen=True)
class Credential:
    pat: str | None = field(repr=False)
    source: str


def credentials_path(config_path: Path | None = None) -> Path:
    return (config_path or default_config_path()).with_name("credentials.toml")


def _read_credentials(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        if set(data) - {"orgs"} or not isinstance(data.get("orgs", {}), dict):
            raise ValueError
        for org, entry in data.get("orgs", {}).items():
            if not org or org != org.lower() or not isinstance(entry, dict) or set(entry) != {"pat"}:
                raise ValueError
            if not isinstance(entry["pat"], str) or not entry["pat"].strip():
                raise ValueError
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise AuthError(
            f"Cannot read credentials at {path}. Check file access and TOML syntax. "
            "Use [orgs.ORG] tables with a non-empty pat string. "
            "You can bypass this file by setting AZWI_PAT.\n" + ENV_HELP
        ) from None


def resolve_credential(env: Mapping[str, str], org: str | None = None, path: Path | None = None) -> Credential:
    pat = env.get("AZWI_PAT", "").strip()
    if pat:
        return Credential(pat, "environment")
    if org:
        data = _read_credentials(path or credentials_path())
        pat = data.get("orgs", {}).get(org.strip().lower(), {}).get("pat", "").strip()
    return Credential(pat or None, "file" if pat else "missing")


def save_credential(org: str, pat: str, path: Path) -> None:
    # Ordinary file creation uses inherited ACLs and the process umask.
    # Do not change permissions or require an OS credential store.
    data = _read_credentials(path)
    if not pat.strip():
        raise AuthError("PAT cannot be empty. Run uvx azwi setup again.")
    data.setdefault("orgs", {})[org.strip().lower()] = {"pat": pat.strip()}
    contents = "\n\n".join(
        f"[orgs.{json.dumps(name, ensure_ascii=False)}]\npat = {json.dumps(entry['pat'], ensure_ascii=False)}"
        for name, entry in sorted(data["orgs"].items())
    ) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        raise AuthError(f"Cannot save credentials at {path}. Set AZWI_PAT instead.\n" + ENV_HELP) from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def missing_pat_message() -> str:
    return (
        "AZWI_PAT is not set and no saved PAT is available for this organization.\n"
        "Run uvx azwi setup with a work item URL in your terminal to enter and save a PAT, "
        "or set AZWI_PAT in this execution environment.\n"
        f"Create a PAT for your organization with {PAT_SCOPES}.\n"
        f"{PAT_EXPIRATION}\nPAT instructions: {PAT_HELP_URL}\n"
        + ENV_HELP
    )


def require_pat(env: Mapping[str, str], org: str | None = None, path: Path | None = None) -> str:
    credential = resolve_credential(env, org, path)
    if not credential.pat:
        raise AuthError(missing_pat_message())
    return credential.pat
