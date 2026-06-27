from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, unquote, urlparse

from azwi.models import AttachmentItem, CommentItem, PullRequestCommentItem, PullRequestItem, RenderedWorkItem, TextSection

SECTION_ORDER = ("metadata", "description", "acceptance", "comments", "attachments", "prs")
IMAGE_PATTERN = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>https?://[^)\s]+)\)")
MENTION_PATTERN = re.compile(r"@<([0-9a-fA-F-]{36})>")


def normalize_sections(requested_sections: list[str] | None) -> tuple[str, ...]:
    if not requested_sections:
        return SECTION_ORDER
    wanted = set(requested_sections)
    return tuple(section for section in SECTION_ORDER if section in wanted)


def build_rendered_work_item(
    work_item: dict[str, Any],
    *,
    comments_payload: dict[str, Any] | None,
    pull_request_payloads: list[dict[str, Any]],
    fields: dict[str, str],
    extra_fields: Iterable[str],
    selected_sections: tuple[str, ...],
    attachment_local_paths: Mapping[str, str] | None = None,
    attachment_names: Iterable[str] = (),
    attachment_urls: Iterable[str] = (),
    pull_request_thread_payloads: Mapping[tuple[str, int], dict[str, Any]] | None = None,
    pr_comment_status: str = "active",
    include_pr_system_comments: bool = False,
) -> RenderedWorkItem:
    raw_fields = work_item.get("fields", {})
    work_item_type = _stringify(raw_fields.get("System.WorkItemType"))
    metadata = {
        "type": work_item_type,
        "state": _stringify(raw_fields.get("System.State")),
        "assigned_to": _display_name(raw_fields.get("System.AssignedTo")),
        "changed_date": _stringify(raw_fields.get("System.ChangedDate")),
    }
    description = TextSection(fields["description"], _render_field(raw_fields.get(fields["description"])))
    acceptance = TextSection(fields["acceptance"], _render_field(raw_fields.get(fields["acceptance"])))
    repro_steps = TextSection(fields["repro_steps"], _render_field(raw_fields.get(fields["repro_steps"])))
    system_info = TextSection(fields["system_info"], _render_field(raw_fields.get(fields["system_info"])))
    comments = parse_comments(comments_payload or {})
    attachments = filter_attachments(
        extract_attachments(work_item.get("relations"), local_paths=attachment_local_paths or {}),
        names=attachment_names,
        urls=attachment_urls,
    )
    thread_payloads = pull_request_thread_payloads or {}
    prs = tuple(
        parse_pull_request(
            item,
            comments=parse_pull_request_comments(
                thread_payloads.get(_pull_request_ref(item), {}),
                status=pr_comment_status,
                include_system_comments=include_pr_system_comments,
            ),
        )
        for item in pull_request_payloads
    )
    rendered_extra_fields = []
    for refname in extra_fields:
        rendered_extra_fields.append((refname, TextSection(refname, _render_field(raw_fields.get(refname)))))

    return RenderedWorkItem(
        work_item_id=int(work_item.get("id", 0)),
        title=_stringify(raw_fields.get("System.Title")),
        url=_work_item_url(work_item),
        work_item_type=work_item_type,
        metadata=metadata,
        description=description,
        acceptance=acceptance,
        repro_steps=repro_steps,
        system_info=system_info,
        comments=comments,
        attachments=attachments,
        prs=prs,
        extra_fields=tuple(rendered_extra_fields),
        selected_sections=selected_sections,
    )


def ensure_sections(
    selected_sections: tuple[str, ...],
    required_sections: Iterable[str],
) -> tuple[str, ...]:
    wanted = set(selected_sections)
    wanted.update(required_sections)
    return tuple(section for section in SECTION_ORDER if section in wanted)


def extract_pull_request_refs(relations: list[dict[str, Any]] | None) -> list[tuple[str, int]]:
    refs: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for relation in relations or []:
        rel_name = _stringify(_dig(relation, "attributes", "name"))
        url = _stringify(relation.get("url"))
        if rel_name != "Pull Request" and "PullRequestId" not in url:
            continue
        match = re.search(r"PullRequestId/([^/]+)$", url)
        if not match:
            continue
        decoded = unquote(match.group(1))
        parts = decoded.split("/")
        if len(parts) != 3:
            continue
        repo_id = parts[1]
        try:
            pr_id = int(parts[2])
        except ValueError:
            continue
        ref = (repo_id, pr_id)
        if ref not in seen:
            refs.append(ref)
            seen.add(ref)
    return refs


