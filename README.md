# azwi

`azwi` fetches Azure DevOps work item context for agentic coding tools such as Codex CLI and Claude Code.

The tool is designed for deterministic, machine-friendly Markdown or JSON output with clean stdout on success and stderr-only logging.

## Install and run

Local checkout:

```text
uv run ./azwi.py --help
uv run ./azwi.py 2195 --org my-org
```

Packaged command:

```text
uvx azwi --help
uvx azwi --about
azwi 2195 --org my-org
```

## Authentication

`azwi` reads an Azure DevOps PAT from:

- `AZWI_PAT`

Required PAT scopes:

- Work Items: Read
- Code: Read

The PAT is not stored in `~/.azwi/config.toml`.

## Primary usage

```text
azwi <work_item_id> [--org ORG] [--section SECTION ...] [--format {markdown,json}]
```

Examples:

```text
azwi 2195
azwi 2195 --section acceptance
azwi 2195 --section metadata --section comments --comment-limit 20
azwi 2195 --format markdown
azwi 2195 --output wi-2195.md
azwi 2195 --output wi-2195.md --download-images assets
azwi 2195 --section attachments --download-attachments wi-2195-assets
azwi 2195 --download-attachments wi-2195-assets --attachment-url URL
azwi 2195 --section prs --include-pr-comments
azwi 2195 --section prs --include-pr-comments --pr-comment-status all
azwi 2195 --field-acceptance Custom.Acceptance
azwi 2195 --extra-field Custom.DevNotes
azwi fields --type Bug --project Payments
azwi config show
uvx azwi skill install
```

## Commands

Fetch a work item:

```text
azwi <work_item_id> [options]
```

List fields for a work item type:

```text
azwi fields --type Bug [--project PROJECT] [--org ORG]
```

Manage config:

```text
azwi config show
azwi config set-defaults --org my-org --project Payments
azwi config set-field --global --acceptance Microsoft.VSTS.Common.AcceptanceCriteria
azwi config set-field --project Payments --description Custom.DevDescription
azwi config add-extra-field --project Payments Custom.ReleaseNotes
```

Show version, project, and license information:

```text
azwi --about
```

Manage the `$azure-workitem` skill:

```text
uvx azwi skill install
uvx azwi skill status
uvx azwi skill remove
```

## Agent skill

`uvx azwi skill install` installs `$azure-workitem` at `~/.agents/skills/azure-workitem/SKILL.md`. The existing `install-skill` and `remove-skill` commands remain available as aliases. Skill commands return JSON by default. Use `skill status --format plain` for plain text.

Normally installed releases automatically synchronize an already-installed managed skill when its version is older and its content is unchanged. The running CLI's version is the authority. Synchronization is local. It does not query PyPI, refresh uv's cache, or update azwi itself. It never installs an absent skill, overwrites an unmanaged skill, or downgrades a newer skill. Notices go to stderr, keeping JSON stdout clean.

Managed skills store `managed-by: azwi`, a quoted `managed-version`, and `managed-content-sha256` under the YAML front matter `metadata` mapping. The SHA-256 hash covers the entire file with LF line endings and the hash value replaced by `""`. Modified or unverifiable skills with valid version metadata are preserved. Legacy HTML markers remain recognized. Missing or malformed managed versions receive a fresh replacement.

Inspect the version, integrity, and update eligibility, or explicitly replace modified managed content:

```text
uvx azwi skill status
uvx azwi skill install --force
```

`--force` on installation still refuses unmanaged content. A normal install creates a missing skill or updates a pristine older managed skill. Reinstalling the canonical skill is a no-op. Automatic checks run for normal commands, including help, version, and `--about`. Skill-management commands skip those checks. `skill status` is read-only.

Automatic synchronization only uses the standard directory. Custom locations require explicit updates with `uvx azwi skill install --skills-dir DIR`, adding `--force` if needed. Local checkouts, direct source installs, and editable builds skip automatic synchronization. Explicit installation still works, including `uvx --from . azwi skill install`. Installed wheels remain eligible. Updates affect future agent skill loading and may not change instructions already loaded in a running agent session.

