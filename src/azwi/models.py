from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextSection:
    field_ref: str
    markdown: str


@dataclass(frozen=True)
class CommentItem:
    created_date: str
    author_display_name: str
    markdown: str


@dataclass(frozen=True)
class PullRequestItem:
    pr_id: int
    title: str
    source_branch: str
    status: str
    url: str


@dataclass(frozen=True)
class RenderedWorkItem:
    work_item_id: int
    title: str
    url: str
    work_item_type: str
    metadata: dict[str, str]
    description: TextSection
    acceptance: TextSection
    repro_steps: TextSection
    system_info: TextSection
    comments: tuple[CommentItem, ...]
    prs: tuple[PullRequestItem, ...]
    extra_fields: tuple[tuple[str, TextSection], ...]
    selected_sections: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedConfig:
    org: str | None
    project: str | None
    fields: dict[str, str]
    extra_fields: tuple[str, ...]
