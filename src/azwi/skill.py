from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml
from packaging.version import InvalidVersion, Version
from yaml.nodes import MappingNode, Node, ScalarNode

from azwi import __version__
from azwi.errors import UsageError
from azwi.runtime import DISTRIBUTION_NAME, is_development_build


SKILL_NAME = "azure-workitem"
MANAGED_MARKER = "<!-- managed-by: azwi -->"
FORCE_INSTALL_COMMAND = "uvx azwi skill install --force"
SKILLS_DIR_HELP = "use only this root (default: ~/.agents/skills plus skills under an existing Claude Code profile)"
HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


_SKILL_TEMPLATE = f"""---
name: azure-workitem
description: Fetch and inspect Azure DevOps work items by numeric ID or Azure DevOps Cloud work item URL using azwi. Use when the user invokes `$azure-workitem`, asks for work item details, comments, attachments, linked pull requests, or PR review comments, or wants work item attachments or images downloaded.
metadata:
  managed-by: {DISTRIBUTION_NAME}
  managed-version: ""
  managed-content-sha256: ""
---

# Azure Work Item

Use `uvx azwi` to fetch deterministic Azure DevOps work item context. Default output is JSON. Parse it and answer the user's request instead of dumping the full payload unless raw output is requested.

## Resolve The Input

- For a numeric ID, use it directly: `uvx azwi <id> --non-interactive`.
- For `https://dev.azure.com/<org>/.../_workitems/edit/<id>`, extract the numeric segment immediately after `_workitems/edit/` and call `uvx azwi <id> --org "<org>" --non-interactive`.
- For `https://<org>.visualstudio.com/.../_workitems/edit/<id>`, extract the same numeric segment and call `uvx azwi <id> --org "<org>" --non-interactive`.
- Reject unrelated URLs or ambiguous numbers instead of guessing.

For fetches, keep URL handling in the skill. The `azwi` fetch command itself accepts a numeric work item ID. Setup and config checks also accept work item URLs.

## Fetch And Respond

Fetch the default JSON payload:

```text
uvx azwi <id> --non-interactive
```

Use `--org "<org>"` when the input URL supplies the organization. Summarize the fields relevant to the request and preserve useful work item or PR links. Use `--format markdown` only when the user asks for Markdown or prompt-ready raw context.

Authentication uses a non-empty `AZWI_PAT` first, then the organization's configured method. Managed and manual PATs use `~/.azwi/credentials.toml`. Entra mode uses the Azure CLI session. Always pass `--non-interactive` for fetch and fields commands, even when a pseudo-terminal is available. Let azwi read credentials internally. Resolve the organization from `--org`, `AZWI_ORG`, or `~/.azwi/config.toml`. Never read or print the credentials file through agent tools, copy PAT values into config or generated files, or ask the user to paste a PAT into the conversation.

For first-time setup, direct the user to run `uvx azwi setup "<work-item-url>"` in their terminal. It offers managed PAT, temporary Entra tokens, a masked manual PAT, or environment authentication. Azure CLI sign-in runs in the user terminal and setup resumes automatically. Setup verifies access, saves the configuration, and installs this skill. For managed PAT or Entra sign-in repair, direct the user to rerun setup in their terminal. For an expired manual PAT, add `--replace-pat`. AZWI_PAT overrides saved authentication and must be unset before configuring another method. Never start `az login` or copy Azure CLI tokens through agent tools.

For missing configuration or credentials, run `uvx azwi config check` or `uvx azwi config check "<work-item-url>"` to report readiness and next steps. Checks do not modify azwi files, launch sign-in, or renew PATs. A URL check in Entra mode may silently acquire a token and let Azure CLI update its own cache. A terminal and a sandboxed or remote agent can have different home directories, file access, or environment variables. Do not run interactive setup inside a non-interactive agent command. `setup --non-interactive` never prompts. It can use AZWI_PAT, saved PATs, or a usable Azure CLI session for an explicitly selected or already configured Azure CLI method. If file access is unavailable, direct the user to configure AZWI_PAT in the execution environment.

## Comments And Pull Requests

The default fetch includes normal work item comments, attachment metadata, and linked PR metadata. Keep the current opt-in behavior for higher-volume PR thread data:

```text
uvx azwi <id> --section prs --include-pr-comments --non-interactive
```

- Add `--pr-comment-status all` only when active and resolved PR threads are requested.
- Add `--include-pr-system-comments` only when system comments are requested.
- Use `--comment-limit <n>` when the user requests a different work item comment count. Valid values are 1 through 50.

## Attachments

Download attachments only when requested. If the user supplies a folder, use it. Otherwise use `./azwi-<id>-attachments`:

```text
uvx azwi <id> --download-attachments "./azwi-<id>-attachments" --non-interactive
```

To download selected attachments, first read the exact attachment names or URLs from the JSON metadata, then use repeatable exact-match selectors:

```text
uvx azwi <id> --download-attachments "<dir>" --attachment-name "<name>" --non-interactive
uvx azwi <id> --download-attachments "<dir>" --attachment-url "<url>" --non-interactive
```

Do not download attachments merely because attachment metadata is present.

## Images And File Output

Download rendered text images only when requested. `--download-images` requires `--output`:

```text
uvx azwi <id> --format markdown --output "azwi-<id>.md" --download-images "azwi-<id>-images" --non-interactive
```

Do not add `--force` unless overwriting the output is explicitly allowed.

Stdout is payload only. Treat stderr as diagnostics, progress, and errors.
"""


