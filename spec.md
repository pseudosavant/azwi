# Spec: `azwi` Azure DevOps Work Item CLI (v2)

## Purpose

Build a new Python CLI named `azwi` that fetches Azure DevOps work items and renders clean, agent-friendly context for coding agents such as Codex CLI and Claude Code.

Humans may still run the tool directly, but v2 should optimize for agent consumption first.

The new tool must support both:

1. local single-file execution via `uv run ./azwi.py ...` using PEP 723 inline script metadata
2. published package execution via `uvx azwi ...` without any manual install step

The v2 design should preserve the useful behavior of `azdo_wi.py` while removing its CLI ambiguities and packaging limitations.

---

## Current v1 review summary

The existing `azdo_wi.py` implementation is competent and self-contained, but the following v1 behaviors should not be carried forward unchanged:

1. The primary command is effectively `fetch`, but that subcommand is hidden behind a legacy argument rewrite. `azdo_wi --help` shows subcommands, while `azdo_wi 2195` is the real common path. This is confusing.
2. The v1 CLI exposes `--repo`, but the main fetch flow does not meaningfully use it. That makes the interface look broader than the implementation.
3. `--sections` is comma-separated text instead of a repeatable structured flag, which is awkward for shell completion, validation, and discoverability.
4. `--comments` and `--prs` are too terse as option names for a public tool. They really mean `comment limit` and `PR status filter`.
5. `--include-images` bundles several behaviors together: writing to disk, choosing a magic folder name, rewriting markdown links, and controlling file naming.
6. The tool is designed for `uv run` from a repo checkout, but it is not packaged for `uvx`.
7. The script has no PEP 723 metadata block, so `uv run azdo_wi.py` does not carry its dependency contract inline.

These points should drive the v2 design.

---

## Product goals

1. `azwi 2195` is the primary happy path.
2. `azwi fields --type Bug` remains available for field discovery.
3. Output stays deterministic and easy for an agent to paste into context.
4. `--help` is optimized for agents: short, explicit, and contract-oriented.
5. The project is publishable to PyPI so plain `uvx azwi --help` works.
6. The repo also contains a standalone `azwi.py` script with PEP 723 metadata for `uv run`.
7. CLI options map directly to context-shaping concerns such as section selection, comment volume, and image handling.
8. There is no compatibility requirement for the old `azdo_wi` command or argument surface.

## Non-goals

1. Full Azure DevOps CRUD support.
2. Interactive auth flows for v2.
3. Rich terminal UI.
4. Maintaining byte-for-byte output compatibility with v1.

---

## Naming and publishing

## Command name

`azwi`

## Distribution target

Publish to public PyPI if the exact user experience requirement is:

`uvx azwi --help`

Reason:

- `uvx` runs tools from package indexes and defaults to PyPI.
- If the package is hosted only on a private index, plain `uvx azwi` will not work unless the user has already configured uv to use that index.

## Package naming requirement

Prefer:

- package name: `azwi`
- console script: `azwi`

If the `azwi` package name is unavailable on PyPI, the user must choose between:

1. a different package name plus `uvx --from <package> azwi`
2. a different command name
3. a private index with custom uv configuration

The exact goal `uvx azwi` is only satisfied if the published package name resolves accordingly.

## Publishing workflow

The project should include a release workflow suitable for PyPI Trusted Publishing from GitHub Actions.

Release expectations:

1. `uv build --no-sources`
2. publish via `uv publish` or the official PyPI publish action in CI
3. prefer Trusted Publishing over long-lived API tokens

---

## Repo and packaging layout

Recommended structure:

```text
azwi/
  pyproject.toml
  README.md
  azwi.py
  src/
    azwi/
      __init__.py
      cli.py
      config.py
      client.py
      render.py
      models.py
```

## Packaging requirements

1. Use a normal `pyproject.toml` package so `uv build` and `uv publish` work cleanly.
2. Expose `azwi` as a console script via `[project.scripts]`.
3. Keep a root-level `azwi.py` wrapper script with PEP 723 inline metadata for local script execution.
4. The standalone script should delegate to the packaged implementation instead of duplicating the full app logic.

## PEP 723 requirement

