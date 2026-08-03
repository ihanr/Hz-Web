import importlib.util
from pathlib import Path


SOURCE = Path(__file__).with_name("production-main.py")
SPEC = importlib.util.spec_from_file_location(
    "production_main_rebuild_timestamp_summary",
    SOURCE,
)
main = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(main)


def test_summary_orders_naive_and_aware_rebuild_timestamps():
    state = {
        "rebuild_stats": {
            "2": {
                "count": 1,
                "last_time": "2026-08-01 08:00",
                "last_time_iso": "2026-08-01T08:00:00",
                "last_source": "historical backfill",
            },
            "1": {
                "count": 1,
                "last_time": "2026-08-02 04:19:25",
                "last_time_iso": "2026-08-02T04:19:25+08:00",
                "last_source": "automatic rebuild",
            },
        }
    }

    summary = main._summarize_rebuild_stats(state)

    assert summary["last"]["server"] == "1"
    assert summary["last"]["time"] == "2026-08-02 04:19:25"
