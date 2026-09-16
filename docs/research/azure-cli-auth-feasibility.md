**Azure CLI authentication feasibility investigation**

Investigated on September 16, 2026 using Windows, Python 3.13.3, Azure CLI 2.80.0, and the existing azwi checkout. This is an investigation record. It does not change the product authentication contract or implement onboarding.

Both authentication routes worked against the configured Azure DevOps organization. Azure CLI supplied a user Entra token that authenticated normal fetches and PAT lifecycle operations. No separate application registration or Azure DevOps CLI extension command was needed. This proves feasibility for the tested user and organization, not every tenant policy or account type.

The managed PAT route fits the requested performance goal. Its ordinary fetch path can continue reading a local PAT. Azure CLI is only needed for initial creation and later management. Temporary Entra tokens also work, but invoking Azure CLI for each azwi process added about 1.92 seconds in this environment even when it returned the identical cached token.

**Live evidence**

The initial `az account show` succeeded and identified a user account. The first attempt to acquire an Azure DevOps token failed with an interaction-required response. The user completed this command in their terminal:

```text
az login --scope 499b84ac-1321-427f-aa17-267ca6975798/.default
```

Token acquisition then succeeded. This is a concrete example of why a signed-in account alone is insufficient evidence of authentication readiness. The diagnostic suggested logging out, but logging out was unnecessary for this successful repair.

The acquired token's observed delegated scope was `user_impersonation`. It successfully listed PAT metadata and created a PAT. The bootstrap token therefore has broader permissions than the PAT generated for ordinary reads. Microsoft recommends `vso.pats` for applications managing PATs, but this experiment did not establish whether Azure CLI can obtain that narrower permission without additional consent or registration. [PAT lifecycle authentication requirements](https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate?view=azure-devops#pat-lifecycle-management-apis).

Five normal default fetches succeeded with each credential type. They included work item data, comments, and one linked PR. The Bearer experiment used the existing fetch implementation with a temporary client subclass that only changed the Authorization header. Downloading attachments, PR thread comments, and field discovery were not exercised. No work item content is stored in this report or the measurements.

The first disposable PAT experiment proved all of the following:

- The create API accepted `vso.work vso.code` and `allOrgs: false`.
- The response confirmed exactly those scopes and one target organization.
- The new secret completed a full fetch including linked PR metadata.
- Updating its expiration succeeded with the same authorization ID.
- The update response did not return a secret. The original secret remained usable.
- Revocation returned HTTP 204. The existing saved PAT was not modified.