`azwi.py` must contain inline script metadata similar to:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "...",
# ]
# ///
```

Notes:

1. The dependencies list is required, even if empty.
2. The script may add `src/` to `sys.path` so `uv run ./azwi.py ...` uses the local package source.
3. The package itself still uses `pyproject.toml` metadata for publishing.

---

## Runtime configuration

## Authentication

Primary auth method for v2:

- PAT from environment variable `AZWI_PAT`

PAT storage policy:

1. Do not store the PAT in `~/.azwi/config.toml`.
2. `config.toml` is for non-secret defaults only.
3. If persistent non-env auth is added later, it should use the OS credential store or another secure secret backend, not plain-text config.
4. Do not add a v2 command that attempts to persistently set shell environment variables for the user across platforms.
5. If an auth helper command is added later, it should target a secure credential store, not shell startup files.

Backward compatibility is not required.

## Target defaults

Preferred environment variables:

- `AZWI_ORG`
- `AZWI_PROJECT`

Backward compatibility is not required.

Recommended semantics:

1. `AZWI_ORG` is the important default for direct work item fetches.
2. `AZWI_PROJECT` is optional and mainly useful for project-scoped operations such as `fields`.
3. For `azwi <work_item_id>`, the tool should fetch by `org` and work item ID without requiring a project.
4. Treat work item IDs as organization-scoped for direct fetch behavior.
5. After fetching the work item, read `System.TeamProject` from the response and treat that as the authoritative project for all follow-up behavior.

## User config file

V2 should automatically load a user config file for persistent defaults.

Recommended path:

- `~/.azwi/config.toml`

Reason:

- TOML is readable, structured, and available via the Python standard library in modern Python versions.

The config file is for persistent per-user defaults such as:

1. default organization
2. optional default project
3. global field mappings
4. project-specific field mappings

The tool should be able to create this file, populate missing sections, and update existing values safely.

Recommended shape:

```toml
[defaults]
org = "my-org"
project = "ProjectA"

[defaults.fields]
description = "System.Description"
acceptance = "Microsoft.VSTS.Common.AcceptanceCriteria"
repro_steps = "Microsoft.VSTS.TCM.ReproSteps"
system_info = "Microsoft.VSTS.TCM.SystemInfo"

[projects."ProjectA".fields]
extra_fields = ["Custom.DevNotes"]

[projects."ProjectB".fields]
acceptance = "Custom.AcceptanceNotes"
extra_fields = ["Custom.ReleaseNotes"]
```

Multi-org support is required in v1, even though the default use case is a single org.

Recommended extended shape:

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

[orgs."other-org".projects."ProjectY".fields]
extra_fields = ["Custom.DevNotes"]
```

Field mapping and config layering behavior must be explicit:

1. `defaults.fields` defines the baseline logical field mapping for all projects.
2. `projects."<ProjectName>".fields` overrides only the keys it defines.
3. Any field mapping key not present in a project section falls back to `defaults.fields`.
4. `extra_fields` from a project section should be additive by default unless the spec later chooses replacement semantics explicitly.
5. The fetched work item's actual `System.TeamProject` determines which project override section applies.
6. If the resolved org has an `orgs."<OrgName>"` section, that org-specific config should layer on top of top-level defaults before project-specific overrides are applied.
7. For the common single-org case, users should only need the top-level `defaults` and `projects` sections.

## Configuration precedence

Recommended precedence:

1. explicit CLI flags
2. matching project-specific config from `~/.azwi/config.toml`
3. matching org-specific config from `~/.azwi/config.toml`
4. top-level defaults from `~/.azwi/config.toml`
5. environment variables
6. built-in defaults

Notes:

1. CLI flags always win for one-off overrides.
2. Project-specific mappings should layer on top of global defaults.
3. Org-specific config should layer on top of top-level defaults and below CLI flags.
4. Environment variables remain appropriate for auth and default target selection, not for complex field mapping.
5. The precedence chain for a fetched work item should use the work item's actual project once known, not just the caller's requested default project.
6. If no project-specific section matches, use the applicable defaults for the resolved org.

---

## CLI design

The CLI should be optimized for agent use, which means:

