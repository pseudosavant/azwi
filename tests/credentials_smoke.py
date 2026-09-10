"""Test normal-user writes and sandbox reads without using a real PAT.

Run write outside the sandbox, check inside it, then cleanup outside it.
Pass the same disposable TOML path to each invocation.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from azwi.auth import require_pat, resolve_credential, save_credential


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["write", "check", "cleanup"])
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    org = "sandbox-probe"
    dummy = "azwi-disposable-nonsecret"
    if args.mode == "write":
        if args.path.exists():
            raise SystemExit("Refusing to overwrite an existing file")
        save_credential(org, dummy, args.path)
    credential = resolve_credential({}, org, args.path)
    assert credential.source == "file" and credential.pat == dummy
    assert require_pat({"AZWI_PAT": "override"}, org, args.path) == "override"
    assert resolve_credential({}, "other-org", args.path).source == "missing"
    if args.mode == "cleanup":
        args.path.unlink()
    print(f"{args.mode}: passed. File retrieval and environment override work.")


if __name__ == "__main__":
    main()
