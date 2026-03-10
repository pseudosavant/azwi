from __future__ import annotations

import base64
import json
import random
import time
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from azwi.errors import ApiError, AuthError, ConfigError, NotFoundError, ThrottledError


class AzureDevOpsClient:
    def __init__(
        self,
        org: str,
        pat: str,
        *,
        verbose: bool = False,
        stderr=None,
        opener=urlopen,
        sleep=time.sleep,
    ) -> None:
        if not org:
            raise ConfigError("Organization is required. Use --org, config defaults, or AZWI_ORG.")
        if not pat:
            raise AuthError("AZWI_PAT is not set.")
        self.org = org
        self.pat = pat
        self.verbose = verbose
        self.stderr = stderr
        self._opener = opener
        self._sleep = sleep

    def get_work_item(self, work_item_id: int) -> dict[str, Any]:
        return self._request_json(
            f"/_apis/wit/workitems/{work_item_id}",
            {"$expand": "relations", "api-version": "7.1-preview.3"},
        )

    def get_comments(self, project: str, work_item_id: int, limit: int) -> dict[str, Any]:
        return self._request_json(
            f"/{quote(project, safe='')}/_apis/wit/workItems/{work_item_id}/comments",
            {"$top": str(limit), "order": "desc", "api-version": "7.1-preview.4"},
        )

    def get_pull_request(self, project: str, repo_id: str, pr_id: int) -> dict[str, Any]:
        return self._request_json(
            f"/{quote(project, safe='')}/_apis/git/repositories/{quote(repo_id, safe='')}/pullRequests/{pr_id}",
            {"api-version": "7.1-preview.1"},
        )

    def get_work_item_type_fields(self, project: str, work_item_type: str) -> dict[str, Any]:
        return self._request_json(
            f"/{quote(project, safe='')}/_apis/wit/workitemtypes/{quote(work_item_type, safe='')}/fields",
            {"api-version": "7.1-preview.2"},
        )

    def download(self, url: str) -> tuple[bytes, str | None]:
        body, headers = self._request(
            url,
            absolute_url=True,
            binary=True,
            allow_auth=_should_send_auth(url),
            accept="*/*",
        )
        return body, headers.get("Content-Type")

    def _request_json(self, path: str, query: Mapping[str, str]) -> dict[str, Any]:
        body, _headers = self._request(
            path,
            query=query,
            binary=False,
            allow_auth=True,
            accept="application/json",
        )
        payload = json.loads(body.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}

    def _request(
        self,
        path_or_url: str,
        *,
        query: Mapping[str, str] | None = None,
        absolute_url: bool = False,
        binary: bool,
        allow_auth: bool,
        accept: str,
    ) -> tuple[bytes, Mapping[str, str]]:
        url = path_or_url if absolute_url else self._build_url(path_or_url, query)
        attempts = 6
        for attempt in range(attempts):
            request = Request(url, headers=self._headers(allow_auth=allow_auth, accept=accept))
            try:
                self._log(f"GET {url}")
                with self._opener(request) as response:
                    return response.read(), dict(response.headers.items())
            except HTTPError as exc:
                if exc.code == 429:
                    if attempt == attempts - 1:
                        raise ThrottledError(f"Azure DevOps throttled the request: HTTP 429 for {url}.") from exc
                    self._retry_sleep(attempt, exc.code)
                    continue
                if 500 <= exc.code < 600 and attempt < attempts - 1:
                    self._retry_sleep(attempt, exc.code)
                    continue
                if exc.code in (401, 403):
                    raise AuthError(f"Azure DevOps authentication failed: HTTP {exc.code}.") from exc
                if exc.code == 404:
                    raise NotFoundError(f"Azure DevOps resource not found: {url}.") from exc
                raise ApiError(f"Azure DevOps request failed: HTTP {exc.code} for {url}.") from exc
            except URLError as exc:
                if attempt < attempts - 1:
                    self._retry_sleep(attempt, "network")
                    continue
                raise ApiError(f"Azure DevOps request failed: {exc.reason}.") from exc
        raise ApiError(f"Azure DevOps request failed after retries: {url}.")

    def _build_url(self, path: str, query: Mapping[str, str] | None = None) -> str:
        base = f"https://dev.azure.com/{quote(self.org, safe='')}"
        url = f"{base}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        return url

    def _headers(self, *, allow_auth: bool, accept: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "azwi/0.9.0",
        }
        if allow_auth:
            token = base64.b64encode(f":{self.pat}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        return headers

    def _retry_sleep(self, attempt: int, reason: int | str) -> None:
        delay = min(8.0, 0.5 * (2**attempt) + random.uniform(0.0, 0.25))
        self._log(f"retry after {reason}: sleeping {delay:.2f}s")
        self._sleep(delay)

    def _log(self, message: str) -> None:
        if self.verbose and self.stderr is not None:
            self.stderr.write(f"{message}\n")


def _should_send_auth(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host.endswith("dev.azure.com") or host.endswith("visualstudio.com")
