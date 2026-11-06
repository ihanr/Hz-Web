import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests


SOURCE = Path(__file__).with_name("production-main.py")
spec = importlib.util.spec_from_file_location("production_main", SOURCE)
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)


def api_error(status, code, message):
    response = requests.Response()
    response.status_code = status
    response.url = "https://api.hetzner.cloud/v1/servers"
    response._content = (
        f'{{"error":{{"code":"{code}","message":"{message}"}}}}'.encode()
    )
    return requests.HTTPError(response=response)


class FakeClient(main.HetznerClient):
    def __init__(self, create_results, release_result=None):
        super().__init__("unused")
        self.create_results = list(create_results)
        self.create_locations = []
        self.deleted = []
        self.events = []
        self.wait_calls = []
        self.release_result = release_result or {
            "success": True,
            "waited_seconds": 0,
        }

    def get_server(self, server_id):
        return {
            "id": server_id,
            "name": "4",
            "server_type": {"name": "cx33"},
            "location": {"name": "nbg1"},
            "public_net": {
                "ipv4": {"id": 7001, "ip": "192.0.2.40"},
                "ipv6": {"id": 7002, "ip": "2001:db8::40"},
            },
        }

    def delete_server(self, server_id):
        self.deleted.append(server_id)
        self.events.append(("delete", server_id))
        return True

    def wait_for_rebuild_resources(
        self, server_id, primary_ip_ids, timeout_seconds, poll_seconds
    ):
        call = (
            server_id,
            list(primary_ip_ids),
            timeout_seconds,
            poll_seconds,
        )
        self.wait_calls.append(call)
        self.events.append(("wait", call))
        return dict(self.release_result)

    def _request(self, method, endpoint, **kwargs):
        self.events.append(("create", kwargs["json"]["location"]))
        self.create_locations.append(kwargs["json"]["location"])
        result = self.create_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return {
            "server": {
                "id": result,
                "name": "4",
                "public_net": {"ipv4": {"ip": "192.0.2.44"}},
            }
        }


class ReleaseProbeClient(main.HetznerClient):
    def __init__(self, states):
        super().__init__("unused")
        self.states = {key: list(values) for key, values in states.items()}

    def _resource_exists(self, endpoint):
        values = self.states[endpoint]
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return value


class LocationNormalizationTests(unittest.TestCase):
    def test_normalizes_configured_locations(self):
        config = {
            "rebuild": {
                "location_fallbacks": ["nbg1", "", "fsn1", "nbg1", "hel1"]
            }
        }
        self.assertEqual(
            main._rebuild_locations(config, "nbg1"),
            ["nbg1", "fsn1", "hel1"],
        )

    def test_missing_config_uses_old_location(self):
        self.assertEqual(main._rebuild_locations({}, "nbg1"), ["nbg1"])