def default_skills_dir() -> Path:
    return Path.home() / ".agents" / "skills"


def skill_dir(skills_dir: Path | None = None) -> Path:
    return (skills_dir or default_skills_dir()) / SKILL_NAME


def _claude_skills_dir(env: Mapping[str, str] | None) -> Path | None:
    override = (os.environ if env is None else env).get("CLAUDE_CONFIG_DIR")
    profile = Path(override) if override else Path.home() / ".claude"
    return profile / "skills" if profile.is_absolute() else None


def _skill_roots(skills_dir: Path | None, env: Mapping[str, str] | None) -> Iterator[Path]:
    if skills_dir is not None:
        yield skills_dir
        return
    primary = default_skills_dir()
    yield primary
    claude = _claude_skills_dir(env)
    try:
        if claude is None or not claude.parent.is_dir():
            return
        if claude == primary or (claude.is_dir() and primary.exists() and primary.samefile(claude)):
            return
        if _is_link(claude.parent):
            raise UsageError(f"refusing linked Claude profile '{claude.parent}'.")
    except OSError as exc:
        raise UsageError(f"cannot inspect Claude skills directory '{claude}': {exc}") from exc
    yield claude


def _location_report(results: list[dict[str, Any]]) -> dict[str, Any]:
    # Existing clients read the primary location's fields at the top level.
    return results[0] if len(results) == 1 else {**results[0], "locations": results}


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _mapping(node: Node | None) -> dict[str, Node]:
    if not isinstance(node, MappingNode):
        raise ValueError("skill front matter and metadata must be YAML mappings")
    result = {}
    for key, value in node.value:
        if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str" or key.value in result:
            raise ValueError("ambiguous skill front matter keys")
        result[key.value] = value
    return result


def _metadata(text: str) -> tuple[dict[str, Node], int]:
    opening = re.match(r"\ufeff?---[ \t]*\n", text)
    if opening is None:
        return {}, 0
    offset = opening.end()
    end = re.search(r"^---[ \t]*(?:\n|$)", text[offset:], re.MULTILINE)
    if end is None:
        raise ValueError("unterminated skill front matter")
    front = _mapping(yaml.compose(text[offset:offset + end.start()], Loader=yaml.SafeLoader))
    return (_mapping(front["metadata"]) if "metadata" in front else {}), offset