1. no hidden argument rewriting
2. no unused public flags
3. explicit, low-ambiguity option names
4. minimal prose in help text
5. stable stdout contract and stable stderr error format
6. context-size controls that are obvious from the option names

## Primary command

Primary usage should not use a hidden subcommand.

```text
azwi <work_item_id> [options]
```

Examples:

```text
azwi 2195
azwi 2195 --section metadata --section comments
azwi 2195 --comment-limit 20
```

## Secondary subcommands

At minimum:

```text
azwi fields --type "User Story"
azwi config show
azwi config set-defaults --org my-org --project ProjectA
```

Optional but reasonable for v2 if desired:

- `azwi doctor`
- `azwi version`

## Config subcommands

V2 should include a `config` command group for managing `~/.azwi/config.toml`.

Recommended commands:

```text
azwi config show
azwi config set-defaults --org my-org --project ProjectA
azwi config set-field --global --acceptance Microsoft.VSTS.Common.AcceptanceCriteria
azwi config set-field --project ProjectB --acceptance Custom.AcceptanceNotes
azwi config set-field --project ProjectB --description Custom.DevDescription
azwi config add-extra-field --global Custom.DevNotes
azwi config add-extra-field --project ProjectB Custom.ReleaseNotes
```

Requirements:

1. If `~/.azwi/config.toml` does not exist, `azwi config ...` should create it.
2. Updates should preserve unrelated existing config content where practical.
3. `azwi config show` should render the effective resolved config in a readable form.
4. Config commands must never write the PAT into `config.toml`.
5. The config command surface should focus on common operations and not require users to understand the full TOML schema.
6. `--global` must target `defaults.fields`.
7. `--project <name>` must target `projects."<name>".fields`.
8. If a future raw-file view is needed, add `azwi config show --raw`; the default `config show` should remain the effective resolved view.

## CLI options for fetch

### Required positional

- `work_item_id` as an integer

### Section selection

Preferred v2 interface:

- `--section <name>` repeatable

Allowed values:

- `metadata`
- `description`
- `acceptance`
- `comments`
- `prs`

Behavior:

1. If no `--section` flags are provided, use the default section set.
2. Default section set should match current behavior unless changed intentionally.
3. Section output order is fixed by the tool, not by argument order.

Compatibility:

- no legacy `--sections` compatibility is required

### Comments

Rename:

- `--comments` -> `--comment-limit`

Behavior:

- integer
- default `10`
- allowed range `1..50`
- only meaningful when comments are requested

Rationale:

- this option controls context volume, so its name should be obvious to an agent

### PR filtering

Rename:

- `--prs` -> `--pr-status`

Allowed values:

- `active`
- `all`

Default:

- `active`

Rationale:

- the option should read like a filter, not like a section toggle

### Output

- `--output PATH`
- `--force`

Behavior:

1. Without `--output`, write Markdown to stdout only.
2. With `--output`, write UTF-8 with `\n` line endings.
3. Existing files are not overwritten without `--force`.

### Image localization

Do not keep the current `--include-images` behavior as-is.

Preferred v2 replacement:

- `--download-images DIR`

Behavior:

1. `--download-images DIR` means remote markdown image URLs should be fetched into `DIR` and rewritten to local relative paths.
2. The option value is required. Do not make the path implicit.
3. If `--download-images DIR` is used without `--output`, fail with a usage error instead of inventing a folder implicitly.
4. Relative `DIR` values must be resolved against the current working directory.
5. The chosen path-resolution rule must be documented clearly and tested.

Rationale:

- a single explicit option is easiest for an agent to discover from `--help`
- agents do better with one obvious parameter than with coupled boolean-plus-path flags

### Target selection

- `--org ORG`

`--repo` must not exist in v2.

Recommended semantics:

1. `--org` remains part of the CLI because Azure DevOps APIs are organization-scoped.
2. For `azwi <work_item_id>`, there should be no `--project` option.
3. Fetch the work item using the resolved org and the work item ID alone.
4. The work item ID must be treated as unambiguous within the resolved organization for this fetch path.
5. After the fetch, use the returned `System.TeamProject` value as the authoritative project for comments, field mapping, and any other project-scoped follow-up calls.
6. For `azwi fields --type ...`, `--project` should be required unless a default project is already available from config or environment.
7. For `azwi config set-field ...` and similar config commands, `--project` should select the target project profile when not using `--global`.

