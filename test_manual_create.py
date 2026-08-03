import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
import requests


SOURCE = Path(__file__).with_name("production-main.py")
SPEC = importlib.util.spec_from_file_location("production_main_manual_create", SOURCE)
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
            "snapshot_id_map": {
                "1": "412977893",
                "2": "412977893",
                "3": "412977893",
            },
            "location_fallbacks": ["nbg1", "fsn1", "hel1"],
        }
    }


def test_missing_projection_excludes_existing_server_names():
    live_servers = [
        {
            "id": 101,
            "name": "1",
            "status": "running",
            "server_type": {"name": "cx33"},
            "location": {"name": "nbg1"},
            "public_net": {"ipv4": {"ip": "192.0.2.1"}},
        },
        {
            "id": 103,
            "name": "3",
            "status": "running",
            "server_type": {"name": "cx33"},
            "location": {"name": "fsn1"},
            "public_net": {"ipv4": {"ip": "192.0.2.3"}},
        },
    ]

    missing = main._configured_missing_servers(manual_config(), live_servers)

    assert missing == [
        {
            "name": "2",
            "missing": True,
            "status": "missing",
            "server_type": "cx33",
            "location": "nbg1 → fsn1 → hel1",
            "snapshot_id": "412977893",
        }
    ]


def test_missing_projection_is_empty_when_manual_creation_disabled():
    assert main._configured_missing_servers(manual_config(enabled=False), []) == []


def test_manual_create_options_are_derived_from_configured_allowlists():
    assert main._manual_create_options(manual_config()) == {
        "enabled": True,
        "server_types": ["cx33", "cx43", "cx53"],
        "locations": ["nbg1", "fsn1", "hel1"],
        "default_server_type": "cx33",
        "default_location": "nbg1",
    }


def hetzner_error(status, code, message):
    response = requests.Response()
    response.status_code = status
    response.url = "https://api.hetzner.cloud/v1/servers"
    response._content = (
        f'{{"error":{{"code":"{code}","message":"{message}"}}}}'.encode()
    )
    return requests.HTTPError(response=response)


class FakeCreateClient(main.HetznerClient):
    def __init__(self, create_results, live_servers=None, images=None, server_types=None, ssh_keys=None):
        super().__init__("unused")
        self.create_results = list(create_results)
        self.live_servers = list(live_servers or [])
        self.create_payloads = []
        self.delete_calls = []
        self.images = images
        self.server_types = server_types
        self.ssh_keys = ssh_keys

    def get_servers(self):
        return self.live_servers

    def delete_server(self, server_id):
        self.delete_calls.append(server_id)
        raise AssertionError("manual create must never delete a server")

    def get_images(self, image_type):
        assert self.images is not None
        return list(self.images.get(image_type, []))

    def get_server_types(self):
        assert self.server_types is not None
        return list(self.server_types)

    def get_ssh_keys(self):
        assert self.ssh_keys is not None
        return list(self.ssh_keys)

    def _request(self, method, endpoint, **kwargs):
        assert method == "POST"
        assert endpoint == "servers"
        self.create_payloads.append(kwargs["json"])
        result = self.create_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return {
            "server": {
                "id": result,
                "name": kwargs["json"]["name"],
                "public_net": {"ipv4": {"ip": "192.0.2.44"}},
            }
        }


def test_manual_create_uses_snapshot_type_and_first_location_without_delete():
    client = FakeCreateClient([9001])

    result = client.create_missing_server("2", manual_config())

    assert result["success"] is True
    assert result["new_server_id"] == 9001
    assert result["new_location"] == "nbg1"
    assert client.create_payloads == [
        {
            "name": "2",
            "server_type": "cx33",
            "image": "412977893",
            "location": "nbg1",
            "start_after_create": True,
        }
    ]
    assert client.delete_calls == []


