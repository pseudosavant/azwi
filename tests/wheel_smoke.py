"""Run with: python tests/wheel_smoke.py dist/azwi-1.2.0-py3-none-any.whl."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    wheel = Path(sys.argv[1]).resolve()
    project = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    with zipfile.ZipFile(wheel) as archive:
        assert "azwi/skill.py" in archive.namelist()
        assert "azwi/runtime.py" in archive.namelist()
        assert any(name.endswith("/licenses/LICENSE") for name in archive.namelist())

    with tempfile.TemporaryDirectory(prefix="azwi-wheel-smoke-") as temporary:
        workspace = Path(temporary)
        home = workspace / "home"
        home.mkdir()
        environment = dict(os.environ)
        environment.update(HOME=str(home), USERPROFILE=str(home), UV_TOOL_DIR=str(workspace / "tools"))
        environment.pop("PYTHONPATH", None)
        environment.pop("VIRTUAL_ENV", None)

        def run(*args: object, cwd: Path = workspace) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                [str(arg) for arg in args], cwd=cwd, env=environment,
                capture_output=True, text=True, encoding="utf-8", timeout=120,
            )
            if result.returncode:
                raise AssertionError(f"Command failed: {args}\n{result.stdout}\n{result.stderr}")
            return result

        venv = workspace / "venv"
        run("uv", "venv", "--python", sys.executable, venv)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run("uv", "pip", "install", "--python", python, wheel)
        path = home / ".agents" / "skills" / "azure-workitem" / "SKILL.md"

        def runtime_info() -> dict:
            result = run(python, "-c", """
import json
from importlib.metadata import distribution
from azwi import __version__
from azwi.runtime import is_development_build
installed = distribution("azwi")
print(json.dumps({"version": __version__, "distribution_version": installed.version,
                  "local": is_development_build(), "direct_url": installed.read_text("direct_url.json")}))
""")
            info = json.loads(result.stdout)
            assert info["version"] == info["distribution_version"] == version
            return info

        def write_older_skill() -> bytes:
            result = run(python, "-c", """
import azwi.skill as skill
skill.__version__ = "0"
print(skill.render_skill(), end="")
""")
            content = result.stdout.encode("utf-8")
            path.write_bytes(content)
            return content

        assert runtime_info()["local"] is False
        empty = json.loads(run(python, "-m", "azwi", "skill", "status").stdout)
        assert empty["installed"] is False
        assert empty["path"] == str(path)
        run(python, "-m", "azwi", "--version")
        assert not path.exists()
        run(python, "-m", "azwi", "skill", "install")
        canonical = path.read_bytes()
        assert b"uvx azwi <id>" in canonical
        assert b"\r" not in canonical
        write_older_skill()
        result = run(python, "-m", "azwi", "--version")
        assert result.stdout.strip() == version
        assert f"0 -> {version}" in result.stderr
        assert path.read_bytes() == canonical
        print("Installed wheel version, canonical skill, and automatic synchronization: passed")

        # Install by distribution name from a local wheel listing. This exercises
        # index-style metadata without querying a package index for azwi.
        run("uv", "pip", "uninstall", "--python", python, "azwi")
        run("uv", "pip", "install", "--python", python, "--no-index", "--no-deps", "--find-links", wheel.parent, f"azwi=={version}")
        info = runtime_info()
        assert info["direct_url"] is None
        assert info["local"] is False
        write_older_skill()
        run(python, "-m", "azwi", "--about")
        assert path.read_bytes() == canonical
        print("Index-style installed metadata remains eligible: passed")

        for editable in (False, True):
            run("uv", "pip", "uninstall", "--python", python, "azwi")
            options = ["--editable"] if editable else []
            run("uv", "pip", "install", "--python", python, "--no-deps", *options, repository)
            assert runtime_info()["local"] is True
            older = write_older_skill()
            result = run(python, "-m", "azwi", "--version")
            assert result.stderr == ""
            assert path.read_bytes() == older
            result = run(python, "-m", "azwi", "skill", "install")
            assert json.loads(result.stdout)["updated"] is True
            assert path.read_bytes() == canonical
        print("Local source and editable installs skip synchronization but allow explicit installation: passed")

        for source, development in ((wheel, False), (repository, True)):
            older = write_older_skill()
            result = run("uvx", "--from", source, "azwi", "skill", "status")
            assert json.loads(result.stdout)["local_development_build"] is development
            assert path.read_bytes() == older
            result = run("uvx", "--from", source, "azwi", "skill", "install")
            assert json.loads(result.stdout)["updated"] is True
            assert path.read_bytes() == canonical
        print("uvx wheel and local-source skill commands: passed")

        older = write_older_skill()
        result = run("uv", "run", "./azwi.py", "--help", cwd=repository)
        assert "azwi <work_item_id> [options]" in result.stdout
        assert "--repo" not in result.stdout
        assert path.read_bytes() == older
        result = run("uv", "run", "./azwi.py", "skill", "status", cwd=repository)
        assert json.loads(result.stdout)["local_development_build"] is True
        assert path.read_bytes() == older
        print("PEP 723 wrapper and development isolation: passed")


if __name__ == "__main__":
    main()