def _string(node: Node | None) -> str | None:
    if isinstance(node, ScalarNode) and node.tag == "tag:yaml.org,2002:str":
        return node.value
    return None


def _hash_span(text: str, node: Node | None, offset: int) -> tuple[int, int] | None:
    value = _string(node)
    if value is None or node is None:
        return None
    start, end = offset + node.start_mark.index, offset + node.end_mark.index
    # Only replace this scalar's token, never other matching text or serialized YAML.
    if text[start:end] not in (value, json.dumps(value), "'" + value + "'"):
        return None
    return start, end


def _digest(text: str, span: tuple[int, int]) -> str:
    start, end = span
    empty_hash_text = text[:start] + '""' + text[end:]
    return "sha256:" + hashlib.sha256(_normalize(empty_hash_text).encode("utf-8")).hexdigest()


def render_skill() -> str:
    """Render the single bundled template with the exact CLI runtime version."""
    text = _normalize(_SKILL_TEMPLATE).replace(
        'managed-version: ""', f"managed-version: {json.dumps(__version__)}", 1
    )
    metadata, offset = _metadata(text)
    span = _hash_span(text, metadata["managed-content-sha256"], offset)
    assert span is not None
    start, end = span
    return text[:start] + json.dumps(_digest(text, span)) + text[end:]


@dataclass(frozen=True)
class SkillState:
    path: Path
    content: bytes | None
    managed: bool = False
    version: str | None = None
    version_state: str = "missing"
    integrity: str = "not_applicable"

    @property
    def parsed_version(self) -> Version | None:
        return Version(self.version) if self.version is not None else None


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name == "nt":
        # Path.is_junction() was added in Python 3.12. azwi also supports 3.11.
        try:
            return path.lstat().st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
        except FileNotFoundError:
            pass
    return False


def _validate_path(path: Path) -> None:
    for directory in (path.parent.parent, path.parent):
        if _is_link(directory):
            raise UsageError(f"refusing linked skill directory '{directory}'.")
        if directory.exists() and not directory.is_dir():
            raise UsageError(f"expected a skill directory at '{directory}'.")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise UsageError(f"expected a regular SKILL.md file at '{path}'.")


@contextmanager
def _skill_errors(action: str, path: Path, *, previous_root: Path | None = None) -> Iterator[None]:
    try:
        yield
    except (OSError, ValueError, yaml.YAMLError, UsageError) as exc:
        if isinstance(exc, UsageError):
            message = str(exc)
        else:
            detail = "invalid YAML front matter." if isinstance(exc, yaml.YAMLError) else str(exc)
            message = f"cannot {action} skill at '{path}': {detail}"
        if previous_root is not None:
            message += f' Completed {action} in "{previous_root}". Retry this location with --skills-dir "{path.parent.parent}".'
        raise UsageError(message) from exc


def _read_state(path: Path) -> SkillState:
    _validate_path(path)
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return SkillState(path, None)
    text = _normalize(content.decode("utf-8"))
    metadata, offset = _metadata(text)
    owner = metadata.get("managed-by")
    legacy = MANAGED_MARKER in text
    managed = _string(owner) == DISTRIBUTION_NAME if owner is not None else legacy
    if not managed:
        return SkillState(path, content)

    raw_version = metadata.get("managed-version")
    version = _string(raw_version)
    version_state = "valid"
    try:
        if version is None:
            raise InvalidVersion("missing string version")
        Version(version)
    except InvalidVersion:
        version = None
        version_state = "missing" if raw_version is None else "malformed"
    if legacy and owner is None and raw_version is None:
        return SkillState(path, content, True, "0", "legacy", "legacy")

    hash_node = metadata.get("managed-content-sha256")
    stored_hash = _string(hash_node)
    span = _hash_span(text, hash_node, offset)
    if hash_node is None:
        integrity = "missing"
    elif stored_hash is None or not HASH_PATTERN.fullmatch(stored_hash) or span is None:
        integrity = "malformed"
    else:
        integrity = "valid" if _digest(text, span) == stored_hash else "altered"
    return SkillState(path, content, True, version, version_state, integrity)