class RebuildFallbackTests(unittest.TestCase):
    def config(self):
        return {
            "rebuild": {
                "snapshot_id_map": {"4": 412977893},
                "location_fallbacks": ["nbg1", "fsn1", "hel1"],
            }
        }

    def run_rebuild(self, results, release_result=None, config=None):
        client = FakeClient(results, release_result=release_result)
        with patch.object(main.time, "sleep"):
            result = client.rebuild_server(444, config or self.config())
        return client, result

    def test_rebuild_waits_for_old_server_primary_ips_before_create(self):
        client, result = self.run_rebuild([9001])
        self.assertTrue(result["success"])
        self.assertEqual(
            client.wait_calls,
            [(444, [7001, 7002], 120.0, 3.0)],
        )
        self.assertLess(
            next(i for i, event in enumerate(client.events) if event[0] == "wait"),
            next(i for i, event in enumerate(client.events) if event[0] == "create"),
        )

    def test_release_timeout_stops_before_create(self):
        timeout = {
            "success": False,
            "error": "Primary IP release timeout after 120 seconds",
            "error_code": "primary_ip_release_timeout",
            "remaining_resources": ["primary_ip:7001"],
        }
        client, result = self.run_rebuild([], release_result=timeout)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "primary_ip_release_timeout")
        self.assertEqual(client.create_locations, [])

    def test_release_wait_settings_can_be_overridden(self):
        config = self.config()
        config["rebuild"]["primary_ip_release_timeout_seconds"] = 30
        config["rebuild"]["primary_ip_release_poll_seconds"] = 2
        client, result = self.run_rebuild([9001], config=config)
        self.assertTrue(result["success"])
        self.assertEqual(
            client.wait_calls,
            [(444, [7001, 7002], 30.0, 2.0)],
        )

    def test_first_location_success_stops_fallback(self):
        client, result = self.run_rebuild([9001])
        self.assertEqual(client.deleted, [444])
        self.assertEqual(client.create_locations, ["nbg1"])
        self.assertTrue(result["success"])
        self.assertEqual(result["new_location"], "nbg1")

    def test_412_falls_back_to_second_location(self):
        client, result = self.run_rebuild(
            [api_error(412, "resource_unavailable", "capacity"), 9002]
        )
        self.assertEqual(client.create_locations, ["nbg1", "fsn1"])
        self.assertTrue(result["success"])
        self.assertEqual(result["new_location"], "fsn1")

    def test_two_412_errors_fall_back_to_third_location(self):
        client, result = self.run_rebuild(
            [
                api_error(412, "resource_unavailable", "capacity"),
                api_error(412, "resource_unavailable", "capacity"),
                9003,
            ]
        )
        self.assertEqual(client.create_locations, ["nbg1", "fsn1", "hel1"])
        self.assertTrue(result["success"])
        self.assertEqual(result["new_location"], "hel1")

    def test_all_locations_unavailable_returns_one_failure(self):
        client, result = self.run_rebuild(
            [
                api_error(412, "resource_unavailable", "capacity"),
                api_error(412, "resource_unavailable", "capacity"),
                api_error(412, "resource_unavailable", "capacity"),
            ]
        )
        self.assertEqual(client.create_locations, ["nbg1", "fsn1", "hel1"])
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "resource_unavailable")
        self.assertEqual(
            result["attempted_locations"], ["nbg1", "fsn1", "hel1"]
        )
        self.assertIn("未自动重试", result["error"])

    def test_non_capacity_error_stops_immediately(self):
        client, result = self.run_rebuild(
            [api_error(403, "forbidden", "insufficient permissions"), 9004]
        )
        self.assertEqual(client.create_locations, ["nbg1"])
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "forbidden")
        self.assertIn("insufficient permissions", result["error"])

    def test_single_location_succeeds_on_fifth_attempt(self):
        config = self.config()
        config["rebuild"]["location_fallbacks"] = ["nbg1"]
        client = FakeClient(
            [api_error(412, "resource_unavailable", "capacity")] * 4 + [9005]
        )
        with patch.object(main.time, "sleep") as sleep:
            result = client.rebuild_server(444, config)
        self.assertTrue(result["success"])
        self.assertEqual(client.deleted, [444])
        self.assertEqual(client.create_locations, ["nbg1"] * 5)
        self.assertEqual([call.args for call in sleep.call_args_list], [(60,)] * 4)

    def test_single_location_stops_after_five_capacity_failures(self):
        config = self.config()
        config["rebuild"]["location_fallbacks"] = ["nbg1"]
        client = FakeClient(
            [api_error(412, "resource_unavailable", "capacity")] * 5
        )
        with patch.object(main.time, "sleep") as sleep:
            result = client.rebuild_server(444, config)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "resource_unavailable")
        self.assertEqual(client.create_locations, ["nbg1"] * 5)
        self.assertEqual([call.args for call in sleep.call_args_list], [(60,)] * 4)

    def test_single_location_quota_error_stops_without_retry(self):
        config = self.config()
        config["rebuild"]["location_fallbacks"] = ["nbg1"]
        client = FakeClient(
            [api_error(403, "resource_limit_exceeded", "shared core limit exceeded")]
        )
        with patch.object(main.time, "sleep") as sleep:
            result = client.rebuild_server(444, config)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "resource_limit_exceeded")
        self.assertEqual(client.create_locations, ["nbg1"])
        sleep.assert_not_called()