### Logging

- `--verbose`

Requirements:

1. Logs go to stderr only.
2. Success path must never emit non-markdown content on stdout.
3. Spinner/progress UI must not be coupled to `--verbose`.
4. If a spinner exists, disable it automatically for non-interactive stderr.

## Help text requirements

`azwi --help` and `azwi <id> --help` should be designed for agent parsing, not for marketing copy.

Requirements:

1. Put the primary usage line first.
2. Show env var names near the top, not buried in a long epilog.
3. Show defaults inline with the options.
4. Include a short list of exact section names and exit codes.
5. Keep examples concise and directly copyable.
6. Avoid long narrative paragraphs.
7. Do not document deprecated compatibility behavior ahead of the preferred behavior.

---

## Output contract

The Markdown contract can stay close to v1.

## Always present header

```md
# {work_item_id} {title}
```

## Section headers

```md
# Metadata
# Description:
# Acceptance Criteria:
# Discussion
# PRs
```

## Metadata section

Keep the current bullets unless intentionally expanded:

- Type
- State
- Assigned To
- Changed Date

## Description section

1. Convert HTML to Markdown.
2. For Bugs, include `## Repro Steps` and `## System Info` when present.

## Discussion section

Keep newest-to-oldest ordering.

Format:

```md
- {createdDate} - {displayName}
    {comment body}
```

## PR section

Format:

```md
- PR {prId} - {title} ({sourceBranch}) [{status}] {url}
```

## Empty data behavior

1. If a requested section is successfully fetched but empty, render the section header and leave it empty.
2. If a requested section cannot be fetched because of auth, permissions, or API failure, fail the command instead of emitting partial output.

## Output formats

V2 should support both:

- `--format markdown`
- `--format json`

Default:

- `markdown`

Rationale:

- Markdown is the best default for dropping work item context directly into an agent prompt.
- JSON is the easiest and most efficient format for an agent that wants to inspect specific fields programmatically.

## JSON contract

The JSON shape should be documented and stable.

Recommended top-level shape:

```json
{
  "work_item": {
    "id": 2195,
    "title": "Title",
    "url": "https://..."
  },
  "sections": {
    "metadata": {},
    "description": {},
    "acceptance": {},
    "comments": [],
    "prs": []
  }
}
```

Requirements:

1. Omit no requested section keys. Use empty objects, empty strings, or empty arrays as appropriate.
2. Preserve a fixed schema regardless of content emptiness.
3. Keep stderr behavior identical across output formats.
4. For rendered text sections, include Markdown-rendered text and the source field reference name.
5. Do not include raw HTML in JSON unless the product explicitly adds a future raw mode.

---

## Azure DevOps behavior

## Required APIs

1. work item with fields and relations
2. comments
3. linked PR details
4. work item type fields

## PAT scopes

Minimum documented scopes:

- Work Items: Read
- Code: Read

## Field discovery

Keep:

```text
azwi fields --type Bug
azwi fields --type "User Story"
```

The output can stay as a simple markdown table or be plain text.

## Field mapping strategy

V2 should use the standard Azure DevOps reference names by default, such as:

- `System.Description`
- `Microsoft.VSTS.Common.AcceptanceCriteria`
- `Microsoft.VSTS.TCM.ReproSteps`
- `Microsoft.VSTS.TCM.SystemInfo`

That is the expected configuration for this project.

For other users, some orgs or inherited processes may use custom fields instead for one or more logical sections, especially acceptance criteria or custom implementation notes.

Recommended v2 approach:

1. Keep the standard field refs as the default behavior.
2. Automatically apply per-org/project field mappings from the user config file when a matching profile exists.
3. Add explicit override flags for the core logical sections:
   - `--field-description REFNAME`
   - `--field-acceptance REFNAME`
   - `--field-repro-steps REFNAME`
   - `--field-system-info REFNAME`
4. Add repeatable `--extra-field REFNAME` for additional custom fields that should be included in output.

