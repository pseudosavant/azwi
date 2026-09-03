from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from azwi import __version__
from azwi.client import AzureDevOpsClient
from azwi.config import (
    add_extra_field,
    default_config_path,
    load_config,
    render_resolved_config,
    resolve_config,
    save_config,
    set_defaults,
    set_fields,
)
from azwi.errors import AzwiError, ConfigError, UsageError
from azwi.render import (
    build_rendered_work_item,
    download_attachments,
    ensure_sections,
    extract_attachments,
    extract_pull_request_refs,
    filter_attachments,
    localize_markdown_images,
    missing_attachment_selectors,
    normalize_sections,
    render_json,
    render_markdown,
)
from azwi.skill import install_skill, remove_skill, skill_status, sync_skill


PROJECT_URL = "https://github.com/pseudosavant/azwi"
LICENSE_NAME = "MIT"

EXIT_CODES = {
    0: "success",
    2: "usage",
    3: "config",
    4: "auth",
    5: "not found",
    6: "api error",
    7: "throttled",
}


class CompactHelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawTextHelpFormatter):
    pass


class ProgressReporter:
    def __init__(self, stderr, *, enabled: bool) -> None:
        self.stderr = stderr
        self.enabled = enabled
        self._last_len = 0

    def update(self, message: str) -> None:
        if not self.enabled or self.stderr is None:
            return
        text = f"\r{message}"
        pad = max(0, self._last_len - len(text))
        self.stderr.write(text + (" " * pad))
        self.stderr.flush()
        self._last_len = len(text)

    def clear(self) -> None:
        if not self.enabled or self.stderr is None:
            return
        self.stderr.write("\r" + (" " * self._last_len) + "\r")
        self.stderr.flush()
        self._last_len = 0


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(
        sys.argv[1:] if argv is None else argv,
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=os.environ,
        config_path=None,
        client_factory=AzureDevOpsClient,
        program="azwi",
    )


def run_cli(
    argv: Sequence[str],
    *,
    stdout,
    stderr,
    env: Mapping[str, str],
    config_path: Path | None,
    client_factory,
    program: str,
) -> int:
    args = list(argv)
    try:
        if args and args[0] in {"skill", "install-skill", "remove-skill", "skill-status"}:
            return _run_skill(args, stdout=stdout, program=program)
        sync_skill(stderr=stderr)
        if not args or args[0] in {"-h", "--help"}:
            stdout.write(build_root_help(program))
            return 0
        if args == ["--about"]:
            stdout.write(build_about_text())
            return 0
        if args[0] in {"version", "--version"}:
            stdout.write(f"{__version__}\n")
            return 0
        if args[0] == "fields":
            return _run_fields(
                args[1:],
                stdout=stdout,
                stderr=stderr,
                env=env,
                config_path=config_path,
                client_factory=client_factory,
                program=program,
            )
        if args[0] == "config":
            return _run_config(
                args[1:],
                stdout=stdout,
                stderr=stderr,
                env=env,
                config_path=config_path,
                program=program,
            )
        return _run_fetch(
            args,
            stdout=stdout,
            stderr=stderr,
            env=env,
            config_path=config_path,
            client_factory=client_factory,
            program=program,
        )
    except AzwiError as exc:
        stderr.write(f"ERROR: {exc}\n")
        return exc.exit_code
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 0