def test_manual_create_falls_back_only_after_capacity_error():
    client = FakeCreateClient(
        [hetzner_error(412, "resource_unavailable", "capacity"), 9002]
    )

    result = client.create_missing_server("2", manual_config())

    assert result["success"] is True
    assert result["new_location"] == "fsn1"
    assert [payload["location"] for payload in client.create_payloads] == [
        "nbg1",
        "fsn1",
    ]
    assert client.delete_calls == []


def test_manual_create_all_capacity_failures_return_without_retry_schedule():
    client = FakeCreateClient(
        [
            hetzner_error(412, "resource_unavailable", "capacity"),
            hetzner_error(412, "resource_unavailable", "capacity"),
            hetzner_error(412, "resource_unavailable", "capacity"),
        ]
    )

    result = client.create_missing_server("2", manual_config())

    assert result["success"] is False
    assert result["error_code"] == "resource_unavailable"
    assert result["attempted_locations"] == ["nbg1", "fsn1", "hel1"]
    assert "不自动重试" in result["error"]
    assert client.delete_calls == []


def test_manual_create_non_capacity_error_stops_immediately():
    client = FakeCreateClient(
        [hetzner_error(403, "forbidden", "insufficient permissions"), 9003]
    )

    result = client.create_missing_server("2", manual_config())

    assert result["success"] is False
    assert result["error_code"] == "forbidden"
    assert [payload["location"] for payload in client.create_payloads] == ["nbg1"]
    assert client.delete_calls == []


def test_manual_create_rejects_name_without_snapshot_mapping():
    client = FakeCreateClient([9004])

    result = client.create_missing_server("9", manual_config())

    assert result["success"] is False
    assert result["error_code"] == "name_not_allowed"
    assert client.create_payloads == []
    assert client.delete_calls == []


def test_manual_create_rejects_existing_server_name():
    live = [
        {
            "id": 102,
            "name": "2",
            "status": "running",
            "server_type": {"name": "cx33"},
            "location": {"name": "nbg1"},
            "public_net": {"ipv4": {"ip": "192.0.2.2"}},
        }
    ]
    client = FakeCreateClient([9005], live_servers=live)

    result = client.create_missing_server("2", manual_config())

    assert result["success"] is False
    assert result["error_code"] == "already_exists"
    assert client.create_payloads == []
    assert client.delete_calls == []


def test_manual_create_accepts_allowed_type_and_moves_preferred_location_first():
    client = FakeCreateClient(
        [hetzner_error(412, "resource_unavailable", "capacity"), 9010]
    )

    result = client.create_missing_server(
        "2",
        manual_config(),
        server_type="cx43",
        preferred_location="fsn1",
        allow_fallback=True,
    )

    assert result["success"] is True
    assert result["new_location"] == "nbg1"
    assert [
        (payload["server_type"], payload["location"])
        for payload in client.create_payloads
    ] == [("cx43", "fsn1"), ("cx43", "nbg1")]
    assert client.delete_calls == []


def test_manual_create_rejects_type_outside_allowlist():
    client = FakeCreateClient([9011])

    result = client.create_missing_server(
        "2",
        manual_config(),
        server_type="ccx63",
        preferred_location="nbg1",
        allow_fallback=True,
    )

    assert result["success"] is False
    assert result["error_code"] == "server_type_not_allowed"
    assert client.create_payloads == []


def test_manual_create_rejects_location_outside_allowlist():
    client = FakeCreateClient([9012])

    result = client.create_missing_server(
        "2",
        manual_config(),
        server_type="cx33",
        preferred_location="ash",
        allow_fallback=True,
    )

    assert result["success"] is False
    assert result["error_code"] == "location_not_allowed"
    assert client.create_payloads == []


def test_manual_create_without_fallback_attempts_only_preferred_location():
    client = FakeCreateClient(
        [hetzner_error(412, "resource_unavailable", "capacity"), 9013]
    )

    result = client.create_missing_server(
        "2",
        manual_config(),
        server_type="cx53",
        preferred_location="hel1",
        allow_fallback=False,
    )

    assert result["success"] is False
    assert result["attempted_locations"] == ["hel1"]
    assert [
        payload["location"] for payload in client.create_payloads
    ] == ["hel1"]


