import importlib.util
import json
from pathlib import Path

import requests


SOURCE = Path(__file__).with_name("production-main.py")
SPEC = importlib.util.spec_from_file_location(
    "production_main_qb_login_compatibility",
    SOURCE,
)
main = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(main)


def response(status, body):
    result = requests.Response()
    result.status_code = status
    result.url = "http://qb.example/api/v2/auth/login"
    result.encoding = "utf-8"
    result._content = body.encode("utf-8")
    return result


def json_response(status, payload):
    result = requests.Response()
    result.status_code = status
    result.url = "http://qb.example/api/v2/sync/maindata"
    result.encoding = "utf-8"
    result.headers["Content-Type"] = "application/json"
    result._content = json.dumps(payload).encode("utf-8")
    return result


def valid_sync_response():
    return json_response(
        200,
        {
            "server_state": {
                "alltime_ul": 123,
                "alltime_dl": 456,
                "up_info_data": 12,
                "dl_info_data": 34,
                "up_info_speed": 5,
                "dl_info_speed": 6,
                "connection_status": "connected",
            }
        },
    )


def instance():
    return {
        "name": "1",
        "url": "http://qb.example",
        "username": "user",
        "password": "password",
        "timeout_seconds": 1,
        "login_retries": 1,
        "login_retry_delay": 0,
        "verify_ssl": False,
    }


class FakeSession:
    def __init__(self, login_response, sync_response):
        self.login_response = login_response
        self.sync_response = sync_response

    def post(self, url, **kwargs):
        return self.login_response

    def get(self, url, **kwargs):
        return self.sync_response


def test_qb_52_login_204_collects_sync_totals(monkeypatch):
    session = FakeSession(response(204, ""), valid_sync_response())
    monkeypatch.setattr(main.requests, "Session", lambda: session)

    result = main._fetch_qb_instance(instance(), "alltime")

    assert result["status"] == "ok"
    assert result["upload_bytes"] == 123
    assert result["download_bytes"] == 456
    assert result["connection_status"] == "connected"


def test_legacy_200_ok_remains_successful(monkeypatch):
    session = FakeSession(response(200, "Ok."), valid_sync_response())
    monkeypatch.setattr(main.requests, "Session", lambda: session)

    result = main._fetch_qb_instance(instance(), "alltime")

    assert result["status"] == "ok"
    assert result["upload_bytes"] == 123


def test_unexpected_200_body_remains_login_failure(monkeypatch):
    session = FakeSession(response(200, "Fails."), valid_sync_response())
    monkeypatch.setattr(main.requests, "Session", lambda: session)

    result = main._fetch_qb_instance(instance(), "alltime")

    assert result["status"] == "error"
    assert result["error"].startswith("login_failed:")