def build_root_help(program: str) -> str:
    exit_lines = "\n".join(f"  {code}  {label}" for code, label in EXIT_CODES.items())
    return (
        f"Usage:\n"
        f"  {program} <work_item_id> [options]\n\n"
        f"azwi - Fetch Azure DevOps work item context for coding agents.\n\n"
        f"Happy path:\n"
        f"  {program} <work_item_id>\n"
        f"  Usually the only argument you need is the work item ID.\n"
        f"  Add --org only when no default organization is configured.\n\n"
        f"Commands:\n"
        f"  {program} <work_item_id>                 Fetch one work item\n"
        f"  {program} fields --type TYPE             List field reference names for a work item type\n"
        f"  {program} config show                    Show resolved config\n"
        f"  {program} config set-defaults ...        Set defaults in ~/.azwi/config.toml\n"
        f"  {program} skill install [--force]        Install or update $azure-workitem\n"
        f"  {program} skill remove [--force]         Remove $azure-workitem\n"
        f"  {program} skill status [--format plain]  Inspect skill lifecycle state (default: json)\n"
        f"  {program} install-skill / remove-skill   Existing skill command aliases\n"
        f"  {program} --about                        Print version, project, and license\n"
        f"  {program} version                        Print version and exit\n\n"
        f"Common fetch options:\n"
        f"  --org ORG                     Override organization\n"
        f"  --format {{json,markdown}}      Output format (default: json)\n"
        f"  --section NAME                Repeatable section selector\n"
        f"  --output PATH                 Write to file instead of stdout\n"
        f"  --download-images DIR         Requires --output\n"
        f"  --download-attachments DIR    Download work item attachments\n\n"
        f"Env:\n"
        f"  AZWI_PAT      Azure DevOps personal access token\n"
        f"  AZWI_ORG      Default organization for fetch and fields\n"
        f"  AZWI_PROJECT  Default project for fields\n\n"
        f"Sections:\n"
        f"  metadata, description, acceptance, comments, attachments, prs\n\n"
        f"Config:\n"
        f"  ~/.azwi/config.toml stores non-secret defaults and field mappings.\n\n"
        f"Skill updates:\n"
        f"  Installed releases synchronize pristine older managed skills in ~/.agents/skills.\n"
        f"  Local builds and custom locations require explicit skill commands.\n"
        f"  To replace modified managed content: uvx azwi skill install --force\n\n"
        f"Exit codes:\n"
        f"{exit_lines}\n\n"
        f"Examples:\n"
        f"  {program} 2195\n"
        f"  {program} 2195 --org my-org\n"
        f"  {program} 2195 --section metadata --section comments\n"
        f"  {program} 2195 --format markdown\n"
        f"  {program} fields --type Bug --project Payments\n"
        f"  {program} config show\n"
        f"  uvx azwi skill install\n"
        f"  {program} --about\n\n"
        f"Project:\n"
        f"  {PROJECT_URL}\n"
        f"License:\n"
        f"  {LICENSE_NAME}\n"
    )


def build_fetch_help(program: str) -> str:
    exit_lines = "\n".join(f"  {code}  {label}" for code, label in EXIT_CODES.items())
    return (
        f"Usage:\n"
        f"  {program} <work_item_id> [options]\n\n"
        f"azwi - Fetch one Azure DevOps work item.\n\n"
        f"Happy path:\n"
        f"  {program} <work_item_id>\n"
        f"  Usually the only required argument is the work item ID.\n"
        f"  Add --org only when it is not already available from config or AZWI_ORG.\n\n"
        f"Required:\n"
        f"  <work_item_id>                Organization-scoped work item ID\n\n"
        f"Selection:\n"
        f"  --section NAME                Repeatable section selector\n"
        f"  --comment-limit N             1..50; used when comments are requested (default: 10)\n"
        f"  --pr-status {{active,all}}      Linked PR filter (default: active)\n"
        f"  --include-pr-comments         Include PR thread comments under linked PRs\n"
        f"  --pr-comment-status {{active,all}}\n"
        f"                               PR thread comment filter (default: active)\n"
        f"  --include-pr-system-comments  Include Azure DevOps PR system comments\n\n"
        f"Output:\n"
        f"  --format {{json,markdown}}      Output format (default: json)\n"
        f"  --output PATH                 Write to file instead of stdout\n"
        f"  --force                       Overwrite --output target if it exists\n"
        f"  --download-images DIR         Download remote markdown images into DIR; requires --output\n"
        f"  --download-attachments DIR    Download work item attachments into DIR\n"
        f"  --attachment-name NAME        Repeatable exact attachment name filter\n"
        f"  --attachment-url URL          Repeatable exact attachment URL filter\n\n"
        f"Field overrides:\n"
        f"  --field-description REFNAME\n"
        f"  --field-acceptance REFNAME\n"
        f"  --field-repro-steps REFNAME\n"
        f"  --field-system-info REFNAME\n"
        f"  --extra-field REFNAME         Repeatable additional field\n\n"
        f"Targeting:\n"
        f"  --org ORG                     Override organization for this fetch\n"
        f"  Project is resolved from the fetched work item's System.TeamProject field.\n\n"
        f"Sections:\n"
        f"  metadata, description, acceptance, comments, attachments, prs\n\n"
        f"Env:\n"
        f"  AZWI_PAT      Azure DevOps personal access token\n"
        f"  AZWI_ORG      Default organization for fetch\n"
        f"  AZWI_PROJECT  Ignored by direct work item fetch\n\n"
        f"Exit codes:\n"
        f"{exit_lines}\n\n"
        f"Examples:\n"
        f"  {program} 2195\n"
        f"  {program} 2195 --org my-org\n"
        f"  {program} 2195 --format markdown\n"
        f"  {program} 2195 --section metadata --section comments --comment-limit 20\n"
        f"  {program} 2195 --section attachments --download-attachments wi-2195-assets\n"
        f"  {program} 2195 --download-attachments wi-2195-assets --attachment-url URL\n"
        f"  {program} 2195 --section prs --include-pr-comments --pr-comment-status all\n"
        f"  {program} 2195 --output wi-2195.md --download-images assets\n"
        f"\nProject:\n"
        f"  {PROJECT_URL}\n"
        f"License:\n"
        f"  {LICENSE_NAME}\n"
    )