The skill accepts a numeric work item ID or a supported Azure DevOps Cloud work item URL. A URL supplies both the work item ID and organization. The skill then calls `uvx azwi <id> --org <org>`. Bare IDs use `uvx azwi <id>` and the normal azwi organization resolution.

The skill parses azwi's default JSON and answers the user's request. It documents work item comments, linked PRs, opt-in PR thread comments, attachment selectors, attachment downloads, and image downloads. Downloads and PR thread comments remain opt-in. When attachment download is requested without a directory, the skill uses `./azwi-<id>-attachments`. An explicitly requested directory wins.

`uvx azwi skill remove` removes the managed `SKILL.md` and its directory if empty. It preserves unrelated files. Removal refuses unmanaged skill content unless `--force` is supplied. Both installation and removal refuse linked paths or unexpected file types.

## Output

Default sections:

- metadata
- description
- acceptance
- comments
- attachments
- prs

Formats:

- `json`
- `markdown`

Default format:

- `json`

JSON includes stable top-level `work_item` metadata plus a `sections` object containing rendered Markdown text and source field reference names for text fields. Markdown remains available as an explicit render mode for prompt-friendly output.

## Attachments

The `attachments` section lists work item attachment metadata from work item relations. Use `--download-attachments DIR` to download those files. This option automatically includes the `attachments` section.

Relative `DIR` paths resolve from the current working directory. When `--output` is used, rendered local paths are relative to the output file location; otherwise they are relative to the current working directory when possible.

By default, `--download-attachments DIR` downloads all attachments. Use repeatable exact-match selectors to list or download only specific attachments:

```text
azwi 2195 --section attachments --attachment-name notes.txt
azwi 2195 --download-attachments files --attachment-url https://dev.azure.com/...
```

The `attachments` section returns exact `name` and `url` values for follow-up selector calls. If any selector does not match, the command fails instead of silently producing a partial result.

## PR comments

The `prs` section lists linked pull requests by default. Use `--include-pr-comments` to include Azure DevOps PR thread comments under each PR. This option automatically includes the `prs` section.

PR thread comments default to active threads only:

```text
azwi 2195 --section prs --include-pr-comments
```

Use `--pr-comment-status all` to include both active and resolved thread comments. System comments are excluded unless `--include-pr-system-comments` is set.

## Configuration

`azwi` uses `~/.azwi/config.toml` for non-secret defaults and field mappings.

Supported config layers:

- top-level defaults
- top-level project overrides
- org-specific defaults
- org-specific project overrides
- per-invocation CLI overrides

Example:

```toml
[defaults]
org = "my-org"
project = "ProjectA"

[defaults.fields]
description = "System.Description"
acceptance = "Microsoft.VSTS.Common.AcceptanceCriteria"
repro_steps = "Microsoft.VSTS.TCM.ReproSteps"
system_info = "Microsoft.VSTS.TCM.SystemInfo"

[projects."ProjectB".fields]
acceptance = "Custom.AcceptanceNotes"
extra_fields = ["Custom.ReleaseNotes"]

[orgs."other-org".defaults]
project = "ProjectX"

[orgs."other-org".defaults.fields]
acceptance = "Custom.Acceptance"
```

`azwi config show` renders the effective resolved config. PATs are never written to the config file.

## Image download behavior

Use `--download-images DIR` together with `--output` to download remote Markdown image URLs and rewrite them to local relative paths.

Relative `DIR` paths resolve from the current working directory, not from the output file location.

## Packaging and publishing

This repo supports both:

- `uv run ./azwi.py ...` via the PEP 723 metadata block in the root wrapper
- `uvx azwi ...` via the package defined in `pyproject.toml`

Build:

```text
uv build --no-sources
```

Release workflow:

- tag a release such as `v1.2.0`
- GitHub Actions builds the package
- publish to PyPI using Trusted Publishing

Validation:

```text
python -m unittest discover -s tests -v
uv build --no-sources
python tests/wheel_smoke.py dist/azwi-1.2.0-py3-none-any.whl
```

The smoke check uses temporary environments and a temporary home directory. It checks wheel packaging, index-style metadata, local and editable installs, `uvx`, and the PEP 723 wrapper. It requires uv and access to build and runtime dependencies. No formatter or linter is configured in this repository.

## License

MIT
