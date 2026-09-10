from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from azwi.auth import Credential, require_pat, resolve_credential, save_credential
from azwi.cli import run_cli
from azwi.config import load_config
from azwi.errors import AuthError, UsageError
from azwi.onboarding import parse_work_item_url
from tests.test_cli import FakeClient


URL = "https://dev.azure.com/contoso/Payments/_workitems/edit/2195"
TOKEN = "test-secret-never-render"


class Terminal(io.StringIO):
    def isatty(self):
        return True


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "config.toml"
        self.skills = self.root / "skills"
        patcher = patch("azwi.cli.sync_skill")
        self.sync = patcher.start()
        self.addCleanup(patcher.stop)
        FakeClient.instances.clear()

    def run_command(self, args, env=None, stdin=None, client=FakeClient):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = run_cli(
            args, stdout=stdout, stderr=stderr, env=env or {}, config_path=self.config,
            client_factory=client, program="azwi", stdin=stdin or io.StringIO(),
        )
        self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())
        if self.config.exists():
            self.assertNotIn(TOKEN, self.config.read_text())
        return code, stdout.getvalue(), stderr.getvalue()

    def setup_args(self, *extra):
        return ["setup", *extra, "--skills-dir", str(self.skills)]

    def check_args(self, *extra):
        return ["config", "check", *extra, "--skills-dir", str(self.skills)]

    def test_setup_url_verifies_full_fetch_and_installs_skill(self):
        env = {"AZWI_PAT": TOKEN}
        code, out, err = self.run_command(self.setup_args(URL), env)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        report = json.loads(out)
        self.assertTrue(report["ready"])
        self.assertEqual(report["verification"]["work_item"]["id"], 2195)
        self.assertEqual(report["verification"]["status"], "verified")
        self.assertEqual(report["credential_source"], "environment")
        self.assertEqual(load_config(self.config)["defaults"]["org"], "contoso")
        self.assertTrue((self.skills / "azure-workitem" / "SKILL.md").exists())
        calls = [call[0] for call in FakeClient.instances[-1].calls]
        self.assertIn("get_comments", calls)
        self.assertIn("get_pull_request", calls)
        self.assertEqual(env, {"AZWI_PAT": TOKEN})
        self.sync.assert_not_called()

    def test_bare_interactive_setup_prompts_for_url(self):
        code, out, err = self.run_command(self.setup_args(), {"AZWI_PAT": TOKEN}, Terminal(URL + "\n"))
        self.assertEqual(code, 0, err)
        self.assertIn("Paste a work item URL", err)
        self.assertEqual(json.loads(out)["org"], "contoso")

    def test_missing_pat_completes_local_setup_with_actionable_error(self):
        env = {}
        code, out, err = self.run_command(self.setup_args(URL), env)
        self.assertEqual(code, 4)
        self.assertFalse(json.loads(out)["ready"])
        self.assertIn('$env:AZWI_PAT = "<your-pat>"', err)
        self.assertIn('export AZWI_PAT="<your-pat>"', err)
        self.assertIn("Work Items: Read and Code: Read", err)
        self.assertIn("A longer lifetime reduces renewal interruptions.", err)
        self.assertTrue(self.config.exists())
        self.assertTrue((self.skills / "azure-workitem" / "SKILL.md").exists())
        self.assertEqual(FakeClient.instances, [])
        self.assertEqual(env, {})

    def test_noninteractive_does_not_read_terminal(self):
        terminal = Terminal(URL + "\n")
        code, out, err = self.run_command(self.setup_args("--org", "contoso", "--non-interactive"), stdin=terminal)
        self.assertEqual(code, 4)
        self.assertEqual(terminal.tell(), 0)

    def test_read_only_check_reports_all_missing_requirements(self):
        code, out, err = self.run_command(self.check_args())
        self.assertEqual(code, 3)
        self.assertEqual(len(json.loads(out)["next_steps"]), 3)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.skills.exists())
        self.sync.assert_not_called()

    def test_check_env_without_org_does_not_claim_pat_missing(self):
        code, out, err = self.run_command(self.check_args(), {"AZWI_PAT": TOKEN})
        self.assertEqual(json.loads(out)["credential_source"], "environment")
        self.assertNotIn("AZWI_PAT is not set", err)

    def test_check_url_verifies_without_saving_target(self):
        self.run_command(self.setup_args("--org", "other"), {"AZWI_PAT": TOKEN})
        before = self.config.read_bytes()
        code, out, err = self.run_command(self.check_args(URL), {"AZWI_PAT": TOKEN})
        self.assertEqual(code, 0, err)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(json.loads(out)["org_source"], "argument")

    def test_check_detects_missing_pat_in_different_environment(self):
        self.run_command(self.setup_args(URL), {"AZWI_PAT": TOKEN})
        code, out, err = self.run_command(self.check_args())
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out)["credential_source"], "missing")
        self.assertIn("no saved PAT is available", err)

    def test_prompted_pat_is_saved_and_works_in_fresh_environment(self):
        with patch("azwi.onboarding.read_masked_pat", return_value=TOKEN) as prompt:
            code, out, err = self.run_command(self.setup_args(URL), stdin=Terminal())
        self.assertEqual(code, 0, err)
        prompt.assert_called_once()
        self.assertIn("normal inherited permissions", err)
        self.assertEqual(json.loads(out)["credential_source"], "file")
        credentials = self.root / "credentials.toml"
        before = credentials.read_bytes()
        code, out, err = self.run_command(self.check_args(URL))
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out)["credential_source"], "file")
        self.assertEqual(credentials.read_bytes(), before)
        code, out, err = self.run_command(["2195"])
        self.assertEqual(code, 0, err)
        code, out, err = self.run_command(["fields", "--type", "Bug", "--project", "Payments"])
        self.assertEqual(code, 0, err)
        code, out, err = self.run_command(["config", "show"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("pat", out.lower())

    def test_env_pat_is_not_automatically_persisted(self):
        self.run_command(self.setup_args(URL), {"AZWI_PAT": TOKEN})
        self.assertFalse((self.root / "credentials.toml").exists())

    def test_failed_verification_preserves_saved_token(self):
        credentials = self.root / "credentials.toml"
        save_credential("contoso", "old-token", credentials)
        before = credentials.read_bytes()
        class DeniedClient(FakeClient):
            def get_work_item(self, work_item_id):
                raise AuthError("Test access denied")
        with patch("azwi.onboarding.read_masked_pat", return_value=TOKEN):
            code, out, err = self.run_command(self.setup_args(URL, "--replace-pat"), stdin=Terminal(), client=DeniedClient)
        self.assertEqual(code, 4)
        self.assertFalse(self.config.exists())
        self.assertEqual(credentials.read_bytes(), before)

    def test_replace_saved_pat_preserves_other_orgs(self):
        credentials = self.root / "credentials.toml"
        save_credential("contoso", "old-token", credentials)
        save_credential("other", "other-token", credentials)
        with patch("azwi.onboarding.read_masked_pat", return_value=TOKEN):
            code, out, err = self.run_command(self.setup_args(URL, "--replace-pat"), stdin=Terminal())
        self.assertEqual(code, 0, err)
        self.assertEqual(require_pat({}, "contoso", credentials), TOKEN)
        self.assertEqual(require_pat({}, "other", credentials), "other-token")

    def test_replacement_rejects_environment_override_and_noninteractive(self):
        for env, stdin in [({"AZWI_PAT": TOKEN}, Terminal()), ({}, io.StringIO())]:
            code, out, err = self.run_command(self.setup_args(URL, "--replace-pat"), env, stdin)
            self.assertEqual(code, 2)
            self.assertIn("unset AZWI_PAT", err)

    def test_write_failure_provides_env_fallback_and_preserves_old_file(self):
        credentials = self.root / "credentials.toml"
        save_credential("other", "other-token", credentials)
        before = credentials.read_bytes()
        import os
        replace = os.replace
        def fail_credentials(source, destination):
            if destination == credentials:
                raise PermissionError
            return replace(source, destination)
        with patch("azwi.onboarding.read_masked_pat", return_value=TOKEN), patch("azwi.auth.os.replace", side_effect=fail_credentials):
            code, out, err = self.run_command(self.setup_args(URL), stdin=Terminal())
        self.assertEqual(code, 4)
        self.assertFalse(json.loads(out)["ready"])
        self.assertIn("Set AZWI_PAT instead", err)
        self.assertEqual(credentials.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_malformed_credentials_are_not_exposed_or_overwritten(self):
        credentials = self.root / "credentials.toml"
        credentials.write_text(TOKEN + " = bad toml")
        before = credentials.read_bytes()
        for args in [self.check_args(URL), self.setup_args(URL)]:
            code, out, err = self.run_command(args)
            self.assertEqual(code, 4)
            self.assertIn("Cannot read credentials", err)
            self.assertEqual(credentials.read_bytes(), before)
        code, out, err = self.run_command(self.setup_args(URL), {"AZWI_PAT": TOKEN})
        self.assertEqual(code, 0, err)
        self.assertEqual(credentials.read_bytes(), before)

    def test_hidden_input_unavailable_does_not_fall_back_to_echo(self):
        with patch("azwi.onboarding.read_masked_pat", side_effect=OSError):
            code, out, err = self.run_command(self.setup_args(URL), stdin=Terminal())
        self.assertEqual(code, 4)
        self.assertIn("Masked PAT input is unavailable", err)
        self.assertFalse((self.root / "credentials.toml").exists())

    def test_repeated_setup_preserves_other_config_and_skill(self):
        self.config.write_text('[defaults]\nproject = "Payments"\n[defaults.fields]\nacceptance = "Custom.Acceptance"\n')
        self.run_command(self.setup_args(URL), {"AZWI_PAT": TOKEN})
        skill = self.skills / "azure-workitem" / "SKILL.md"
        before = skill.read_bytes()
        code, out, err = self.run_command(self.setup_args(URL), {"AZWI_PAT": TOKEN})
        self.assertEqual(code, 0, err)
        self.assertEqual(load_config(self.config)["defaults"]["fields"]["acceptance"], "Custom.Acceptance")
        self.assertEqual(skill.read_bytes(), before)

    def test_no_linked_pr_does_not_claim_code_access(self):
        class NoPrClient(FakeClient):
            def get_work_item(self, item_id):
                work_item = super().get_work_item(item_id)
                work_item["relations"] = []
                return work_item
        code, out, err = self.run_command(self.setup_args(URL), {"AZWI_PAT": TOKEN}, client=NoPrClient)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["verification"]["code_access"], "not_verified")

    def test_config_check_reports_api_failure_without_changes(self):
        self.run_command(self.setup_args(URL), {"AZWI_PAT": TOKEN})
        before = self.config.read_bytes()
        class DeniedClient(FakeClient):
            def get_comments(self, *args):
                raise AuthError("Comments access denied")
        code, out, err = self.run_command(self.check_args(URL), {"AZWI_PAT": TOKEN}, client=DeniedClient)
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out)["verification"]["status"], "failed")
        self.assertEqual(self.config.read_bytes(), before)

    def test_verification_failure_does_not_save_org(self):
        class DeniedClient(FakeClient):
            def get_work_item(self, work_item_id):
                raise AuthError("Test access denied")
        code, out, err = self.run_command(self.setup_args(URL), {"AZWI_PAT": TOKEN}, client=DeniedClient)
        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertFalse(self.config.exists())

    def test_unmanaged_skill_is_not_overwritten(self):
        path = self.skills / "azure-workitem" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("user content")
        code, out, err = self.run_command(self.setup_args(URL), {"AZWI_PAT": TOKEN})
        self.assertEqual(code, 2)
        self.assertIn("Skill installation failed", err)
        self.assertEqual(path.read_text(), "user content")

    def test_target_conflict_rejected_before_side_effects(self):
        code, out, err = self.run_command(self.setup_args(URL, "--org", "other"))
        self.assertEqual(code, 2)
        self.assertFalse(self.config.exists())

    def test_malformed_config_is_safe_error(self):
        self.config.write_text("[bad")
        code, out, err = self.run_command(self.check_args())
        self.assertEqual(code, 3)
        self.assertNotIn("Traceback", err)