def build_about_text() -> str:
    return (
        f"azwi {__version__}\n\n"
        f"Agent-first CLI for fetching Azure DevOps work item context as Markdown or JSON.\n\n"
        f"Project: {PROJECT_URL}\n"
        f"License: {LICENSE_NAME}\n"
    )


def _write_json_payload(payload: object, stdout: object) -> None:
    stdout.write(json.dumps(payload, indent=2))
    stdout.write("\n")


def _skill_arguments(parser: argparse.ArgumentParser, command: str) -> None:
    parser.add_argument("--skills-dir", type=Path, help="skills root (default: ~/.agents/skills)")
    if command == "install":
        parser.add_argument("--force", action="store_true", help="replace altered managed content. Never overwrite unmanaged content")
    elif command == "remove":
        parser.add_argument("--force", action="store_true", help="remove SKILL.md even if unmanaged. Preserve unrelated files")
    else:
        parser.add_argument("--format", choices=["json", "plain"], default="json", help="output format (default: json)")


def _run_skill(argv: Sequence[str], *, stdout: object, program: str) -> int:
    descriptions = {
        "install": "Install or update $azure-workitem. Preserve altered content unless --force is used.",
        "remove": "Remove $azure-workitem SKILL.md. Preserve unrelated files.",
        "status": "Inspect skill version, integrity, and automatic update eligibility without making changes.",
    }
    aliases = {"install-skill": "install", "remove-skill": "remove", "skill-status": "status"}
    parser = argparse.ArgumentParser(prog=f"{program} {argv[0]}", description="Manage $azure-workitem. These commands do not run automatic synchronization.")
    if argv[0] in aliases:
        command = aliases[argv[0]]
        parser.description = descriptions[command]
        parser.set_defaults(skill_command=command)
        _skill_arguments(parser, command)
    else:
        commands = parser.add_subparsers(dest="skill_command", required=True)
        for command, description in descriptions.items():
            child = commands.add_parser(command, help=description, description=description)
            _skill_arguments(child, command)
    namespace = parser.parse_args(list(argv[1:]))
    if namespace.skill_command == "install":
        payload = install_skill(namespace.skills_dir, force=namespace.force)
    elif namespace.skill_command == "remove":
        payload = remove_skill(namespace.skills_dir, force=namespace.force)
    else:
        payload = skill_status(namespace.skills_dir)
        if namespace.format == "plain":
            for key, value in payload.items():
                rendered = json.dumps(value) if value is None or isinstance(value, bool) else str(value)
                stdout.write(f"{key}: {rendered}\n")
            return 0
    _write_json_payload(payload, stdout)
    return 0


