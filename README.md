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
azwi install-skill
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
azwi install-skill
azwi remove-skill
```

## Agent skill

`azwi install-skill` installs or updates `$azure-workitem` at `~/.agents/skills/azure-workitem/SKILL.md`. Use `--skills-dir DIR` to target a different skills root. Installation overwrites existing skill content by default.

The skill accepts a numeric work item ID or a supported Azure DevOps Cloud work item URL. A URL supplies both the work item ID and organization; the skill then calls `uvx azwi <id> --org <org>`. Bare IDs use `uvx azwi <id>` and the normal azwi organization resolution.

The skill parses azwi's default JSON and answers the user's request. It documents work item comments, linked PRs, opt-in PR thread comments, attachment selectors, attachment downloads, and image downloads. Downloads and PR thread comments remain opt-in. When attachment download is requested without a directory, the skill uses `./azwi-<id>-attachments`; an explicitly requested directory wins.

`azwi remove-skill` removes the managed skill. It refuses unmanaged skill content unless `--force` is supplied.

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

- tag a release such as `v1.1.0`
- GitHub Actions builds the package
- publish to PyPI using Trusted Publishing

## License

MIT
