import importlib.util
from pathlib import Path

import pytest


SOURCE = Path(__file__).with_name("production-main.py")
SPEC = importlib.util.spec_from_file_location("production_main_manual_catalog", SOURCE)
main = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(main)


def manual_config(enabled=True):
    return {
        "rebuild": {
            "manual_create": {
                "enabled": enabled,
                "server_type": "cx33",
                "server_types": ["cx33", "cx43", "cx53"],
            },
            "snapshot_id_map": {"1": "412977893", "2": "412977893"},
            "location_fallbacks": ["nbg1", "fsn1", "hel1"],
        }
    }


class FakeCatalogClient:
    def __init__(self, servers=None):
        self.servers = list(servers or [])

    def get_servers(self):
        return self.servers

    def get_images(self, image_type):
        images = {
            "snapshot": [
                {
                    "id": 412977893,
                    "type": "snapshot",
                    "status": "available",
                    "description": "seedbox-main",
                    "created": "2026-08-04T02:00:00+00:00",
                    "architecture": "x86",
                    "disk_size": 40,
                },
                {
                    "id": 412600001,
                    "type": "snapshot",
                    "status": "available",
                    "description": "seedbox-old",
                    "created": "2026-08-01T02:00:00+00:00",
                    "architecture": "x86",
                    "disk_size": 80,
                },
                {
                    "id": 400000000,
                    "type": "snapshot",
                    "status": "creating",
                    "description": "unfinished",
                    "architecture": "x86",
                    "disk_size": 40,
                },
            ],
            "system": [
                {
                    "id": 1001,
                    "type": "system",
                    "status": "available",
                    "name": "debian-12",
                    "description": "Debian 12",
                    "architecture": "x86",
                    "disk_size": 5,
                },
                {
                    "id": 1002,
                    "type": "system",
                    "status": "available",
                    "name": "ubuntu-24.04",
                    "description": "Ubuntu 24.04",
                    "architecture": "x86",
                    "disk_size": 5,
                },
                {
                    "id": 1003,
                    "type": "system",
                    "status": "available",
                    "name": "arm-system",
                    "architecture": "arm",
                    "disk_size": 5,
                },
                {
                    "id": 1004,
                    "type": "system",
                    "status": "available",
                    "name": "old-system",
                    "deprecated": "2026-01-01T00:00:00+00:00",
                    "architecture": "x86",
                    "disk_size": 5,
                },
            ],
        }
        return images[image_type]

    def get_server_types(self):
        return [
            {"name": "cx23", "architecture": "x86", "cores": 2, "memory": 4, "disk": 40},
            {"name": "cx33", "architecture": "x86", "cores": 4, "memory": 8, "disk": 80},
            {"name": "cx43", "architecture": "x86", "cores": 8, "memory": 16, "disk": 160},
            {"name": "cx53", "architecture": "x86", "cores": 16, "memory": 32, "disk": 320},
            {"name": "cax11", "architecture": "arm", "cores": 2, "memory": 4, "disk": 40},
            {"name": "cx-old", "architecture": "x86", "deprecated": True, "disk": 40},
        ]

    def get_ssh_keys(self):
        return [{"id": 77, "name": "shoo", "fingerprint": "SHA256:test"}]


def test_client_catalog_methods_use_paginated_resources():
    client = main.HetznerClient("unused")
    calls = []

    def fake_paginated(endpoint, result_key, params=None):
        calls.append((endpoint, result_key, params))
        return [{"id": 1}]

    client._request_paginated = fake_paginated

    assert client.get_images("snapshot") == [{"id": 1}]
    assert client.get_images("system") == [{"id": 1}]
    assert client.get_server_types() == [{"id": 1}]
    assert client.get_ssh_keys() == [{"id": 1}]
    assert calls == [
        ("images", "images", {"type": "snapshot"}),
        ("images", "images", {"type": "system"}),
        ("server_types", "server_types", None),
        ("ssh_keys", "ssh_keys", None),
    ]


def test_get_images_rejects_unsupported_types():
    with pytest.raises(ValueError, match="unsupported_image_type"):
        main.HetznerClient("unused").get_images("backup")


def test_catalog_normalizes_available_project_resources():
    catalog = main._build_manual_create_catalog("2", manual_config(), FakeCatalogClient())

    assert catalog["name"] == "2"
    assert catalog["default_source"] == "snapshot"
    assert catalog["default_snapshot_id"] == 412977893
    assert [row["id"] for row in catalog["snapshots"]] == [412977893, 412600001]
    assert [row["name"] for row in catalog["system_images"]] == ["debian-12", "ubuntu-24.04"]
    assert [row["name"] for row in catalog["server_types"]] == ["cx23", "cx33", "cx43", "cx53"]
    assert catalog["locations"] == ["nbg1", "fsn1", "hel1"]
    assert catalog["ssh_keys"] == [
        {"id": 77, "name": "shoo", "fingerprint": "SHA256:test"}
    ]
    assert catalog["snapshots"][0]["architecture"] == "x86"
    assert catalog["snapshots"][0]["disk_size"] == 40
    assert catalog["server_types"][0] == {
        "name": "cx23",
        "architecture": "x86",
        "cores": 2,
        "memory": 4.0,
        "disk": 40,
    }


@pytest.mark.parametrize(
    ("name", "enabled", "servers", "code"),
    [
        ("2", False, [], "manual_create_disabled"),
        ("9", True, [], "name_not_allowed"),
        ("2", True, [{"name": "2"}], "already_exists"),
    ],
)
def test_catalog_rejects_invalid_targets(name, enabled, servers, code):
    with pytest.raises(main.ManualCreateCatalogError) as exc_info:
        main._build_manual_create_catalog(
            name, manual_config(enabled=enabled), FakeCatalogClient(servers)
        )

    assert exc_info.value.code == code