class UrlTests(unittest.TestCase):
    def test_supported_urls(self):
        for url in [URL, URL + "?view=edit#discussion", URL + "/", "https://Contoso.visualstudio.com/DefaultCollection/Payments/_workitems/edit/2195", "https://dev.azure.com/contoso/_workitems/edit/2195"]:
            with self.subTest(url=url):
                self.assertEqual(parse_work_item_url(url), ("contoso", 2195))

    def test_reject_unsafe_or_ambiguous_urls(self):
        for url in [URL.replace("https", "http"), URL.replace("dev.azure.com", "dev.azure.com.evil.test"), URL.replace("contoso/", "contoso%2Fevil/"), URL + "/extra", URL.replace("2195", "0"), URL.replace("2195", "abc"), URL.replace("dev.azure.com", "user:password@dev.azure.com"), URL.replace("dev.azure.com", "dev.azure.com:444"), "2195", "https://dev.azure.com/_workitems/edit/2195", "https://evil.visualstudio.com.evil.test/_workitems/edit/2195"]:
            with self.subTest(url=url), self.assertRaises(UsageError):
                parse_work_item_url(url)


class CredentialTests(unittest.TestCase):
    def test_file_org_selection_and_env_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.toml"
            save_credential("Contoso", TOKEN, path)
            self.assertEqual(require_pat({}, "CONTOSO", path), TOKEN)
            self.assertEqual(resolve_credential({}, "other", path).source, "missing")
            self.assertEqual(require_pat({"AZWI_PAT": "override"}, "contoso", path), "override")
            path.write_text("invalid TOML " + TOKEN)
            self.assertEqual(require_pat({"AZWI_PAT": "override"}, "contoso", path), "override")

    def test_credentials_follow_normal_file_creation_permissions(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ordinary = root / "ordinary.txt"
            ordinary.write_text("nonsecret")
            path = root / "credentials.toml"
            with patch("os.chmod", side_effect=AssertionError("No permission changes")):
                save_credential("contoso", TOKEN, path)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, ordinary.stat().st_mode & 0o777)

    def test_environment_only_and_no_secret_in_repr(self):
        self.assertEqual(resolve_credential({"AZWI_PAT": TOKEN}).source, "environment")
        self.assertEqual(require_pat({"AZWI_PAT": TOKEN}), TOKEN)
        self.assertNotIn(TOKEN, repr(Credential(TOKEN, "environment")))

    def test_blank_environment_values_are_missing(self):
        for env in [{}, {"AZWI_PAT": ""}, {"AZWI_PAT": "  "}]:
            with self.subTest(env=env), self.assertRaises(AuthError):
                require_pat(env)


if __name__ == "__main__":
    unittest.main()
