from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WRAPPER = ROOT / "azwi.py"


def _prefer_src_package() -> None:
    src_str = str(SRC)
    if src_str in sys.path:
        sys.path.remove(src_str)
    sys.path.insert(0, src_str)

    loaded = sys.modules.get("azwi")
    if loaded is None or hasattr(loaded, "__path__"):
        return

    module_file = getattr(loaded, "__file__", None)
    if module_file and Path(module_file).resolve() == WRAPPER:
        del sys.modules["azwi"]


_prefer_src_package()
