# azwi

`azwi` fetches Azure DevOps work items and turns them into clean context for coding agents. It returns descriptions, acceptance criteria, comments, attachments, and linked pull requests as deterministic JSON or readable Markdown.

The CLI is designed for both people and agents. Successful output stays on stdout. Logs, progress, and maintenance notices go to stderr.

## Prerequisite

`azwi` is designed to be used with [`uv`](https://docs.astral.sh/uv/getting-started/installation/). Install `uv` before continuing. The documented workflows and managed agent skill use `uvx` to run the tool without requiring a global installation.

## Quick start with an agent

1. Install `uv` using the link above.
2. For automatic authentication, install [Azure CLI](https://aka.ms/installazurecli) if it is not already available. A manually supplied PAT also works without Azure CLI.
3. Run setup using the PowerShell or Bash commands below and choose an authentication method. Replace the example URL with a work item you can access.
4. Invoke `$azure-workitem` in Codex or `/azure-workitem` in Claude Code.

PowerShell:

```powershell
uvx azwi setup "https://dev.azure.com/my-org/Payments/_workitems/edit/2195"
```

Bash or zsh:

```bash
uvx azwi setup "https://dev.azure.com/my-org/Payments/_workitems/edit/2195"
```

Setup extracts your organization, verifies the supplied work item through a normal fetch, saves the configuration, and installs the managed agent skill. When no authentication is already configured, setup offers these methods:

| Method | Behavior |
| --- | --- |
| `managed-pat` | Default menu choice. Azure CLI signs you in so azwi can create and renew an organization-specific PAT with **Work Items: Read** and **Code: Read**. Ordinary fetches use the saved PAT without starting Azure CLI. |
| `entra` | Azure CLI supplies a temporary Microsoft Entra token. azwi keeps it in memory for the command and does not save a token. It uses your account's Azure DevOps permissions. |
| `manual-pat` | Paste a PAT at a masked terminal prompt. You manage its expiration and replacement. Azure CLI is not required. |
| `environment` | Use `AZWI_PAT` from the execution environment. azwi prints instructions but does not set or persist shell variables. |

PATs are saved in `~/.azwi/credentials.toml`, separately from non-secret settings in `~/.azwi/config.toml`. A non-empty `AZWI_PAT` overrides every saved method and is never copied to disk automatically. Unset it before configuring a different method.

Bare `uvx azwi setup` asks for a work item URL or organization name. You can also use `uvx azwi setup --org my-org`. Azure CLI methods verify organization access even without a work item URL. A URL additionally verifies comments and linked PR metadata through the normal fetch. No project default is required.

In Codex, you can ask for a work item by ID.

> Use $azure-workitem to inspect 2195. Summarize the requested change, acceptance criteria, and any relevant pull request discussion.

In Claude Code, type `/azure-workitem 2195`.

The skill accepts numeric IDs and supported Azure DevOps Cloud URLs. It requests PR comments and downloads only when needed. Start a new agent session if the installed skill is not yet available.

### Microsoft sign-in and automatic renewal

Select a method explicitly to skip the method menu:

```powershell
uvx azwi setup "<work-item-url>" --auth managed-pat
uvx azwi setup "<work-item-url>" --auth entra
```

azwi first tries your existing Azure CLI session. If Microsoft requires sign-in, setup offers normal sign-in, device-code sign-in, another authentication method, or cancellation. After you choose sign-in, Azure CLI owns the account window, browser, or device-code instructions. Keep the original terminal open. When Azure CLI finishes, azwi automatically checks the account, verifies access, and completes setup. You do not copy a token or rerun the original command. Sign-in also updates your shared Azure CLI session.

The handoff runs `az login --tenant TENANT_ID --scope 499b84ac-1321-427f-aa17-267ca6975798/.default --allow-no-subscriptions --output none`. The subscription picker is disabled only for that child process. Azure CLI uses its normal platform sign-in experience. Device-code sign-in adds `--use-device-code`. Both child output streams go to azwi's stderr, keeping stdout available for the final JSON report. Ctrl+C cancels the handoff. azwi also stops it after five minutes. On Windows, child process containment also stops Azure CLI descendants if the terminal or launcher terminates azwi abruptly. In that case the launcher may determine the exit code.

azwi discovers the organization's tenant when possible. Use `--tenant TENANT_ID` if discovery is unavailable or the account has access to several tenants. Use `--login-method device-code` to prefer device-code sign-in when login is needed. Setup saves the verified tenant and Azure DevOps identity per organization. A later account mismatch fails without changing the managed PAT.

Managed PATs request a 30-day lifetime and renew when used within seven days of expiration. Repeating setup reuses a healthy managed PAT. You can configure the lifetime and renewal window:

```powershell
uvx azwi setup --org my-org --auth managed-pat --pat-lifetime-days 30 --renew-before-days 7
```

Organization policy may require a shorter lifetime or prohibit PAT creation. Active PAT renewal preserves the secret. Failed early renewal uses the still-valid PAT, reports the repair command, and waits an hour before trying again. An expired managed PAT is replaced and verified before being saved. azwi attempts to revoke the old token afterward. These operations happen when the tool runs, with no background service. Run setup in your terminal when sign-in is required. Manually supplied and environment PATs are never renewed automatically.

Entra mode invokes Azure CLI for each command, then shares that token across the command's API calls. Azure CLI can reuse its own cache without interactive sign-in. azwi does not maintain an additional token cache. In a local investigation, cached acquisition added about two seconds per invocation. This varies by machine. Managed PAT fetches avoid that cost outside the renewal window.

An interactive first fetch with missing configuration offers setup and resumes the fetch on success. For agents and automation, always use `--non-interactive` with fetches and `fields`. It prevents prompts and sign-in windows even when a terminal is attached. The managed skill includes this flag. Missing or unusable authentication returns an error with setup guidance.

### Existing users and changing authentication methods

Existing saved PATs and `AZWI_PAT` continue to work after upgrading. Existing manual PATs stay manually managed. Upgrading does not replace them or enable automatic renewal. Setup reuses the configured method unless you explicitly select another with `--auth`.

To switch to managed PAT authentication, unset `AZWI_PAT` in the terminal if it is set, then run:

```powershell
uvx azwi setup "https://dev.azure.com/my-org/Payments/_workitems/edit/2195" --auth managed-pat
```

Use `--auth entra` instead for temporary Azure CLI tokens. Use `--auth manual-pat` to return to a manually managed PAT, or `--auth environment` to require `AZWI_PAT` for that organization. These choices are saved per organization. They do not change other organizations' authentication methods. Supplying a work item URL verifies the selected method before completing setup.

After upgrading, start a new agent session to pick up the updated skill's non-interactive commands. Installed releases update pristine older managed copies in the detected locations automatically. Local source builds and skills installed outside those locations require an explicit skill install. See [Manage the agent skill](#manage-the-agent-skill).

### Credentials and environment overrides

Saved credentials use one PAT per organization:

```toml
[orgs."my-org"]
pat = "<your-pat>"
```

Setup creates this file for you. It stores the PAT as plain text with normal inherited filesystem permissions. It does not apply special restrictions or change ACLs. Keep it out of repositories and shared config exports. `config show`, setup reports, and diagnostics never display saved PATs.

Managed entries also contain the authorization ID, expiry, tenant, identity, and optional renewal retry time. azwi uses atomic replacement and a credential-file lock when updating them. Other organization entries are preserved. The selected method and non-secret settings live under `[orgs."my-org".auth]` in `config.toml`.

To create a manual PAT, open Azure DevOps **User settings > Personal access tokens** and select **Work Items: Read** and **Code: Read**. See [Microsoft's PAT instructions](https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate). Choose an expiration that fits your organization's policy. A longer lifetime reduces renewal interruptions. Use a shorter lifetime when the information or environment calls for it. Enter it with `uvx azwi setup "<work-item-url>" --auth manual-pat`.

A non-empty `AZWI_PAT` overrides the saved token. This also works if the credentials file is unavailable or malformed. If file storage is unavailable, set the variable in the environment where azwi runs:

PowerShell:

```powershell
$env:AZWI_PAT = "<your-pat>"
uvx azwi setup "https://dev.azure.com/my-org/Payments/_workitems/edit/2195"
```

Bash or zsh:

```bash
export AZWI_PAT="<your-pat>"
uvx azwi setup "https://dev.azure.com/my-org/Payments/_workitems/edit/2195"
```

These assignments affect the current shell and its child processes. An already-running agent application does not receive the change. Configure the environment where the agent actually executes `uvx`, including remote or sandboxed environments. Setup cannot change its parent shell's environment. Do not paste the PAT into an agent conversation.

On Windows, you can optionally persist the current value for your own account without administrator rights:

```powershell
[Environment]::SetEnvironmentVariable("AZWI_PAT", $env:AZWI_PAT, "User")
```

Future applications must inherit the updated environment. On Linux and macOS, persistent shell variables usually belong in the appropriate user shell startup file. Bash login shells and interactive shells read different files. Shell configuration does not automatically configure every desktop application. Persisting an environment variable this way stores the PAT as an ordinary setting. Environment variables are not encrypted secret storage.

### Check or repair setup

```powershell
uvx azwi config check
uvx azwi config check "https://dev.azure.com/my-org/Payments/_workitems/edit/2195"
```

`config check` reports the effective organization and its source, authentication method, credential source, known expiry, credentials file path, skill status, and next steps. Without a URL it only checks local readiness and never starts Azure CLI. A locally configured Entra profile does not prove that its session still works. With a URL it performs a default fetch and may silently acquire an Entra token. Checks never launch sign-in, renew or create PATs, or modify azwi files. Azure CLI may update its own cache during token acquisition. Run checks through the agent when diagnosing differences between terminal and agent access. A verified work item without returned linked PRs does not establish Code access.

For managed PAT or Entra sign-in repair, rerun `uvx azwi setup "<work-item-url>"` in your terminal. If a manually supplied PAT expires or is rejected, add `--replace-pat`. Setup verifies a replacement before saving it. If you use `AZWI_PAT`, update its value in the execution environment instead. Unset it before using `--replace-pat`. Make sure the token applies to the selected organization and has both required scopes. Rejected credentials never cause a silent switch to another authentication method.

Setup and checks return JSON by default. Add `--format plain` for a compact text report. Setup installs the skill in each selected directory. Readiness requires a managed copy in each one. Use `--skills-dir DIR` with either command to select a single directory.

`setup --non-interactive` can use a saved PAT, `AZWI_PAT`, or a usable Azure CLI session for an explicitly selected or already configured Azure CLI method. It never prompts or launches sign-in. Without a configured method or PAT, it saves the organization and installs the skill, then returns exit code 4 with `ready: false` and repair instructions. A failure to save an entered PAT also returns incomplete readiness and environment-variable instructions. Sandboxed and remote agents need access to the credentials or Azure CLI session in their own execution environment.

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

`config show` displays the effective resolved configuration. Config updates create the file when needed and never write `AZWI_PAT` into it. `config check` does not create or update azwi configuration, credentials, or skill files.

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

`setup` and `skill install` install `azure-workitem/SKILL.md` under `~/.agents/skills`. When the Claude Code profile directory exists, they also install the skill under its `skills` subdirectory and create that subdirectory if needed. The profile defaults to `~/.claude`. An absolute `CLAUDE_CONFIG_DIR` selects a different profile. A shared directory is handled once.

```powershell
uvx azwi skill install
uvx azwi skill status
uvx azwi skill status --format plain
uvx azwi skill remove
```

`skill status` and `skill remove` use the same directories. All three skill commands accept `--skills-dir DIR` to select one directory exclusively.

Skill commands return JSON by default. When both directories are selected, the existing fields describe the primary copy under `~/.agents/skills`. The `locations` array contains both results in order, with Claude Code second. Setup and config check report the locations under `skill`. The older `install-skill`, `skill-status`, and `remove-skill` command aliases remain available.

Normal invocations of an installed release automatically update an older managed skill when its installed content is unchanged. Synchronization is local. It does not query PyPI, refresh uv's cache, or update the CLI. Missing skills, unmanaged skills, modified managed skills, equal versions, and newer versions are left alone.

Inspect version, integrity, and update eligibility before replacing modified managed content:

```powershell
uvx azwi skill status
uvx azwi skill install --force
```

Install-time `--force` can replace altered content only when the skill is managed by `azwi`. It never overwrites an unmanaged skill or downgrades a newer version.

Automatic synchronization checks existing copies under `~/.agents/skills` and the detected Claude Code skills directory. To add a copy after Claude Code creates its profile directory, rerun `uvx azwi skill install`. Local checkouts, direct source installs, editable builds, and directories outside these locations require explicit skill commands. Updates affect future agent sessions and may not change instructions already loaded by a running agent.

`skill remove` removes the managed `SKILL.md` and its directory when empty. It preserves unrelated files. Removing unmanaged content requires `--force`. Installation and removal refuse linked paths and unexpected file types.

## Reference

Useful discovery and metadata commands:

```powershell
uvx azwi --help
uvx azwi 2195 --help
uvx azwi fields --help
uvx azwi config --help
uvx azwi setup --help
uvx azwi config check --help
uvx azwi skill --help
uvx azwi --about
uvx azwi version
```

Environment variables:

| Variable | Purpose |
| --- | --- |
| `AZWI_PAT` | Overrides every saved authentication method. Otherwise use the selected organization's configured method, with manual PAT lookup as the legacy default |
| `AZWI_ORG` | Default organization for fetch and fields |
| `AZWI_PROJECT` | Default project for project-scoped commands such as `fields` |
| `CLAUDE_CONFIG_DIR` | Absolute path to an existing Claude Code profile directory |

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

These codes describe exits controlled by azwi. Cancelling at the Microsoft sign-in menu returns `4`. Ctrl+C can cause a terminal or launcher to terminate the command and report a different nonzero status, such as `-1` in PowerShell on Windows. Treat any nonzero status as an unsuccessful command. Cancellation does not produce a success report, and Azure CLI login processes are cleaned up when azwi exits.

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
uv run python tests/wheel_smoke.py dist/azwi-1.5.0-py3-none-any.whl
```

The wheel smoke check uses temporary environments and a temporary home directory. It validates wheel packaging, index-style metadata, local and editable installs, `uvx`, and the PEP 723 wrapper. It requires access to build and runtime dependencies.

To release a version:

1. Update the version and changelog, then commit and tag the release, such as `v1.5.0`.
2. Push the commit and tag. The tag triggers GitHub Actions to build and publish to PyPI using Trusted Publishing.
3. Verify the workflow succeeds and create the GitHub release for that tag.

## License

MIT
