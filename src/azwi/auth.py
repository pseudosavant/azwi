from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import os
from pathlib import Path
import tomllib
from typing import Mapping
from uuid import uuid4

from azwi.config import default_config_path, dumps_toml
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
    scheme: str = "Basic"
    expires_on: int | None = None
    tenant: str | None = None
    refresh: object = field(default=None, repr=False, compare=False)

    def client_value(self):
        return self if self.scheme == "Bearer" else self.pat


def _validate_entry(entry):
    allowed = {"pat", "managed", "authorization_id", "expires_on", "tenant", "identity", "retry_after"}
    if not isinstance(entry, dict) or set(entry) - allowed:
        raise ValueError
    if not isinstance(entry.get("pat"), str) or not entry["pat"].strip():
        raise ValueError
    if "managed" in entry and entry["managed"] is not True:
        raise ValueError
    if entry.get("managed"):
        from uuid import UUID
        for key in ("authorization_id", "tenant", "identity"):
            if not isinstance(entry.get(key), str) or UUID(entry[key]).int == 0:
                raise ValueError
        if type(entry.get("expires_on")) is not int or entry["expires_on"] <= 0:
            raise ValueError
        if "retry_after" in entry and (type(entry["retry_after"]) is not int or entry["retry_after"] < 0):
            raise ValueError
    elif set(entry) != {"pat"}:
        raise ValueError


def credentials_path(config_path: Path | None = None) -> Path:
    return (config_path or default_config_path()).with_name("credentials.toml")


def _read_credentials(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        if set(data) - {"orgs"} or not isinstance(data.get("orgs", {}), dict):
            raise ValueError
        for org, entry in data.get("orgs", {}).items():
            if not org or org != org.lower():
                raise ValueError
            _validate_entry(entry)
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


@contextmanager
def credential_lock(path: Path):
    """An OS lock releases on process exit. Never wait while another process signs in."""
    handle = None
    acquired = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.with_name(f".{path.name}.lock").open("a+b")
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
    except OSError:
        if handle:
            handle.close()
        raise AuthError("Cannot lock credentials. Another azwi process may be updating them. Retry setup or set AZWI_PAT.\n" + ENV_HELP) from None
    try:
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def credential_entry(org: str, path: Path) -> dict:
    return dict(_read_credentials(path).get("orgs", {}).get(org.lower(), {}))


def save_credential(org: str, pat: str, path: Path) -> None:
    with credential_lock(path):
        save_entry(org, {"pat": pat.strip()}, path)


def save_entry(org: str, entry: dict, path: Path) -> None:
    """Write one entry while the caller holds credential_lock. Empty removes it."""
    # Ordinary file creation uses inherited ACLs and the process umask.
    # Do not change permissions or require an OS credential store.
    data = _read_credentials(path)
    if entry:
        try:
            _validate_entry(entry)
        except (ValueError, TypeError, AttributeError):
            raise AuthError("Invalid credential metadata. Run uvx azwi setup again.") from None
        data.setdefault("orgs", {})[org.strip().lower()] = entry
    else:
        data.setdefault("orgs", {}).pop(org.strip().lower(), None)
    contents = dumps_toml({"orgs": dict(sorted(data["orgs"].items()))})
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
        "Run uvx azwi setup with a work item URL in your terminal to choose an authentication method, "
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