def extract_attachments(
    relations: list[dict[str, Any]] | None,
    *,
    local_paths: Mapping[str, str] | None = None,
) -> tuple[AttachmentItem, ...]:
    paths = local_paths or {}
    items: list[AttachmentItem] = []
    seen: set[str] = set()
    for relation in relations or []:
        rel = _stringify(relation.get("rel"))
        rel_name = _stringify(_dig(relation, "attributes", "name"))
        url = _stringify(relation.get("url"))
        if rel != "AttachedFile" and rel_name != "AttachedFile":
            continue
        if not url or url in seen:
            continue
        seen.add(url)
        items.append(
            AttachmentItem(
                name=_attachment_name(relation),
                url=url,
                comment=_stringify(_dig(relation, "attributes", "comment")),
                size=_optional_int(_dig(relation, "attributes", "resourceSize")),
                local_path=paths.get(url, ""),
            )
        )
    return tuple(items)


def filter_attachments(
    items: Iterable[AttachmentItem],
    *,
    names: Iterable[str] = (),
    urls: Iterable[str] = (),
) -> tuple[AttachmentItem, ...]:
    name_set = {item for item in names if item}
    url_set = {item for item in urls if item}
    if not name_set and not url_set:
        return tuple(items)
    return tuple(item for item in items if item.name in name_set or item.url in url_set)


def missing_attachment_selectors(
    items: Iterable[AttachmentItem],
    *,
    names: Iterable[str] = (),
    urls: Iterable[str] = (),
) -> tuple[str, ...]:
    attachment_items = tuple(items)
    available_names = {item.name for item in attachment_items}
    available_urls = {item.url for item in attachment_items}
    missing = []
    for name in names:
        if name and name not in available_names:
            missing.append(f"name={name}")
    for url in urls:
        if url and url not in available_urls:
            missing.append(f"url={url}")
    return tuple(missing)


def download_attachments(
    attachment_items: Iterable[AttachmentItem],
    *,
    output_path: Path | None,
    download_dir: Path | str,
    downloader,
) -> dict[str, str]:
    downloader_helper = _AttachmentDownloader(output_path=output_path, download_dir=download_dir, downloader=downloader)
    return {item.url: downloader_helper.download(item) for item in attachment_items}


def render_markdown(document: RenderedWorkItem) -> str:
    parts = [f"# {document.work_item_id} {document.title}", ""]
    if "metadata" in document.selected_sections:
        parts.extend(
            [
                "# Metadata",
                "",
                f"- Type: {document.metadata['type']}",
                f"- State: {document.metadata['state']}",
                f"- Assigned To: {document.metadata['assigned_to']}",
                f"- Changed Date: {document.metadata['changed_date']}",
                "",
            ]
        )
    if "description" in document.selected_sections:
        parts.extend(["# Description:", ""])
        if document.description.markdown:
            parts.extend([document.description.markdown, ""])
        if document.work_item_type.lower() == "bug" and document.repro_steps.markdown:
            parts.extend(["## Repro Steps", "", document.repro_steps.markdown, ""])
        if document.work_item_type.lower() == "bug" and document.system_info.markdown:
            parts.extend(["## System Info", "", document.system_info.markdown, ""])
    if "acceptance" in document.selected_sections:
        parts.extend(["# Acceptance Criteria:", ""])
        if document.acceptance.markdown:
            parts.extend([document.acceptance.markdown, ""])
    if "comments" in document.selected_sections:
        parts.extend(["# Discussion", ""])
        for comment in document.comments:
            parts.append(f"- {comment.created_date} - {comment.author_display_name}")
            if comment.markdown:
                for line in comment.markdown.splitlines():
                    parts.append(f"    {line}")
            parts.append("")
    if "attachments" in document.selected_sections:
        parts.extend(["# Attachments", ""])
        for attachment in document.attachments:
            parts.append(f"- {attachment.name}")
            parts.append(f"  - URL: {attachment.url}")
            if attachment.local_path:
                parts.append(f"  - Local Path: {attachment.local_path}")
            if attachment.comment:
                parts.append(f"  - Comment: {attachment.comment}")
            if attachment.size is not None:
                parts.append(f"  - Size: {attachment.size}")
            parts.append("")
    if "prs" in document.selected_sections:
        parts.extend(["# PRs", ""])
        for pr in document.prs:
            parts.append(f"- PR {pr.pr_id} - {pr.title} ({pr.source_branch}) [{pr.status}] {pr.url}")
            if pr.comments:
                parts.append("")
                parts.append("  Comments:")
                for comment in pr.comments:
                    parts.append(f"  - {comment.created_date} - {comment.author_display_name} - {comment.status}")
                    if comment.path:
                        location = comment.path
                        if comment.line is not None:
                            location = f"{location}:{comment.line}"
                        parts.append(f"    {location}")
                    if comment.markdown:
                        for line in comment.markdown.splitlines():
                            parts.append(f"    {line}")
                parts.append("")
        parts.append("")
    if document.extra_fields:
        parts.extend(["# Additional Fields", ""])
        for refname, field_value in document.extra_fields:
            parts.extend([f"## {refname}", ""])
            if field_value.markdown:
                parts.extend([field_value.markdown, ""])

    while parts and parts[-1] == "":
        parts.pop()
    return "\n".join(parts) + "\n"


