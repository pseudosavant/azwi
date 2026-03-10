from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Mapping

from azwi.errors import ConfigError
from azwi.models import ResolvedConfig

DEFAULT_FIELD_REFS = {
    "description": "System.Description",
    "acceptance": "Microsoft.VSTS.Common.AcceptanceCriteria",
    "repro_steps": "Microsoft.VSTS.TCM.ReproSteps",
    "system_info": "Microsoft.VSTS.TCM.SystemInfo",
}

CONFIG_DIRECTORY = ".azwi"
CONFIG_FILENAME = "config.toml"


def default_config_path(home: Path | None = None) -> Path:
    base = home if home is not None else Path.home()
    return base / CONFIG_DIRECTORY / CONFIG_FILENAME


def load_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path if path is not None else default_config_path()
    if not config_path.exists():
        return {}
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    return data if isinstance(data, dict) else {}


def save_config(data: Mapping[str, Any], path: Path | None = None) -> Path:
    config_path = path if path is not None else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(dumps_toml(dict(data)), encoding="utf-8", newline="\n")
    return config_path


def resolve_config(
    raw_config: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    cli_org: str | None = None,
    cli_project: str | None = None,
    resolved_project: str | None = None,
    cli_field_overrides: Mapping[str, str | None] | None = None,
    cli_extra_fields: list[str] | tuple[str, ...] | None = None,
) -> ResolvedConfig:
    env_map = env if env is not None else os.environ
    defaults = _get_table(raw_config, "defaults")
    default_org = _as_string(defaults.get("org"))
    org = _first_non_empty(cli_org, default_org, env_map.get("AZWI_ORG"))
    org_profile = _get_org_profile(raw_config, org)
    org_defaults = _get_table(org_profile, "defaults")

    project = _first_non_empty(
        cli_project,
        _as_string(org_defaults.get("project")),
        _as_string(defaults.get("project")),
        env_map.get("AZWI_PROJECT"),
    )
    if resolved_project:
        project = resolved_project

    fields = dict(DEFAULT_FIELD_REFS)
    extra_fields: list[str] = []
    _merge_field_table(fields, extra_fields, _get_table(defaults, "fields"))
    _merge_field_table(fields, extra_fields, _get_table(org_defaults, "fields"))
    if project:
        _merge_field_table(fields, extra_fields, _get_project_field_table(raw_config, project))
        _merge_field_table(fields, extra_fields, _get_project_field_table(org_profile, project))

    if cli_field_overrides:
        for key, value in cli_field_overrides.items():
            if value:
                fields[key] = value
    extra_fields = _merge_unique(extra_fields, cli_extra_fields or [])
    return ResolvedConfig(org=org, project=project, fields=fields, extra_fields=tuple(extra_fields))


def render_resolved_config(resolved: ResolvedConfig) -> str:
    document: dict[str, Any] = {"resolved": {}}
    if resolved.org:
        document["resolved"]["org"] = resolved.org
    if resolved.project:
        document["resolved"]["project"] = resolved.project
    field_table = dict(resolved.fields)
    field_table["extra_fields"] = list(resolved.extra_fields)
    document["resolved"]["fields"] = field_table
    return dumps_toml(document)


def set_defaults(
    raw_config: dict[str, Any],
    *,
    org: str | None = None,
    project: str | None = None,
    scope_org: str | None = None,
) -> dict[str, Any]:
    target = (
        _ensure_table(raw_config, "defaults")
        if scope_org is None
        else _ensure_table(raw_config, "orgs", scope_org, "defaults")
    )
    if scope_org is not None and org:
        raise ConfigError("--org cannot be used with --for-org for config set-defaults.")
    if org is not None:
        target["org"] = org
    if project is not None:
        target["project"] = project
    return raw_config


def set_fields(
    raw_config: dict[str, Any],
    *,
    field_values: Mapping[str, str | None],
    global_scope: bool,
    project: str | None = None,
    scope_org: str | None = None,
) -> dict[str, Any]:
    field_table = _target_field_table(
        raw_config,
        global_scope=global_scope,
        project=project,
        scope_org=scope_org,
    )
    changed = False
    for key, value in field_values.items():
        if value:
            field_table[key] = value
            changed = True
    if not changed:
        raise ConfigError("No field updates were provided.")
    return raw_config