def _running_version() -> Version | None:
    try:
        return Version(__version__)
    except InvalidVersion:
        return None


def _needs_update(state: SkillState, running: Version | None) -> bool:
    if not state.managed or running is None:
        return False
    if state.version_state != "valid":
        return True
    return state.parsed_version < running and state.integrity == "valid"


def _force_recommendation(skills_dir: Path | None) -> str:
    command = FORCE_INSTALL_COMMAND
    if skills_dir is not None:
        command += f' --skills-dir "{skills_dir}"'
    return command


def _atomic_write(state: SkillState, text: str) -> bool:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=state.path.parent,
            prefix=".SKILL.md-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # Revalidate after staging, immediately before replacement. Any concurrent
        # change, including a newer version or removal, cancels this attempt.
        if _read_state(state.path).content != state.content:
            return False
        os.replace(temporary, state.path)
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _installation_needed(
    state: SkillState, running: Version | None, text: str, force: bool, force_command: str,
) -> bool:
    if state.content is None:
        if state.path.parent.exists() and any(state.path.parent.iterdir()):
            raise UsageError(f"refusing to install into nonempty skill directory '{state.path.parent}' without SKILL.md.")
        return True
    if not state.managed:
        raise UsageError(f"refusing to overwrite unmanaged skill '{state.path}', even with --force.")
    if state.version_state == "valid":
        if running is not None and state.parsed_version > running:
            return False
        if state.integrity != "valid" and not force:
            raise UsageError(
                f"refusing to overwrite altered or unverifiable managed skill '{state.path}'. "
                f"Use {force_command}."
            )
    return _needs_update(state, running) or (force and state.content != text.encode("utf-8"))


