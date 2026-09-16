from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

from azwi.auth import Credential, credential_entry, credential_lock, save_entry, save_credential
from azwi.auth_flow import Authentication
from azwi.azure_cli import AzureCLI, RESOURCE
from azwi.cli import run_cli
from azwi.client import AzureDevOpsClient, SafeRedirect, _should_send_auth
from azwi.config import save_config, load_config
from azwi.errors import ApiError, AuthError, AzureCLIUnavailable, ChooseAuthentication, ConfigError, LoginRequired
from azwi.pat_lifecycle import PatLifecycle, discover_tenant
from tests.test_cli import FakeClient
from tests.test_onboarding import Terminal, URL

TENANT = "11111111-1111-4111-8111-111111111111"
IDENTITY = "22222222-2222-4222-8222-222222222222"
PAT_ID = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"
SECRET = "test-never-output-this-token"


def token():
    return Credential(SECRET, "azure-cli", "Bearer", int(time.time()) + 3600, TENANT)


def entry(days=30):
    return {"pat": SECRET, "managed": True, "authorization_id": PAT_ID,
            "expires_on": int(time.time()) + days * 86400, "tenant": TENANT, "identity": IDENTITY}


def profile(method="managed-pat"):
    return {"defaults": {"org": "contoso"}, "orgs": {"contoso": {"auth": {
        "method": method, "tenant": TENANT, "identity": IDENTITY,
        "pat_lifetime_days": 30, "renew_before_days": 7}}}}