def _run_fetch(
    argv: Sequence[str],
    *,
    stdout,
    stderr,
    env: Mapping[str, str],
    config_path: Path | None,
    client_factory,
    program: str,
) -> int:
    if any(arg in {"-h", "--help"} for arg in argv):
        stdout.write(build_fetch_help(program))
        return 0
    parser = _build_fetch_parser(program)
    namespace = parser.parse_args(list(argv))
    selected_sections = normalize_sections(namespace.section)
    required_sections = []
    attachment_names = namespace.attachment_name or []
    attachment_urls = namespace.attachment_url or []
    if namespace.download_attachments or attachment_names or attachment_urls:
        required_sections.append("attachments")
    if namespace.include_pr_comments or namespace.pr_comment_status != "active" or namespace.include_pr_system_comments:
        namespace.include_pr_comments = True
        required_sections.append("prs")
    selected_sections = ensure_sections(selected_sections, required_sections)
    if namespace.download_images and not namespace.output:
        raise UsageError("--download-images requires --output.")
    output_path = Path(namespace.output).resolve() if namespace.output else None
    if output_path and output_path.exists() and not namespace.force:
        raise UsageError(f"Refusing to overwrite existing file without --force: {output_path}")

    raw_config = load_config(config_path or default_config_path())
    initial_config = resolve_config(raw_config, env=env, cli_org=namespace.org)
    if not initial_config.org:
        raise ConfigError("Organization is required. Use --org, config defaults, or AZWI_ORG.")

    client = client_factory(initial_config.org, env.get("AZWI_PAT", ""), verbose=namespace.verbose, stderr=stderr)
    progress = ProgressReporter(stderr, enabled=_should_show_progress(stderr=stderr, verbose=namespace.verbose))
    try:
        progress.update(f"Fetching work item {namespace.work_item_id}")
        work_item = client.get_work_item(namespace.work_item_id)
        actual_project = work_item.get("fields", {}).get("System.TeamProject")
        resolved = resolve_config(
            raw_config,
            env=env,
            cli_org=namespace.org,
            resolved_project=str(actual_project) if actual_project else None,
            cli_field_overrides={
                "description": namespace.field_description,
                "acceptance": namespace.field_acceptance,
                "repro_steps": namespace.field_repro_steps,
                "system_info": namespace.field_system_info,
            },
            cli_extra_fields=namespace.extra_field or [],
        )
        if not resolved.project:
            raise ConfigError("Fetched work item is missing System.TeamProject.")

        comments_payload = None
        if "comments" in selected_sections:
            progress.update(f"Fetching comments for {namespace.work_item_id}")
            comments_payload = client.get_comments(resolved.project, namespace.work_item_id, namespace.comment_limit)

        attachment_local_paths: dict[str, str] = {}
        selected_attachments = ()
        if "attachments" in selected_sections:
            all_attachments = extract_attachments(work_item.get("relations"))
            missing_selectors = missing_attachment_selectors(
                all_attachments,
                names=attachment_names,
                urls=attachment_urls,
            )
            if missing_selectors:
                raise UsageError("Attachment selector did not match: " + ", ".join(missing_selectors))
            selected_attachments = filter_attachments(
                all_attachments,
                names=attachment_names,
                urls=attachment_urls,
            )
        if namespace.download_attachments:
            progress.update("Downloading attachments")
            attachment_local_paths = download_attachments(
                selected_attachments,
                output_path=output_path,
                download_dir=namespace.download_attachments,
                downloader=client.download,
            )

        pull_request_payloads: list[dict[str, Any]] = []
        pull_request_thread_payloads: dict[tuple[str, int], dict[str, Any]] = {}
        if "prs" in selected_sections:
            pull_request_refs = extract_pull_request_refs(work_item.get("relations"))
            total_pull_requests = len(pull_request_refs)
            for index, (repo_id, pr_id) in enumerate(pull_request_refs, start=1):
                progress.update(f"Fetching linked PRs ({index}/{total_pull_requests})")
                pull_request_payload = client.get_pull_request(resolved.project, repo_id, pr_id)
                if not isinstance(pull_request_payload.get("repository"), dict):
                    pull_request_payload["repository"] = {"id": repo_id}
                if namespace.pr_status != "all" and str(pull_request_payload.get("status", "")).lower() != namespace.pr_status:
                    continue
                pull_request_payloads.append(pull_request_payload)
                if namespace.include_pr_comments:
                    progress.update(f"Fetching PR comments ({index}/{total_pull_requests})")
                    pull_request_thread_payloads[(repo_id, pr_id)] = client.get_pull_request_threads(
                        resolved.project,
                        repo_id,
                        pr_id,
                    )

        rendered = build_rendered_work_item(
            work_item,
            comments_payload=comments_payload,
            pull_request_payloads=pull_request_payloads,
            fields=resolved.fields,
            extra_fields=resolved.extra_fields,
            selected_sections=selected_sections,
            attachment_local_paths=attachment_local_paths,
            attachment_names=attachment_names,
            attachment_urls=attachment_urls,
            pull_request_thread_payloads=pull_request_thread_payloads,
            pr_comment_status=namespace.pr_comment_status,
            include_pr_system_comments=namespace.include_pr_system_comments,
        )
        if namespace.download_images and output_path is not None:
            progress.update("Downloading and rewriting images")
            rendered = localize_markdown_images(
                rendered,
                output_path=output_path,
                download_dir=namespace.download_images,
                downloader=client.download,
            )

        progress.update("Rendering output")
        serialized = render_markdown(rendered) if namespace.format == "markdown" else render_json(rendered)
        progress.clear()
        if output_path is None:
            stdout.write(serialized)
            return 0

        output_path.write_text(serialized, encoding="utf-8", newline="\n")
        return 0
    finally:
        progress.clear()


