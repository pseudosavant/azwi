from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from azwi import __version__, cli, runtime, skill
from azwi.errors import UsageError
from tests.test_cli import FakeClient


HASH_LINE = re.compile(r'(?m)^(  managed-content-sha256: )"[^"]*"')


def signed_text(text: str) -> str:
    """Independent implementation of the documented canonical hash algorithm."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    empty, count = HASH_LINE.subn(r'\1""', text, count=1)
    assert count == 1
    digest = hashlib.sha256(empty.encode("utf-8")).hexdigest()
    return HASH_LINE.sub(lambda match: match[1] + f'"sha256:{digest}"', empty, count=1)


def bundled_version(version: str) -> str:
    with patch.object(skill, "__version__", version):
        return skill.render_skill()


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.skills = self.root / "skills"
        self.path = self.skills / skill.SKILL_NAME / "SKILL.md"
        self.stderr = io.StringIO()
        for guard in (
            patch.object(skill, "default_skills_dir", return_value=self.skills),
            patch.object(skill, "is_development_build", return_value=False),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def write(self, text: str) -> bytes:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        self.path.write_bytes(data)
        return data

    def run_cli(self, args: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.run_cli(
                args, stdout=stdout, stderr=stderr,
                env={"AZWI_ORG": "example", "AZWI_PAT": "token"},
                config_path=self.root / "config.toml", client_factory=FakeClient, program="azwi",
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_metadata_version_hash_encoding_and_single_file(self) -> None:
        result = skill.install_skill()
        content = self.path.read_bytes()
        text = content.decode("utf-8")
        front = yaml.safe_load(text.split("---\n", 2)[1])
        self.assertTrue(result["updated"])
        self.assertEqual(front["name"], "azure-workitem")
        self.assertIn("Fetch and inspect Azure DevOps", front["description"])
        self.assertEqual(front["metadata"]["managed-by"], "azwi")
        self.assertEqual(front["metadata"]["managed-version"], __version__)
        self.assertIn(f'managed-version: "{__version__}"', text)
        self.assertRegex(front["metadata"]["managed-content-sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(signed_text(text), text)
        self.assertNotIn("version", front)
        self.assertNotIn(skill.MANAGED_MARKER, text)
        self.assertNotIn(b"\r", content)
        self.assertFalse(content.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(content.endswith(b"\n"))
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertEqual(self.run_cli(["--version"])[1].strip(), front["metadata"]["managed-version"])

    def test_hash_normalizes_lf_crlf_and_cr(self) -> None:
        for ending in ("\n", "\r\n", "\r"):
            with self.subTest(ending=repr(ending)):
                self.write(skill.render_skill().replace("\n", ending))
                self.assertEqual(skill.skill_status()["integrity"], "valid")

    def test_entire_file_is_covered_without_reserializing_yaml(self) -> None:
        original = skill.render_skill()
        for text in (
            original.replace("# Azure Work Item", "# My Work Item"),
            original.replace("name: azure-workitem", "name: altered"),
            original.replace("description: Fetch", "description: Custom"),
            original.replace("metadata:\n", "metadata:\n  custom: value\n"),
            original.replace("  managed-by: azwi", "  managed-by: 'azwi'"),
            "\ufeff" + original,
            original + "\n",
        ):
            with self.subTest(text=text[:80]):
                self.write(text)
                self.assertEqual(skill.skill_status()["integrity"], "altered")
        # Supported unrelated metadata and comments are hashed as written.
        text = signed_text(original.replace("metadata:\n", "metadata:\n  category: azure # keep this\n"))
        self.write(text)
        self.assertEqual(skill.skill_status()["integrity"], "valid")

    def test_only_metadata_hash_value_is_excluded(self) -> None:
        text = signed_text(skill.render_skill() + '\nmanaged-content-sha256: "example"\n')
        self.write(text)
        self.assertEqual(skill.skill_status()["integrity"], "valid")
        self.write(text.replace('"example"', '"different"'))
        self.assertEqual(skill.skill_status()["integrity"], "altered")

    def test_absent_file_or_directory_is_not_installed_automatically(self) -> None:
        skill.sync_skill(stderr=self.stderr)
        self.assertFalse(self.skills.exists())
        self.path.parent.mkdir(parents=True)
        skill.sync_skill(stderr=self.stderr)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.stderr.getvalue(), "")

    def test_unmanaged_and_conflicting_owner_are_protected(self) -> None:
        for text in (
            "# Personal skill\n",
            "---\nname: azure-workitem\nmetadata:\n  managed-by: another-tool\n---\n" + skill.MANAGED_MARKER,
            "---\nmetadata:\n  managed-by: null\n---\n" + skill.MANAGED_MARKER,
            "\ufeff--- \nmetadata:\n  managed-by: other\n---\n" + skill.MANAGED_MARKER,
        ):
            with self.subTest(text=text):
                before = self.write(text)
                skill.sync_skill(stderr=self.stderr)
                self.assertFalse(skill.skill_status()["managed"])
                for force in (False, True):
                    with self.assertRaisesRegex(UsageError, "unmanaged"):
                        skill.install_skill(force=force)
                self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.stderr.getvalue(), "")

    def test_pristine_older_skill_with_different_body_updates(self) -> None:
        old = signed_text(bundled_version("1.0.0").replace("# Azure Work Item", "# Older Bundled Instructions"))
        self.write(old)
        self.assertTrue(skill.skill_status()["auto_sync_eligible"])
        skill.sync_skill(stderr=self.stderr)
        self.assertEqual(self.path.read_text(encoding="utf-8"), skill.render_skill())
        self.assertIn(f"1.0.0 -> {__version__}", self.stderr.getvalue())
        self.assertIn(str(self.path), self.stderr.getvalue())
        self.assertEqual(len(self.stderr.getvalue().splitlines()), 1)

    def test_older_altered_or_unverifiable_files_are_preserved(self) -> None:
        original = bundled_version("1.0.0")
        variants = {
            "altered": original + "User changes\n",
            "missing": HASH_LINE.sub("", original),
            "malformed": HASH_LINE.sub(r'\1"SHA256:bad"', original),
        }
        for integrity, text in variants.items():
            with self.subTest(integrity=integrity):
                self.stderr = io.StringIO()
                before = self.write(text)
                status = skill.skill_status()
                self.assertEqual(status["integrity"], integrity)
                self.assertFalse(status["auto_sync_eligible"])
                self.assertEqual(status["force_install_command"], "uvx azwi skill install --force")
                skill.sync_skill(stderr=self.stderr)
                self.assertIn(skill.FORCE_INSTALL_COMMAND, self.stderr.getvalue())
                self.assertEqual(len(self.stderr.getvalue().splitlines()), 1)
                self.assertEqual(self.path.read_bytes(), before)
                with self.assertRaisesRegex(UsageError, "uvx azwi skill install --force"):
                    skill.install_skill()
                self.assertTrue(skill.install_skill(force=True)["updated"])
                self.assertEqual(self.path.read_text(encoding="utf-8"), skill.render_skill())

    def test_equal_or_newer_skill_is_never_automatically_rewritten(self) -> None:
        for version in (__version__, "99.0.0"):
            for altered in (False, True):
                with self.subTest(version=version, altered=altered):
                    before = self.write(bundled_version(version) + ("edited\n" if altered else ""))
                    with patch.object(skill.os, "replace") as replace:
                        skill.sync_skill(stderr=self.stderr)
                        replace.assert_not_called()
                    self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.stderr.getvalue(), "")

    def test_explicit_install_is_idempotent_and_never_downgrades(self) -> None:
        skill.install_skill()
        with patch.object(skill.os, "replace") as replace:
            self.assertFalse(skill.install_skill()["updated"])
            replace.assert_not_called()
        for force in (False, True):
            before = self.write(bundled_version("99.0.0"))
            self.assertFalse(skill.install_skill(force=force)["updated"])
            self.assertEqual(self.path.read_bytes(), before)
        self.write(bundled_version("1.0.0"))
        self.assertTrue(skill.install_skill()["updated"])

    def test_equal_altered_explicit_install_requires_force(self) -> None:
        self.write(skill.render_skill() + "edited\n")
        with self.assertRaisesRegex(UsageError, "--force"):
            skill.install_skill()
        self.assertTrue(skill.install_skill(force=True)["updated"])

    def test_pep440_version_ordering(self) -> None:
        for running, installed, comparison in (
            ("1.10", "1.9", "older"),
            ("1.0", "1.0rc1", "older"),
            ("1.0rc1", "1.0.dev1", "older"),
            ("1.0", "1.0.post1", "newer"),
            ("1.0", "1.0+local", "newer"),
            ("1.0", "1!0.1", "newer"),
            ("1.0", "1.0.0", "equal"),
        ):
            with self.subTest(running=running, installed=installed), patch.object(skill, "__version__", running):
                before = self.write(bundled_version(installed))
                self.assertEqual(skill.skill_status()["version_comparison"], comparison)
                skill.sync_skill(stderr=self.stderr)
                if comparison == "older":
                    self.assertEqual(skill.skill_status()["managed_version"], running)
                else:
                    self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_migration_and_invalid_version_recovery_precede_hash_check(self) -> None:
        original = bundled_version("1.0.0") + "edited\n"
        for text, version_state in (
            ("---\nname: azure-workitem\n---\n" + skill.MANAGED_MARKER + "\n", "legacy"),
            (original.replace('  managed-version: "1.0.0"\n', ""), "missing"),
            (original.replace('managed-version: "1.0.0"', 'managed-version: "oops"'), "malformed"),
            (original.replace('managed-version: "1.0.0"', 'managed-version: 1.0'), "malformed"),
        ):
            with self.subTest(version_state=version_state):
                self.write(text)
                status = skill.skill_status()
                self.assertEqual(status["version_state"], version_state)
                self.assertTrue(status["auto_sync_eligible"])
                if version_state == "legacy":
                    self.assertEqual(status["managed_version"], "0")
                    self.assertEqual(status["integrity"], "legacy")
                skill.sync_skill(stderr=self.stderr)
                self.assertEqual(self.path.read_text(encoding="utf-8"), skill.render_skill())
                self.write(text)
                self.assertTrue(skill.install_skill()["updated"])

    def test_invalid_running_version_skips_automatic_work(self) -> None:
        with patch.object(skill, "__version__", "invalid"), patch.object(skill, "default_skills_dir") as default:
            skill.sync_skill(stderr=self.stderr)
            default.assert_not_called()
        self.assertEqual(self.stderr.getvalue(), "")

    def test_local_development_skips_automatic_work_but_allows_install(self) -> None:
        with patch.object(skill, "is_development_build", return_value=True):
            before = self.write(bundled_version("1.0.0"))
            with patch.object(skill, "_read_state", side_effect=AssertionError("must not inspect skills")):
                skill.sync_skill(stderr=self.stderr)
            self.assertEqual(self.path.read_bytes(), before)
            status = skill.skill_status()
            self.assertTrue(status["local_development_build"])
            self.assertFalse(status["auto_sync_eligible"])
            self.assertTrue(skill.install_skill()["updated"])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_custom_directory_requires_explicit_updates(self) -> None:
        custom = self.root / "custom skills"
        custom_path = custom / skill.SKILL_NAME / "SKILL.md"
        with patch.object(skill, "__version__", "1.0.0"):
            skill.install_skill(custom)
        before = custom_path.read_bytes()
        self.assertFalse(skill.skill_status(custom)["standard_location"])
        self.assertFalse(skill.skill_status(custom)["auto_sync_eligible"])
        skill.sync_skill(stderr=self.stderr)
        self.assertEqual(custom_path.read_bytes(), before)
        self.assertFalse(self.skills.exists())
        self.assertTrue(skill.install_skill(custom)["updated"])
        self.assertTrue(skill.remove_skill(custom)["removed"])

    def test_status_is_read_only_in_json_and_plain(self) -> None:
        before = self.write(bundled_version("1.0.0"))
        for format in ("json", "plain"):
            code, stdout, stderr = self.run_cli(["skill", "status", "--format", format])
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            if format == "json":
                result = json.loads(stdout)
                self.assertEqual(result["path"], str(self.path))
                self.assertTrue(result["installed"])
                self.assertTrue(result["managed"])
                self.assertEqual(result["cli_version"], __version__)
                self.assertEqual(result["version_comparison"], "older")
                self.assertTrue(result["auto_sync_eligible"])
            else:
                self.assertIn("version_comparison: older", stdout)
            self.assertEqual(self.path.read_bytes(), before)

    def test_skill_commands_and_alias_help_skip_sync(self) -> None:
        for command in (["skill", "install"], ["skill", "remove"], ["skill", "status"],
                        ["install-skill"], ["remove-skill"], ["skill-status"]):
            for help_args in ([], ["--help"]):
                with self.subTest(command=command, help_args=help_args), patch.object(cli, "sync_skill") as sync:
                    code, _, stderr = self.run_cli(command + help_args)
                    self.assertEqual(code, 0)
                    self.assertEqual(stderr, "")
                    sync.assert_not_called()
        code, stdout, _ = self.run_cli(["skill", "install", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("--force", stdout)
        self.assertIn("--skills-dir", stdout)
        self.write(skill.render_skill() + "altered\n")
        self.assertEqual(self.run_cli(["skill", "install"])[0], 2)
        self.assertTrue(json.loads(self.run_cli(["skill", "install", "--force"])[1])["updated"])

    def test_every_normal_command_runs_sync(self) -> None:
        for command in ([], ["--help"], ["2195", "--help"], ["--about"], ["version"],
                        ["--version"], ["2195"], ["fields", "--type", "Bug", "--project", "Payments"],
                        ["config", "show"]):
            with self.subTest(command=command):
                self.write(bundled_version("1.0.0"))
                code, stdout, stderr = self.run_cli(command)
                self.assertEqual(code, 0)
                self.assertIn("Updated managed skill", stderr)
                if command == ["2195"]:
                    self.assertEqual(json.loads(stdout)["work_item"]["id"], 2195)

    def test_filesystem_and_parse_failures_preserve_primary_result(self) -> None:
        for failure in (PermissionError("denied"), OSError("disk full"), ValueError("bad front matter"), yaml.YAMLError("bad YAML\n  excerpt")):
            with self.subTest(failure=failure), patch.object(skill, "_read_state", side_effect=failure):
                code, stdout, stderr = self.run_cli(["2195"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout)["work_item"]["id"], 2195)
                self.assertIn("WARNING:", stderr)
                self.assertEqual(len(stderr.splitlines()), 1)
                self.assertEqual(self.run_cli(["2195", "--download-images", "images"])[0], 2)

    def test_atomic_boundary_contains_complete_closed_file_and_preserves_other_files(self) -> None:
        before = self.write(bundled_version("1.0.0"))
        unrelated = self.path.parent / "notes.txt"
        unrelated.write_text("keep", encoding="utf-8")
        real_replace = os.replace

        def replace(source, destination):
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(destination, self.path)
            self.assertEqual(self.path.read_bytes(), before)
            with Path(source).open("r+b") as stream:
                self.assertEqual(stream.read(), skill.render_skill().encode("utf-8"))
            real_replace(source, destination)

        with patch.object(skill.os, "replace", side_effect=replace) as replaced:
            skill.sync_skill(stderr=self.stderr)
            replaced.assert_called_once()
        self.assertEqual(self.path.read_bytes(), skill.render_skill().encode("utf-8"))
        self.assertEqual(unrelated.read_text(), "keep")
        self.assertEqual(set(self.path.parent.iterdir()), {self.path, unrelated})

    def test_failed_staging_and_replacement_leave_no_temporary_files(self) -> None:
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation):
                before = self.write(bundled_version("1.0.0"))
                with patch.object(skill.os, operation, side_effect=OSError("disk failure")):
                    skill.sync_skill(stderr=self.stderr)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_concurrent_change_or_removal_cancels_replacement(self) -> None:
        real_fsync = os.fsync
        for changed in (bundled_version("99.0.0"), "Unmanaged replacement\n", None):
            with self.subTest(changed=changed and changed[:50]):
                self.write(bundled_version("1.0.0"))

                def change_during_staging(fd):
                    real_fsync(fd)
                    if changed is None:
                        self.path.unlink()
                    else:
                        self.path.write_bytes(changed.encode("utf-8"))

                with patch.object(skill.os, "fsync", side_effect=change_during_staging), patch.object(skill.os, "replace") as replace:
                    skill.sync_skill(stderr=self.stderr)
                    replace.assert_not_called()
                self.assertEqual(self.path.read_bytes() if self.path.exists() else None, changed.encode("utf-8") if changed else None)
                self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_removal_accepts_legacy_and_metadata_and_preserves_unrelated_files(self) -> None:
        for content in (skill.MANAGED_MARKER, skill.render_skill() + "edited\n"):
            with self.subTest(content=content[:30]):
                self.write(content)
                unrelated = self.path.parent / "notes.txt"
                unrelated.write_text("keep", encoding="utf-8")
                self.assertTrue(skill.remove_skill()["removed"])
                self.assertFalse(self.path.exists())
                self.assertEqual(unrelated.read_text(), "keep")
        self.write("unmanaged")
        with self.assertRaises(UsageError):
            skill.remove_skill()
        self.assertTrue(skill.remove_skill(force=True)["removed"])
        self.assertTrue(unrelated.exists())

    def test_unexpected_directory_and_ambiguous_yaml_are_preserved(self) -> None:
        self.path.mkdir(parents=True)
        with self.assertRaises(UsageError):
            skill.install_skill(force=True)
        self.path.rmdir()
        stray = self.path.parent / "notes.txt"
        stray.write_text("keep", encoding="utf-8")
        with self.assertRaises(UsageError):
            skill.install_skill()
        with self.assertRaises(UsageError):
            skill.remove_skill(force=True)
        for text in ("---\nmetadata: [\n---\n", "---\nmetadata:\n  managed-by: azwi\n  managed-by: other\n---\n"):
            before = self.write(text)
            skill.sync_skill(stderr=self.stderr)
            with self.assertRaises(UsageError):
                skill.install_skill(force=True)
            self.assertEqual(self.path.read_bytes(), before)

    def test_linked_skill_path_is_not_followed(self) -> None:
        outside = self.root / "personal.md"
        outside.write_text(skill.render_skill(), encoding="utf-8")
        self.path.parent.mkdir(parents=True)
        try:
            self.path.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(UsageError):
            skill.install_skill(force=True)
        with self.assertRaises(UsageError):
            skill.remove_skill(force=True)
        self.assertTrue(outside.exists())


class RuntimeSourceTests(unittest.TestCase):
    def test_pep610_and_installed_package_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            module = Path(temp) / "azwi" / "runtime.py"
            installed = Mock()
            installed.files = [Path("azwi/runtime.py")]
            installed.locate_file.return_value = module
            for source, development in (
                (None, False),
                ({"url": "file:///tmp/azwi-1.1.0-py3-none-any.whl", "archive_info": {}}, False),
                ({"url": "https://example.invalid/azwi.whl", "archive_info": {}}, False),
                ({"url": "file:///tmp/azwi", "dir_info": {}}, True),
                ({"url": "file:///tmp/azwi", "dir_info": {"editable": True}}, True),
                ({"url": "file:///tmp/azwi.tar.gz", "archive_info": {}}, True),
                ({"url": "file:///tmp/source"}, True),
                ({"url": "file:///tmp/source.whl", "dir_info": {}}, True),
                ({"url": 123}, True),
                ([], True),
            ):
                with self.subTest(source=source), patch.object(runtime, "__file__", str(module)), patch.object(runtime, "distribution", return_value=installed):
                    installed.read_text.return_value = json.dumps(source) if source is not None else None
                    self.assertEqual(runtime.is_development_build(), development)
            with patch.object(runtime, "distribution", return_value=installed):
                self.assertTrue(runtime.is_development_build())
            with patch.object(runtime, "distribution", side_effect=runtime.PackageNotFoundError):
                self.assertTrue(runtime.is_development_build())
            with patch.object(runtime, "__file__", str(module)), patch.object(runtime, "distribution", return_value=installed):
                installed.read_text.return_value = "invalid JSON"
                self.assertTrue(runtime.is_development_build())
                installed.files = None
                self.assertTrue(runtime.is_development_build())

    def test_checkout_is_ineligible(self) -> None:
        self.assertTrue(runtime.is_development_build())


if __name__ == "__main__":
    unittest.main()
