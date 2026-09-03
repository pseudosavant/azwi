from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from azwi import __version__
from azwi.cli import run_cli
from azwi.client import AzureDevOpsClient


@contextlib.contextmanager
def workspace_dir(name: str):
    base = ROOT / ".test-output"
    base.mkdir(exist_ok=True)
    path = base / f"{name}-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, org: str, pat: str, *, verbose: bool = False, stderr=None) -> None:
        self.org = org
        self.pat = pat
        self.verbose = verbose
        self.stderr = stderr
        self.calls: list[tuple] = []
        FakeClient.instances.append(self)

    def get_work_item(self, work_item_id: int) -> dict:
        self.calls.append(("get_work_item", work_item_id))
        return {
            "id": work_item_id,
            "_links": {"html": {"href": f"https://dev.azure.com/example/_workitems/edit/{work_item_id}"}},
            "fields": {
                "System.Title": "Sample",
                "System.TeamProject": "Payments",
                "System.WorkItemType": "Task",
                "System.State": "Active",
                "System.AssignedTo": {"displayName": "Alice"},
                "System.ChangedDate": "2026-03-10T10:00:00Z",
                "System.Description": "<p>Description</p>",
                "Microsoft.VSTS.Common.AcceptanceCriteria": "<p>Acceptance</p>",
                "Microsoft.VSTS.TCM.ReproSteps": "",
                "Microsoft.VSTS.TCM.SystemInfo": "",
            },
            "relations": [
                {
                    "rel": "AttachedFile",
                    "url": "https://dev.azure.com/example/_apis/wit/attachments/attachment-guid?fileName=notes.txt",
                    "attributes": {"name": "notes.txt", "comment": "Dev notes", "resourceSize": 12},
                },
                {
                    "rel": "AttachedFile",
                    "url": "https://dev.azure.com/example/_apis/wit/attachments/attachment-guid-2?fileName=trace.log",
                    "attributes": {"name": "trace.log", "comment": "Trace", "resourceSize": 34},
                },
                {
                    "rel": "ArtifactLink",
                    "url": "vstfs:///Git/PullRequestId/project-guid%2Frepo-guid%2F17",
                    "attributes": {"name": "Pull Request"},
                }
            ],
        }

    def get_comments(self, project: str, work_item_id: int, limit: int) -> dict:
        self.calls.append(("get_comments", project, work_item_id, limit))
        return {
            "comments": [
                {
                    "createdDate": "2026-03-10T09:00:00Z",
                    "createdBy": {"displayName": "Bob"},
                    "text": "<p>Comment body</p>",
                }
            ]
        }

    def get_pull_request(self, project: str, repo_id: str, pr_id: int) -> dict:
        self.calls.append(("get_pull_request", project, repo_id, pr_id))
        return {
            "pullRequestId": pr_id,
            "title": "Fix",
            "sourceRefName": "refs/heads/fix",
            "status": "active",
            "repository": {"id": repo_id},
            "_links": {"web": {"href": "https://dev.azure.com/example/_git/repo/pullrequest/17"}},
        }

    def get_pull_request_threads(self, project: str, repo_id: str, pr_id: int) -> dict:
        self.calls.append(("get_pull_request_threads", project, repo_id, pr_id))
        return {
            "value": [
                {
                    "id": 101,
                    "status": "active",
                    "threadContext": {"filePath": "/src/app.py", "rightFileEnd": {"line": 42}},
                    "comments": [
                        {
                            "publishedDate": "2026-03-10T11:00:00Z",
                            "author": {"displayName": "Reviewer"},
                            "content": "<p>Please handle empty values.</p>",
                            "commentType": "text",
                        }
                    ],
                },
                {
                    "id": 102,
                    "status": "fixed",
                    "threadContext": {"filePath": "/src/done.py", "rightFileEnd": {"line": 9}},
                    "comments": [
                        {
                            "publishedDate": "2026-03-10T12:00:00Z",
                            "author": {"displayName": "Reviewer"},
                            "content": "<p>Resolved issue.</p>",
                            "commentType": "text",
                        }
                    ],
                },
            ]
        }

    def get_work_item_type_fields(self, project: str, work_item_type: str) -> dict:
        self.calls.append(("get_work_item_type_fields", project, work_item_type))
        return {
            "value": [
                {"name": "Title", "referenceName": "System.Title", "type": "string"},
                {"name": "Description", "referenceName": "System.Description", "type": "html"},
            ]
        }

    def download(self, url: str) -> tuple[bytes, str]:
        self.calls.append(("download", url))
        if "attachments" in url:
            return b"attachment-data", "text/plain"
        return b"image-data", "image/png"


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.instances.clear()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        skills = patch("azwi.skill.default_skills_dir", return_value=Path(temporary.name) / "skills")
        skills.start()
        self.addCleanup(skills.stop)

    def test_root_help_and_fetch_help_match_contract(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = run_cli([], stdout=stdout, stderr=stderr, env={}, config_path=None, client_factory=FakeClient, program="azwi")
        self.assertEqual(exit_code, 0)
        self.assertIn("azwi <work_item_id> [options]", stdout.getvalue())
        self.assertIn("Usually the only argument you need is the work item ID.", stdout.getvalue())
        self.assertIn("install-skill", stdout.getvalue())
        self.assertIn("remove-skill", stdout.getvalue())
        self.assertIn("azwi --about", stdout.getvalue())
        self.assertIn("Project:\n  https://github.com/pseudosavant/azwi", stdout.getvalue())
        self.assertIn("License:\n  MIT", stdout.getvalue())
        self.assertNotIn("--repo", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = run_cli(
                ["2195", "--help"],
                stdout=stdout,
                stderr=stderr,
                env={},
                config_path=None,
                client_factory=FakeClient,
                program="azwi",
            )
        self.assertEqual(exit_code, 0)
        self.assertIn("Usually the only required argument is the work item ID.", stdout.getvalue())
        self.assertIn("--field-acceptance REFNAME", stdout.getvalue())
        self.assertIn("Project:\n  https://github.com/pseudosavant/azwi", stdout.getvalue())
        self.assertIn("License:\n  MIT", stdout.getvalue())
        self.assertNotIn("--project", stdout.getvalue())

    def test_about_prints_project_and_license(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = run_cli(
            ["--about"],
            stdout=stdout,
            stderr=stderr,
            env={},
            config_path=None,
            client_factory=FakeClient,
            program="azwi",
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            stdout.getvalue(),
            f"azwi {__version__}\n\n"
            "Agent-first CLI for fetching Azure DevOps work item context as Markdown or JSON.\n\n"
            "Project: https://github.com/pseudosavant/azwi\n"
            "License: MIT\n",
        )
        self.assertEqual(stderr.getvalue(), "")

    def test_client_user_agent_tracks_package_version(self) -> None:
        client = AzureDevOpsClient("example-org", "token")

        headers = client._headers(allow_auth=False, accept="application/json")
        self.assertEqual(headers["User-Agent"], f"azwi/{__version__}")

    def test_install_and_remove_skill_commands_output_json(self) -> None:
        with workspace_dir("skill-command") as skills_root:
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = run_cli(
                ["install-skill", "--skills-dir", str(skills_root)],
                stdout=stdout,
                stderr=stderr,
                env={},
                config_path=None,
                client_factory=FakeClient,
                program="azwi",
            )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["installed"])
            self.assertEqual(payload["skill"], "azure-workitem")
            self.assertTrue((skills_root / "azure-workitem" / "SKILL.md").exists())
            self.assertEqual(stderr.getvalue(), "")

            stdout = io.StringIO()
            exit_code = run_cli(
                ["remove-skill", "--skills-dir", str(skills_root)],
                stdout=stdout,
                stderr=stderr,
                env={},
                config_path=None,
                client_factory=FakeClient,
                program="azwi",
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue(json.loads(stdout.getvalue())["removed"])
            self.assertFalse((skills_root / "azure-workitem").exists())

    def test_fetch_selected_sections_and_stdout_stderr_behavior(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = run_cli(
            ["2195", "--format", "markdown", "--section", "comments", "--comment-limit", "20"],
            stdout=stdout,
            stderr=stderr,
            env={"AZWI_ORG": "example-org", "AZWI_PAT": "token"},
            config_path=None,
            client_factory=FakeClient,
            program="azwi",
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("# Discussion", stdout.getvalue())
        self.assertNotIn("# Metadata", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn(("get_comments", "Payments", 2195, 20), FakeClient.instances[-1].calls)

    def test_default_fetch_output_is_json(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = run_cli(
            ["2195"],
            stdout=stdout,
            stderr=stderr,
            env={"AZWI_ORG": "example-org", "AZWI_PAT": "token"},
            config_path=None,
            client_factory=FakeClient,
            program="azwi",
        )

        self.assertEqual(exit_code, 0)
        self.assertIn('"work_item"', stdout.getvalue())
        self.assertIn('"sections"', stdout.getvalue())
        self.assertIn('"attachments"', stdout.getvalue())
        self.assertNotIn("# Metadata", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_interactive_fetch_shows_progress_on_stderr(self) -> None:
        stdout = io.StringIO()
        stderr = TtyStringIO()
        exit_code = run_cli(
            ["2195"],
            stdout=stdout,
            stderr=stderr,
            env={"AZWI_ORG": "example-org", "AZWI_PAT": "token"},
            config_path=None,
            client_factory=FakeClient,
            program="azwi",
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("Fetching work item 2195", stderr.getvalue())
        self.assertIn("Rendering output", stderr.getvalue())
        self.assertIn('"work_item"', stdout.getvalue())

    def test_fetch_with_output_writes_file_and_not_stdout(self) -> None:
        with workspace_dir("fetch-output") as temp_dir:
            output_path = temp_dir / "wi.md"
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = run_cli(
                ["2195", "--format", "markdown", "--output", str(output_path), "--force", "--section", "metadata"],
                stdout=stdout,
                stderr=stderr,
                env={"AZWI_ORG": "example-org", "AZWI_PAT": "token"},
                config_path=None,
                client_factory=FakeClient,
                program="azwi",
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("# Metadata", output_path.read_text(encoding="utf-8"))

    def test_config_commands_create_file(self) -> None:
        with workspace_dir("config-output") as temp_dir:
            config_path = temp_dir / ".azwi" / "config.toml"
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = run_cli(
                ["config", "set-defaults", "--org", "my-org", "--project", "Payments"],
                stdout=stdout,
                stderr=stderr,
                env={},
                config_path=config_path,
                client_factory=FakeClient,
                program="azwi",
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(config_path.exists())
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")

    def test_download_images_requires_output(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = run_cli(
            ["2195", "--download-images", "assets"],
            stdout=stdout,
            stderr=stderr,
            env={"AZWI_ORG": "example-org", "AZWI_PAT": "token"},
            config_path=None,
            client_factory=FakeClient,
            program="azwi",
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("--download-images requires --output", stderr.getvalue())

    def test_download_attachments_implies_attachments_section(self) -> None:
        with workspace_dir("attachments-output") as temp_dir:
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = run_cli(
                ["2195", "--format", "json", "--section", "metadata", "--download-attachments", str(temp_dir / "files")],
                stdout=stdout,
                stderr=stderr,
                env={"AZWI_ORG": "example-org", "AZWI_PAT": "token"},
                config_path=None,
                client_factory=FakeClient,
                program="azwi",
            )

            self.assertEqual(exit_code, 0)
            self.assertIn('"attachments"', stdout.getvalue())
            self.assertIn('"local_path"', stdout.getvalue())
            self.assertEqual((temp_dir / "files" / "notes.txt").read_bytes(), b"attachment-data")
            self.assertEqual((temp_dir / "files" / "trace.log").read_bytes(), b"attachment-data")
            self.assertIn(
                ("download", "https://dev.azure.com/example/_apis/wit/attachments/attachment-guid?fileName=notes.txt"),
                FakeClient.instances[-1].calls,
            )

    def test_attachment_name_filter_downloads_only_matching_attachment(self) -> None:
        with workspace_dir("attachment-filter-output") as temp_dir:
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = run_cli(
                [
                    "2195",
                    "--format",
                    "json",
                    "--download-attachments",
                    str(temp_dir / "files"),
                    "--attachment-name",
                    "trace.log",
                ],
                stdout=stdout,
                stderr=stderr,
                env={"AZWI_ORG": "example-org", "AZWI_PAT": "token"},
                config_path=None,
                client_factory=FakeClient,
                program="azwi",
            )

            self.assertEqual(exit_code, 0)
            self.assertIn('"name": "trace.log"', stdout.getvalue())
            self.assertNotIn('"name": "notes.txt"', stdout.getvalue())
            self.assertFalse((temp_dir / "files" / "notes.txt").exists())
            self.assertEqual((temp_dir / "files" / "trace.log").read_bytes(), b"attachment-data")

    def test_attachment_url_filter_is_exact_and_reports_misses(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = run_cli(
            ["2195", "--section", "attachments", "--attachment-url", "https://example.invalid/missing"],
            stdout=stdout,
            stderr=stderr,
            env={"AZWI_ORG": "example-org", "AZWI_PAT": "token"},
            config_path=None,
            client_factory=FakeClient,
            program="azwi",
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Attachment selector did not match: url=https://example.invalid/missing", stderr.getvalue())

    def test_include_pr_comments_implies_prs_and_defaults_to_active_threads(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = run_cli(
            ["2195", "--format", "json", "--section", "metadata", "--include-pr-comments"],
            stdout=stdout,
            stderr=stderr,
            env={"AZWI_ORG": "example-org", "AZWI_PAT": "token"},
            config_path=None,
            client_factory=FakeClient,
            program="azwi",
        )

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("Please handle empty values.", output)
        self.assertNotIn("Resolved issue.", output)
        self.assertIn(("get_pull_request_threads", "Payments", "repo-guid", 17), FakeClient.instances[-1].calls)

    def test_pr_comment_status_all_includes_resolved_threads(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = run_cli(
            ["2195", "--format", "json", "--section", "prs", "--include-pr-comments", "--pr-comment-status", "all"],
            stdout=stdout,
            stderr=stderr,
            env={"AZWI_ORG": "example-org", "AZWI_PAT": "token"},
            config_path=None,
            client_factory=FakeClient,
            program="azwi",
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("Please handle empty values.", stdout.getvalue())
        self.assertIn("Resolved issue.", stdout.getvalue())

    def test_missing_pat_returns_auth_exit_code(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = run_cli(
            ["2195"],
            stdout=stdout,
            stderr=stderr,
            env={"AZWI_ORG": "example-org"},
            config_path=None,
            client_factory=AzureDevOpsClient,
            program="azwi",
        )

        self.assertEqual(exit_code, 4)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("AZWI_PAT is not set", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