def _run_fields(
    argv: Sequence[str],
    *,
    stdout,
    stderr,
    env: Mapping[str, str],
    config_path: Path | None,
    client_factory,
    program: str,
) -> int:
    parser = _build_fields_parser(program)
    namespace = parser.parse_args(list(argv))
    raw_config = load_config(config_path or default_config_path())
    resolved = resolve_config(raw_config, env=env, cli_org=namespace.org, cli_project=namespace.project)
    if not resolved.org:
        raise ConfigError("Organization is required. Use --org, config defaults, or AZWI_ORG.")
    if not resolved.project:
        raise ConfigError("Project is required for fields. Use --project, config defaults, or AZWI_PROJECT.")

    client = client_factory(resolved.org, env.get("AZWI_PAT", ""), verbose=namespace.verbose, stderr=stderr)
    response = client.get_work_item_type_fields(resolved.project, namespace.type)
    items = response.get("value")
    if not isinstance(items, list):
        items = response.get("fields", [])

    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rows.append((str(item.get("name", "")), str(item.get("referenceName", "")), str(item.get("type", ""))))
    rows.sort(key=lambda row: (row[1], row[0]))

    stdout.write("| Name | Reference Name | Type |\n")
    stdout.write("| --- | --- | --- |\n")
    for name, refname, field_type in rows:
        stdout.write(f"| {name} | {refname} | {field_type} |\n")
    return 0


def _run_config(
    argv: Sequence[str],
    *,
    stdout,
    stderr,
    env: Mapping[str, str],
    config_path: Path | None,
    program: str,
) -> int:
    parser = _build_config_parser(program)
    namespace = parser.parse_args(list(argv))
    path = config_path or default_config_path()
    raw_config = load_config(path)

    if namespace.config_command == "show":
        resolved = resolve_config(raw_config, env=env, cli_org=namespace.org, cli_project=namespace.project)
        stdout.write(render_resolved_config(resolved))
        return 0

    if namespace.config_command == "set-defaults":
        updated = set_defaults(raw_config, org=namespace.org, project=namespace.project, scope_org=namespace.for_org)
        save_config(updated, path)
        return 0

    if namespace.config_command == "set-field":
        updated = set_fields(
            raw_config,
            field_values={
                "description": namespace.description,
                "acceptance": namespace.acceptance,
                "repro_steps": namespace.repro_steps,
                "system_info": namespace.system_info,
            },
            global_scope=namespace.global_scope,
            project=namespace.project,
            scope_org=namespace.for_org,
        )
        save_config(updated, path)
        return 0

    if namespace.config_command == "add-extra-field":
        updated = add_extra_field(
            raw_config,
            refname=namespace.refname,
            global_scope=namespace.global_scope,
            project=namespace.project,
            scope_org=namespace.for_org,
        )
        save_config(updated, path)
        return 0

    raise UsageError("Unknown config command.")


