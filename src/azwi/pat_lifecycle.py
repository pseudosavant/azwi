"""Organization-bound Azure DevOps identity and PAT management requests."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID, uuid4

from azwi.errors import ApiError, AuthError, AzwiError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def discover_tenant(org):
    request = Request(f"https://vssps.dev.azure.com/{quote(org, safe='')}", method="HEAD")
    try:
        with build_opener(NoRedirect).open(request, timeout=20) as response:
            header = response.headers.get("X-VSS-ResourceTenant")
    except HTTPError as exc:
        header = exc.headers.get("X-VSS-ResourceTenant")
    except (OSError, URLError):
        raise ApiError("Cannot discover the organization's tenant. Check network access or supply --tenant.") from None
    try:
        value = UUID(header)
        return str(value) if value.int else None
    except (ValueError, TypeError, AttributeError):
        return None


class PatLifecycle:
    def __init__(self, org, credential, *, opener=None):
        self.org = org
        self.credential = credential
        self.opener = opener or build_opener(NoRedirect).open

    def _request(self, method, path, payload=None, *, identity=False):
        host = "dev.azure.com" if identity else "vssps.dev.azure.com"
        url = f"https://{host}/{quote(self.org, safe='')}{path}"
        request = Request(url, method=method, data=json.dumps(payload).encode() if payload is not None else None,
                          headers={"Authorization": "Bearer " + self.credential.pat,
                                   "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=30) as response:
                body = response.read()
                result = json.loads(body) if body else {}
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise AuthError("Azure DevOps denied authentication or PAT management. Check the account, tenant, and organization PAT policies.") from None
            raise ApiError(f"Azure DevOps authentication service failed with HTTP {exc.code}.") from None
        except (ValueError, OSError, URLError):
            # Mutations are deliberately never retried after ambiguous failures.
            raise ApiError("Azure DevOps authentication service returned an invalid response or could not be reached. A PAT operation may have completed. Check your Personal access tokens page before retrying.") from None

    def identity(self):
        payload = self._request("GET", "/_apis/connectionData?api-version=7.1-preview.1", identity=True)
        user = payload.get("authenticatedUser") or {}
        try:
            identifier = UUID(user["id"])
            if not identifier.int or user.get("isActive") is False:
                raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError):
            raise AuthError("Could not verify the signed-in Azure DevOps identity for this organization.") from None
        name = user.get("providerDisplayName") or user.get("customDisplayName") or "Azure DevOps user"
        name = "".join(c for c in str(name) if c.isprintable())[:120]
        return str(identifier), name

    def _result(self, result):
        error = result.get("patTokenError")
        if error != "none":
            messages = {
                "invalidToken": "The PAT can no longer be extended. Run setup to create a replacement.",
                "patLifespanPolicyViolation": "Organization policy rejected the PAT lifetime. Run setup with a shorter --pat-lifetime-days.",
                "accessDenied": "Organization policy or account permissions denied PAT management.",
                "fullScopePatPolicyViolation": "Organization policy rejected the PAT scopes.",
                "globalPatPolicyViolation": "Organization policy rejected PAT creation.",
            }
            raise AuthError(messages.get(error, "Azure DevOps rejected the PAT operation. Check organization policies and run setup."))
        pat = result.get("patToken")
        if not isinstance(pat, dict):
            raise AuthError("Azure DevOps returned incomplete PAT metadata.")
        return pat

    def create(self, lifetime_days):
        display_name = "azwi-" + uuid4().hex
        result = self._request("POST", "/_apis/tokens/pats?api-version=7.1-preview.1", {
            "displayName": display_name, "scope": "vso.work vso.code", "allOrgs": False,
            "validTo": datetime.fromtimestamp(time.time() + lifetime_days * 86400, timezone.utc).isoformat(),
        })
        pat = self._result(result)
        try:
            identifier = str(UUID(pat["authorizationId"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AuthError(f"PAT creation returned incomplete metadata. Check and remove the token named {display_name} on your Personal access tokens page.") from None
        try:
            secret = pat["token"]
            expiry = self._expiry(pat)
            if (not isinstance(secret, str) or not secret or any(c.isspace() for c in secret)
                    or set(pat.get("scope", "").split()) != {"vso.work", "vso.code"}
                    or len(pat.get("targetAccounts") or []) != 1):
                raise ValueError
            return {"pat": secret, "managed": True, "authorization_id": identifier,
                    "expires_on": expiry, "tenant": self.credential.tenant}
        except (ValueError, KeyError, TypeError, AttributeError, AuthError):
            try:
                self.revoke(identifier)
            except AzwiError:
                raise AuthError(f"The generated PAT had invalid metadata and could not be revoked. Remove PAT {identifier} on your Personal access tokens page.") from None
            raise AuthError("The generated PAT did not match the requested scope or lifetime. It was revoked.") from None

    def extend(self, identifier, lifetime_days):
        result = self._request("PUT", "/_apis/tokens/pats?api-version=7.1-preview.1", {
            "authorizationId": identifier,
            "validTo": datetime.fromtimestamp(time.time() + lifetime_days * 86400, timezone.utc).isoformat(),
        })
        pat = self._result(result)
        if pat.get("authorizationId") != identifier:
            raise AuthError("PAT renewal returned an unexpected authorization ID.")
        return self._expiry(pat)

    @staticmethod
    def _expiry(pat):
        try:
            value = datetime.fromisoformat(pat["validTo"].replace("Z", "+00:00"))
            if value.tzinfo is None or value.timestamp() <= time.time() + 60:
                raise ValueError
            return int(value.timestamp())
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AuthError("Azure DevOps returned an invalid PAT expiration.") from None

    def revoke(self, identifier):
        identifier = str(UUID(identifier))
        self._request("DELETE", "/_apis/tokens/pats?api-version=7.1-preview.1&authorizationId=" + identifier)
