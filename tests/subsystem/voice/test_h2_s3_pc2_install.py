"""H2 S3: scripts/cockpit/install-pc2.ps1 and undo-pc2.ps1 over temp folders laid out like PC2's Row-Bot 5.3.0 folder."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
INSTALL = REPO / "scripts" / "cockpit" / "install-pc2.ps1"
UNDO = REPO / "scripts" / "cockpit" / "undo-pc2.ps1"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None or sys.platform != "win32", reason="the PC2 scripts run with pwsh on Windows")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def pc2(tmp_path: Path) -> dict[str, Path]:
    """A Row-Bot 5.3.0 folder, a data folder with update_config.json, a source checkout with the patch, and no backup."""
    root = tmp_path / "Row-Bot"
    app = root / "versions" / "5.3.0" / "app"
    _write(root / "versions" / "5.3.0" / "version.json", json.dumps({"version": "5.3.0"}))
    _write(root / "state.json", json.dumps({"schema": 1, "active": "5.3.0", "previous": None}))
    _write(app / "launcher.py", "OLD = 1\n")
    _write(app / "pyproject.toml", "[project]\nname='row-bot'\n")
    _write(app / "src" / "row_bot" / "__init__.py", "OLD = 1\n")
    _write(app / "src" / "row_bot" / "voice" / "runtime.py", "OLD = 1\n")
    _write(app / "src" / "row_bot" / "__pycache__" / "runtime.cpython-313.pyc", "old bytecode")
    _write(app / "src" / "row_bot" / "static" / "client-v2" / "assets" / "old-1a2b.js", "old screen")
    read_only = _write(app / "sounds" / "ding.txt", "read only")
    read_only.chmod(stat.S_IREAD)
    data = tmp_path / "data"
    _write(data / "update_config.json", json.dumps({"channel": "stable", "auto_check": True, "auto_download": True}))

    source = tmp_path / "checkout"
    shutil.copy2(REPO / "scripts" / "app_payload_manifest.py", _write(source / "scripts" / "app_payload_manifest.py", "").resolve())
    _write(source / "scripts" / "verify_runtime_dependencies.py", "NEW = 1\n")
    _write(source / "launcher.py", "NEW = 1\n")
    for name in ("pyproject.toml", "uv.lock", "requirements.txt", "row-bot.ico"):
        _write(source / name, "new\n")
    _write(source / "src" / "row_bot" / "__init__.py", "NEW = 1\n")
    _write(source / "src" / "row_bot" / "voice" / "runtime.py", "NEW = 1\n")
    _write(source / "src" / "row_bot" / "voice" / "gemini_live.py", "NEW = 1\n")
    _write(source / "src" / "row_bot" / "static" / "client-v2" / "index.html", "<html>talk</html>")
    _write(source / "src" / "row_bot" / "static" / "client-v2" / "assets" / "talk-9z.js", "new screen")
    for folder in ("static", "sounds", "bundled_skills", "tool_guides"):
        _write(source / folder / "keep.txt", "new\n")
    return {"root": root, "app": app, "data": data, "source": source, "backup": tmp_path / "backup"}


def _run(script: Path, pc2: dict[str, Path], *args: str, source: bool = True) -> subprocess.CompletedProcess[str]:
    command = [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script), "-AppDir", str(pc2["root"]), "-BackupDir", str(pc2["backup"]),
               "-DataDir", str(pc2["data"])]
    if source and script is INSTALL:
        command += ["-SourceDir", str(pc2["source"])]
    env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    return subprocess.run([*command, *args], capture_output=True, text=True, timeout=300, env=env)


def test_dry_run_changes_nothing(pc2):
    before = (_hashes(pc2["root"]), _hashes(pc2["data"]))
    result = _run(INSTALL, pc2, "-DryRun")
    assert result.returncode == 0, result.stdout
    assert (_hashes(pc2["root"]), _hashes(pc2["data"])) == before and not pc2["backup"].exists()


def test_install_puts_the_patch_in_and_undo_restores_everything_byte_for_byte(pc2):
    before = (_hashes(pc2["root"]), _hashes(pc2["data"]))
    result = _run(INSTALL, pc2)
    assert result.returncode == 0, result.stdout
    assert (pc2["backup"] / "backup.json").is_file(), "no backup was made"
    assert (pc2["app"] / "src" / "row_bot" / "voice" / "gemini_live.py").read_text() == "NEW = 1\n"
    assert (pc2["app"] / "src" / "row_bot" / "voice" / "runtime.py").read_text() == "NEW = 1\n"
    assert not list(pc2["app"].rglob("__pycache__")), "stale bytecode was left"
    assert (pc2["app"] / "src" / "row_bot" / "static" / "client-v2" / "index.html").is_file()
    assert not (pc2["app"] / "src" / "row_bot" / "static" / "client-v2" / "assets" / "old-1a2b.js").exists()
    config = json.loads((pc2["data"] / "update_config.json").read_text())
    assert config == {"channel": "stable", "auto_check": False, "auto_download": False}
    assert {k: v for k, v in _hashes(pc2["root"]).items() if k.startswith("state") or k.endswith("version.json")} == \
        {k: v for k, v in before[0].items() if k.startswith("state") or k.endswith("version.json")}

    installed = _hashes(pc2["root"])
    again = _run(INSTALL, pc2)
    assert again.returncode == 3 and _hashes(pc2["root"]) == installed, "a second install must exit 3 and change nothing"

    # Row-Bot, running after the install, writes bytecode of its own; undo must not keep it
    _write(pc2["app"] / "src" / "row_bot" / "__pycache__" / "new.cpython-313.pyc", "bytecode written later")
    undone = _run(UNDO, pc2)
    assert undone.returncode == 0, undone.stdout
    assert (_hashes(pc2["root"]), _hashes(pc2["data"])) == before
    assert not pc2["backup"].exists(), "undo left the backup behind"
    assert _run(UNDO, pc2).returncode == 4, "a second undo has no backup"


def test_undo_deletes_an_update_config_that_was_not_there(pc2):
    (pc2["data"] / "update_config.json").unlink()
    before = (_hashes(pc2["root"]), _hashes(pc2["data"]))
    assert _run(INSTALL, pc2).returncode == 0
    assert json.loads((pc2["data"] / "update_config.json").read_text()) == {"auto_check": False, "auto_download": False}
    assert _run(UNDO, pc2).returncode == 0
    assert (_hashes(pc2["root"]), _hashes(pc2["data"])) == before


@pytest.mark.parametrize("break_it", ["state", "version"])
def test_another_version_exits_2_and_changes_nothing(pc2, break_it):
    if break_it == "state":
        _write(pc2["root"] / "state.json", json.dumps({"schema": 1, "active": "5.2.0", "previous": None}))
    else:
        _write(pc2["root"] / "versions" / "5.3.0" / "version.json", json.dumps({"version": "5.4.0"}))
    before = (_hashes(pc2["root"]), _hashes(pc2["data"]))
    assert _run(INSTALL, pc2).returncode == 2
    assert (_hashes(pc2["root"]), _hashes(pc2["data"])) == before and not pc2["backup"].exists()
