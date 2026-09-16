"""Explicit disposable PAT lifecycle experiment. No existing PAT is modified.

Creates one organization-specific read-only PAT, extends it, and revokes it.
The token secret remains in memory. A recovery file contains only its ID.
"""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, build_opener
from uuid import uuid4

from azure_cli_auth_probe import NoRedirect, RESOURCE, api, cli
from azwi.cli import _run_fetch
from azwi.client import AzureDevOpsClient
from azwi.config import default_config_path, load_config, resolve_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expire-before-renewal", action="store_true")
    args = parser.parse_args()
    config = resolve_config(load_config(default_config_path()), env=os.environ)
    if not config.org:
        raise RuntimeError("No configured organization")
    result, status = cli(["account", "get-access-token", "--resource", RESOURCE])
    if not result:
        print(json.dumps(status))
        return
    bearer = "Bearer " + result["accessToken"]
    base = "https://vssps.dev.azure.com/" + quote(config.org, safe="") + "/_apis/tokens/pats"
    url = base + "?api-version=7.1-preview.1"
    output = Path(__file__).with_name("azure-cli-pat-expiration-measurements.json" if args.expire_before_renewal
                                     else "azure-cli-pat-lifecycle-measurements.json")
    recovery = Path(__file__).with_name("azure-cli-pat-lifecycle-recovery.json")
    if recovery.exists():
        raise RuntimeError("An earlier disposable PAT needs cleanup. Inspect the recovery metadata before another run.")
    records = []
    authorization_id = None
    token = None
    test_name = "azwi-feasibility-disposable-" + uuid4().hex

    def record(name, start, **details):
        row = {"name": name, "seconds": round(time.perf_counter() - start, 6), **details}
        records.append(row)
        output.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(row), flush=True)

    def request(method, payload=None, suffix=""):
        data = json.dumps(payload).encode() if payload is not None else None
        req = Request(url + suffix, data=data, method=method,
                      headers={"Authorization": bearer, "Content-Type": "application/json", "Accept": "application/json"})
        with build_opener(NoRedirect).open(req, timeout=30) as response:
            body = response.read()
            return response.status, json.loads(body) if body else None

    try:
        start = time.perf_counter()
        expiry = datetime.now(timezone.utc) + timedelta(seconds=45 if args.expire_before_renewal else 300)
        status, created = request("POST", {"displayName": test_name, "scope": "vso.work vso.code",
                                           "allOrgs": False, "validTo": expiry.isoformat()})
        pat = created.get("patToken") or {}
        authorization_id = pat.get("authorizationId")
        if authorization_id:
            recovery.write_text(json.dumps({"organization": config.org, "authorization_id": authorization_id,
                                            "display_name": test_name}) + "\n", encoding="utf-8")
        token = pat.get("token")
        record("create_disposable_pat", start, http_status=status, token_returned=bool(token),
               expected_scopes=sorted(pat.get("scope", "").split()) == ["vso.code", "vso.work"],
               single_organization=len(pat.get("targetAccounts") or []) == 1,
               api_error=created.get("patTokenError"))
        if not authorization_id or not token:
            return
        expiry = datetime.fromisoformat(pat["validTo"].replace("Z", "+00:00"))
        discovery = api("https://dev.azure.com/" + quote(config.org, safe="") +
                        "/_apis/wit/wiql?api-version=7.1&%24top=1", bearer,
                        {"query": "SELECT [System.Id] FROM WorkItems ORDER BY [System.ChangedDate] DESC"})
        item_id = discovery["workItems"][0]["id"]

        def verify(name):
            start = time.perf_counter()
            stdout, stderr = io.StringIO(), io.StringIO()
            code = _run_fetch([str(item_id), "--org", config.org], stdout=stdout, stderr=stderr,
                              env={**os.environ, "AZWI_PAT": token}, config_path=None,
                              client_factory=AzureDevOpsClient, program="azwi")
            result = json.loads(stdout.getvalue())
            record(name, start, exit_code=code, linked_pr_count=len(result["sections"].get("prs", [])),
                   stderr_empty=not stderr.getvalue())

        verify("new_pat_full_fetch")
        if args.expire_before_renewal:
            print(json.dumps({"waiting": "disposable_pat_expiration"}), flush=True)
            while (remaining := expiry.timestamp() + 5 - time.time()) > 0:
                time.sleep(min(remaining, 5))
            start = time.perf_counter()
            pat_auth = "Basic " + base64.b64encode((":" + token).encode()).decode()
            try:
                api("https://dev.azure.com/" + quote(config.org, safe="") +
                    "/_apis/wit/workitems/" + str(item_id) + "?api-version=7.1", pat_auth)
                record("expired_pat_request", start, http_status=200, rejected=False)
            except HTTPError as exc:
                record("expired_pat_request", start, http_status=exc.code, rejected=exc.code in (401, 403))
            start = time.perf_counter()
            _, metadata = request("GET", suffix="&authorizationId=" + quote(authorization_id, safe=""))
            metadata_pat = metadata.get("patToken") or {}
            record("expired_pat_metadata", start, api_error=metadata.get("patTokenError"),
                   expiration_in_past=bool(metadata_pat.get("validTo")) and
                   datetime.fromisoformat(metadata_pat["validTo"].replace("Z", "+00:00")) < datetime.now(timezone.utc))
        start = time.perf_counter()
        status, updated = request("PUT", {"authorizationId": authorization_id,
                                           "validTo": (expiry + timedelta(minutes=5)).isoformat()})
        updated_pat = updated.get("patToken") or {}
        returned_expiry = (datetime.fromisoformat(updated_pat["validTo"].replace("Z", "+00:00"))
                           if updated_pat.get("validTo") else None)
        record("extend_disposable_pat_expiration", start, http_status=status,
               expiration_extended=returned_expiry is not None and returned_expiry > expiry,
               same_authorization_id=updated_pat.get("authorizationId") == authorization_id,
               token_returned=bool(updated_pat.get("token")), api_error=updated.get("patTokenError"))
        if returned_expiry and updated.get("patTokenError") == "none":
            verify("original_secret_full_fetch_after_extension")
    except Exception as exc:
        record("experiment_failure", time.perf_counter(), error_type=type(exc).__name__,
               http_status=getattr(exc, "code", None))
    finally:
        if authorization_id:
            start = time.perf_counter()
            try:
                status, _ = request("DELETE", suffix="&authorizationId=" + quote(authorization_id, safe=""))
                record("revoke_disposable_pat", start, http_status=status)
                recovery.unlink(missing_ok=True)
            except Exception as exc:
                record("cleanup_required", start, error_type=type(exc).__name__,
                       recovery_file=str(recovery))


if __name__ == "__main__":
    main()