def install_skill(
    skills_dir: Path | None = None, *, force: bool = False, env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    roots = list(_skill_roots(skills_dir, env))
    text = render_skill()
    running = _running_version()
    plans = []
    for root in roots:
        path = skill_dir(root) / "SKILL.md"
        with _skill_errors("install", path):
            state = _read_state(path)
            force_command = _force_recommendation(root if len(roots) > 1 else skills_dir)
            plans.append((state, _installation_needed(state, running, text, force, force_command)))

    results = []
    for index, (state, update) in enumerate(plans):
        path = state.path
        previous_root = roots[0] if index else None
        with _skill_errors("install", path, previous_root=previous_root):
            if update:
                if previous_root is not None:
                    root = path.parent.parent
                    if _is_link(root.parent):
                        raise UsageError(f"refusing linked Claude profile '{root.parent}'.")
                    # Create skills only while the detected profile still exists.
                    root.mkdir(exist_ok=True)
                    _validate_path(path)
                path.parent.mkdir(parents=previous_root is None, exist_ok=True)
                if not _atomic_write(state, text):
                    raise UsageError(f"skill changed during installation at '{path}'. Retry the command.")
            results.append({"installed": True, "updated": update, "skill": SKILL_NAME, "path": str(path)})
    return _location_report(results)


def skill_status(
    skills_dir: Path | None = None, *, env: Mapping[str, str] | None = None, report_errors: bool = False,
) -> dict[str, Any]:
    roots = []
    discovery_error = None
    try:
        roots.extend(_skill_roots(skills_dir, env))
    except UsageError as exc:
        if not report_errors:
            raise
        # The primary root is always yielded before Claude directory discovery.
        claude = _claude_skills_dir(env)
        assert claude is not None
        discovery_error = _unreadable_status(skill_dir(claude) / "SKILL.md", exc)
    local = is_development_build()
    running = _running_version()
    results = []
    for root in roots:
        path = skill_dir(root) / "SKILL.md"
        try:
            with _skill_errors("inspect", path):
                state = _read_state(path)
                standard = (
                    skills_dir is None or root.absolute() == default_skills_dir()
                    or (root.absolute() == _claude_skills_dir(env) and root.parent.is_dir() and not _is_link(root.parent))
                )
        except UsageError as exc:
            if not report_errors:
                raise
            results.append(_unreadable_status(path, exc))
            continue
        comparison = "not_applicable"
        if state.parsed_version is not None and running is not None:
            comparison = "older" if state.parsed_version < running else "newer" if state.parsed_version > running else "equal"
        recommendation = None
        if state.managed and state.version_state == "valid" and state.integrity != "valid" and comparison != "newer":
            recommendation = _force_recommendation(root if len(roots) > 1 else skills_dir)
        results.append({
            "skill": SKILL_NAME, "path": str(path), "standard_location": standard,
            "installed": state.content is not None, "managed": state.managed,
            "cli_version": __version__, "managed_version": state.version,
            "version_state": state.version_state, "version_comparison": comparison,
            "integrity": state.integrity,
            "auto_sync_eligible": standard and not local and _needs_update(state, running),
            "local_development_build": local, "force_install_command": recommendation,
        })
    if discovery_error:
        results.append(discovery_error)
    return _location_report(results)


def _unreadable_status(path: Path, error: UsageError) -> dict[str, Any]:
    return {"path": str(path), "installed": False, "managed": False, "error": str(error)}


@contextmanager
def _sync_warnings(stderr: TextIO) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        # Maintenance must not affect help, JSON stdout, or the primary exit status.
        try:
            detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            stderr.write(f"WARNING: could not synchronize managed skill: {detail}\n")
        except (OSError, ValueError):
            pass


def sync_skill(*, stderr: TextIO, env: Mapping[str, str] | None = None) -> None:
    """Maintain installed copies independently. Never install a missing skill."""
    with _sync_warnings(stderr):
        if is_development_build():
            return
        running = _running_version()
        if running is None:
            return
        for root in _skill_roots(None, env):
            with _sync_warnings(stderr):
                state = _read_state(skill_dir(root) / "SKILL.md")
                if _needs_update(state, running):
                    if _atomic_write(state, render_skill()):
                        old = state.version or state.version_state
                        stderr.write(f"Updated managed skill {old} -> {__version__} at '{state.path}'.\n")
                elif (
                    state.managed and state.parsed_version is not None
                    and state.parsed_version < running and state.integrity != "valid"
                ):
                    stderr.write(
                        f"Preserved altered or unverifiable managed skill at '{state.path}'. "
                        f"Use {_force_recommendation(root)}.\n"
                    )


def _removal_snapshot(path: Path) -> tuple[int, int, int, int] | None:
    _validate_path(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        if path.parent.exists():
            raise UsageError(f"refusing to remove '{path.parent}' because SKILL.md is missing.") from None
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def remove_skill(
    skills_dir: Path | None = None, *, force: bool = False, env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    roots = list(_skill_roots(skills_dir, env))
    plans = []
    for root in roots:
        path = skill_dir(root) / "SKILL.md"
        with _skill_errors("remove", path):
            snapshot = _removal_snapshot(path)
            if snapshot is not None and not force and not _read_state(path).managed:
                raise UsageError(
                    f"refusing to remove '{path.parent}' because it is not marked as managed by azwi. "
                    "Use --force to override."
                )
            plans.append((path, snapshot))

    results = []
    for path, snapshot in plans:
        result = {"removed": snapshot is not None, "skill": SKILL_NAME, "path": str(path.parent)}
        with _skill_errors("remove", path, previous_root=roots[0] if results else None):
            if snapshot is None:
                result["reason"] = "not_installed"
            else:
                if _removal_snapshot(path) != snapshot:
                    raise UsageError(f"skill changed during removal at '{path}'. Retry the command.")
                path.unlink()
                if not any(path.parent.iterdir()):
                    path.parent.rmdir()
        results.append(result)
    return _location_report(results)
