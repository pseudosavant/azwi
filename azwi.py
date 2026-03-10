#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import sys
from pathlib import Path


def _add_local_src_to_path() -> None:
    repo_root = Path(__file__).resolve().parent
    src_path = repo_root / "src"
    if src_path.is_dir():
        sys.path.insert(0, str(src_path))


def main() -> int:
    _add_local_src_to_path()
    from azwi.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