def dynamic_resources():
    return {
        "images": {
            "snapshot": [
                {
                    "id": 412977893,
                    "type": "snapshot",
                    "status": "available",
                    "architecture": "x86",
                    "disk_size": 80,
                },
                {
                    "id": 412977894,
                    "type": "snapshot",
                    "status": "available",
                    "architecture": "x86",
                    "disk_size": 120,
                },
            ],
            "system": [
                {
                    "id": 1001,
                    "type": "system",
                    "status": "available",
                    "name": "debian-12",
                    "architecture": "x86",
                    "disk_size": 5,
                },
                {
                    "id": 1002,
                    "type": "system",
                    "status": "available",
                    "name": "deprecated-os",
                    "deprecated": "2026-01-01T00:00:00+00:00",
                    "architecture": "x86",
                    "disk_size": 5,
                },
                {
                    "id": 1003,
                    "type": "system",
                    "status": "available",
                    "name": "arm-os",
                    "architecture": "arm",
                    "disk_size": 5,
                },
            ],
        },
        "server_types": [
            {"name": "cx23", "architecture": "x86", "disk": 40},
            {"name": "cx33", "architecture": "x86", "disk": 80},
            {"name": "cx43", "architecture": "x86", "disk": 160},
            {"name": "cax11", "architecture": "arm", "disk": 40},
            {"name": "cx-old", "architecture": "x86", "disk": 80, "deprecated": True},
        ],
        "ssh_keys": [{"id": 77, "name": "shoo"}, {"id": 88, "name": "backup"}],
    }


def dynamic_client(create_results=None, **overrides):
    resources = dynamic_resources()
    resources.update(overrides)
    return FakeCreateClient(create_results or [9100], **resources)


def test_manual_create_from_official_image_uses_selected_resources():
    client = dynamic_client()

    result = client.create_missing_server(
        "2",
        manual_config(),
        source="system",
        image_id=1001,
        server_type="cx23",
        preferred_location="nbg1",
        ssh_key_ids=[77],
    )

    assert result["success"] is True
    assert result["source"] == "system"
    assert result["image_id"] == 1001
    assert client.create_payloads == [
        {
            "name": "2",
            "server_type": "cx23",
            "image": 1001,
            "location": "nbg1",
            "start_after_create": True,
            "ssh_keys": [77],
        }
    ]
    assert "root_password" not in result


@pytest.mark.parametrize(
    ("options", "error_code"),
    [
        ({"source": "backup", "image_id": 1001, "server_type": "cx23", "ssh_key_ids": [77]}, "invalid_source"),
        ({"source": "system", "image_id": 9999, "server_type": "cx23", "ssh_key_ids": [77]}, "image_not_available"),
        ({"source": "system", "image_id": 1002, "server_type": "cx23", "ssh_key_ids": [77]}, "image_not_available"),
        ({"source": "system", "image_id": 1003, "server_type": "cx23", "ssh_key_ids": [77]}, "architecture_mismatch"),
        ({"source": "snapshot", "image_id": 412977894, "server_type": "cx33"}, "snapshot_disk_too_large"),
        ({"source": "system", "image_id": 1001, "server_type": "cx-old", "ssh_key_ids": [77]}, "server_type_not_available"),
        ({"source": "system", "image_id": 1001, "server_type": "cx23", "ssh_key_ids": [999]}, "ssh_key_not_available"),
        ({"source": "system", "image_id": 1001, "server_type": "cx23", "ssh_key_ids": []}, "ssh_key_required"),
    ],
)
def test_manual_create_revalidates_dynamic_selection(options, error_code):
    client = dynamic_client()

    result = client.create_missing_server("2", manual_config(), **options)

    assert result["success"] is False
    assert result["error_code"] == error_code
    assert client.create_payloads == []