def render_json(document: RenderedWorkItem) -> str:
    payload = {
        "work_item": {
            "id": document.work_item_id,
            "title": document.title,
            "url": document.url,
        },
        "sections": {
            "metadata": {
                "type": document.metadata["type"] if "metadata" in document.selected_sections else "",
                "state": document.metadata["state"] if "metadata" in document.selected_sections else "",
                "assigned_to": document.metadata["assigned_to"] if "metadata" in document.selected_sections else "",
                "changed_date": document.metadata["changed_date"] if "metadata" in document.selected_sections else "",
            },
            "description": {
                "field": document.description.field_ref,
                "markdown": document.description.markdown if "description" in document.selected_sections else "",
                "repro_steps": {
                    "field": document.repro_steps.field_ref,
                    "markdown": document.repro_steps.markdown if "description" in document.selected_sections else "",
                },
                "system_info": {
                    "field": document.system_info.field_ref,
                    "markdown": document.system_info.markdown if "description" in document.selected_sections else "",
                },
            },
            "acceptance": {
                "field": document.acceptance.field_ref,
                "markdown": document.acceptance.markdown if "acceptance" in document.selected_sections else "",
            },
            "comments": [
                {
                    "created_date": item.created_date,
                    "author": item.author_display_name,
                    "markdown": item.markdown,
                }
                for item in (document.comments if "comments" in document.selected_sections else ())
            ],
            "attachments": [
                {
                    "name": item.name,
                    "url": item.url,
                    "comment": item.comment,
                    "size": item.size,
                    "local_path": item.local_path,
                }
                for item in (document.attachments if "attachments" in document.selected_sections else ())
            ],
            "prs": [
                {
                    "id": item.pr_id,
                    "title": item.title,
                    "source_branch": item.source_branch,
                    "status": item.status,
                    "url": item.url,
                    "comments": [
                        {
                            "thread_id": comment.thread_id,
                            "status": comment.status,
                            "created_date": comment.created_date,
                            "author": comment.author_display_name,
                            "path": comment.path,
                            "line": comment.line,
                            "markdown": comment.markdown,
                        }
                        for comment in item.comments
                    ],
                }
                for item in (document.prs if "prs" in document.selected_sections else ())
            ],
            "extra_fields": {
                refname: {
                    "field": field_value.field_ref,
                    "markdown": field_value.markdown,
                }
                for refname, field_value in document.extra_fields
            },
        },
    }
    return json.dumps(payload, indent=2) + "\n"