Recommended output behavior for `--extra-field`:

1. In Markdown, render an `# Additional Fields` section with stable subsection headers using the field reference name.
2. In JSON, include an `extra_fields` object keyed by reference name.
3. Preserve requested order for `--extra-field` entries.

Rationale:

1. Agents benefit from a stable default contract.
2. Explicit overrides are better than trying to guess equivalent custom fields.
3. A user config file lets custom mappings be set once and reused automatically.
4. `fields` remains the discovery tool, while config plus override flags control the fetch behavior deterministically.

## Mention handling

Carry forward the v1 behavior:

1. resolve `@<GUID>` tokens to display names when the same work item's comment metadata makes that possible
2. leave unresolved tokens unchanged

## Retry behavior

Carry forward the v1 retry policy:

1. retry on 429 and transient 5xx
2. exponential backoff with jitter
3. distinct exit code for final throttling failure

---

## Implementation guidance

## Internal structure

Separate these concerns:

1. CLI parsing
2. configuration and env resolution
3. Azure DevOps HTTP client
4. Markdown rendering
5. filesystem output and image download behavior

Do not keep everything in one large script module internally, even if the repo still ships a single-file wrapper script.

## Dependencies

Standard library only is not required for v2.

If a dependency improves correctness materially, prefer it over custom parsing, especially for:

1. HTTP client behavior
2. HTML to Markdown conversion
3. CLI ergonomics

Reasonable examples:

- `httpx`
- `typer` or `argparse`
- `markdownify` or similar

The exact dependency set is an implementation choice, but it must be compatible with both `uv run` and published packaging.

---

## Compatibility guidance

There is no backward compatibility requirement with `azdo_wi`.

The v2 implementation should optimize for a clean agent-facing interface rather than preserving legacy names or flags.

What should be preserved conceptually:

1. same general exit code meanings
2. same core Markdown sections
3. same PAT-based auth model
4. same field discovery capability

What should not be preserved:

1. hidden `fetch` subcommand
2. unused `--repo`
3. comma-only `--sections`
4. magic directory creation for image download mode
5. legacy env var names
6. alias command names

---

## Testing requirements

The v2 implementation should include automated tests for:

1. CLI parsing and help text
2. section selection behavior
3. output formatting for each section
4. bug-specific Description rendering
5. mention token resolution
6. PR relation parsing
7. image download path rewriting
8. error classification and exit codes

Use recorded fixtures or mocked HTTP responses for Azure DevOps API calls.

---

## Release acceptance criteria

The implementation is done when all of the following are true:

1. `uv run ./azwi.py --help` works from a fresh checkout using PEP 723 metadata
2. `uv run ./azwi.py 2195 --help` shows the primary fetch interface
3. `uv build --no-sources` succeeds
4. the built package exposes an `azwi` console script
5. after publishing to PyPI, `uvx azwi --help` works
6. the README documents setup, PAT scopes, examples, and publishing notes

---

## Product decisions captured

1. Public command name is `azwi` only.
2. No compatibility with `azdo_wi` is required.
3. Default output includes all sections.
4. Agents must be able to request only specific sections easily.
5. Both Markdown and JSON outputs are required.
6. Public PyPI distribution is acceptable and intended.
7. Standard Azure DevOps field refs are the default expected configuration.
8. `--download-images DIR` is the preferred image-localization interface.
9. Persistent field overrides should be auto-loaded from a user config file.
10. `--org` remains part of the fetch CLI; `--project` does not.
11. `azwi` should manage `~/.azwi/config.toml` via `config` subcommands.
12. PATs must not be stored in `config.toml`.
13. Field mappings support both global defaults and project-specific overrides.
14. Direct work item fetch is organization-scoped and then resolves the authoritative project from the work item itself.
15. V2 should not try to persist shell environment variables for the user; future auth persistence should use a secure credential store instead.
16. Multi-org config support is required in v1, though the common case is a single org.
17. Relative `--download-images DIR` paths resolve from the current working directory.
18. JSON should include rendered Markdown text plus source field reference names, not raw HTML.
19. `azwi config show` should display the effective resolved config by default.
20. The project license is MIT.
