# azwi

`azwi` fetches Azure DevOps work items and turns them into clean context for coding agents. It returns descriptions, acceptance criteria, comments, attachments, and linked pull requests as deterministic JSON or readable Markdown.

The CLI is designed for both people and agents. Successful output stays on stdout. Logs, progress, and maintenance notices go to stderr.

## Prerequisite

`azwi` is designed to be used with [`uv`](https://docs.astral.sh/uv/getting-started/installation/). Install `uv` before continuing. The documented workflows and managed agent skill use `uvx` to run the tool without requiring a global installation.

You also need an Azure DevOps PAT with these scopes:

- Work Items: Read
- Code: Read

Set the PAT in `AZWI_PAT`. For example, in PowerShell:

```powershell
$env:AZWI_PAT = "<your-pat>"
```

In Bash or zsh:

```bash
export AZWI_PAT="<your-pat>"
```

The PAT is never stored in `~/.azwi/config.toml`.

## Quick start with an agent

Install the managed agent skill:

```powershell
uvx azwi skill install
```

Then use `$azure-workitem` in Codex, Claude Code, or another agent harness that supports skills:

> Use $azure-workitem to inspect https://dev.azure.com/my-org/Payments/_workitems/edit/2195. Summarize the requested change, acceptance criteria, and any relevant pull request discussion.

The skill accepts a numeric work item ID or a supported Azure DevOps Cloud work item URL. It fetches the work item with `uvx azwi`, parses the default JSON output, and requests high-volume details such as PR comments or downloads only when needed.

## What it returns

The default JSON output is stable and designed for agent parsing. Markdown output is designed for reading or adding directly to a prompt.

A Markdown result looks like this:

```markdown
# 2195 Login bug

# Metadata

- Type: Bug
- State: Active
- Assigned To: Alice
- Changed Date: 2026-03-10T10:00:00Z

# Description:

Main **issue**

## Repro Steps

1. Open app
2. Click sign in

# Acceptance Criteria:

Should be fixed
```

JSON always contains top-level `work_item` metadata and a `sections` object. Text fields contain both rendered Markdown and the Azure DevOps field reference name. Raw HTML is not included.

| Format | Best for |
| --- | --- |
| `json` | Agent tools, scripts, and automation |
| `markdown` | Reading, saved context, and direct prompt input |

## Use the CLI directly

Fetch a work item without installing the package globally:

```powershell
uvx azwi 2195 --org my-org
```

Save the organization as a default so later calls need only the work item ID:

```powershell
uvx azwi config set-defaults --org my-org
uvx azwi 2195
```

Request Markdown instead of the default JSON:

```powershell
uvx azwi 2195 --format markdown
```

Write the result to a file:

```powershell
uvx azwi 2195 --format markdown --output work-item-2195.md
```

Existing output files are preserved unless `--force` is supplied.

To install the command as a persistent tool:

```powershell
uv tool install azwi
azwi 2195
```

The examples below continue to use `uvx azwi` so they work without a global installation.

## How fetching works

The fetch model has four core rules:

1. A work item ID is looked up within an Azure DevOps organization.
2. The organization comes from `--org`, user config, or `AZWI_ORG`.
3. The fetched work item's `System.TeamProject` field determines the project used for field mappings and follow-up requests.
4. Requested sections are returned in a fixed order and failures stop the command instead of producing partial output.

The main interface is:

```text
azwi <work_item_id> [options]
```

There is no `fetch` subcommand and no `--project` option for direct work item lookup. Project selection remains available for project-scoped commands such as `fields`.

## Choose the context you need

Without `--section`, `azwi` returns all standard sections. Repeat `--section` to request a smaller result.

| Goal | Command |
| --- | --- |
| Fetch all default context | `uvx azwi 2195` |
| Fetch acceptance criteria only | `uvx azwi 2195 --section acceptance` |
| Fetch metadata and comments | `uvx azwi 2195 --section metadata --section comments` |
| Increase the comment limit | `uvx azwi 2195 --section comments --comment-limit 20` |
| Include all linked PR states | `uvx azwi 2195 --section prs --pr-status all` |
| Add a custom field once | `uvx azwi 2195 --extra-field Custom.DevNotes` |
| Save prompt-ready Markdown | `uvx azwi 2195 --format markdown --output work-item-2195.md` |

Available sections:

| Section | Content |
| --- | --- |
| `metadata` | Type, state, assignee, and changed date |
| `description` | Description plus bug repro steps and system information |
| `acceptance` | Acceptance criteria |
| `comments` | Work item discussion, newest first |
| `attachments` | Attachment names, URLs, comments, sizes, and local paths when downloaded |
| `prs` | Linked pull requests and optional review discussion |

Section output order is fixed by the tool, not by the order of `--section` arguments.

The comment limit defaults to 10 and accepts values from 1 through 50. Linked pull request metadata defaults to active PRs. Requested section keys remain present in JSON even when their content is empty.

## Configure defaults and fields

`azwi` stores non-secret defaults and field mappings in `~/.azwi/config.toml`. Use the CLI to manage the common settings:

```powershell
uvx azwi config show
uvx azwi config set-defaults --org my-org --project Payments
uvx azwi config set-field --global --acceptance Microsoft.VSTS.Common.AcceptanceCriteria
uvx azwi config set-field --project Payments --description Custom.DevDescription
uvx azwi config add-extra-field --project Payments Custom.ReleaseNotes
```

`config show` displays the effective resolved configuration. Config commands create the file when needed and never write `AZWI_PAT` into it.

Settings are resolved in this order:

1. Explicit CLI flags
2. Matching project-specific config
3. Matching organization-specific config
4. Top-level config defaults
5. Environment variables
6. Built-in defaults

The common single-organization configuration looks like this:

```toml
[defaults]
org = "my-org"
project = "Payments"

[defaults.fields]
description = "System.Description"
acceptance = "Microsoft.VSTS.Common.AcceptanceCriteria"
repro_steps = "Microsoft.VSTS.TCM.ReproSteps"
system_info = "Microsoft.VSTS.TCM.SystemInfo"

[projects."Payments".fields]
extra_fields = ["Custom.DevNotes"]
```

Organization-specific profiles are also supported:

```toml
[orgs."other-org".defaults]
project = "ProjectX"

[orgs."other-org".defaults.fields]
acceptance = "Custom.Acceptance"

[orgs."other-org".projects."ProjectY".fields]
extra_fields = ["Custom.ReleaseNotes"]
```

### Discover and override fields

List the available field reference names for a work item type:

```powershell
uvx azwi fields --type Bug --project Payments
uvx azwi fields --type "User Story" --project Payments
```

Override logical fields for one invocation:

```powershell
uvx azwi 2195 --field-description Custom.DevDescription
uvx azwi 2195 --field-acceptance Custom.Acceptance
uvx azwi 2195 --field-repro-steps Custom.ReproSteps
uvx azwi 2195 --field-system-info Custom.SystemInfo
```

Use repeatable `--extra-field REFNAME` options to add fields without replacing the standard sections. Markdown labels extra fields by reference name. JSON returns them in the `extra_fields` object.

## Download attachments and images

The `attachments` section lists attachment metadata without downloading files. Downloads are always explicit:

```powershell
uvx azwi 2195 --download-attachments work-item-2195-files
```

`--download-attachments DIR` automatically includes the `attachments` section. Without selectors, it downloads every work item attachment.

Use repeatable exact-match selectors to list or download specific attachments:

```powershell
uvx azwi 2195 --section attachments --attachment-name notes.txt
uvx azwi 2195 --download-attachments files --attachment-url https://dev.azure.com/...
```

The attachment output contains exact `name` and `url` values for follow-up calls. If any selector does not match, the command fails instead of silently returning a partial result.

Relative download directories resolve from the current working directory. With `--output`, rendered attachment paths are relative to the output file location. Without `--output`, they are relative to the current working directory when possible.

Use `--download-images DIR` with `--output` to download remote images found in rendered Markdown and rewrite their links to local relative paths:

```powershell
uvx azwi 2195 --format markdown --output work-item-2195.md --download-images work-item-2195-images
```

Relative image directories also resolve from the current working directory. Image downloading without `--output` is a usage error.

## Include pull request discussion

The `prs` section lists linked pull requests. PR thread comments are high-volume context and remain opt-in:

```powershell
uvx azwi 2195 --include-pr-comments
```

This option automatically includes the `prs` section. Active threads are included by default. Include active and resolved threads with:

```powershell
uvx azwi 2195 --include-pr-comments --pr-comment-status all
```

Azure DevOps system comments are excluded unless `--include-pr-system-comments` is supplied.

## Manage the agent skill

The standard skill location is `~/.agents/skills/azure-workitem/SKILL.md`.

```powershell
uvx azwi skill install
uvx azwi skill status
uvx azwi skill status --format plain
uvx azwi skill remove
```

Skill commands return JSON by default. All three commands accept `--skills-dir DIR` for a custom skills root. The older `install-skill`, `skill-status`, and `remove-skill` command aliases remain available.

Normal invocations of an installed release automatically update an older managed skill when its installed content is unchanged. Synchronization is local. It does not query PyPI, refresh uv's cache, or update the CLI. Missing skills, unmanaged skills, modified managed skills, equal versions, and newer versions are left alone.

Inspect version, integrity, and update eligibility before replacing modified managed content:

```powershell
uvx azwi skill status
uvx azwi skill install --force
```

Install-time `--force` can replace altered content only when the skill is managed by `azwi`. It never overwrites an unmanaged skill or downgrades a newer version.

Automatic synchronization checks only the standard location. Local checkouts, direct source installs, editable builds, and custom locations require explicit skill commands. Updates affect future agent sessions and may not change instructions already loaded by a running agent.

`skill remove` removes the managed `SKILL.md` and its directory when empty. It preserves unrelated files. Removing unmanaged content requires `--force`. Installation and removal refuse linked paths and unexpected file types.

## Reference

Useful discovery and metadata commands:

```powershell
uvx azwi --help
uvx azwi 2195 --help
uvx azwi fields --help
uvx azwi config --help
uvx azwi skill --help
uvx azwi --about
uvx azwi version
```

Environment variables:

| Variable | Purpose |
| --- | --- |
| `AZWI_PAT` | Azure DevOps personal access token |
| `AZWI_ORG` | Default organization for fetch and fields |
| `AZWI_PROJECT` | Default project for project-scoped commands such as `fields` |

Exit codes:

| Code | Meaning |
| ---: | --- |
| `0` | Success |
| `2` | Usage or input error |
| `3` | Configuration error |
| `4` | Authentication error |
| `5` | Work item or resource not found |
| `6` | Azure DevOps API error |
| `7` | Throttling retries exhausted |

`uvx azwi --about` prints the command name and version, a short summary, the project URL, and the MIT license. The project source is available on [GitHub](https://github.com/pseudosavant/azwi).

## Development and release

The repository supports both execution paths required by the project:

- `uv run ./azwi.py ...` through PEP 723 metadata in the root wrapper
- `uvx azwi ...` through the package defined in `pyproject.toml`

Run the CLI from a checkout:

```powershell
uv run ./azwi.py --help
uv run ./azwi.py 2195 --org my-org
```

Run the tests and build the distribution:

```powershell
uv run python -m unittest discover -s tests -v
uv build --no-sources
uv run python tests/wheel_smoke.py dist/azwi-1.2.0-py3-none-any.whl
```

The wheel smoke check uses temporary environments and a temporary home directory. It validates wheel packaging, index-style metadata, local and editable installs, `uvx`, and the PEP 723 wrapper. It requires access to build and runtime dependencies.

To release a version:

1. Tag a release such as `v1.2.0`.
2. Let GitHub Actions build the package.
3. Publish to PyPI using Trusted Publishing.

## License

MIT
