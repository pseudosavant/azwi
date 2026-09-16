"""Read-only authentication investigation. Never prints tokens or resource content.

Run from a checkout with Python 3.11+ and the project dependencies available.
Azure CLI may update its own login cache and logs. No login is launched here.
PAT creation, updates, and revocation are deliberately outside this probe.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from azwi.auth import resolve_credential
from azwi.config import default_config_path, load_config, resolve_config

RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(url, authorization, payload=None):
    headers = {"Authorization": authorization, "Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    with build_opener(NoRedirect).open(Request(url, data=data, headers=headers), timeout=30) as response:
        return json.load(response)


def cli(args):
    result = subprocess.run([shutil.which("az"), *args, "--output", "json", "--only-show-errors"],
                            capture_output=True, text=True, timeout=45)
    if result.returncode:
        diagnostic = result.stderr.lower()
        return None, {"exit_code": result.returncode,
                      "interaction_required": "interaction" in diagnostic,
                      "login_required": "az login" in diagnostic,
                      "permission_denied": "permissionerror" in diagnostic or "permission denied" in diagnostic,
                      "network_error": "connection" in diagnostic or "name resolution" in diagnostic,
                      "aad_error_codes": sorted(set(re.findall(r"AADSTS[0-9]+", result.stderr)))}
    return json.loads(result.stdout), {"exit_code": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.samples <= 10:
        parser.error("samples must be between 1 and 10")
    records = []

    def measure(name, operation):
        started = time.perf_counter()
        try:
            value, details = operation()
        except HTTPError as exc:
            value, details = None, {"http_status": exc.code, "ok": False}
        except URLError as exc:
            value, details = None, {"error_type": type(exc.reason).__name__,
                                    "errno": getattr(exc.reason, "errno", None),
                                    "winerror": getattr(exc.reason, "winerror", None), "ok": False}
        except Exception as exc:
            value, details = None, {"error_type": type(exc).__name__, "ok": False}
        row = {"name": name, "seconds": round(time.perf_counter() - started, 6), **details}
        records.append(row)
        print(json.dumps(row), flush=True)
        return value

    def command(args):
        result = subprocess.run(args, capture_output=True, timeout=30)
        return None, {"exit_code": result.returncode}

    config = resolve_config(load_config(default_config_path()), env=os.environ)
    credential = resolve_credential(os.environ, config.org)
    version = measure("azure_cli_version", lambda: cli(["version"])) if shutil.which("az") else None
    for _ in range(args.samples):
        measure("azwi_help_subprocess", lambda: command([sys.executable, str(ROOT / "azwi.py"), "--help"]))
        if shutil.which("az"):
            measure("azure_account_show", lambda: cli(["account", "show"]))

    def resolve_many():
        for _ in range(100):
            resolve_credential(os.environ, config.org)
        return None, {"iterations": 100, "ok": True}

    measure("resolve_existing_credential_100_times", resolve_many)
    token_data = measure("entra_acquisition_first", lambda: cli([
        "account", "get-access-token", "--resource", RESOURCE])) if shutil.which("az") else None
    token = token_data.get("accessToken") if token_data else None
    if token:
        for _ in range(args.samples):
            def acquire():
                result, details = cli(["account", "get-access-token", "--resource", RESOURCE])
                if result:
                    details["same_token_as_first"] = result.get("accessToken") == token
                return result, details
            measure("entra_acquisition_repeated", acquire)
        segment = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        records.append({"name": "entra_metadata", "scope": claims.get("scp"),
                        "remaining_seconds": round(claims["exp"] - time.time()),
                        "expected_audience": claims.get("aud") == RESOURCE})
    if config.org:
        base = "https://dev.azure.com/" + quote(config.org, safe="")
        auth = "Basic " + base64.b64encode((":" + credential.pat).encode()).decode() if credential.pat else None
        if token:
            def list_pats():
                result = api("https://vssps.dev.azure.com/" + quote(config.org, safe="") +
                             "/_apis/tokens/pats?api-version=7.1-preview.1&%24top=1", "Bearer " + token)
                return None, {"ok": True, "expected_schema": isinstance(result.get("patTokens"), list)}
            measure("pat_lifecycle_list_with_entra", list_pats)
        discovery_auth = auth or ("Bearer " + token if token else None)
        if discovery_auth:
            def discover():
                result = api(base + "/_apis/wit/wiql?api-version=7.1&%24top=1", discovery_auth,
                             {"query": "SELECT [System.Id] FROM WorkItems ORDER BY [System.ChangedDate] DESC"})
                items = result.get("workItems", [])
                return items[0]["id"] if items else None, {"ok": True, "found_work_item": bool(items)}
            item_id = measure("discover_work_item_read_only", discover)
            if item_id:
                from azwi.cli import _run_fetch
                from azwi.client import AzureDevOpsClient

                class BearerClient(AzureDevOpsClient):
                    def _headers(self, *, allow_auth, accept):
                        headers = super()._headers(allow_auth=False, accept=accept)
                        if allow_auth:
                            headers["Authorization"] = "Bearer " + token
                        return headers

                def fetch(factory, bearer=False):
                    output, errors = io.StringIO(), io.StringIO()
                    env = dict(os.environ)
                    if bearer:
                        env["AZWI_PAT"] = "in-memory-bearer-probe-placeholder"
                    code = _run_fetch([str(item_id), "--org", config.org], stdout=output,
                                      stderr=errors, env=env, config_path=None,
                                      client_factory=factory, program="azwi")
                    result = json.loads(output.getvalue())
                    return None, {"exit_code": code, "linked_pr_count": len(result["sections"].get("prs", [])),
                                  "stderr_empty": not errors.getvalue()}
                for index in range(args.samples):
                    modes = [("pat_full_fetch", AzureDevOpsClient, False)] if auth else []
                    if token:
                        modes.append(("entra_full_fetch_token_already_acquired", BearerClient, True))
                    for name, factory, bearer in modes[::1 if index % 2 == 0 else -1]:
                        measure(name, lambda f=factory, b=bearer: fetch(f, b))

    summary = {}
    for name in sorted({r["name"] for r in records if "seconds" in r}):
        rows = [r for r in records if r["name"] == name]
        times = [r["seconds"] for r in rows]
        summary[name] = {"samples": len(times), "median_seconds": round(statistics.median(times), 6),
                         "min_seconds": min(times), "max_seconds": max(times),
                         "successful_samples": sum(r.get("exit_code") == 0 or r.get("ok") is True for r in rows)}
    report = {"platform": sys.platform, "python": sys.version.split()[0],
              "azure_cli": version.get("azure-cli") if version else None,
              "credential_source": credential.source, "records": records, "summary": summary}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": summary}), flush=True)


if __name__ == "__main__":
    main()
