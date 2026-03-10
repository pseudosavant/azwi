# azwi

`azwi` fetches Azure DevOps work item context for agentic coding tools such as Codex CLI and Claude Code.

The tool is designed to make Azure DevOps work items easy to pull into an agent session as deterministic Markdown or JSON.

## Status

This repo is the planned home of `azwi`.

The intended distribution targets are:

- `uv run ./azwi.py ...` via PEP 723 inline script metadata
- `uvx azwi ...` via a published PyPI package

## Primary usage

```text
azwi <work_item_id> [--org ORG] [--section SECTION ...] [--format {markdown,json}]
```

Examples:

```text
azwi 2195
azwi 2195 --section acceptance
azwi 2195 --section metadata --section comments --comment-limit 20
azwi 2195 --format json
azwi 2195 --output wi-2195.md
azwi 2195 --output wi-2195.md --download-images assets
azwi fields --type Bug --project Payments
```

## What it returns

By default, `azwi` includes all major work item sections:

- metadata
- description
- acceptance criteria
- comments
- linked PRs

Output formats:

- `markdown` for prompt-friendly agent context
- `json` for structured automation

## Authentication

`azwi` reads an Azure DevOps PAT from:

- `AZWI_PAT`

Required PAT scopes:

- Work Items: Read
- Code: Read

The PAT is not stored in `~/.azwi/config.toml`.

## Configuration

`azwi` uses a user config file for non-secret defaults:

- `~/.azwi/config.toml`

The config can hold:

- default org
- optional default project
- global field mappings
- project-specific field overrides
- optional org-specific overrides for multi-org setups

Example:

```toml
[defaults]
org = "my-org"
project = "ProjectA"

[defaults.fields]
description = "System.Description"
acceptance = "Microsoft.VSTS.Common.AcceptanceCriteria"

[projects."ProjectB".fields]
acceptance = "Custom.AcceptanceNotes"
extra_fields = ["Custom.ReleaseNotes"]

[orgs."other-org".defaults]
project = "ProjectX"
```

Planned config commands:

```text
azwi config show
azwi config set-defaults --org my-org --project ProjectA
azwi config set-field --global --acceptance Microsoft.VSTS.Common.AcceptanceCriteria
azwi config set-field --project ProjectB --acceptance Custom.AcceptanceNotes
azwi config add-extra-field --project ProjectB Custom.ReleaseNotes
```

`azwi config show` is intended to show the effective resolved config.

## Design goals

- agent-first CLI and help output
- deterministic stdout
- stderr-only logging
- org-scoped work item lookup
- project-specific follow-up behavior derived from the fetched work item
- explicit image download behavior via `--download-images DIR`
- relative `--download-images` paths resolved from the current working directory

## License

MIT

## Repository files

- `spec.md`: implementation spec
- `README.md`: high-level project overview
