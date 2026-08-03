import importlib.util
import json
import threading
from pathlib import Path

import pytest
import yaml


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


def test_concurrent_rebuild_events_do_not_overwrite_each_other(tmp_path, monkeypatch):
    configure_paths(tmp_path)
    write_json(Path(main.REPORT_STATE_PATH), {"rebuild_stats": {}})
    original_load = main._load_report_state
    both_loaded = threading.Barrier(2)

    def synchronized_load():
        state = original_load()
        both_loaded.wait(timeout=3)
        return state

    monkeypatch.setattr(main, "_load_report_state", synchronized_load)
    errors = []

    def record(server_id: int, name: str) -> None:
        try:
            main._record_rebuild_event(server_id, name, "test")
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=record, args=(1, "1")),
        threading.Thread(target=record, args=(2, "2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    state = json.loads(Path(main.REPORT_STATE_PATH).read_text(encoding="utf-8"))
    assert set(state["rebuild_stats"]) == {"1", "2"}


def test_tracking_start_does_not_predate_available_history():
    hourly = {
        "2026-07-28 09:25": {
            "1": {"name": "1", "outbound_bytes": 100, "inbound_bytes": 20}
        },
        "2026-07-28 09:30": {
            "1": {"name": "1", "outbound_bytes": 150, "inbound_bytes": 30}
        },
    }

    result = main._compute_tracking_totals(hourly, "2026-07-01 00:00")

    assert result == {
        "start": "2026-07-28 09:25",
        "outbound_tb": "0.000",
        "inbound_tb": "0.000",
    }


def test_tracking_start_keeps_an_override_inside_available_history():
    hourly = {
        "2026-07-28 09:25": {},
        "2026-07-28 09:30": {},
        "2026-07-28 09:35": {},
    }

    result = main._compute_tracking_totals(hourly, "2026-07-28 09:30")

    assert result["start"] == "2026-07-28 09:30"


def test_compose_mounts_mutable_state_as_one_directory():
    compose_path = Path(__file__).with_name("docker-compose.yml")
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    service = compose["services"]["hetzner-web"]

    assert service["environment"]["REPORT_STATE_PATH"] == "/app/state/report_state.json"
    assert service["environment"]["REPORT_STATE_BACKUP_DIR"] == "/app/state/report_state_backups"
    assert service["environment"]["THRESHOLD_STATE_PATH"] == "/app/state/threshold_state.json"
    assert "./state:/app/state" in service["volumes"]
    assert all(
        not volume.endswith(container_path)
        for volume in service["volumes"]
        for container_path in (
            ":/app/report_state.json",
            ":/app/report_state_backups",
            ":/app/threshold_state.json",
        )
    )
