#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import runpy
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
SRC_PATH = REPO_ROOT / "src"
PACKAGE_PATH = SRC_PATH / "azwi"


def _add_local_src_to_path() -> None:
    src_path = SRC_PATH
    if src_path.is_dir():
        sys.path.insert(0, str(src_path))


def _expose_package_path() -> None:
    if not PACKAGE_PATH.is_dir():
        return

    globals()["__path__"] = [str(PACKAGE_PATH)]
    init_path = PACKAGE_PATH / "__init__.py"
    if init_path.is_file():
        package_globals = runpy.run_path(str(init_path))
        globals()["__all__"] = package_globals.get("__all__", [])
        for name in package_globals.get("__all__", []):
            globals()[name] = package_globals[name]
    if __spec__ is not None:
        __spec__.submodule_search_locations = [str(PACKAGE_PATH)]


_expose_package_path()


def main() -> int:
    _add_local_src_to_path()
    from azwi.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
