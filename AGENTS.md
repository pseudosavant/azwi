# AGENTS.md

## Purpose

This repository is for `azwi`, an Azure DevOps work item fetcher designed first for agentic coding tools.

Optimize for deterministic machine-friendly behavior over human-oriented CLI convenience.

## Read first

Before making changes, read these files in order:

1. `spec.md`
2. `README.md`

If the README and spec ever diverge, follow `spec.md`.

## Non-negotiable product constraints

1. The public command is `azwi`.
2. The main fetch interface is `azwi <work_item_id> [options]`.
3. Do not add a hidden `fetch` subcommand.
4. Do not add `--repo`.
5. Do not add `--project` to the main fetch command.
6. Support both Markdown and JSON output.
7. Keep stdout deterministic and free of non-output chatter on success.
8. Send logs and progress information to stderr only.

## Packaging constraints

1. The repo must support `uv run ./azwi.py ...` via PEP 723 inline script metadata.
2. The project must also be packaged for `uvx azwi ...` via PyPI.
3. Use a normal `pyproject.toml` package plus a thin `azwi.py` wrapper.
4. Do not duplicate the full implementation inside the root script wrapper.

## Configuration constraints

1. Use `~/.azwi/config.toml` for non-secret defaults.
2. `azwi` should manage this file through `config` subcommands.
3. Do not store PATs in `config.toml`.
4. Use `AZWI_PAT` for authentication in v2.
5. Do not implement a command that tries to persistently set shell environment variables for the user across platforms.
6. Support both the common single-org case and multi-org config in v1.
7. `config show` should display the effective resolved config by default.

## Azure DevOps behavior constraints

1. Treat direct work item lookup as organization-scoped.
2. `--org` remains part of the fetch CLI.
3. After fetching a work item, use `System.TeamProject` as the authoritative project for follow-up behavior.
4. Keep `fields` as a project-scoped command.

## Field mapping constraints

1. Support global default field mappings plus project-specific overrides.
2. Also support org-specific config layering in v1.
3. Project-specific mappings layer on top of the applicable defaults.
4. Explicit CLI override flags must win for the current invocation.
5. `extra_fields` add additional output; they do not replace standard sections.
6. In Markdown, `extra_fields` should be labeled by field reference name.

## Output-format constraints

1. In JSON, rendered text fields should include Markdown-rendered text plus the source field reference name.
2. Do not include raw HTML in JSON unless a future raw mode is explicitly added.
3. Relative `--download-images` paths should resolve from the current working directory.

## CLI design guidance

1. Keep option names explicit and agent-readable.
2. Prefer stable, low-ambiguity flag names over short human-centric names.
3. Keep `--help` compact, concrete, and contract-focused.
4. Avoid long narrative help text.

## Verification

Before considering a task complete:

1. verify the implemented CLI shape still matches `spec.md`
2. verify help text examples are still correct
3. run relevant tests
4. verify stdout/stderr behavior for successful commands
5. verify packaging still supports both `uv run ./azwi.py ...` and `uvx azwi ...`
6. keep the repo under the MIT license
