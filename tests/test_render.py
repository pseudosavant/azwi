from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from azwi.render import (
    build_rendered_work_item,
    download_attachments,
    extract_attachments,
    filter_attachments,
    extract_pull_request_refs,
    localize_markdown_images,
    missing_attachment_selectors,
    render_json,
    render_markdown,
)


def workspace_dir(name: str) -> Path:
    base = ROOT / ".test-output"
    base.mkdir(exist_ok=True)
    path = base / f"{name}-{uuid.uuid4().hex}"
    path.mkdir()
    return path


class RenderTests(unittest.TestCase):
    def test_bug_rendering_mentions_and_json_contract(self) -> None:
        work_item = {
            "id": 2195,
            "url": "https://dev.azure.com/example/_apis/wit/workItems/2195",
            "_links": {"html": {"href": "https://dev.azure.com/example/_workitems/edit/2195"}},
            "fields": {
                "System.Title": "Login bug",
                "System.WorkItemType": "Bug",
                "System.State": "Active",
                "System.AssignedTo": {"displayName": "Alice"},
                "System.ChangedDate": "2026-03-10T10:00:00Z",
                "System.Description": "<p>Main <strong>issue</strong></p>",
                "Microsoft.VSTS.Common.AcceptanceCriteria": "<p>Should be fixed</p>",
                "Microsoft.VSTS.TCM.ReproSteps": "<ol><li>Open app</li><li>Click sign in</li></ol>",
                "Microsoft.VSTS.TCM.SystemInfo": "<p>Windows 11</p>",
                "Custom.DevNotes": "<p>Internal note</p>",
            },
            "relations": [
                {
                    "rel": "AttachedFile",
                    "url": "https://dev.azure.com/example/_apis/wit/attachments/a1?fileName=notes.txt",
                    "attributes": {"name": "notes.txt", "comment": "Implementation notes", "resourceSize": 25},
                }
            ],
        }
        comments_payload = {
            "comments": [
                {
                    "createdDate": "2026-03-10T09:00:00Z",
                    "createdBy": {"displayName": "Bob"},
                    "text": "<p>Hello @<11111111-1111-1111-1111-111111111111></p>",
                    "mentions": [{"id": "11111111-1111-1111-1111-111111111111", "displayName": "Alice Smith"}],
                }
            ]
        }
        prs = [
            {
                "pullRequestId": 17,
                "title": "Fix login bug",
                "sourceRefName": "refs/heads/fix-login",
                "status": "active",
                "repository": {"id": "repo-1"},
                "_links": {"web": {"href": "https://dev.azure.com/example/_git/repo/pullrequest/17"}},
            }
        ]
        pr_threads = {
            ("repo-1", 17): {
                "value": [
                    {
                        "id": 501,
                        "status": "active",
                        "threadContext": {"filePath": "/src/login.py", "rightFileEnd": {"line": 8}},
                        "comments": [
                            {
                                "publishedDate": "2026-03-10T10:15:00Z",
                                "author": {"displayName": "Dana"},
                                "content": "<p>Check null users.</p>",
                                "commentType": "text",
                            }
                        ],
                    }
                ]
            }
        }

        rendered = build_rendered_work_item(
            work_item,
            comments_payload=comments_payload,
            pull_request_payloads=prs,
            pull_request_thread_payloads=pr_threads,
            fields={
                "description": "System.Description",
                "acceptance": "Microsoft.VSTS.Common.AcceptanceCriteria",
                "repro_steps": "Microsoft.VSTS.TCM.ReproSteps",
                "system_info": "Microsoft.VSTS.TCM.SystemInfo",
            },
            extra_fields=["Custom.DevNotes"],
            selected_sections=("metadata", "description", "acceptance", "comments", "attachments", "prs"),
        )

        markdown = render_markdown(rendered)
        payload = json.loads(render_json(rendered))

        self.assertIn("# Metadata", markdown)
        self.assertIn("## Repro Steps", markdown)
        self.assertIn("## System Info", markdown)
        self.assertIn("@Alice Smith", markdown)
        self.assertIn("# Attachments", markdown)
        self.assertIn("notes.txt", markdown)
        self.assertIn("Check null users.", markdown)
        self.assertIn("# Additional Fields", markdown)
        self.assertEqual(payload["sections"]["description"]["field"], "System.Description")
        self.assertEqual(
            payload["sections"]["extra_fields"]["Custom.DevNotes"]["field"],
            "Custom.DevNotes",
        )
        self.assertEqual(payload["sections"]["comments"][0]["author"], "Bob")
        self.assertEqual(payload["sections"]["attachments"][0]["name"], "notes.txt")
        self.assertEqual(payload["sections"]["prs"][0]["comments"][0]["path"], "/src/login.py")

    def test_extract_pull_request_refs(self) -> None:
        relations = [
            {
                "rel": "ArtifactLink",
                "url": "vstfs:///Git/PullRequestId/11111111-1111-1111-1111-111111111111%2Frepo-1%2F42",
                "attributes": {"name": "Pull Request"},
            }
        ]

        self.assertEqual(extract_pull_request_refs(relations), [("repo-1", 42)])

    def test_extract_and_download_attachments(self) -> None:
        relations = [
            {
                "rel": "AttachedFile",
                "url": "https://dev.azure.com/example/_apis/wit/attachments/a1?fileName=notes.txt",
                "attributes": {"name": "notes.txt", "comment": "Notes", "resourceSize": "9"},
            },
            {
                "rel": "AttachedFile",
                "url": "https://dev.azure.com/example/_apis/wit/attachments/a2?fileName=trace.log",
                "attributes": {"name": "trace.log", "comment": "Trace", "resourceSize": "11"},
            }
        ]
        attachments = extract_attachments(relations)
        self.assertEqual(attachments[0].name, "notes.txt")
        self.assertEqual(
            filter_attachments(attachments, names=["trace.log"])[0].url,
            "https://dev.azure.com/example/_apis/wit/attachments/a2?fileName=trace.log",
        )
        self.assertEqual(
            missing_attachment_selectors(attachments, urls=["https://example.invalid/missing"]),
            ("url=https://example.invalid/missing",),
        )

        root = workspace_dir("attachments")
        try:
            previous_cwd = Path.cwd()
            os.chdir(root)
            try:
                local_paths = download_attachments(
                    filter_attachments(attachments, names=["notes.txt"]),
                    output_path=None,
                    download_dir="files",
                    downloader=lambda _url: (b"contents", "text/plain"),
                )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(local_paths[relations[0]["url"]], "files/notes.txt")
            self.assertEqual((root / "files" / "notes.txt").read_bytes(), b"contents")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_localize_images_resolves_relative_to_cwd(self) -> None:
        rendered = build_rendered_work_item(
            {
                "id": 1,
                "url": "https://example.invalid/1",
                "fields": {
                    "System.Title": "Title",
                    "System.WorkItemType": "Task",
                    "System.State": "Active",
                    "System.AssignedTo": "Alice",
                    "System.ChangedDate": "2026-03-10",
                    "System.Description": "![diagram](https://example.invalid/media/diagram.png)",
                    "Microsoft.VSTS.Common.AcceptanceCriteria": "",
                    "Microsoft.VSTS.TCM.ReproSteps": "",
                    "Microsoft.VSTS.TCM.SystemInfo": "",
                },
            },
            comments_payload=None,
            pull_request_payloads=[],
            fields={
                "description": "System.Description",
                "acceptance": "Microsoft.VSTS.Common.AcceptanceCriteria",
                "repro_steps": "Microsoft.VSTS.TCM.ReproSteps",
                "system_info": "Microsoft.VSTS.TCM.SystemInfo",
            },
            extra_fields=[],
            selected_sections=("description",),
        )

        root = workspace_dir("render-images")
        try:
            output_dir = root / "out"
            output_dir.mkdir()
            output_path = output_dir / "work-item.md"
            previous_cwd = Path.cwd()
            os.chdir(root)
            try:
                localized = localize_markdown_images(
                    rendered,
                    output_path=output_path,
                    download_dir="assets",
                    downloader=lambda _url: (b"png-bytes", "image/png"),
                )
            finally:
                os.chdir(previous_cwd)

            self.assertIn("![diagram](../assets/diagram.png)", localized.description.markdown)
            self.assertEqual((root / "assets" / "diagram.png").read_bytes(), b"png-bytes")
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