def _build_fetch_parser(program: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=program,
        formatter_class=CompactHelpFormatter,
        description=(
            "Fetch an Azure DevOps work item.\n\n"
            "Env: AZWI_PAT, AZWI_ORG, AZWI_PROJECT\n"
            "Sections: metadata, description, acceptance, comments, attachments, prs\n"
            "Exit codes: 0 success, 2 usage, 3 config, 4 auth, 5 not found, 6 api error, 7 throttled"
        ),
    )
    parser.add_argument("work_item_id", type=int, help="organization-scoped work item ID")
    parser.add_argument("--org", help="Azure DevOps organization")
    parser.add_argument(
        "--section",
        action="append",
        choices=["metadata", "description", "acceptance", "comments", "attachments", "prs"],
        help="repeatable output section selector",
    )
    parser.add_argument("--comment-limit", type=_comment_limit, default=10, help="max comments when comments are requested")
    parser.add_argument("--pr-status", choices=["active", "all"], default="active", help="linked PR status filter")
    parser.add_argument("--include-pr-comments", action="store_true", help="include PR thread comments under linked PRs")
    parser.add_argument("--pr-comment-status", choices=["active", "all"], default="active", help="PR thread comment status filter")
    parser.add_argument("--include-pr-system-comments", action="store_true", help="include Azure DevOps PR system comments")
    parser.add_argument("--format", choices=["markdown", "json"], default="json", help="output format")
    parser.add_argument("--output", help="write output to PATH instead of stdout")
    parser.add_argument("--force", action="store_true", help="overwrite --output target if it exists")
    parser.add_argument("--download-images", metavar="DIR", help="download remote markdown images into DIR")
    parser.add_argument("--download-attachments", metavar="DIR", help="download work item attachments into DIR")
    parser.add_argument("--attachment-name", action="append", help="repeatable exact attachment name filter")
    parser.add_argument("--attachment-url", action="append", help="repeatable exact attachment URL filter")
    parser.add_argument("--field-description", help="override description field refname")
    parser.add_argument("--field-acceptance", help="override acceptance field refname")
    parser.add_argument("--field-repro-steps", help="override repro steps field refname")
    parser.add_argument("--field-system-info", help="override system info field refname")
    parser.add_argument("--extra-field", action="append", help="repeatable extra field refname")
    parser.add_argument("--verbose", action="store_true", help="send request logs to stderr")
    return parser


def _build_fields_parser(program: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{program} fields",
        formatter_class=CompactHelpFormatter,
        description="List fields for an Azure DevOps work item type.",
    )
    parser.add_argument("--type", required=True, help="work item type name")
    parser.add_argument("--project", help="Azure DevOps project; required unless configured")
    parser.add_argument("--org", help="Azure DevOps organization")
    parser.add_argument("--verbose", action="store_true", help="send request logs to stderr")
    return parser


def _build_config_parser(program: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{program} config",
        formatter_class=CompactHelpFormatter,
        description="Manage ~/.azwi/config.toml.",
    )
    subparsers = parser.add_subparsers(dest="config_command", required=True)

    show = subparsers.add_parser("show", formatter_class=CompactHelpFormatter, help="show effective resolved config")
    show.add_argument("--org", help="resolve config for this organization")
    show.add_argument("--project", help="resolve config for this project")

    set_defaults_parser = subparsers.add_parser("set-defaults", formatter_class=CompactHelpFormatter, help="set default org/project")
    set_defaults_parser.add_argument("--org", help="default organization")
    set_defaults_parser.add_argument("--project", help="default project")
    set_defaults_parser.add_argument("--for-org", help="target an org-specific defaults profile")

    set_field_parser = subparsers.add_parser("set-field", formatter_class=CompactHelpFormatter, help="set field mappings")
    scope = set_field_parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--global", dest="global_scope", action="store_true", help="target defaults.fields")
    scope.add_argument("--project", help='target projects."<ProjectName>".fields')
    set_field_parser.add_argument("--for-org", help="target an org-specific config profile")
    set_field_parser.add_argument("--description", help="logical description field refname")
    set_field_parser.add_argument("--acceptance", help="logical acceptance field refname")
    set_field_parser.add_argument("--repro-steps", help="logical repro steps field refname")
    set_field_parser.add_argument("--system-info", help="logical system info field refname")

    add_extra_parser = subparsers.add_parser("add-extra-field", formatter_class=CompactHelpFormatter, help="append an extra field")
    scope = add_extra_parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--global", dest="global_scope", action="store_true", help="target defaults.fields")
    scope.add_argument("--project", help='target projects."<ProjectName>".fields')
    add_extra_parser.add_argument("--for-org", help="target an org-specific config profile")
    add_extra_parser.add_argument("refname", help="field reference name")

    return parser


def _comment_limit(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 50:
        raise argparse.ArgumentTypeError("comment limit must be between 1 and 50")
    return parsed


def _should_show_progress(*, stderr, verbose: bool) -> bool:
    if verbose or stderr is None:
        return False
    isatty = getattr(stderr, "isatty", None)
    return bool(callable(isatty) and isatty())
