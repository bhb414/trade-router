from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZIP_DEFLATED, ZipFile

import trade_router_studio.updates as updates


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _release_files(version: str) -> dict[str, bytes]:
    return {
        "run.cmd": b"@echo off\necho updated\n",
        "pyproject.toml": f'[project]\nversion = "{version}"\n'.encode(),
        "tools/run.ps1": b"Write-Host updated\n",
        "apps/studio/src/trade_router_studio/__init__.py": (
            f'__version__ = "{version}"\n'.encode()
        ),
    }


def _write_release(tmp_path: Path, version: str) -> tuple[Path, Path]:
    files = _release_files(version)
    manifest = {
        "format": 1,
        "version": version,
        "files": [
            {"path": name, "sha256": _digest(value)}
            for name, value in sorted(files.items())
        ],
    }
    archive = tmp_path / f"trade-router-v{version}-windows.zip"
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        for name, value in files.items():
            bundle.writestr(name, value)
        bundle.writestr("release-manifest.json", json.dumps(manifest))
    checksum = tmp_path / f"{archive.name}.sha256"
    checksum.write_text(f"{updates._sha256(archive)}  {archive.name}\n", encoding="ascii")
    return archive, checksum


def test_update_download_is_verified_and_staged(tmp_path: Path, monkeypatch) -> None:
    archive, checksum = _write_release(tmp_path, "0.2.0")
    monkeypatch.setattr(
        updates,
        "_latest_release",
        lambda: {
            "tag_name": "v0.2.0",
            "html_url": "https://example.invalid/release",
            "assets": [
                {"name": archive.name, "browser_download_url": archive.as_uri()},
                {"name": checksum.name, "browser_download_url": checksum.as_uri()},
            ],
        },
    )
    install = tmp_path / "install"
    install.mkdir()
    (install / "run.cmd").write_text("old", encoding="utf-8")
    (install / "pyproject.toml").write_text("old", encoding="utf-8")
    workspace = tmp_path / "workspace"

    result = asyncio.run(
        updates.prepare_update(
            "0.1.0",
            workspace_root=workspace,
            install_root=install,
        )
    )

    assert result["prepared"] is True
    pending = json.loads(
        (workspace / "updates" / "pending-update.json").read_text(encoding="utf-8")
    )
    payload = Path(pending["payload_root"])
    assert payload.joinpath("run.cmd").read_bytes() == _release_files("0.2.0")["run.cmd"]
    assert pending["archive_sha256"] == updates._sha256(archive)


def test_release_without_both_verified_assets_cannot_apply() -> None:
    details = updates._release_details(
        {
            "tag_name": "v0.2.0",
            "html_url": "https://example.invalid/release",
            "assets": [
                {
                    "name": "trade-router-v0.2.0-windows.zip",
                    "browser_download_url": "https://example.invalid/archive",
                }
            ],
        },
        "0.1.0",
    )
    assert details["available"] is True
    assert details["can_apply"] is False


def test_launcher_applies_pending_update_without_touching_user_data(tmp_path: Path) -> None:
    source_launcher = Path(__file__).parents[1] / "tools" / "run.ps1"
    install = tmp_path / "install"
    tools = install / "tools"
    tools.mkdir(parents=True)
    shutil.copy2(source_launcher, tools / "run.ps1")
    (install / "run.cmd").write_text("old", encoding="utf-8")
    (install / "pyproject.toml").write_text("old", encoding="utf-8")

    local_app_data = tmp_path / "local-app-data"
    updates_root = local_app_data / "TradeRouter" / "updates"
    payload = updates_root / "v0.2.0-test" / "payload"
    payload_tools = payload / "tools"
    payload_tools.mkdir(parents=True)
    shutil.copy2(source_launcher, payload_tools / "run.ps1")
    new_run = b"@echo off\necho updated\n"
    new_project = b'[project]\nversion = "0.2.0"\n'
    (payload / "run.cmd").write_bytes(new_run)
    (payload / "pyproject.toml").write_bytes(new_project)
    rows = []
    for path in (payload / "run.cmd", payload / "pyproject.toml", payload_tools / "run.ps1"):
        rows.append(
            {
                "path": path.relative_to(payload).as_posix(),
                "sha256": updates._sha256(path),
            }
        )
    (payload / "release-manifest.json").write_text(
        json.dumps({"format": 1, "version": "0.2.0", "files": rows}),
        encoding="utf-8",
    )
    pending = {
        "version": "0.2.0",
        "install_root": str(install),
        "payload_root": str(payload),
        "archive_sha256": "0" * 64,
    }
    updates_root.mkdir(parents=True, exist_ok=True)
    pending_path = updates_root / "pending-update.json"
    pending_path.write_text(json.dumps(pending), encoding="utf-8")
    user_marker = local_app_data / "TradeRouter" / "data" / "orders.sqlite3"
    user_marker.parent.mkdir(parents=True)
    user_marker.write_text("preserve", encoding="utf-8")

    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(local_app_data)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(tools / "run.ps1"),
            "-ApplyUpdateOnly",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert (install / "run.cmd").read_bytes() == new_run
    assert (install / "pyproject.toml").read_bytes() == new_project
    assert user_marker.read_text(encoding="utf-8") == "preserve"
    assert not pending_path.exists()


def test_launcher_finds_python_when_py_launcher_is_unavailable(tmp_path: Path) -> None:
    source_launcher = Path(__file__).parents[1] / "tools" / "run.ps1"
    project = tmp_path / "project"
    tools = project / "tools"
    tools.mkdir(parents=True)
    shutil.copy2(source_launcher, tools / "run.ps1")

    command_directory = tmp_path / "commands"
    command_directory.mkdir()
    python_command = command_directory / "python.cmd"
    python_command.write_text(
        f'@echo off\n"{sys.executable}" %*\n',
        encoding="ascii",
    )

    powershell = shutil.which("powershell.exe")
    assert powershell is not None
    environment = os.environ.copy()
    environment["PATH"] = str(command_directory)
    environment.pop("TRADE_ROUTER_PYTHON", None)
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(tools / "run.ps1"),
            "-CheckPythonOnly",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "python"


def test_launcher_first_run_returns_only_the_venv_python_path(tmp_path: Path) -> None:
    source_launcher = Path(__file__).parents[1] / "tools" / "run.ps1"
    project = tmp_path / "first-run-project"
    tools = project / "tools"
    tools.mkdir(parents=True)
    shutil.copy2(source_launcher, tools / "run.ps1")
    (project / "pyproject.toml").write_text(
        "\n".join(
            (
                "[build-system]",
                'requires = ["setuptools>=75"]',
                'build-backend = "setuptools.build_meta"',
                "",
                "[project]",
                'name = "launcher-first-run-test"',
                'version = "0.0.1"',
            )
        ),
        encoding="utf-8",
    )

    powershell = shutil.which("powershell.exe")
    assert powershell is not None
    environment = os.environ.copy()
    environment["TRADE_ROUTER_PYTHON"] = sys.executable
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(tools / "run.ps1"),
            "-PrepareEnvironmentOnly",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )

    expected_python = project / ".venv" / "Scripts" / "python.exe"
    assert completed.returncode == 0, completed.stderr
    assert expected_python.is_file()
    assert completed.stdout.splitlines()[-1] == str(expected_python)