This behavior matches the [create API](https://learn.microsoft.com/en-us/rest/api/azure/devops/tokens/pats/create?view=azure-devops-rest-7.1), [update API](https://learn.microsoft.com/en-us/rest/api/azure/devops/tokens/pats/update?view=azure-devops-rest-7.1), and [revoke API](https://learn.microsoft.com/en-us/rest/api/azure/devops/tokens/pats/revoke?view=azure-devops-rest-7.1). Calls used `https://vssps.dev.azure.com/{organization}/_apis/tokens/pats` with API version `7.1-preview.1` and Bearer authentication. The generic Basic/PAT security block in these REST reference pages conflicts with the dedicated lifecycle guidance. The tested Bearer flow is the relevant evidence.

The expiration experiment created a separate PAT with a 45-second lifetime. Five seconds after its server-returned expiration, a work item request still returned HTTP 200. A metadata lookup confirmed that the stored expiration was in the past. Extending this PAT returned HTTP 200 with `patTokenError: invalidToken` and no updated PAT. The test token was then revoked with HTTP 204.

This demonstrates two implementation requirements. HTTP status alone does not establish lifecycle success. The response's application error must be checked. Also, active-token extension and recovery from expiration must be distinct operations. The tested API rejected renewal after expiration, so replacement is the dependable recovery route to design for. The brief successful resource read after expiration could reflect propagation or validation caching. Its cause and duration were not established, and it must not be relied upon.

An initial expiration probe assumed successful update metadata would always be present and reported a parsing error. Its cleanup still revoked the disposable token. The corrected repeat captured `invalidToken` explicitly. Three disposable PATs were created across the investigation, and all three were revoked successfully. No existing PAT was extended or revoked.

**Measurements**

| Operation | Samples | Median | Observed range |
| --- | ---: | ---: | ---: |
| Launch Python and render azwi help | 5 | 0.591 s | 0.563 to 0.607 s |
| Azure CLI account show | 5 | 1.868 s | 1.799 to 1.916 s |
| Acquire cached Entra token through Azure CLI | 5 | 1.919 s | 1.880 to 1.960 s |
| Full fetch using saved PAT, within a running Python process | 5 | 0.934 s | 0.708 to 1.529 s |
| Full fetch using already acquired Entra token, within a running Python process | 5 | 0.885 s | 0.589 to 1.598 s |
| Read the existing saved credential | 100 in one batch | 0.326 ms average | Not sampled individually |

All five repeated token acquisitions returned exactly the same token. Equality was checked in memory without printing or storing the token. This demonstrates reuse, but does not establish that no ancillary network activity occurred. Azure CLI startup and authentication processing still happen on every subprocess invocation.

Adding the measured median token-acquisition time to the median Bearer fetch time estimates 2.804 seconds, compared with 0.934 seconds for the saved-PAT fetch. This is an estimate from separate measurements. It excludes common Python, uv, and package startup costs. It is not a measured end-to-end uvx comparison. The small difference between PAT and Bearer HTTP fetch medians is within the observed network variation and does not establish that either authentication header is intrinsically faster.

The first acquisition after the user signed in took 2.130 seconds. This is not a cold-refresh benchmark because login may already have populated the cache. Network token refresh after natural expiration and interactive sign-in duration were not measured.

Single lifecycle samples, excluding Entra acquisition, were 0.399 seconds to create, 0.431 seconds to extend expiration, and 0.235 seconds to revoke. A new-PAT full verification took 1.269 seconds. These are feasibility observations, not statistically stable latency estimates.

The observed Entra token still had approximately 79 minutes remaining. Therefore a hardcoded one-hour lifetime would be incorrect for this case. Microsoft documents variable default lifetimes of 60 to 90 minutes. Production code should use the expiration supplied by Azure CLI rather than decode token internals or assume a fixed duration. JWT claims were inspected only as part of this investigation. [Entra token lifetimes](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens#token-lifetime).

**Compatibility and practical limits**

| Scenario | Evidence |
| --- | --- |
| Existing user login with additional interactive authentication needed | Observed and repaired successfully |
| Current user and configured organization | PAT management and both fetch routes succeeded |
| Extend an active PAT | Succeeded and preserved the original secret |
| Extend a PAT after its expiration | Rejected with `invalidToken` in the corrected live test |
| Service principal or managed identity managing PATs | Unsupported by Microsoft's lifecycle guidance |
| Organizations restricting PAT creation or lifetime | Restrictions apply to creation and renewal. Not exercised here |
| Multiple tenants, guest users, and personal Microsoft accounts | Not tested |
| Users without Azure subscriptions | CLI offers `--allow-no-subscriptions`. This account scenario was not tested |
| macOS and Linux | Not benchmarked |
| Revoked PAT recovery | Not tested. Revocation is different from expiration |
| Silent Entra refresh after token expiration | Not benchmarked |

Organization and tenant policies can restrict token creation, scopes, and lifetime. Successful creation here does not bypass or predict those policies elsewhere. [PAT policy reference](https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/manage-pats-with-policies-for-administrators?view=azure-devops). The no-subscription login option is documented in the [Azure CLI login reference](https://learn.microsoft.com/en-us/cli/azure/reference-index?view=azure-cli-latest#az-login).

The restricted execution sandbox denied Azure CLI filesystem access and outbound sockets. Those failed samples are stored separately and excluded from the successful measurements above. Running the reviewed probes with normal cache and network access succeeded. An agent may therefore have access to an azwi PAT file while lacking access needed by Azure CLI. Both modes still require permission to make Azure DevOps requests.

The present credential reader only accepts a `pat` key for each organization. Adding renewal metadata requires a deliberate schema update. The HTTP client currently assumes PAT authentication. No production files were modified during these experiments.

**Decision supported by this investigation**

Proceeding to design managed PAT onboarding is justified for Entra user accounts where organization policy allows it. It avoids the measured Azure CLI overhead during ordinary fetches and preserves explicit read-only scopes. Temporary-token mode is technically viable as an optional choice. Its approximately two-second per-invocation cost should be disclosed or addressed if it becomes a frequent-use path.

The lifecycle design should distinguish extending a still-active PAT from issuing a replacement after expiration. Both require a usable Entra login. A PAT cannot renew itself through the lifecycle API. The creation and active-extension tests succeeded using Azure CLI's existing delegated user token without an azwi app registration.

Caching an Entra token within one process does not remove Azure CLI startup from the next independent azwi invocation. Avoiding that cost across invocations would require an azwi-owned expiring token cache, a persistent helper, or another supported authentication integration. None was implemented or benchmarked here. Reading Azure CLI's private cache files directly would add implementation coupling.

Detailed onboarding and renewal design should follow separately. The remaining compatibility cases above should remain explicit limits rather than assumed support.

**Reproduction and artifacts**

The read-only probe automatically selects one accessible work item through a read-only WIQL query. It does not print the ID or content. It captures subprocess output, uses secret-free result fields, and bypasses skill synchronization for the in-process fetch measurements.

```text
python docs/research/azure_cli_auth_probe.py --samples 5 --output docs/research/azure-cli-auth-measurements.json
```

The lifecycle probe explicitly creates a disposable organization-scoped PAT, verifies it, extends it, and revokes it in a cleanup block. Do not treat it as a read-only command. Its recovery file contains only metadata and is removed after successful revocation. The secret stays in memory.

```text
python docs/research/azure_cli_pat_lifecycle_probe.py
python docs/research/azure_cli_pat_lifecycle_probe.py --expire-before-renewal
```

Raw results are stored in the adjacent measurement JSON files. The probes use the existing local organization and authentication configuration. No token value is written to the measurements or azwi credentials file.
