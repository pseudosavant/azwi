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
    extract_pull_request_refs,
    localize_markdown_images,
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
                "_links": {"web": {"href": "https://dev.azure.com/example/_git/repo/pullrequest/17"}},
            }
        ]

        rendered = build_rendered_work_item(
            work_item,
            comments_payload=comments_payload,
            pull_request_payloads=prs,
            fields={
                "description": "System.Description",
                "acceptance": "Microsoft.VSTS.Common.AcceptanceCriteria",
                "repro_steps": "Microsoft.VSTS.TCM.ReproSteps",
                "system_info": "Microsoft.VSTS.TCM.SystemInfo",
            },
            extra_fields=["Custom.DevNotes"],
            selected_sections=("metadata", "description", "acceptance", "comments", "prs"),
        )

        markdown = render_markdown(rendered)
        payload = json.loads(render_json(rendered))

        self.assertIn("# Metadata", markdown)
        self.assertIn("## Repro Steps", markdown)
        self.assertIn("## System Info", markdown)
        self.assertIn("@Alice Smith", markdown)
        self.assertIn("# Additional Fields", markdown)
        self.assertEqual(payload["sections"]["description"]["field"], "System.Description")
        self.assertEqual(
            payload["sections"]["extra_fields"]["Custom.DevNotes"]["field"],
            "Custom.DevNotes",
        )
        self.assertEqual(payload["sections"]["comments"][0]["author"], "Bob")

    def test_extract_pull_request_refs(self) -> None:
        relations = [
            {
                "rel": "ArtifactLink",
                "url": "vstfs:///Git/PullRequestId/11111111-1111-1111-1111-111111111111%2Frepo-1%2F42",
                "attributes": {"name": "Pull Request"},
            }
        ]

        self.assertEqual(extract_pull_request_refs(relations), [("repo-1", 42)])

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
