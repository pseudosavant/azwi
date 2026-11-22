# Changelog

## 1.5.0 (2026-11-21)

- Install `azure-workitem` in an existing Claude Code profile alongside `~/.agents/skills`. Setup and explicit installation create the profile's `skills` directory if needed and honor an absolute `CLAUDE_CONFIG_DIR`.
- Check, update, and remove managed copies across both locations. Ordinary runs update eligible installed copies. First-time installation requires setup or an explicit install.
- Report both copies in a `locations` array when two directories are selected. Readiness checks identify missing, unmanaged, or unreadable copies by path. Use `--skills-dir` to select a single directory.
- Document the Claude Code skill invocation and test installation, reporting, and updates across both locations.
- Update the PowerShell Ctrl+C test to check the launcher exit code and verify Azure CLI process cleanup.

## 1.4.0 (2026-09-16)

- Add organization-specific managed PAT and temporary Microsoft Entra authentication through Azure CLI. Preserve manual PATs and the AZWI_PAT override.
- Add a setup method menu, tenant discovery, optional device-code sign-in, and an Azure CLI login handoff that resumes setup automatically.
- Keep Windows sign-in waits responsive to Ctrl+C and contain Azure CLI descendants so they cannot survive an abrupt azwi exit.
- Renew managed PATs near expiration and verify expired-token replacements before saving them. Bind management to the verified tenant and account.
- Add non-interactive fetch and fields flags. Update the managed agent skill to prevent unattended sign-in prompts.
- Report authentication method and expiry in readiness checks. Keep local checks free of network calls and URL checks free of PAT mutations.

## 1.2.0 (2026-09-03)

- Automatically synchronize already-installed, pristine older `$azure-workitem` skills with the running CLI release. Keep maintenance notices on stderr.
- Generate lifecycle metadata in `SKILL.md` YAML front matter with the exact CLI version and a normalized content SHA-256 hash. Migrate legacy managed skills and recover missing or malformed versions.
- Add `skill install`, `skill remove`, and read-only `skill status` with JSON and plain status output. Preserve the existing `install-skill` and `remove-skill` aliases.
- Protect modified and unverifiable managed skills. Use `uvx azwi skill install --force` for an explicit replacement. Installation always refuses unmanaged content and newer versions are never downgraded.
- Limit automatic updates to the standard skills directory. Custom locations, local source builds, and editable installs require explicit updates. Installed wheels remain eligible.
- Replace skill files atomically and preserve unrelated files during removal. Updates affect future skill loading. Running agent sessions may retain previously loaded instructions.
- Synchronization does not query PyPI, refresh uv's cache, or update the CLI.