class Response:
    def __init__(self, payload, headers=None):
        self.payload, self.headers = payload, headers or {}
    def read(self):
        return json.dumps(self.payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


class AzureCLITests(unittest.TestCase):
    def setUp(self):
        self.stdin, self.stderr = Terminal("1\n"), Terminal()
        self.cli = AzureCLI(env={"PATH": "test-path", "AZWI_PAT": SECRET}, stdin=self.stdin, stderr=self.stderr)
        self.which = patch("azwi.azure_cli.shutil.which", return_value="az.cmd").start()
        self.addCleanup(patch.stopall)

    def payload(self, **changes):
        return {"accessToken": SECRET, "expires_on": int(time.time()) + 3600,
                "tenant": TENANT, "tokenType": "Bearer", **changes}

    def process(self, code=0, output=None, diagnostic=""):
        process = Mock(returncode=code)
        process.communicate.return_value = (json.dumps(output or self.payload()), diagnostic)
        return process

    def test_cached_token_capture_is_quiet_and_tenant_bound(self):
        with patch("azwi.azure_cli.subprocess.Popen", return_value=self.process()) as run:
            result = self.cli.token(TENANT)
        self.assertEqual(result.pat, SECRET)
        self.assertEqual(result.scheme, "Bearer")
        self.assertNotIn(SECRET, repr(result) + self.stderr.getvalue())
        args, kwargs = run.call_args
        self.assertIn(TENANT, args[0])
        self.assertIn(RESOURCE, args[0])
        self.assertNotIn("AZWI_PAT", kwargs["env"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_login_round_trip_reacquires_and_routes_both_streams_to_stderr(self):
        process = Mock()
        process.wait.return_value = 0
        with patch.object(self.cli, "_capture", side_effect=[LoginRequired("sign in"), self.payload()]) as acquire, patch("azwi.azure_cli.subprocess.Popen", return_value=process) as launch:
            result = self.cli.token(TENANT, interactive=True)
        self.assertEqual(result.pat, SECRET)
        self.assertEqual(acquire.call_count, 2)
        args, kwargs = launch.call_args
        self.assertIn("--allow-no-subscriptions", args[0])
        self.assertEqual(args[0][-2:], ["--output", "none"])
        self.assertIs(kwargs["stdout"], self.stderr)
        self.assertIs(kwargs["stderr"], self.stderr)
        self.assertIs(kwargs["stdin"], self.stdin)
        self.assertEqual(kwargs["env"]["AZURE_CORE_LOGIN_EXPERIENCE_V2"], "off")
        self.assertNotIn("AZURE_CORE_LOGIN_EXPERIENCE_V2", self.cli.env)
        self.assertNotIn(SECRET, self.stderr.getvalue())

    def test_device_code_and_choose_another_method(self):
        self.cli.stdin = Terminal("2\n")
        with patch.object(self.cli, "_capture", side_effect=[LoginRequired(), self.payload()]), patch.object(self.cli, "login") as login:
            self.cli.token(TENANT, interactive=True)
        login.assert_called_once_with(TENANT, "device-code")
        self.cli.stdin = Terminal("3\n")
        with patch.object(self.cli, "_capture", side_effect=LoginRequired()), patch.object(self.cli, "login") as login:
            with self.assertRaises(ChooseAuthentication):
                self.cli.token(TENANT, interactive=True)
            login.assert_not_called()

    def test_noninteractive_and_redirected_terminal_never_launch_login(self):
        for interactive, stderr in [(False, Terminal()), (True, io.StringIO())]:
            self.cli.stderr = stderr
            with patch.object(self.cli, "_capture", side_effect=LoginRequired()), patch.object(self.cli, "login") as login:
                with self.assertRaises(LoginRequired):
                    self.cli.token(TENANT, interactive=interactive)
                login.assert_not_called()

    def test_login_cancellation_and_timeout_stop_child(self):
        for error in [KeyboardInterrupt(), subprocess.TimeoutExpired("az", 300)]:
            process = Mock()
            with patch("azwi.azure_cli.subprocess.Popen", return_value=process), patch.object(self.cli, "_wait_for_exit", side_effect=error), patch.object(self.cli, "_stop") as stop:
                with self.assertRaises(AuthError):
                    self.cli.login(TENANT, "auto")
                stop.assert_called_once_with(process)

    def test_malformed_expired_wrong_tenant_and_secret_errors_are_safe(self):
        for payload in [self.payload(expires_on=0), self.payload(tenant=OTHER), self.payload(accessToken=None)]:
            with patch.object(self.cli, "_capture", return_value=payload), self.assertRaises(AuthError) as caught:
                self.cli.token(TENANT)
            self.assertNotIn(SECRET, str(caught.exception))
        with patch("azwi.azure_cli.subprocess.Popen", return_value=self.process(1, diagnostic=SECRET)), self.assertRaises(AuthError) as caught:
            self.cli.token(TENANT)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_network_error_does_not_launch_login(self):
        with patch("azwi.azure_cli.subprocess.Popen", return_value=self.process(1, diagnostic="ConnectionError. Try az login " + SECRET)), patch.object(self.cli, "login") as login:
            with self.assertRaises(AuthError):
                self.cli.token(TENANT, interactive=True)
            login.assert_not_called()

    def test_capture_timeout_stops_child_without_echoing_output(self):
        process = Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired("az", 45, output=SECRET)
        with patch("azwi.azure_cli.subprocess.Popen", return_value=process), patch.object(self.cli, "_stop") as stop:
            with self.assertRaises(AuthError) as caught:
                self.cli.token(TENANT)
        stop.assert_called_once_with(process)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_login_success_without_usable_token_does_not_loop(self):
        with patch.object(self.cli, "_capture", side_effect=[LoginRequired(), LoginRequired()]) as capture, patch.object(self.cli, "login") as login:
            with self.assertRaises(LoginRequired):
                self.cli.token(TENANT, interactive=True)
        self.assertEqual(capture.call_count, 2)
        login.assert_called_once()

    def test_login_wait_checks_signals_and_keeps_overall_deadline(self):
        process = Mock(args=["az", "login"])
        process.wait.side_effect = [subprocess.TimeoutExpired("az", 0.2), 0]
        with patch("azwi.azure_cli.time.monotonic", side_effect=[0, 0, 0.2]):
            self.assertEqual(self.cli._wait_for_exit(process, timeout=300), 0)
        self.assertTrue(all(call.kwargs["timeout"] <= 0.2 for call in process.wait.call_args_list))
        with patch("azwi.azure_cli.time.monotonic", side_effect=[0, 301]):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.cli._wait_for_exit(process, timeout=300)

    def test_windows_stop_terminates_tree_and_waits(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch("azwi.azure_cli.os.name", "nt"), patch("azwi.azure_cli.subprocess.run") as terminate:
            self.cli._stop(process)
        self.assertEqual(terminate.call_args.args[0], ["taskkill", "/PID", "12345", "/T", "/F"])
        process.wait.assert_called_once_with(timeout=5)

    def test_posix_stop_targets_process_group(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch("azwi.azure_cli.os.name", "posix"), patch("azwi.azure_cli.os.killpg", create=True) as terminate:
            self.cli._stop(process)
        self.assertEqual(terminate.call_args.args[0], 12345)
        process.wait.assert_called_once_with(timeout=5)


class LifecycleTests(unittest.TestCase):
    def pat_payload(self):
        return {"patTokenError": "none", "patToken": {
            "authorizationId": PAT_ID, "token": SECRET, "scope": "vso.work vso.code",
            "targetAccounts": [OTHER],
            "validTo": datetime.fromtimestamp(time.time() + 86400, timezone.utc).isoformat()}}

    def test_create_uses_bearer_and_only_two_read_scopes(self):
        opener = Mock(return_value=Response(self.pat_payload()))
        result = PatLifecycle("contoso", token(), opener=opener).create(30)
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer " + SECRET)
        body = json.loads(request.data)
        self.assertFalse(body["allOrgs"])
        self.assertEqual(body["scope"], "vso.work vso.code")
        self.assertEqual(result["pat"], SECRET)

    def test_http_200_application_error_is_failure(self):
        service = PatLifecycle("contoso", token(), opener=Mock(return_value=Response({"patTokenError": "invalidToken", "patToken": None})))
        with self.assertRaisesRegex(AuthError, "replacement"):
            service.extend(PAT_ID, 30)

    def test_invalid_created_scope_is_revoked(self):
        payload = self.pat_payload()
        payload["patToken"]["scope"] = "app_token"
        opener = Mock(side_effect=[Response(payload), Response({})])
        with self.assertRaises(AuthError):
            PatLifecycle("contoso", token(), opener=opener).create(30)
        self.assertEqual(opener.call_args.args[0].method, "DELETE")
        self.assertIn(PAT_ID, opener.call_args.args[0].full_url)

    def test_ambiguous_mutation_is_not_retried(self):
        opener = Mock(side_effect=OSError(SECRET))
        with self.assertRaises(ApiError) as error:
            PatLifecycle("contoso", token(), opener=opener).create(30)
        self.assertNotIn(SECRET, str(error.exception))
        self.assertEqual(opener.call_count, 1)

    def test_discovery_rejects_zero_tenant(self):
        for header, expected in [("00000000-0000-0000-0000-000000000000", None), (TENANT, TENANT), ("bad", None)]:
            with patch("azwi.pat_lifecycle.build_opener") as opener:
                opener.return_value.open.return_value = Response({}, {"X-VSS-ResourceTenant": header})
                self.assertEqual(discover_tenant("contoso"), expected)


class AuthFlowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "config.toml"
        self.credentials = self.root / "credentials.toml"
        self.stderr = io.StringIO()
        self.flow = Authentication("contoso", profile(), env={}, config_path=self.path, stderr=self.stderr)

    def store(self, value):
        with credential_lock(self.credentials):
            save_entry("contoso", value, self.credentials)

    def test_healthy_pat_has_no_azure_cli_cost(self):
        self.store(entry())
        with patch.object(self.flow, "connect") as connect:
            self.assertEqual(self.flow.acquire().pat, SECRET)
        connect.assert_not_called()

    def test_environment_override_bypasses_invalid_credentials_and_provider(self):
        self.credentials.write_text("bad toml")
        flow = Authentication("contoso", {"orgs": {"contoso": {"auth": "bad"}}}, env={"AZWI_PAT": SECRET}, config_path=self.path)
        with patch.object(flow, "connect") as connect:
            self.assertEqual(flow.acquire().source, "environment")
        connect.assert_not_called()

    def test_active_renewal_preserves_secret_and_other_org(self):
        self.store(entry(2))
        save_credential("other", "other-secret", self.credentials)
        service = Mock()
        expiry = int(time.time()) + 30 * 86400
        service.extend.return_value = expiry
        with patch.object(self.flow, "connect", return_value=(token(), service, IDENTITY, "User")):
            self.assertEqual(self.flow.acquire().pat, SECRET)
        result = credential_entry("contoso", self.credentials)
        self.assertEqual(result["expires_on"], expiry)
        self.assertEqual(credential_entry("other", self.credentials)["pat"], "other-secret")
        service.create.assert_not_called()

    def test_early_renewal_failure_uses_old_token_with_cooldown(self):
        self.store(entry(2))
        with patch.object(self.flow, "connect", side_effect=LoginRequired()) as connect:
            self.assertEqual(self.flow.acquire(interactive=True).pat, SECRET)
            self.assertEqual(self.flow.acquire().pat, SECRET)
        self.assertEqual(connect.call_count, 1)
        self.assertFalse(connect.call_args.kwargs["interactive"])
        self.assertIn("still-valid", self.stderr.getvalue())
        self.assertGreater(credential_entry("contoso", self.credentials)["retry_after"], time.time())

    def test_expired_pat_replacement_verifies_before_commit_and_revokes_old(self):
        old = entry(-1)
        self.store(old)
        new = {**entry(), "pat": "replacement-secret", "authorization_id": OTHER}
        service = Mock()
        service.create.return_value = new
        def verify(candidate):
            self.assertEqual(credential_entry("contoso", self.credentials), old)
            self.assertEqual(candidate.pat, "replacement-secret")
        with patch.object(self.flow, "connect", return_value=(token(), service, IDENTITY, "User")):
            credential = self.flow.acquire(verify=verify)
        self.assertEqual(credential.pat, "replacement-secret")
        service.extend.assert_not_called()
        service.revoke.assert_called_once_with(PAT_ID)

    def test_failed_replacement_preserves_old_and_revokes_candidate(self):
        old = entry(-1)
        self.store(old)
        service = Mock()
        service.create.return_value = {**entry(), "authorization_id": OTHER}
        with patch.object(self.flow, "connect", return_value=(token(), service, IDENTITY, "User")), self.assertRaises(AuthError):
            self.flow.acquire(verify=Mock(side_effect=AuthError("verification failed")))
        self.assertEqual(credential_entry("contoso", self.credentials), old)
        service.revoke.assert_called_once_with(OTHER)

    def test_wrong_identity_never_manages_pat(self):
        with patch.object(self.flow.cli, "token", return_value=token()), patch("azwi.auth_flow.PatLifecycle") as service:
            service.return_value.identity.return_value = (OTHER, "Wrong user")
            with self.assertRaisesRegex(AuthError, "different"):
                self.flow.connect(TENANT, expected_identity=IDENTITY)
            service.return_value.create.assert_not_called()

    def test_read_only_check_never_renews(self):
        self.store(entry(2))
        before = self.credentials.read_bytes()
        with patch.object(self.flow, "connect") as connect:
            self.flow.acquire(renew=False)
        connect.assert_not_called()
        self.assertEqual(self.credentials.read_bytes(), before)

    def test_explicit_tenant_wins_discovery(self):
        with patch("azwi.auth_flow.discover_tenant") as discover:
            self.assertEqual(self.flow.select_tenant(OTHER), OTHER)
        discover.assert_not_called()

    def test_concurrent_renewal_uses_fresh_credential(self):
        self.store(entry(2))
        service = Mock()
        def connect(*args, **kwargs):
            self.store(entry(30))
            return token(), service, IDENTITY, "User"
        with patch.object(self.flow, "connect", side_effect=connect):
            self.flow.acquire()
        service.extend.assert_not_called()
        service.create.assert_not_called()


class AzureSetupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "config.toml"
        self.credentials = self.root / "credentials.toml"
        self.skills = self.root / "skills"
        patcher = patch("azwi.cli.sync_skill")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = Mock()
        self.service.create.side_effect = lambda days: dict(entry())
        self.service.extend.return_value = int(time.time()) + 30 * 86400
        self.connect = patch("azwi.auth_flow.Authentication.connect", return_value=(token(), self.service, IDENTITY, "User")).start()
        self.addCleanup(patch.stopall)
        FakeClient.instances.clear()

    def command(self, args, *, stdin=None, env=None, client=FakeClient):
        stdout = io.StringIO()
        stderr = Terminal() if stdin is not None and stdin.isatty() else io.StringIO()
        code = run_cli(args, stdout=stdout, stderr=stderr, stdin=stdin or io.StringIO(),
                       env=env or {}, config_path=self.config, client_factory=client, program="azwi")
        self.assertNotIn(SECRET, stdout.getvalue() + stderr.getvalue())
        return code, stdout.getvalue(), stderr.getvalue()

    def args(self, method, *more):
        return ["setup", URL, "--auth", method, "--tenant", TENANT, "--skills-dir", str(self.skills), *more]

    def test_managed_setup_saves_verified_credentials(self):
        code, output, err = self.command(self.args("managed-pat", "--non-interactive"))
        self.assertEqual(code, 0, err)
        result = json.loads(output)
        self.assertTrue(result["ready"])
        self.assertEqual(result["auth_method"], "managed-pat")
        self.assertEqual(result["verification"]["code_access"], "verified")
        self.assertEqual(credential_entry("contoso", self.credentials)["pat"], SECRET)
        self.assertNotIn(SECRET, self.config.read_text())
        self.assertEqual(load_config(self.config)["orgs"]["contoso"]["auth"]["identity"], IDENTITY)

    def test_entra_setup_and_fetch_use_bearer_without_pat_storage(self):
        code, output, err = self.command(self.args("entra"))
        self.assertEqual(code, 0, err)
        self.assertFalse(self.credentials.exists())
        self.assertIsInstance(FakeClient.instances[-1].pat, Credential)
        self.assertEqual(FakeClient.instances[-1].pat.scheme, "Bearer")
        code, output, err = self.command(["2195", "--non-interactive"])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")

    def test_entra_setup_ignores_inaccessible_pat_file(self):
        self.credentials.write_text("invalid toml")
        code, _, err = self.command(self.args("entra"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.credentials.read_text(), "invalid toml")

    def test_failed_verification_revokes_new_pat_and_saves_nothing(self):
        class Denied(FakeClient):
            def get_comments(self, *args):
                raise AuthError("Denied")
        code, output, _ = self.command(self.args("managed-pat"), client=Denied)
        self.assertEqual(code, 4)
        self.assertEqual(output, "")
        self.assertFalse(self.config.exists())
        self.assertFalse(self.credentials.exists())
        self.service.revoke.assert_called_once_with(PAT_ID)

    def test_config_write_failure_rolls_back_credential_and_revokes_candidate(self):
        save_credential("contoso", "old-secret", self.credentials)
        with patch("azwi.onboarding.save_config", side_effect=ConfigError("cannot save")):
            code, _, _ = self.command(self.args("managed-pat"))
        self.assertEqual(code, 3)
        self.assertEqual(credential_entry("contoso", self.credentials)["pat"], "old-secret")
        self.service.revoke.assert_called_once_with(PAT_ID)

    def test_repeated_managed_setup_does_not_create_another_token(self):
        self.command(self.args("managed-pat"))
        code, _, err = self.command(self.args("managed-pat"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.service.create.call_count, 1)

    def test_environment_override_conflict_fails_before_signin(self):
        code, _, err = self.command(self.args("entra"), env={"AZWI_PAT": SECRET})
        self.assertEqual(code, 2)
        self.assertIn("Unset AZWI_PAT", err)
        self.connect.assert_not_called()

    def test_noninteractive_missing_credentials_never_reads_terminal(self):
        stdin = Terminal("y\n")
        code, _, _ = self.command(["2195", "--org", "contoso", "--non-interactive"], stdin=stdin)
        self.assertEqual(code, 4)
        self.assertEqual(stdin.tell(), 0)
        self.connect.assert_not_called()

    def test_interactive_first_fetch_resumes_and_only_outputs_work_item(self):
        with patch("azwi.onboarding.install_skill", return_value={"installed": True}), patch("azwi.onboarding.read_masked_pat", return_value=SECRET):
            code, output, err = self.command(["2195", "--org", "contoso"], stdin=Terminal("y\n3\n"))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(output)["work_item"]["id"], 2195)
        self.assertNotIn('"ready"', output)

    def test_entra_local_check_does_not_get_token(self):
        self.command(self.args("entra"))
        self.connect.reset_mock()
        before = self.config.read_bytes()
        with patch("azwi.azure_cli.AzureCLI.executable", return_value="az"):
            code, output, err = self.command(["config", "check", "--skills-dir", str(self.skills)])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(output)["verification"]["status"], "not_checked")
        self.connect.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)

    def test_expired_local_check_does_not_create_or_extend(self):
        self.command(self.args("managed-pat"))
        with credential_lock(self.credentials):
            save_entry("contoso", entry(-1), self.credentials)
        before = self.credentials.read_bytes()
        self.service.reset_mock()
        self.connect.reset_mock()
        code, output, _ = self.command(["config", "check", "--skills-dir", str(self.skills)])
        self.assertEqual(code, 4)
        self.assertFalse(json.loads(output)["ready"])
        self.connect.assert_not_called()
        self.service.create.assert_not_called()
        self.assertEqual(self.credentials.read_bytes(), before)

    def test_org_only_setup_verifies_identity(self):
        class OrganizationClient(FakeClient):
            def get_connection_data(self):
                return {"authenticatedUser": {"id": IDENTITY}}
        args = self.args("entra")
        args[1:2] = ["--org", "contoso"]
        code, output, err = self.command(args, client=OrganizationClient)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(output)["verification"]["status"], "organization_verified")

    def test_missing_cli_can_return_to_manual_setup(self):
        self.connect.side_effect = AzureCLIUnavailable("Azure CLI is unavailable.")
        with patch("azwi.onboarding.read_masked_pat", return_value=SECRET):
            code, output, err = self.command(self.args("managed-pat"), stdin=Terminal("3\n"))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(output)["auth_method"], "manual-pat")
        self.service.create.assert_not_called()

    def test_cancelled_signin_preserves_existing_config_and_credential(self):
        save_config({"defaults": {"org": "other"}}, self.config)
        save_credential("contoso", "old-secret", self.credentials)
        before = (self.config.read_bytes(), self.credentials.read_bytes())
        self.connect.side_effect = AuthError("Sign-in cancelled.")
        code, output, _ = self.command(self.args("managed-pat"), stdin=Terminal())
        self.assertEqual(code, 4)
        self.assertEqual(output, "")
        self.assertEqual((self.config.read_bytes(), self.credentials.read_bytes()), before)
        self.service.create.assert_not_called()

    def test_credential_write_failure_revokes_candidate_and_preserves_old(self):
        save_credential("contoso", "old-secret", self.credentials)
        with patch("azwi.onboarding.save_entry", side_effect=AuthError("cannot save")):
            code, _, _ = self.command(self.args("managed-pat"))
        self.assertEqual(code, 4)
        self.assertFalse(self.config.exists())
        self.assertEqual(credential_entry("contoso", self.credentials)["pat"], "old-secret")
        self.service.revoke.assert_called_once_with(PAT_ID)

    def test_invalid_renewal_options_fail_before_creation(self):
        for options in [("--pat-lifetime-days", "0"), ("--renew-before-days", "30")]:
            code, _, _ = self.command(self.args("managed-pat", *options))
            self.assertEqual(code, 3)
        self.connect.assert_not_called()
        self.service.create.assert_not_called()

    def test_environment_override_does_not_change_saved_provider(self):
        self.command(self.args("entra"))
        settings = load_config(self.config)["orgs"]["contoso"]["auth"]
        code, _, err = self.command(["setup", URL, "--skills-dir", str(self.skills)], env={"AZWI_PAT": SECRET})
        self.assertEqual(code, 0, err)
        self.assertEqual(load_config(self.config)["orgs"]["contoso"]["auth"], settings)

    def test_first_fetch_without_org_resumes_with_configured_org(self):
        with patch("azwi.onboarding.install_skill", return_value={"installed": True}), patch("azwi.onboarding.read_masked_pat", return_value=SECRET):
            code, output, err = self.command(["2195"], stdin=Terminal("y\ncontoso\n3\n"))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(output)["work_item"]["id"], 2195)
        self.assertEqual(load_config(self.config)["defaults"]["org"], "contoso")

    def test_fields_uses_entra_credential(self):
        self.command(self.args("entra"))
        code, output, err = self.command(["fields", "--type", "Bug", "--project", "Payments", "--non-interactive"])
        self.assertEqual(code, 0, err)
        self.assertIn("Reference Name", output)
        self.assertEqual(FakeClient.instances[-1].pat.scheme, "Bearer")


class ClientAuthTests(unittest.TestCase):
    def test_bearer_is_used_for_every_resource_and_trusted_download(self):
        seen = []
        def opener(request):
            seen.append(request)
            return Response({})
        client = AzureDevOpsClient("contoso", token(), opener=opener)
        client.get_work_item(2195)
        client.get_comments("Payments", 2195, 10)
        client.get_pull_request("Payments", "repo", 1)
        client.get_pull_request_threads("Payments", "repo", 1)
        client.get_work_item_type_fields("Payments", "Bug")
        client.download("https://dev.azure.com/contoso/_apis/wit/attachments/attachment")
        self.assertEqual(len(seen), 6)
        self.assertTrue(all(request.get_header("Authorization") == "Bearer " + SECRET for request in seen))
        client.download("https://example.com/image.png")
        self.assertIsNone(seen[-1].get_header("Authorization"))

    def test_bearer_headers_and_refresh(self):
        credential = replace(token(), expires_on=1, refresh=Mock(return_value=token()))
        client = AzureDevOpsClient("contoso", credential)
        self.assertEqual(client._headers(allow_auth=True, accept="application/json")["Authorization"], "Bearer " + SECRET)
        credential.refresh.assert_called_once()
        self.assertNotIn("Authorization", client._headers(allow_auth=False, accept="image/png"))

    def test_auth_not_sent_to_lookalike_hosts_or_http(self):
        for url in ["https://evildev.azure.com/path", "https://dev.azure.com.evil.test/path", "http://dev.azure.com/path"]:
            self.assertFalse(_should_send_auth(url))
        self.assertTrue(_should_send_auth("https://dev.azure.com/contoso/path"))

    def test_redirect_to_another_origin_strips_credentials(self):
        request = Request("https://dev.azure.com/contoso/path", headers={"Authorization": "Bearer " + SECRET})
        redirected = SafeRedirect().redirect_request(request, None, 302, "Found", {}, "https://example.com/path")
        self.assertIsNone(redirected.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
