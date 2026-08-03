import importlib.util
import json
from pathlib import Path

import pytest


SOURCE = Path(__file__).with_name("production-main.py")
SPEC = importlib.util.spec_from_file_location("production_main_report_state", SOURCE)
main = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(main)


def configure_paths(tmp_path: Path) -> None:
    main.REPORT_STATE_PATH = str(tmp_path / "report_state.json")
    main.REPORT_STATE_BACKUP_DIR = str(tmp_path / "backups")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_save_report_state_preserves_active_file_when_serialization_fails(tmp_path):
    configure_paths(tmp_path)
    active = Path(main.REPORT_STATE_PATH)
    original = {"hourly": {"2026-08-03 10:00": {}}}
    write_json(active, original)

    with pytest.raises(TypeError):
        main._save_report_state({"not_json_serializable": object()})

    assert json.loads(active.read_text(encoding="utf-8")) == original
    assert list(tmp_path.glob(".report_state.json.*.tmp")) == []


def test_load_report_state_recovers_newest_valid_backup(tmp_path):
    configure_paths(tmp_path)
    active = Path(main.REPORT_STATE_PATH)
    active.write_text("{broken", encoding="utf-8")
    backups = Path(main.REPORT_STATE_BACKUP_DIR)
    write_json(backups / "report_state.json.bak.20260801", {"hourly": {"old": {}}})
    write_json(backups / "report_state.json.bak.20260802", {"hourly": {"new": {}}})

    assert main._load_report_state()["hourly"] == {"new": {}}


def test_load_report_state_refuses_silent_reset_without_valid_backup(tmp_path):
    configure_paths(tmp_path)
    active = Path(main.REPORT_STATE_PATH)
    active.write_text("{broken", encoding="utf-8")
    backups = Path(main.REPORT_STATE_BACKUP_DIR)
    backups.mkdir()
    (backups / "report_state.json.bak.20260803").write_text("also broken", encoding="utf-8")

    error_type = getattr(main, "ReportStateError", RuntimeError)
    with pytest.raises(error_type):
        main._load_report_state()