def test_explicit_snapshot_mode_uses_numeric_id_and_no_ssh_keys():
    client = dynamic_client()

    result = client.create_missing_server(
        "2",
        manual_config(),
        source="snapshot",
        image_id=412977893,
        server_type="cx33",
        preferred_location="fsn1",
        ssh_key_ids=[77],
    )

    assert result["success"] is True
    assert client.create_payloads[0]["image"] == 412977893
    assert "ssh_keys" not in client.create_payloads[0]


def workflow_config():
    config = manual_config()
    config["telegram"] = {
        "enabled": True,
        "bot_token": "bot-token",
        "chat_id": "chat-id",
    }
    config["cloudflare"] = {
        "api_token": "cf-token",
        "zone_id": "zone-id",
        "update_retries": 3,
        "update_retry_delay": 0,
        "record_map": {"2": "2.hanxu.me"},
    }
    return config


class FakeWorkflowClient:
    def __init__(self, create_result):
        self.create_result = create_result
        self.create_calls = []
        self.create_options = []
        self.dns_calls = []

    def create_missing_server(self, name, config, **options):
        self.create_calls.append(name)
        self.create_options.append(options)
        return dict(self.create_result)

    def update_cloudflare_a_record(
        self,
        api_token,
        zone_id,
        record_name,
        ip,
        attempts=3,
        delay_seconds=3,
    ):
        self.dns_calls.append(
            {
                "api_token": api_token,
                "zone_id": zone_id,
                "record_name": record_name,
                "ip": ip,
                "attempts": attempts,
                "delay_seconds": delay_seconds,
            }
        )
        return {"success": True, "changed": True, "new_ip": ip}


def test_manual_create_workflow_updates_name_based_dns_and_notifies_once():
    client = FakeWorkflowClient(
        {
            "success": True,
            "new_server_id": 9002,
            "new_ip": "192.0.2.44",
            "new_location": "fsn1",
            "attempted_locations": ["nbg1", "fsn1"],
        }
    )

    with patch.object(main, "_send_telegram_markdown", return_value=True) as send:
        result = main._perform_manual_create(
            "2", workflow_config(), client
        )

    assert result["success"] is True
    assert result["dns"]["success"] is True
    assert client.create_calls == ["2"]
    assert client.dns_calls == [
        {
            "api_token": "cf-token",
            "zone_id": "zone-id",
            "record_name": "2.hanxu.me",
            "ip": "192.0.2.44",
            "attempts": 3,
            "delay_seconds": 0.0,
        }
    ]
    assert send.call_count == 1
    assert "手动创建成功" in send.call_args.args[2]


def test_manual_create_workflow_failure_notifies_once_without_dns_or_retry():
    client = FakeWorkflowClient(
        {
            "success": False,
            "error": "cx33 创建失败：nbg1、fsn1、hel1 均无可用容量；不自动重试",
            "error_code": "resource_unavailable",
            "attempted_locations": ["nbg1", "fsn1", "hel1"],
        }
    )

    with patch.object(main, "_send_telegram_markdown", return_value=True) as send:
        result = main._perform_manual_create(
            "2", workflow_config(), client
        )

    assert result["success"] is False
    assert client.create_calls == ["2"]
    assert client.dns_calls == []
    assert send.call_count == 1
    assert "手动创建失败" in send.call_args.args[2]
    assert "不自动重试" in send.call_args.args[2]


def test_manual_create_workflow_forwards_selected_creation_options():
    client = FakeWorkflowClient(
        {
            "success": False,
            "error": "capacity",
            "error_code": "resource_unavailable",
        }
    )

    with patch.object(main, "_send_telegram_markdown", return_value=True):
        main._perform_manual_create(
            "2",
            workflow_config(),
            client,
            server_type="cx43",
            preferred_location="hel1",
            allow_fallback=False,
        )

    assert client.create_options == [
        {
            "server_type": "cx43",
            "preferred_location": "hel1",
            "allow_fallback": False,
        }
    ]