def localize_markdown_images(
    document: RenderedWorkItem,
    *,
    output_path: Path,
    download_dir: Path | str,
    downloader,
) -> RenderedWorkItem:
    localizer = _ImageLocalizer(output_path=output_path, download_dir=download_dir, downloader=downloader)
    return replace(
        document,
        description=replace(document.description, markdown=localizer.rewrite(document.description.markdown)),
        acceptance=replace(document.acceptance, markdown=localizer.rewrite(document.acceptance.markdown)),
        repro_steps=replace(document.repro_steps, markdown=localizer.rewrite(document.repro_steps.markdown)),
        system_info=replace(document.system_info, markdown=localizer.rewrite(document.system_info.markdown)),
        comments=tuple(replace(item, markdown=localizer.rewrite(item.markdown)) for item in document.comments),
        extra_fields=tuple(
            (refname, replace(field_value, markdown=localizer.rewrite(field_value.markdown)))
            for refname, field_value in document.extra_fields
        ),
    )


def parse_comments(payload: dict[str, Any]) -> tuple[CommentItem, ...]:
    raw_comments = payload.get("comments")
    if not isinstance(raw_comments, list):
        raw_comments = payload.get("value")
    if not isinstance(raw_comments, list):
        raw_comments = []
    items = []
    for raw_comment in raw_comments:
        mentions = _mention_map(raw_comment.get("mentions"))
        raw_text = _stringify(raw_comment.get("renderedText") or raw_comment.get("text"))
        resolved_text = MENTION_PATTERN.sub(
            lambda match: f"@{mentions.get(match.group(1).lower(), match.group(1))}"
            if match.group(1).lower() in mentions
            else match.group(0),
            raw_text,
        )
        items.append(
            CommentItem(
                created_date=_stringify(raw_comment.get("createdDate")),
                author_display_name=_display_name(raw_comment.get("createdBy")),
                markdown=_render_field(resolved_text),
            )
        )
    return tuple(items)


def parse_pull_request(payload: dict[str, Any], *, comments: tuple[PullRequestCommentItem, ...] = ()) -> PullRequestItem:
    return PullRequestItem(
        pr_id=int(payload.get("pullRequestId") or payload.get("id") or 0),
        title=_stringify(payload.get("title")),
        source_branch=_stringify(payload.get("sourceRefName")),
        status=_stringify(payload.get("status")),
        url=_stringify(_dig(payload, "_links", "web", "href") or payload.get("url")),
        comments=comments,
    )


def parse_pull_request_comments(
    payload: dict[str, Any],
    *,
    status: str,
    include_system_comments: bool,
) -> tuple[PullRequestCommentItem, ...]:
    raw_threads = payload.get("value")
    if not isinstance(raw_threads, list):
        raw_threads = payload.get("threads")
    if not isinstance(raw_threads, list):
        raw_threads = []

    items: list[PullRequestCommentItem] = []
    for thread in raw_threads:
        if not isinstance(thread, dict):
            continue
        thread_status = _stringify(thread.get("status")).lower()
        if status == "active" and thread_status != "active":
            continue
        thread_id = _optional_int(thread.get("id")) or 0
        path = _stringify(_dig(thread, "threadContext", "filePath"))
        line = _optional_int(
            _dig(thread, "threadContext", "rightFileEnd", "line")
            or _dig(thread, "threadContext", "rightFileStart", "line")
            or _dig(thread, "threadContext", "leftFileEnd", "line")
            or _dig(thread, "threadContext", "leftFileStart", "line")
        )
        comments = thread.get("comments")
        if not isinstance(comments, list):
            continue
        for comment in comments:
            if not isinstance(comment, dict) or comment.get("isDeleted") is True:
                continue
            comment_type = _stringify(comment.get("commentType")).lower()
            if comment_type == "system" and not include_system_comments:
                continue
            markdown = _render_field(comment.get("content"))
            if not markdown:
                continue
            items.append(
                PullRequestCommentItem(
                    thread_id=thread_id,
                    status=thread_status,
                    created_date=_stringify(comment.get("publishedDate") or comment.get("lastUpdatedDate")),
                    author_display_name=_display_name(comment.get("author")),
                    path=path,
                    line=line,
                    markdown=markdown,
                )
            )
    return tuple(items)


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list["_Node | str"] = field(default_factory=list)


