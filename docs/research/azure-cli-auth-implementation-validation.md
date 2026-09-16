# Authentication implementation validation

Validated on Windows on September 16, 2026 with the existing Azure CLI user session.

The production setup and fetch paths were exercised with an isolated config, credentials file, and skill directory. No resource content or tokens were included in the results.

| Operation | Result | Elapsed time |
| --- | --- | ---: |
| Entra setup with full work item verification | Passed | 2.703 s |
| Entra fetch including token acquisition | Passed | 2.396 s |
| Managed PAT setup with full work item verification | Passed | 2.947 s |
| Managed PAT fetch | Passed | 0.724 s |
| Repeated managed PAT setup | Passed | 0.891 s |
| Readiness check with a work item URL | Passed | 0.472 s |

These are single samples within a running Python process. They include production authentication and API calls but exclude Python and uv startup. They are functional checks rather than a benchmark.

The disposable PAT was revoked after the checks. The existing user config and credentials files were compared before and afterward and were unchanged. The test did not launch a fresh interactive login.

Automated tests cover login output routing, device-code selection, token reacquisition after login, cancellation and timeout cleanup, unattended execution, identity mismatches, PAT scopes, renewal cooldown, expired-token replacement, verification and storage failures, repeated setup, credential update races, bearer requests, refresh, and redirect handling.

## Manual Windows acceptance and cancellation correction

The user's PowerShell 7.6.6 transcript confirmed successful normal Microsoft sign-in and device-code completion, automatic return to setup, valid JSON output, and a subsequent fetch. An unattended fetch with an empty Azure CLI cache returned code 4 with empty stdout and no interactive prompt. The unattended setup also printed the expected authentication error, although its exit code was not shown. Work item 2879 had no linked PRs, so Code access correctly remained unverified.

Ctrl+C during device-code sign-in preserved the hashes of both azwi files, but returned -1 and left an Azure CLI Python process running. This failed the cleanup acceptance check. The separate menu-cancellation case was not included in the transcript.

The correction uses short process waits to allow Python's signal handler to run and a Windows job that kills descendants when azwi exits, including abrupt exits that bypass Python cleanup. New Windows integration tests use a fake Azure CLI in an isolated hidden console, send a real console Ctrl+C through uv and PowerShell, and check descendant termination through retained process handles. A separate test terminates the azwi parent forcibly and checks that its child is stopped. These tests do not access a Microsoft account or the network.

The process containment follows [Microsoft's Windows job object lifecycle](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

The user's follow-up transcript confirmed both cancellation cases after the correction, each with a fresh Azure CLI cache:

| Case | Observed exit code | Saved files unchanged | New login processes remaining | Stdout bytes | Result |
| --- | ---: | --- | --- | ---: | --- |
| Ctrl+C during device-code sign-in | -1 | Yes | None | 0 | Passed |
| Option 4 at the sign-in menu | 4 | Yes | None | 0 | Passed |

Ctrl+C emitted the expected cancellation diagnostic, although it appeared after the PowerShell prompt. The shell reported -1 for that interrupted invocation. The file hashes, empty stdout, and absence of a surviving login process establish successful cleanup independently of the launcher-reported interrupt status. Menu cancellation returned the expected authentication exit code directly.

These results complete the requested Windows manual cancellation acceptance checks. Native sign-in UI acceptance on other platforms remains untested.
