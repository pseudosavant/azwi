from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from urllib.parse import unquote, urlsplit


DISTRIBUTION_NAME = "azwi"


def is_development_build() -> bool:
    """Require installed-distribution evidence before maintaining a user's skill."""
    try:
        installed = distribution(DISTRIBUTION_NAME)
        # A globally installed distribution must not authorize a shadowing checkout.
        module_path = Path(__file__).resolve()
        if not any(
            str(entry).replace("\\", "/") == "azwi/runtime.py"
            and Path(installed.locate_file(entry)).resolve() == module_path
            for entry in installed.files or ()
        ):
            return True

        direct_url = installed.read_text("direct_url.json")
        if direct_url is None:
            return False
        source = json.loads(direct_url)
        if not isinstance(source, dict) or not isinstance(source.get("url"), str):
            return True
        if "dir_info" in source:
            return True
        url = urlsplit(source["url"])
        if url.scheme.lower() == "file":
            # An installed wheel is a release artifact, even when supplied locally.
            return not (
                unquote(url.path).lower().endswith(".whl")
                and isinstance(source.get("archive_info"), dict)
            )
        return not bool(url.scheme)
    except (PackageNotFoundError, OSError, ValueError, TypeError, AttributeError):
        return True
