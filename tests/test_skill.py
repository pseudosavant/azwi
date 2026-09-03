from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from azwi.errors import UsageError
from azwi.skill import MANAGED_MARKER, install_skill, remove_skill, render_skill


class SkillTests(unittest.TestCase):
    def test_install_creates_and_preserves_unmanaged_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skills_root = Path(temp)
            first = install_skill(skills_root)
            skill_path = skills_root / "azure-workitem" / "SKILL.md"

            self.assertTrue(first["installed"])
            self.assertTrue(first["updated"])
            self.assertEqual(skill_path.read_text(encoding="utf-8"), render_skill())

            second = install_skill(skills_root)
            self.assertFalse(second["updated"])

            skill_path.write_text("---\nname: azure-workitem\n---\ncustom\n", encoding="utf-8")
            with self.assertRaises(UsageError):
                install_skill(skills_root)

    def test_skill_covers_ids_urls_attachments_and_pr_comments(self) -> None:
        SKILL_MD = render_skill()
        self.assertIn("$azure-workitem", SKILL_MD)
        self.assertIn("dev.azure.com/<org>", SKILL_MD)
        self.assertIn("<org>.visualstudio.com", SKILL_MD)
        self.assertIn("uvx azwi <id> --org", SKILL_MD)
        self.assertIn("./azwi-<id>-attachments", SKILL_MD)
        self.assertIn("--attachment-name", SKILL_MD)
        self.assertIn("--include-pr-comments", SKILL_MD)
        self.assertIn("--pr-comment-status all", SKILL_MD)
        self.assertIn("--include-pr-system-comments", SKILL_MD)
        self.assertIn("--download-images", SKILL_MD)
        self.assertNotIn(MANAGED_MARKER, SKILL_MD)

    def test_remove_only_removes_managed_skill_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            skills_root = Path(temp)
            install_skill(skills_root)
            removed = remove_skill(skills_root)
            self.assertTrue(removed["removed"])
            self.assertFalse((skills_root / "azure-workitem").exists())

            skill_dir = skills_root / "azure-workitem"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: azure-workitem\n---\ncustom\n", encoding="utf-8")
            with self.assertRaises(UsageError):
                remove_skill(skills_root)
            self.assertTrue(remove_skill(skills_root, force=True)["removed"])


if __name__ == "__main__":
    unittest.main()