def add_extra_field(
    raw_config: dict[str, Any],
    *,
    refname: str,
    global_scope: bool,
    project: str | None = None,
    scope_org: str | None = None,
) -> dict[str, Any]:
    field_table = _target_field_table(
        raw_config,
        global_scope=global_scope,
        project=project,
        scope_org=scope_org,
    )
    values = field_table.get("extra_fields")
    if isinstance(values, list):
        current = [item for item in values if isinstance(item, str)]
    elif values is None:
        current = []
    else:
        raise ConfigError("Existing extra_fields value is not a TOML array of strings.")
    field_table["extra_fields"] = _merge_unique(current, [refname])
    return raw_config


def dumps_toml(data: Mapping[str, Any]) -> str:
    lines: list[str] = []
    _emit_table(lines, (), data)
    return "\n".join(lines).rstrip() + "\n"


def _emit_table(lines: list[str], path: tuple[str, ...], table: Mapping[str, Any]) -> None:
    scalars: list[tuple[str, Any]] = []
    subtables: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in table.items():
        if isinstance(value, Mapping):
            subtables.append((key, value))
        else:
            scalars.append((key, value))

    if path:
        if lines:
            lines.append("")
        lines.append(f"[{'.'.join(_toml_key(part) for part in path)}]")
    for key, value in scalars:
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, value in subtables:
        _emit_table(lines, path + (key,), value)


def _toml_key(key: str) -> str:
    if key.isidentifier() and "-" not in key:
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ConfigError(f"Unsupported config value type: {type(value).__name__}")


def _ensure_table(root: dict[str, Any], *path: str) -> dict[str, Any]:
    current = root
    for part in path:
        next_value = current.get(part)
        if next_value is None:
            next_value = {}
            current[part] = next_value
        if not isinstance(next_value, dict):
            raise ConfigError(f"Config path {'.'.join(path)} is not a table.")
        current = next_value
    return current


def _target_field_table(
    raw_config: dict[str, Any],
    *,
    global_scope: bool,
    project: str | None,
    scope_org: str | None,
) -> dict[str, Any]:
    if global_scope == (project is not None):
        raise ConfigError("Use exactly one of --global or --project for this config command.")
    if global_scope:
        return (
            _ensure_table(raw_config, "defaults", "fields")
            if scope_org is None
            else _ensure_table(raw_config, "orgs", scope_org, "defaults", "fields")
        )
    return (
        _ensure_table(raw_config, "projects", project, "fields")
        if scope_org is None
        else _ensure_table(raw_config, "orgs", scope_org, "projects", project, "fields")
    )


def _get_org_profile(raw_config: Mapping[str, Any], org: str | None) -> Mapping[str, Any]:
    if not org:
        return {}
    return _get_table(_get_table(raw_config, "orgs"), org)


def _get_project_field_table(raw_config: Mapping[str, Any], project: str) -> Mapping[str, Any]:
    return _get_table(_get_table(_get_table(raw_config, "projects"), project), "fields")


def _get_table(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, Mapping) else {}


def _merge_field_table(fields: dict[str, str], extra_fields: list[str], table: Mapping[str, Any]) -> None:
    for key in DEFAULT_FIELD_REFS:
        value = _as_string(table.get(key))
        if value:
            fields[key] = value
    extra = table.get("extra_fields")
    if isinstance(extra, list):
        values = [item for item in extra if isinstance(item, str) and item]
        extra_fields[:] = _merge_unique(extra_fields, values)


def _merge_unique(existing: list[str], new_values: list[str] | tuple[str, ...]) -> list[str]:
    seen = {item for item in existing if item}
    merged = list(existing)
    for item in new_values:
        if item and item not in seen:
            merged.append(item)
            seen.add(item)
    return merged


def _first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value:
            return value
    return None


def _as_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
