"""Windows UI readers must not prevent the worker's atomic state replace."""
import importlib
import json
import os

import pytest

from trade_router_studio.file_io import open_shared_text, read_shared_text, replace_snapshot, snapshot_backup_path
from test_studio import workspace


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing semantics")
def test_plain_python_reader_reproduces_reported_replace_failure(tmp_path):
    target = tmp_path / "상태.json"
    target.write_text("old", encoding="utf-8")
    replacement = tmp_path / "new.json"
    replacement.write_text("new", encoding="utf-8")
    with target.open("r", encoding="utf-8"):
        with pytest.raises(PermissionError):
            replacement.replace(target)
    replacement.replace(target)
    assert target.read_text(encoding="utf-8") == "new"


def test_shared_reader_keeps_consistent_old_snapshot_during_replace(tmp_path):
    target = tmp_path / "한글 상태.json"
    target.write_text('{"instrument_names":{"000001":"이전"}}', encoding="utf-8")
    replacement = tmp_path / "new.json"
    replacement.write_text('{"instrument_names":{"000001":"최신"}}', encoding="utf-8")
    with open_shared_text(target) as reader:
        replace_snapshot(replacement, target)
        assert json.load(reader)["instrument_names"]["000001"] == "이전"
    assert json.loads(read_shared_text(target))["instrument_names"]["000001"] == "최신"


def test_shared_reader_closes_handle_on_decode_error_and_reports_missing(tmp_path):
    target = tmp_path / "state.json"
    with pytest.raises(FileNotFoundError):
        read_shared_text(target)
    target.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        read_shared_text(target)
    target.unlink()
    assert not target.exists()


def test_dashboard_names_reader_uses_replace_compatible_snapshot(tmp_path, monkeypatch):
    module = importlib.import_module("trade_router_studio.app")
    paths = workspace(tmp_path)
    directory = paths.data / "strategy-state"
    directory.mkdir(parents=True)
    target = directory / "예제_py.json"
    target.write_text(json.dumps({"instrument_names": {"000001": "이전"}}), encoding="utf-8")
    replacement = directory / "next.tmp"
    replacement.write_text(json.dumps({"instrument_names": {"000001": "최신"}}), encoding="utf-8")
    def concurrent_read(path):
        with open_shared_text(path) as reader:
            replace_snapshot(replacement, path)
            return reader.read()
    monkeypatch.setattr(module, "read_shared_text", concurrent_read)
    assert module._workspace_instrument_names(paths, "예제.py") == {"000001": "이전"}
    assert json.loads(read_shared_text(target))["instrument_names"]["000001"] == "최신"


@pytest.mark.skipif(os.name != "nt", reason="Windows native partial replacement semantics")
@pytest.mark.parametrize("error", [1176, 1177])
def test_native_partial_replace_always_has_known_previous_snapshot(tmp_path, monkeypatch, error):
    import ctypes
    from pathlib import Path
    from types import SimpleNamespace

    target, source = tmp_path / "state.json", tmp_path / "next.tmp"
    target.write_text("previous acknowledged state", encoding="utf-8")
    source.write_text("attempted state", encoding="utf-8")

    # Model the documented native outcomes, conditional on backup argument:
    # 1176 without a backup removes target; 1177 renames it to the backup.
    def partial_replace(replaced, replacement, backup, *_):
        assert backup == str(snapshot_backup_path(target))
        if error == 1177:
            os.replace(Path(replaced), Path(backup))
        elif not backup:
            Path(replaced).unlink()
        ctypes.set_last_error(error)
        return 0

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: SimpleNamespace(ReplaceFileW=partial_replace))
    with pytest.raises(OSError) as caught:
        replace_snapshot(source, target)
    assert caught.value.winerror == error
    survivor = target if target.exists() else snapshot_backup_path(target)
    assert survivor.read_text(encoding="utf-8") == "previous acknowledged state"
    assert source.read_text(encoding="utf-8") == "attempted state"


@pytest.mark.skipif(os.name != "nt", reason="Windows replacement backups")
def test_successful_replacements_keep_only_previous_target_in_backup(tmp_path):
    target, source = tmp_path / "state.json", tmp_path / "next.tmp"
    target.write_text("one", encoding="utf-8")
    for previous, current in [("one", "two"), ("two", "three")]:
        source.write_text(current, encoding="utf-8")
        replace_snapshot(source, target)
        assert target.read_text(encoding="utf-8") == current
        assert snapshot_backup_path(target).read_text(encoding="utf-8") == previous
