from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
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

- For a numeric ID, use it directly: `uvx azwi <id>`.
- For `https://dev.azure.com/<org>/.../_workitems/edit/<id>`, extract the numeric segment immediately after `_workitems/edit/` and call `uvx azwi <id> --org "<org>"`.
- For `https://<org>.visualstudio.com/.../_workitems/edit/<id>`, extract the same numeric segment and call `uvx azwi <id> --org "<org>"`.
- Reject unrelated URLs or ambiguous numbers instead of guessing.

Keep URL handling in the skill. The `azwi` fetch command itself accepts a numeric work item ID.

## Fetch And Respond

Fetch the default JSON payload:

```text
uvx azwi <id>
```

Use `--org "<org>"` when the input URL supplies the organization. Summarize the fields relevant to the request and preserve useful work item or PR links. Use `--format markdown` only when the user asks for Markdown or prompt-ready raw context.

The common setup requires `AZWI_PAT` and an organization from `--org`, `AZWI_ORG`, or `~/.azwi/config.toml`. Never store PAT values in config or generated files.

## Comments And Pull Requests

The default fetch includes normal work item comments, attachment metadata, and linked PR metadata. Keep the current opt-in behavior for higher-volume PR thread data:

```text
uvx azwi <id> --section prs --include-pr-comments
```

- Add `--pr-comment-status all` only when active and resolved PR threads are requested.
- Add `--include-pr-system-comments` only when system comments are requested.
- Use `--comment-limit <n>` when the user requests a different work item comment count. Valid values are 1 through 50.

## Attachments

Download attachments only when requested. If the user supplies a folder, use it. Otherwise use `./azwi-<id>-attachments`:

```text
uvx azwi <id> --download-attachments "./azwi-<id>-attachments"
```

To download selected attachments, first read the exact attachment names or URLs from the JSON metadata, then use repeatable exact-match selectors:

```text
uvx azwi <id> --download-attachments "<dir>" --attachment-name "<name>"
uvx azwi <id> --download-attachments "<dir>" --attachment-url "<url>"
```

Do not download attachments merely because attachment metadata is present.

## Images And File Output

Download rendered text images only when requested. `--download-images` requires `--output`:

```text
uvx azwi <id> --format markdown --output "azwi-<id>.md" --download-images "azwi-<id>-images"
```

Do not add `--force` unless overwriting the output is explicitly allowed.

Stdout is payload only. Treat stderr as diagnostics, progress, and errors.
"""


def default_skills_dir() -> Path:
    return Path.home() / ".agents" / "skills"


def skill_dir(skills_dir: Path | None = None) -> Path:
    return (skills_dir or default_skills_dir()) / SKILL_NAME


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


def _validate_path(path: Path) -> None:
    for directory in (path.parent.parent, path.parent):
        if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
            raise UsageError(f"refusing linked skill directory '{directory}'.")
        if directory.exists() and not directory.is_dir():
            raise UsageError(f"expected a skill directory at '{directory}'.")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise UsageError(f"expected a regular SKILL.md file at '{path}'.")


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


def install_skill(skills_dir: Path | None = None, *, force: bool = False) -> dict[str, Any]:
    path = skill_dir(skills_dir) / "SKILL.md"
    try:
        state = _read_state(path)
        running = _running_version()
        if state.content is not None and not state.managed:
            raise UsageError(f"refusing to overwrite unmanaged skill '{path}', even with --force.")
        newer = (
            state.version_state == "valid" and running is not None
            and state.parsed_version > running
        )
        if not newer and state.version_state == "valid" and state.integrity != "valid" and not force:
            raise UsageError(
                f"refusing to overwrite altered or unverifiable managed skill '{path}'. "
                f"Use {_force_recommendation(skills_dir)}."
            )
        text = render_skill()
        updated = False
        if not newer and (
            state.content is None or _needs_update(state, running)
            or (force and state.content != text.encode("utf-8"))
        ):
            if state.content is None and path.parent.exists() and any(path.parent.iterdir()):
                raise UsageError(f"refusing to install into nonempty skill directory '{path.parent}' without SKILL.md.")
            path.parent.mkdir(parents=True, exist_ok=True)
            updated = _atomic_write(state, text)
            if not updated:
                raise UsageError(f"skill changed during installation at '{path}'. Retry the command.")
        return {"installed": True, "updated": updated, "skill": SKILL_NAME, "path": str(path)}
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise UsageError(f"cannot install skill at '{path}': {exc}") from exc


def skill_status(skills_dir: Path | None = None) -> dict[str, Any]:
    path = skill_dir(skills_dir) / "SKILL.md"
    try:
        state = _read_state(path)
        running = _running_version()
        local = is_development_build()
        standard = path.absolute() == (skill_dir() / "SKILL.md").absolute()
        comparison = "not_applicable"
        if state.parsed_version is not None and running is not None:
            comparison = "older" if state.parsed_version < running else "newer" if state.parsed_version > running else "equal"
        recommendation = None
        if state.managed and state.version_state == "valid" and state.integrity != "valid" and comparison != "newer":
            recommendation = _force_recommendation(skills_dir)
        return {
            "skill": SKILL_NAME, "path": str(path), "standard_location": standard,
            "installed": state.content is not None, "managed": state.managed,
            "cli_version": __version__, "managed_version": state.version,
            "version_state": state.version_state, "version_comparison": comparison,
            "integrity": state.integrity,
            "auto_sync_eligible": standard and not local and _needs_update(state, running),
            "local_development_build": local, "force_install_command": recommendation,
        }
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise UsageError(f"cannot inspect skill at '{path}': {exc}") from exc


def sync_skill(*, stderr: TextIO) -> None:
    """Best-effort local maintenance. Never install or discover custom locations."""
    try:
        if is_development_build():
            return
        running = _running_version()
        if running is None:
            return
        state = _read_state(skill_dir() / "SKILL.md")
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
                f"Use {FORCE_INSTALL_COMMAND}.\n"
            )
    except Exception as exc:
        # Maintenance must not affect help, JSON stdout, or the primary exit status.
        try:
            detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            stderr.write(f"WARNING: could not synchronize managed skill: {detail}\n")
        except (OSError, ValueError):
            pass


def remove_skill(skills_dir: Path | None = None, *, force: bool = False) -> dict[str, Any]:
    target = skill_dir(skills_dir)
    skill_path = target / "SKILL.md"
    if not target.exists():
        return {"removed": False, "skill": SKILL_NAME, "path": str(target), "reason": "not_installed"}
    if not skill_path.exists():
        raise UsageError(f"refusing to remove '{target}' because SKILL.md is missing.")
    try:
        _validate_path(skill_path)
        if not force and not _read_state(skill_path).managed:
            raise UsageError(
                f"refusing to remove '{target}' because it is not marked as managed by azwi. "
                "Use --force to override."
            )
        skill_path.unlink()
        # Unrelated files belong to the user. Only remove an empty skill directory.
        if not any(target.iterdir()):
            target.rmdir()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise UsageError(f"cannot remove skill at '{skill_path}': {exc}") from exc
    return {"removed": True, "skill": SKILL_NAME, "path": str(target)}
