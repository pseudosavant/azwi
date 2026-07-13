from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from azwi.errors import UsageError


SKILL_NAME = "azure-workitem"
MANAGED_MARKER = "<!-- managed-by: azwi -->"


SKILL_MD = f"""---
name: azure-workitem
description: Fetch and inspect Azure DevOps work items by numeric ID or Azure DevOps Cloud work item URL using azwi. Use when the user invokes `$azure-workitem`, asks for work item details, comments, attachments, linked pull requests, or PR review comments, or wants work item attachments or images downloaded.
---

{MANAGED_MARKER}

# Azure Work Item

Use `uvx azwi` to fetch deterministic Azure DevOps work item context. Default output is JSON; parse it and answer the user's request instead of dumping the full payload unless raw output is requested.

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
- Use `--comment-limit <n>` when the user requests a different work item comment count; valid values are 1 through 50.

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


def install_skill(skills_dir: Path | None = None) -> dict[str, Any]:
    target = skill_dir(skills_dir)
    target.mkdir(parents=True, exist_ok=True)
    skill_path = target / "SKILL.md"
    previous = skill_path.read_text(encoding="utf-8") if skill_path.exists() else ""
    updated = previous != SKILL_MD
    skill_path.write_text(SKILL_MD, encoding="utf-8")
    return {
        "installed": True,
        "updated": updated,
        "skill": SKILL_NAME,
        "path": str(skill_path),
    }


def remove_skill(skills_dir: Path | None = None, *, force: bool = False) -> dict[str, Any]:
    target = skill_dir(skills_dir)
    skill_path = target / "SKILL.md"
    if not target.exists():
        return {"removed": False, "skill": SKILL_NAME, "path": str(target), "reason": "not_installed"}
    if not skill_path.exists():
        raise UsageError(f"refusing to remove '{target}' because SKILL.md is missing.")
    content = skill_path.read_text(encoding="utf-8")
    if MANAGED_MARKER not in content and not force:
        raise UsageError(
            f"refusing to remove '{target}' because it is not marked as managed by azwi; "
            "use --force to override."
        )
    shutil.rmtree(target)
    return {"removed": True, "skill": SKILL_NAME, "path": str(target)}