class _HtmlFragmentParser(HTMLParser):
    VOID_TAGS = {"br", "img", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.lower(), {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag.lower() not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == lowered:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


class _ImageLocalizer:
    def __init__(self, *, output_path: Path, download_dir: Path | str, downloader) -> None:
        self.output_path = output_path.resolve()
        target_dir = Path(download_dir)
        self.download_dir = target_dir if target_dir.is_absolute() else (Path.cwd() / target_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.downloader = downloader
        self.cache: dict[str, Path] = {}
        self.used_names: set[str] = set()

    def rewrite(self, markdown: str) -> str:
        if not markdown:
            return markdown

        def replace_match(match: re.Match[str]) -> str:
            alt = match.group("alt")
            url = match.group("url")
            if url not in self.cache:
                payload, content_type = self.downloader(url)
                target_path = self._target_path(url, content_type)
                target_path.write_bytes(payload)
                self.cache[url] = target_path
            relative = os.path.relpath(self.cache[url], self.output_path.parent)
            return f"![{alt}]({Path(relative).as_posix()})"

        return IMAGE_PATTERN.sub(replace_match, markdown)

    def _target_path(self, url: str, content_type: str | None) -> Path:
        parsed = urlparse(url)
        base_name = Path(unquote(parsed.path)).name
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(base_name).stem).strip("-._")
        if not stem:
            stem = f"image-{len(self.cache) + 1}"
        suffix = Path(base_name).suffix.lower() or _content_type_suffix(content_type)
        name = f"{stem}{suffix}"
        candidate = name
        index = 2
        while candidate in self.used_names:
            candidate = f"{stem}-{index}{suffix}"
            index += 1
        self.used_names.add(candidate)
        return self.download_dir / candidate


class _AttachmentDownloader:
    def __init__(self, *, output_path: Path | None, download_dir: Path | str, downloader) -> None:
        self.output_path = output_path.resolve() if output_path is not None else None
        target_dir = Path(download_dir)
        self.download_dir = target_dir if target_dir.is_absolute() else (Path.cwd() / target_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.downloader = downloader
        self.used_names: set[str] = set()

    def download(self, attachment: AttachmentItem) -> str:
        payload, content_type = self.downloader(attachment.url)
        target_path = self._target_path(attachment, content_type)
        target_path.write_bytes(payload)
        if self.output_path is not None:
            return Path(os.path.relpath(target_path, self.output_path.parent)).as_posix()
        try:
            return Path(os.path.relpath(target_path, Path.cwd())).as_posix()
        except ValueError:
            return str(target_path)

    def _target_path(self, attachment: AttachmentItem, content_type: str | None) -> Path:
        base_name = attachment.name or Path(unquote(urlparse(attachment.url).path)).name
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(base_name).stem).strip("-._")
        if not stem:
            stem = f"attachment-{len(self.used_names) + 1}"
        suffix = Path(base_name).suffix or _content_type_suffix(content_type)
        name = f"{stem}{suffix}"
        candidate = name
        index = 2
        while candidate in self.used_names:
            candidate = f"{stem}-{index}{suffix}"
            index += 1
        self.used_names.add(candidate)
        return self.download_dir / candidate


def html_to_markdown(fragment: str) -> str:
    parser = _HtmlFragmentParser()
    parser.feed(fragment)
    parser.close()
    rendered = "".join(_render_node(child) for child in parser.root.children)
    rendered = re.sub(r"[ \t]+\n", "\n", rendered)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip()


def _render_node(node: _Node | str) -> str:
    if isinstance(node, str):
        return re.sub(r"\s+", " ", node)
    if node.tag in {"p", "div", "section", "article"}:
        content = "".join(_render_node(child) for child in node.children).strip()
        return f"{content}\n\n" if content else "\n\n"
    if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(node.tag[1])
        text = "".join(_render_node(child) for child in node.children).strip()
        return f"{'#' * level} {text}\n\n" if text else ""
    if node.tag == "br":
        return "\n"
    if node.tag in {"strong", "b"}:
        content = "".join(_render_node(child) for child in node.children).strip()
        return f"**{content}**" if content else ""
    if node.tag in {"em", "i"}:
        content = "".join(_render_node(child) for child in node.children).strip()
        return f"_{content}_" if content else ""
    if node.tag == "code":
        content = "".join(_render_node(child) for child in node.children).strip()
        return f"`{content}`" if content else ""
    if node.tag == "pre":
        content = _raw_text(node).strip("\n")
        return f"```\n{content}\n```\n\n" if content else "\n\n"
    if node.tag == "a":
        href = node.attrs.get("href", "").strip()
        content = "".join(_render_node(child) for child in node.children).strip() or href
        if href:
            return f"[{content}]({href})"
        return content
    if node.tag == "img":
        src = node.attrs.get("src", "").strip()
        alt = node.attrs.get("alt", "").strip()
        return f"![{alt}]({src})" if src else alt
    if node.tag == "ul":
        return _render_list(node, ordered=False)
    if node.tag == "ol":
        return _render_list(node, ordered=True)
    if node.tag == "blockquote":
        content = "".join(_render_node(child) for child in node.children).strip()
        if not content:
            return ""
        return "\n".join(f"> {line}" if line else ">" for line in content.splitlines()) + "\n\n"
    return "".join(_render_node(child) for child in node.children)


def _render_list(node: _Node, *, ordered: bool) -> str:
    items = [child for child in node.children if isinstance(child, _Node) and child.tag == "li"]
    lines: list[str] = []
    for index, child in enumerate(items, start=1):
        content = "".join(_render_node(item) for item in child.children).strip()
        if not content:
            continue
        prefix = f"{index}. " if ordered else "- "
        content_lines = content.splitlines() or [""]
        lines.append(prefix + content_lines[0])
        for line in content_lines[1:]:
            lines.append(" " * len(prefix) + line)
    return "\n".join(lines) + ("\n\n" if lines else "")


def _raw_text(node: _Node | str) -> str:
    if isinstance(node, str):
        return node
    if node.tag == "br":
        return "\n"
    return "".join(_raw_text(child) for child in node.children)


def _render_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return ""
        if _looks_like_html(cleaned):
            return html_to_markdown(cleaned)
        return cleaned
    return str(value)


def _looks_like_html(value: str) -> bool:
    return bool(re.search(r"<[A-Za-z/][^>]*>", value))


def _display_name(value: Any) -> str:
    if isinstance(value, dict):
        return _stringify(value.get("displayName") or value.get("name") or value.get("uniqueName"))
    return _stringify(value)


def _attachment_name(relation: dict[str, Any]) -> str:
    name = _stringify(_dig(relation, "attributes", "name"))
    if name and name != "AttachedFile":
        return name
    url = _stringify(relation.get("url"))
    parsed = urlparse(url)
    query_name = parse_qs(parsed.query).get("fileName", [""])[0]
    if query_name:
        return unquote(query_name)
    path_name = Path(unquote(parsed.path)).name
    return path_name or "attachment"


def _pull_request_ref(payload: dict[str, Any]) -> tuple[str, int]:
    repo_id = _stringify(_dig(payload, "repository", "id"))
    pr_id = int(payload.get("pullRequestId") or payload.get("id") or 0)
    return repo_id, pr_id


def _work_item_url(work_item: dict[str, Any]) -> str:
    return _stringify(_dig(work_item, "_links", "html", "href") or work_item.get("url"))


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dig(value: Any, *path: str) -> Any:
    current = value
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _mention_map(mentions: Any) -> dict[str, str]:
    if not isinstance(mentions, list):
        return {}
    mapping: dict[str, str] = {}
    for item in mentions:
        if not isinstance(item, dict):
            continue
        mention_id = _stringify(
            item.get("id")
            or item.get("artifactId")
            or _dig(item, "mentionedArtifact", "id")
            or _dig(item, "identity", "id")
            or _dig(item, "user", "id")
        ).lower()
        name = _stringify(
            item.get("displayName")
            or item.get("name")
            or _dig(item, "mentionedArtifact", "displayName")
            or _dig(item, "identity", "displayName")
            or _dig(item, "user", "displayName")
        )
        if mention_id and name:
            mapping[mention_id] = name
    return mapping


def _content_type_suffix(content_type: str | None) -> str:
    if not content_type:
        return ".bin"
    normalized = content_type.split(";", 1)[0].strip().lower()
    mapping = {
        "image/gif": ".gif",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/svg+xml": ".svg",
        "image/webp": ".webp",
    }
    return mapping.get(normalized, ".bin")
