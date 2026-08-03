import base64
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


SOURCE = Path(__file__).with_name("production-main.py")
SPEC = importlib.util.spec_from_file_location("production_main_manual_api", SOURCE)
main = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(main)


def manual_config():
    return {
        "hetzner": {"api_token": "test-token"},
        "rebuild": {
            "manual_create": {
                "enabled": True,
                "server_type": "cx33",
                "server_types": ["cx33", "cx43", "cx53"],
            },
            "snapshot_id_map": {"2": "412977893"},
            "location_fallbacks": ["nbg1", "fsn1", "hel1"],
        },
    }


class FakeCatalogClient:
    def get_servers(self):
        return []

    def get_images(self, image_type):
        if image_type == "snapshot":
            return [{
                "id": 412977893,
                "type": "snapshot",
                "status": "available",
                "description": "seedbox",
                "architecture": "x86",
                "disk_size": 40,
            }]
        return [{
            "id": 1001,
            "type": "system",
            "status": "available",
            "name": "debian-12",
            "architecture": "x86",
            "disk_size": 5,
        }]

    def get_server_types(self):
        return [{
            "name": "cx23",
            "architecture": "x86",
            "cores": 2,
            "memory": 4,
            "disk": 40,
        }]

    def get_ssh_keys(self):
        return [{"id": 77, "name": "shoo", "fingerprint": "SHA256:test"}]


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(main, "_load_json", lambda path: {"username": "admin", "password": "secret"})
    monkeypatch.setattr(main, "_load_yaml", lambda path: manual_config())
    fake = FakeCatalogClient()
    monkeypatch.setattr(main, "HetznerClient", lambda token: fake)
    client = TestClient(main.app)
    token = base64.b64encode(b"admin:secret").decode()
    return client, {"Authorization": f"Basic {token}"}, fake


def test_create_catalog_requires_authentication(api):
    client, _, _ = api
    response = client.get("/api/create_catalog?name=2")
    assert response.status_code == 401


def test_create_catalog_returns_dynamic_resources(api):
    client, headers, _ = api
    response = client.get("/api/create_catalog?name=2", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "2"
    assert body["default_snapshot_id"] == 412977893
    assert body["system_images"][0]["name"] == "debian-12"
    assert body["server_types"][0]["name"] == "cx23"


def test_create_catalog_maps_target_errors(api, monkeypatch):
    client, headers, _ = api
    monkeypatch.setattr(
        main,
        "_build_manual_create_catalog",
        lambda *args: (_ for _ in ()).throw(main.ManualCreateCatalogError("already_exists", "exists")),
    )
    response = client.get("/api/create_catalog?name=2", headers=headers)
    assert response.status_code == 409
    assert response.json()["error_code"] == "already_exists"


def test_create_endpoint_forwards_dynamic_selection(api, monkeypatch):
    client, headers, _ = api
    captured = {}

    def fake_perform(name, config, hetzner, **options):
        captured.update({"name": name, **options})
        return {"success": True, "new_server_id": 9001, "dns": None}

    monkeypatch.setattr(main, "_perform_manual_create", fake_perform)
    response = client.post(
        "/api/create_missing",
        headers=headers,
        json={
            "name": "2",
            "source": "system",
            "image_id": 1001,
            "server_type": "cx23",
            "preferred_location": "nbg1",
            "allow_fallback": True,
            "ssh_key_ids": [77],
        },
    )

    assert response.status_code == 200
    assert captured == {
        "name": "2",
        "source": "system",
        "image_id": 1001,
        "server_type": "cx23",
        "preferred_location": "nbg1",
        "allow_fallback": True,
        "ssh_key_ids": [77],
    }


@pytest.mark.parametrize(
    ("changes", "error_code"),
    [
        ({"source": "backup"}, "invalid_source"),
        ({"image_id": "abc"}, "invalid_image_id"),
        ({"ssh_key_ids": "77"}, "invalid_ssh_key_ids"),
        ({"ssh_key_ids": ["abc"]}, "invalid_ssh_key_id"),
    ],
)
def test_create_endpoint_rejects_malformed_dynamic_fields(api, monkeypatch, changes, error_code):
    client, headers, _ = api
    monkeypatch.setattr(
        main,
        "_perform_manual_create",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("workflow must not run")),
    )
    payload = {
        "name": "2",
        "source": "system",
        "image_id": 1001,
        "server_type": "cx23",
        "preferred_location": "nbg1",
        "allow_fallback": True,
        "ssh_key_ids": [77],
    }
    payload.update(changes)

    response = client.post("/api/create_missing", headers=headers, json=payload)
    assert response.status_code == 400
    assert response.json()["error_code"] == error_code

