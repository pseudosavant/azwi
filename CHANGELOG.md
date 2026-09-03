# Changelog

## 1.2.0 (2026-09-03)

- Automatically synchronize already-installed, pristine older `$azure-workitem` skills with the running CLI release. Keep maintenance notices on stderr.
- Generate lifecycle metadata in `SKILL.md` YAML front matter with the exact CLI version and a normalized content SHA-256 hash. Migrate legacy managed skills and recover missing or malformed versions.
- Add `skill install`, `skill remove`, and read-only `skill status` with JSON and plain status output. Preserve the existing `install-skill` and `remove-skill` aliases.
- Protect modified and unverifiable managed skills. Use `uvx azwi skill install --force` for an explicit replacement. Installation always refuses unmanaged content and newer versions are never downgraded.
- Limit automatic updates to the standard skills directory. Custom locations, local source builds, and editable installs require explicit updates. Installed wheels remain eligible.
- Replace skill files atomically and preserve unrelated files during removal. Updates affect future skill loading. Running agent sessions may retain previously loaded instructions.
- Synchronization does not query PyPI, refresh uv's cache, or update the CLI.