class ResourceReleaseWaitTests(unittest.TestCase):
    def run_wait(self, states, timeout=120.0, poll=3.0):
        client = ReleaseProbeClient(states)
        clock = [0.0]

        def advance(seconds):
            clock[0] += seconds

        with (
            patch.object(main.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(main.time, "sleep", side_effect=advance) as sleep,
        ):
            result = client.wait_for_rebuild_resources(
                444,
                [7001, 7002],
                timeout,
                poll,
            )
        return result, sleep

    def test_immediate_release_does_not_sleep(self):
        result, sleep = self.run_wait(
            {
                "servers/444": [False],
                "primary_ips/7001": [False],
                "primary_ips/7002": [False],
            }
        )
        self.assertTrue(result["success"])
        sleep.assert_not_called()

    def test_delayed_release_polls_before_success(self):
        result, sleep = self.run_wait(
            {
                "servers/444": [True, False],
                "primary_ips/7001": [True, False],
                "primary_ips/7002": [True, False],
            }
        )
        self.assertTrue(result["success"])
        sleep.assert_called_once_with(3.0)

    def test_timeout_reports_remaining_resources(self):
        result, sleep = self.run_wait(
            {
                "servers/444": [False],
                "primary_ips/7001": [True],
                "primary_ips/7002": [False],
            },
            timeout=6.0,
            poll=3.0,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "primary_ip_release_timeout")
        self.assertEqual(result["remaining_resources"], ["primary_ip:7001"])
        self.assertEqual(sleep.call_count, 2)

    def test_non_404_check_failure_stops_safely(self):
        result, sleep = self.run_wait(
            {
                "servers/444": [False],
                "primary_ips/7001": [
                    api_error(403, "forbidden", "insufficient permissions")
                ],
                "primary_ips/7002": [False],
            }
        )
        self.assertFalse(result["success"])
        self.assertEqual(
            result["error_code"],
            "primary_ip_release_check_failed",
        )
        self.assertEqual(result["resource"], "primary_ip:7001")
        sleep.assert_not_called()

    def test_resource_probe_treats_only_404_as_missing(self):
        client = main.HetznerClient("unused")
        with patch.object(
            client,
            "_request",
            side_effect=api_error(404, "not_found", "missing"),
        ):
            self.assertFalse(client._resource_exists("primary_ips/7001"))
        with patch.object(
            client,
            "_request",
            side_effect=api_error(403, "forbidden", "denied"),
        ):
            with self.assertRaises(requests.HTTPError):
                client._resource_exists("primary_ips/7001")


class RebuildNotificationTests(unittest.TestCase):
    def test_success_notification_contains_actual_location(self):
        config = {
            "telegram": {
                "enabled": True,
                "bot_token": "bot",
                "chat_id": "chat",
            },
            "cloudflare": {},
        }
        client = Mock()
        client.rebuild_server.return_value = {
            "success": True,
            "new_server_id": 9002,
            "new_ip": "192.0.2.44",
            "new_location": "fsn1",
            "attempted_locations": ["nbg1", "fsn1"],
        }
        with (
            patch.object(main, "_send_telegram_markdown", return_value=True) as send,
            patch.object(main, "_record_rebuild_event"),
            patch.object(main, "_save_yaml"),
        ):
            main._perform_rebuild(444, "4", config, "测试", client)
        messages = "\n".join(call.args[2] for call in send.call_args_list)
        self.assertIn("fsn1", messages)

    def test_release_timeout_uses_existing_failure_notification(self):
        config = {
            "telegram": {
                "enabled": True,
                "bot_token": "bot",
                "chat_id": "chat",
            },
            "cloudflare": {},
        }
        client = Mock()
        client.rebuild_server.return_value = {
            "success": False,
            "error": "Primary IP release timeout after 120 seconds",
            "error_code": "primary_ip_release_timeout",
        }
        with patch.object(
            main,
            "_send_telegram_markdown",
            return_value=True,
        ) as send:
            result = main._perform_rebuild(444, "4", config, "test", client)
        self.assertFalse(result["success"])
        messages = "\n".join(call.args[2] for call in send.call_args_list)
        self.assertIn("Primary IP release timeout", messages)


if __name__ == "__main__":
    unittest.main()
