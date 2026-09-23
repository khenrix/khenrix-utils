#!/usr/bin/env python3
"""Fully offline security and transport tests for the interactive relay."""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import pathlib
import plistlib
import socket
import subprocess
import tempfile
import threading
import unittest
from dataclasses import dataclass
from unittest import mock

import maka_openai_relay as relay
import install_maka_openai_relay as installer
import maka_auth_mode as selector
from install_maka_openai_relay import (
    build_launch_agent,
    default_paths,
    ensure_private_token,
    legacy_service_paths,
)


CALLER_TOKEN = "A" * 64
DUMMY_PROVIDER_KEY = "dummy-provider-key-for-offline-test"
KEYCHAIN_ACCOUNT = "cli|offline-test-account"


def valid_body(*, tools: bool = False, effort: str = "xhigh") -> bytes:
    document: dict[str, object] = {
        "model": relay.MODEL_ID,
        "input": [{"role": "user", "content": "offline fixture"}],
        "parallel_tool_calls": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": effort, "summary": "auto"},
        "prompt_cache_key": "maka:offline-session",
        "stream": True,
    }
    if tools:
        document["tools"] = [
            {
                "type": "function",
                "name": "Read",
                "description": "offline fixture",
                "parameters": {"type": "object"},
            },
            {"type": "apply_patch"},
        ]
        document["tool_choice"] = "auto"
    return json.dumps(document, separators=(",", ":")).encode()


class FakeResponse:
    status = 200

    def __init__(self, body: bytes = b"data: offline\n\n") -> None:
        midpoint = max(1, len(body) // 2)
        self._chunks = [body[:midpoint], body[midpoint:]]
        self._headers = {
            "content-type": "text/event-stream",
            "x-request-id": "offline-request-id",
            "set-cookie": "must-not-be-forwarded=true",
        }

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name.lower(), default)

    def read(self, amount: int | None = None) -> bytes:
        raise AssertionError("stream relay must use read1, not buffering read")

    def read1(self, amount: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


@dataclass
class FakeExchange:
    response: FakeResponse
    closed: bool = False

    def close(self) -> None:
        self.closed = True


class RecordingForwarder:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str]] = []
        self.exchanges: list[FakeExchange] = []

    def forward(self, body: bytes, provider_key: str) -> FakeExchange:
        self.calls.append((body, provider_key))
        exchange = FakeExchange(FakeResponse())
        self.exchanges.append(exchange)
        return exchange


class RelayServerFixture:
    def __init__(self, telemetry_path: pathlib.Path | None = None) -> None:
        self.key_loads = 0
        self.forwarder = RecordingForwarder()

        def load_key() -> str:
            self.key_loads += 1
            return DUMMY_PROVIDER_KEY

        dependencies = relay.RelayDependencies(
            caller_token=CALLER_TOKEN,
            attestation="C" * 64,
            key_loader=load_key,
            forwarder=self.forwarder,
            telemetry_path=telemetry_path,
        )
        self.server = relay.create_server(relay.LOOPBACK_HOST, 0, dependencies)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        *,
        authorize: bool = True,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        request_headers = dict(headers or {})
        if authorize:
            request_headers["Authorization"] = f"Bearer {CALLER_TOKEN}"
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
            request_headers.setdefault("Content-Length", str(len(body)))
        connection = http.client.HTTPConnection(relay.LOOPBACK_HOST, self.port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            response_body = response.read()
            return response.status, dict(response.getheaders()), response_body
        finally:
            connection.close()


class RelayIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RelayServerFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_server_is_ipv4_loopback_only(self) -> None:
        self.assertEqual(self.fixture.server.server_address[0], relay.LOOPBACK_HOST)
        with self.assertRaises(relay.RelayConfigurationError):
            relay.create_server(
                "0.0.0.0",
                0,
                relay.RelayDependencies(
                    caller_token=CALLER_TOKEN,
                    attestation="C" * 64,
                    key_loader=lambda: DUMMY_PROVIDER_KEY,
                    forwarder=self.fixture.forwarder,
                ),
            )

    def test_models_is_local_and_requires_caller_auth(self) -> None:
        status, _, _ = self.fixture.request("GET", "/v1/models", authorize=False)
        self.assertEqual(status, 401)
        status, _, body = self.fixture.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([model["id"] for model in json.loads(body)["data"]],
                         list(relay.MODEL_IDS))
        self.assertEqual(self.fixture.key_loads, 0)

    def test_health_proves_attestation_without_disclosing_it(self) -> None:
        status, _, _ = self.fixture.request(
            "GET", relay.HEALTH_PATH, authorize=False
        )
        self.assertEqual(status, 400)
        challenge = "N" * 43
        status, _, body = self.fixture.request(
            "GET",
            relay.HEALTH_PATH,
            authorize=False,
            headers={relay.HEALTH_CHALLENGE_HEADER: challenge},
        )
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertNotIn("attestation", document)
        self.assertNotIn(("C" * 64).encode(), body)
        self.assertEqual(
            document["proof"], relay.health_challenge_proof("C" * 64, challenge)
        )
        self.assertEqual(self.fixture.key_loads, 0)

    def test_websocket_probe_is_disabled_without_loading_key(self) -> None:
        status, _, body = self.fixture.request(
            "GET",
            "/v1/responses",
            headers={"Upgrade": "websocket", "Connection": "Upgrade"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "websocket_disabled")
        self.assertEqual(self.fixture.key_loads, 0)

    def test_valid_stream_request_is_forwarded_after_validation(self) -> None:
        request_body = valid_body(tools=True)
        status, headers, body = self.fixture.request(
            "POST", "/v1/responses", request_body
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"data: offline\n\n")
        self.assertEqual(headers["content-type"], "text/event-stream")
        self.assertNotIn("set-cookie", {name.lower() for name in headers})
        self.assertEqual(self.fixture.key_loads, 1)
        self.assertEqual(len(self.fixture.forwarder.calls), 1)
        forwarded, key = self.fixture.forwarder.calls[0]
        self.assertEqual(key, DUMMY_PROVIDER_KEY)
        self.assertEqual(json.loads(forwarded)["service_tier"], "default")
        self.assertEqual(json.loads(forwarded)["model"], relay.MODEL_ID)
        self.assertTrue(self.fixture.forwarder.exchanges[0].closed)

    def test_model_default_medium_is_elevated_before_openai_forwarding(self) -> None:
        request_body = valid_body(effort="medium")
        status, _, _ = self.fixture.request("POST", "/v1/responses", request_body)
        self.assertEqual(status, 200)
        forwarded = json.loads(self.fixture.forwarder.calls[0][0])
        self.assertEqual(forwarded["reasoning"], {"effort": "xhigh", "summary": "auto"})
        self.assertEqual(self.fixture.key_loads, 1)

    def test_explicit_max_is_preserved_for_api_key_planning(self) -> None:
        request_body = valid_body(effort="max")
        status, _, _ = self.fixture.request("POST", "/v1/responses", request_body)
        self.assertEqual(status, 200)
        forwarded = json.loads(self.fixture.forwarder.calls[0][0])
        self.assertEqual(forwarded["reasoning"], {"effort": "max", "summary": "auto"})
        self.assertEqual(self.fixture.key_loads, 1)

    def test_legacy_model_is_still_accepted_and_inbound_tier_is_rejected(self) -> None:
        document = json.loads(valid_body())
        document["model"] = relay.LEGACY_MODEL_ID
        status, _, _ = self.fixture.request(
            "POST", "/v1/responses", json.dumps(document).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(self.fixture.forwarder.calls[0][0])["model"],
                         relay.LEGACY_MODEL_ID)
        document["service_tier"] = "priority"
        status, _, _ = self.fixture.request(
            "POST", "/v1/responses", json.dumps(document).encode()
        )
        self.assertEqual(status, 403)
        self.assertEqual(len(self.fixture.forwarder.calls), 1)

    def test_tier_receipt_contains_only_bounded_response_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = pathlib.Path(directory)
            parent.chmod(0o700)
            path = parent / "last-tier.json"
            self.fixture.close()
            self.fixture = RelayServerFixture(telemetry_path=path)
            event = {
                "type": "response.created",
                "response": {"model": relay.MODEL_ID, "service_tier": "default",
                             "output": [{"text": "private prompt must not persist"}]},
            }
            payload = ("data: " + json.dumps(event) + "\n\n").encode()
            self.fixture.forwarder.forward = lambda _body, _key: FakeExchange(FakeResponse(payload))
            status, _, body = self.fixture.request("POST", "/v1/responses", valid_body())
            self.assertEqual(status, 200)
            self.assertEqual(body, payload)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["requested_service_tier"], "default")
            self.assertEqual(saved["observed_service_tier"], "default")
            self.assertEqual(saved["observed_model"], relay.MODEL_ID)
            self.assertNotIn("private prompt", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_shapes_never_load_key(self) -> None:
        fixtures = []
        for mutation in (
            lambda item: item.update(model="another-model"),
            lambda item: item.update(stream=False),
            lambda item: item.update(store=True),
            lambda item: item.update(reasoning={"effort": "high", "summary": "auto"}),
            lambda item: item.update(previous_response_id="provider-state"),
            lambda item: item.update(tools=[{"type": "web_search_preview"}], tool_choice="auto"),
        ):
            document = json.loads(valid_body())
            mutation(document)
            fixtures.append(json.dumps(document).encode())
        for body in fixtures:
            with self.subTest(body=body):
                status, _, _ = self.fixture.request("POST", "/v1/responses", body)
                self.assertEqual(status, 403)
        self.assertEqual(self.fixture.key_loads, 0)
        self.assertEqual(self.fixture.forwarder.calls, [])

    def test_missing_field_rejection_names_only_the_closed_schema_field(self) -> None:
        document = json.loads(valid_body())
        del document["reasoning"]
        self.assertEqual(
            relay.responses_body_rejection(json.dumps(document).encode()),
            "missing_reasoning",
        )

    def test_prompt_cache_key_is_optional_but_strict_when_present(self) -> None:
        document = json.loads(valid_body())
        del document["prompt_cache_key"]
        self.assertIsNone(relay.responses_body_rejection(json.dumps(document).encode()))
        document["prompt_cache_key"] = "unsafe key"
        self.assertEqual(
            relay.responses_body_rejection(json.dumps(document).encode()),
            "cache_key",
        )

    def test_reasoning_rejections_expose_only_fixed_schema_categories(self) -> None:
        expected = {
            None: "reasoning_type",
            "missing_effort": "reasoning_missing_effort",
            "missing_summary": "reasoning_missing_summary",
            "extra": "reasoning_unexpected_key",
            "wrong_effort": "reasoning_effort_high",
            "wrong_effort_type": "reasoning_effort_unknown",
            "wrong_summary": "reasoning_summary",
        }
        mutations = {
            None: None,
            "missing_effort": {"summary": "auto"},
            "missing_summary": {"effort": "xhigh"},
            "extra": {"effort": "xhigh", "summary": "auto", "extra": True},
            "wrong_effort": {"effort": "high", "summary": "auto"},
            "wrong_effort_type": {"effort": ["xhigh"], "summary": "auto"},
            "wrong_summary": {"effort": "xhigh", "summary": "detailed"},
        }
        for name, reasoning in mutations.items():
            document = json.loads(valid_body())
            document["reasoning"] = reasoning
            self.assertEqual(
                relay.responses_body_rejection(json.dumps(document).encode()),
                expected[name],
            )

    def test_explicit_null_tools_is_rejected(self) -> None:
        document = json.loads(valid_body())
        document["tools"] = None
        self.assertEqual(
            relay.responses_body_rejection(json.dumps(document).encode()),
            "tools",
        )

    def test_duplicate_json_keys_and_nonfinite_numbers_are_denied(self) -> None:
        valid = valid_body().decode()
        duplicate = valid.replace(
            f'"model":"{relay.MODEL_ID}"',
            f'"model":"another-model","model":"{relay.MODEL_ID}"',
            1,
        ).encode()
        nonfinite = valid.replace('"content":"offline fixture"', '"content":NaN', 1).encode()
        exponent_overflow = valid.replace(
            '"content":"offline fixture"', '"content":1e999', 1
        ).encode()
        for body in (duplicate, nonfinite, exponent_overflow):
            with self.subTest(body=body):
                status, _, _ = self.fixture.request("POST", "/v1/responses", body)
                self.assertEqual(status, 403)
        self.assertEqual(self.fixture.key_loads, 0)

    def test_method_path_query_and_content_type_are_fail_closed(self) -> None:
        status, _, _ = self.fixture.request("DELETE", "/v1/responses")
        self.assertEqual(status, 405)
        status, _, _ = self.fixture.request("POST", "/v1/chat/completions", valid_body())
        self.assertEqual(status, 404)
        status, _, _ = self.fixture.request("POST", "/v1/responses?x=1", valid_body())
        self.assertEqual(status, 404)
        status, _, _ = self.fixture.request(
            "POST",
            "/v1/responses",
            valid_body(),
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        self.assertEqual(self.fixture.key_loads, 0)

    def test_absolute_form_and_connect_can_never_reach_third_party(self) -> None:
        status, _, _ = self.fixture.request(
            "GET", "http://opencode.ai/session", authorize=False
        )
        self.assertEqual(status, 407)
        proxy_value = base64.b64encode(
            f"{relay.PROXY_USERNAME}:{CALLER_TOKEN}".encode()
        ).decode()
        status, _, body = self.fixture.request(
            "GET",
            "http://opencode.ai/session",
            authorize=False,
            headers={"Proxy-Authorization": f"Basic {proxy_value}"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "proxy_destination_denied")

        with socket.create_connection((relay.LOOPBACK_HOST, self.fixture.port), timeout=2) as client:
            request = (
                "CONNECT opencode.ai:443 HTTP/1.1\r\n"
                "Host: opencode.ai:443\r\n"
                f"Proxy-Authorization: Basic {proxy_value}\r\n"
                "Connection: close\r\n\r\n"
            )
            client.sendall(request.encode("ascii"))
            response = client.recv(4096)
        self.assertIn(b" 403 ", response.split(b"\r\n", 1)[0])
        self.assertEqual(self.fixture.key_loads, 0)


class RelayUnitTests(unittest.TestCase):
    def test_legacy_service_label_is_explicit_and_path_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = default_paths(pathlib.Path(directory))
            launch_agent, service = legacy_service_paths(paths, "local.previous-maka")
            self.assertEqual(
                launch_agent,
                pathlib.Path(directory)
                / "Library/LaunchAgents/local.previous-maka.plist",
            )
            self.assertTrue(service.endswith("/local.previous-maka"))
            for invalid in ("", "../escape", "/absolute", installer.LABEL, "bad label"):
                with self.assertRaises(relay.RelayConfigurationError):
                    legacy_service_paths(paths, invalid)

    def test_default_paths_use_the_managed_maka_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            account_home = pathlib.Path(directory)
            paths = default_paths(account_home)
            expected_root = account_home / ".local/share/khenrix-utils/maka"
            self.assertEqual(paths.lab_root, expected_root)
            self.assertEqual(
                paths.script,
                expected_root / "interactive" / "maka_openai_relay.py",
            )
            self.assertEqual(
                paths.configure_script,
                expected_root / "interactive" / "configure_maka_openai_relay.mjs",
            )
            self.assertEqual(
                paths.keychain_account_file,
                account_home / ".config/khenrix-utils/maka/maka-openai-keychain-account",
            )
            self.assertEqual(paths.install_lock_file, paths.config_directory / "install.lock")

    def test_installer_lock_refuses_concurrent_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = default_paths(pathlib.Path(directory))
            with installer.exclusive_install_lock(paths):
                with self.assertRaisesRegex(
                    relay.RelayConfigurationError, "already running"
                ):
                    with installer.exclusive_install_lock(paths):
                        self.fail("concurrent installer unexpectedly acquired the lock")
            self.assertEqual(paths.install_lock_file.stat().st_mode & 0o777, 0o600)

    def test_configurer_uses_explicit_attestation_root_and_minimal_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = default_paths(pathlib.Path(directory))
            package_root = pathlib.Path(directory) / "pinned-maka"
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with mock.patch.object(
                installer, "discover_maka_package_root", return_value=package_root
            ), mock.patch.object(installer.subprocess, "run", return_value=completed) as run:
                installer.configure_maka(paths, 48173, purge_credentials=True)
            arguments = run.call_args.args[0]
            environment = run.call_args.kwargs["env"]
            self.assertIn("--attestation-file", arguments)
            self.assertEqual(
                arguments[arguments.index("--attestation-file") + 1],
                str(paths.attestation_file),
            )
            self.assertIn("--package-root", arguments)
            self.assertEqual(
                arguments[arguments.index("--package-root") + 1], str(package_root)
            )
            self.assertIn("--purge-credentials", arguments)
            self.assertEqual(
                set(environment), {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL"}
            )

    def test_readiness_never_sends_bearer_before_attestation_proof_matches(self) -> None:
        calls: list[tuple[str, dict[str, str]]] = []

        def wrong_listener(port, path, headers):
            calls.append((path, headers))
            challenge = headers[relay.HEALTH_CHALLENGE_HEADER]
            return 200, {
                "status": "ready",
                "proof": relay.health_challenge_proof("D" * 64, challenge),
            }

        with mock.patch.object(installer, "_read_attestation", return_value="C" * 64), mock.patch.object(
            installer, "_get_local_json", side_effect=wrong_listener
        ):
            with self.assertRaises(relay.RelayConfigurationError):
                installer.wait_until_ready(
                    CALLER_TOKEN,
                    pathlib.Path("/unused-attestation"),
                    48173,
                    timeout_seconds=0.01,
                )
        self.assertTrue(calls)
        self.assertTrue(all(path == relay.HEALTH_PATH for path, _ in calls))
        self.assertTrue(all("Authorization" not in headers for _, headers in calls))
        self.assertTrue(
            all(
                relay._HEALTH_CHALLENGE_RE.fullmatch(
                    headers[relay.HEALTH_CHALLENGE_HEADER]
                )
                for _, headers in calls
            )
        )

        calls.clear()

        def matching_listener(port, path, headers):
            calls.append((path, headers))
            if path == relay.HEALTH_PATH:
                challenge = headers[relay.HEALTH_CHALLENGE_HEADER]
                return 200, {
                    "status": "ready",
                    "proof": relay.health_challenge_proof("C" * 64, challenge),
                }
            return 200, {"data": [{"id": model} for model in relay.MODEL_IDS]}

        with mock.patch.object(installer, "_read_attestation", return_value="C" * 64), mock.patch.object(
            installer, "_get_local_json", side_effect=matching_listener
        ):
            installer.wait_until_ready(
                CALLER_TOKEN,
                pathlib.Path("/unused-attestation"),
                48173,
                timeout_seconds=0.1,
            )
            first_challenge = calls[0][1][relay.HEALTH_CHALLENGE_HEADER]
            calls.clear()
            installer.wait_until_ready(
                CALLER_TOKEN,
                pathlib.Path("/unused-attestation"),
                48173,
                timeout_seconds=0.1,
            )
            second_challenge = calls[0][1][relay.HEALTH_CHALLENGE_HEADER]
        self.assertEqual([path for path, _ in calls], [relay.HEALTH_PATH, "/v1/models"])
        self.assertNotIn("Authorization", calls[0][1])
        self.assertRegex(
            calls[0][1][relay.HEALTH_CHALLENGE_HEADER], relay._HEALTH_CHALLENGE_RE
        )
        self.assertEqual(calls[1][1]["Authorization"], f"Bearer {CALLER_TOKEN}")
        self.assertNotEqual(second_challenge, first_challenge)

    def test_relay_sanitizes_tls_logging_and_uses_no_keylog_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            keylog = pathlib.Path(directory) / "tls.keys"
            with mock.patch.dict(os.environ, {"SSLKEYLOGFILE": str(keylog)}, clear=False):
                relay._sanitize_relay_environment()
                forwarder = relay.OpenAIForwarder()
                self.assertNotIn("SSLKEYLOGFILE", os.environ)
                self.assertIsNone(forwarder._tls_context.keylog_filename)
                self.assertFalse(keylog.exists())

    def test_listener_can_rebind_immediately_after_a_handled_connection(self) -> None:
        dependencies = relay.RelayDependencies(
            caller_token=CALLER_TOKEN,
            attestation="C" * 64,
            key_loader=lambda: DUMMY_PROVIDER_KEY,
            forwarder=RecordingForwarder(),
        )
        first = relay.create_server(relay.LOOPBACK_HOST, 0, dependencies)
        port = first.server_address[1]
        thread = threading.Thread(target=first.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(relay.LOOPBACK_HOST, port, timeout=2)
        try:
            connection.request(
                "GET",
                relay.HEALTH_PATH,
                headers={relay.HEALTH_CHALLENGE_HEADER: "N" * 43},
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
        finally:
            connection.close()
            first.shutdown()
            first.server_close()
            thread.join(timeout=2)
        second = relay.create_server(relay.LOOPBACK_HOST, port, dependencies)
        second.server_close()

    def test_installer_creates_and_reuses_an_owner_only_decoy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = default_paths(pathlib.Path(directory))
            first = ensure_private_token(paths)
            second = ensure_private_token(paths)
            self.assertEqual(first, second)
            self.assertRegex(first, relay._TOKEN_RE)
            self.assertEqual(paths.config_directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(paths.token_file.stat().st_mode & 0o777, 0o600)

    def test_relay_install_requires_private_api_key_mode_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = default_paths(pathlib.Path(directory))
            with self.assertRaises(relay.RelayConfigurationError):
                installer.assert_api_key_relay_mode(paths)

            paths.auth_mode_file.parent.mkdir(parents=True, mode=0o700)
            paths.auth_mode_file.write_text("chatgpt-subscription\n")
            paths.auth_mode_file.chmod(0o600)
            with self.assertRaises(relay.RelayConfigurationError):
                installer.assert_api_key_relay_mode(paths)

            paths.auth_mode_file.write_text("api-key-relay\n")
            installer.assert_api_key_relay_mode(paths)

            paths.auth_mode_file.chmod(0o644)
            with self.assertRaises(relay.RelayConfigurationError):
                installer.assert_api_key_relay_mode(paths)

    def test_relay_install_refuses_subscription_before_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = default_paths(pathlib.Path(directory))
            paths.auth_mode_file.parent.mkdir(parents=True, mode=0o700)
            paths.auth_mode_file.write_text("chatgpt-subscription\n")
            paths.auth_mode_file.chmod(0o600)
            with mock.patch.object(installer, "default_paths", return_value=paths), mock.patch.object(
                installer, "select_keychain_account"
            ) as select_account, mock.patch.object(installer, "ensure_private_token"
            ) as ensure_token, mock.patch.object(installer, "atomic_write_plist") as write_plist:
                with self.assertRaises(relay.RelayConfigurationError):
                    installer.install(48173, pathlib.Path("/usr/bin/python3"))
            select_account.assert_not_called()
            ensure_token.assert_not_called()
            write_plist.assert_not_called()

    def test_relay_install_invalidates_readiness_before_attempting_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = default_paths(pathlib.Path(directory))
            selector.write_mode("api-key-relay", paths.auth_mode_file)
            selector.write_keychain_account(KEYCHAIN_ACCOUNT, paths.keychain_account_file)
            selector.write_relay_ready(paths.relay_ready_file, paths.keychain_account_file)
            with mock.patch.object(installer, "default_paths", return_value=paths), mock.patch.object(
                installer, "select_keychain_account", return_value=KEYCHAIN_ACCOUNT
            ), mock.patch.object(
                installer, "assert_current_runtime", return_value=pathlib.Path("/usr/bin/python3")
            ):
                with self.assertRaises(relay.RelayConfigurationError):
                    installer.install(48173, pathlib.Path("/usr/bin/python3"))
            self.assertFalse(paths.relay_ready_file.exists())

    def test_relay_install_records_readiness_only_after_final_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = default_paths(pathlib.Path(directory))
            selector.write_mode("api-key-relay", paths.auth_mode_file)
            selector.write_keychain_account(KEYCHAIN_ACCOUNT, paths.keychain_account_file)
            paths.script.parent.mkdir(parents=True, exist_ok=True)
            paths.script.write_text("# relay\n")
            paths.configure_script.write_text("// configure\n")
            (paths.lab_root / "mise.toml").write_text("[tools]\n")
            with mock.patch.object(installer, "default_paths", return_value=paths), mock.patch.object(
                installer, "select_keychain_account", return_value=KEYCHAIN_ACCOUNT
            ), mock.patch.object(
                installer, "assert_current_runtime", return_value=pathlib.Path("/usr/bin/python3")
            ), mock.patch.object(installer, "ensure_private_token", return_value="B" * 64
            ), mock.patch.object(installer, "atomic_write_plist"), mock.patch.object(
                installer, "run_launchctl"
            ), mock.patch.object(installer, "wait_until_unloaded"), mock.patch.object(
                installer, "wait_until_ready"
            ) as ready, mock.patch.object(installer, "configure_maka") as configure, mock.patch.object(
                installer, "assert_private_install_artifacts"
            ) as verify:
                installer.install(48173, pathlib.Path("/usr/bin/python3"))
            self.assertEqual(ready.call_count, 2)
            configure.assert_called_once_with(paths, 48173, purge_credentials=True)
            verify.assert_called_once_with(paths)
            selector.require_relay_ready(
                paths.auth_mode_file,
                paths.relay_ready_file,
                paths.keychain_account_file,
            )

    def test_account_selection_requires_one_exact_existing_keychain_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = default_paths(pathlib.Path(directory))
            paths.auth_mode_file.parent.mkdir(parents=True, mode=0o700)
            loader = mock.Mock(return_value=DUMMY_PROVIDER_KEY)
            with mock.patch.object(installer, "KeychainOpenAIKey", return_value=loader) as factory:
                selected = installer.select_keychain_account(paths, KEYCHAIN_ACCOUNT)
            self.assertEqual(selected, KEYCHAIN_ACCOUNT)
            self.assertEqual(selector.read_keychain_account(paths.keychain_account_file), selected)
            self.assertEqual(paths.keychain_account_file.stat().st_mode & 0o777, 0o600)
            factory.assert_called_once_with(KEYCHAIN_ACCOUNT)
            loader.assert_called_once_with()

            with mock.patch.object(installer, "KeychainOpenAIKey", return_value=loader):
                self.assertEqual(installer.select_keychain_account(paths, None), selected)

    def test_account_selection_fails_closed_for_missing_or_ambiguous_choice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = default_paths(pathlib.Path(directory))
            paths.auth_mode_file.parent.mkdir(parents=True, mode=0o700)
            with mock.patch.object(
                installer,
                "KeychainOpenAIKey",
                side_effect=relay.ProviderCredentialError("missing"),
            ):
                with self.assertRaises(relay.RelayConfigurationError):
                    installer.select_keychain_account(paths, KEYCHAIN_ACCOUNT)
            self.assertFalse(paths.keychain_account_file.exists())

            selector.write_keychain_account(KEYCHAIN_ACCOUNT, paths.keychain_account_file)
            with mock.patch.object(installer, "KeychainOpenAIKey") as loader:
                with self.assertRaisesRegex(
                    relay.RelayConfigurationError, "explicit replacement"
                ):
                    installer.select_keychain_account(paths, "cli|different-account")
            loader.assert_not_called()
            self.assertEqual(
                selector.read_keychain_account(paths.keychain_account_file), KEYCHAIN_ACCOUNT
            )

    def test_account_replacement_is_an_explicit_validated_installer_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = default_paths(pathlib.Path(directory))
            paths.auth_mode_file.parent.mkdir(parents=True, mode=0o700)
            selector.write_keychain_account(KEYCHAIN_ACCOUNT, paths.keychain_account_file)
            replacement = "cli|replacement-account"
            loader = mock.Mock(return_value=DUMMY_PROVIDER_KEY)
            with mock.patch.object(installer, "KeychainOpenAIKey", return_value=loader):
                selected = installer.select_keychain_account(paths, replacement, replace=True)
            self.assertEqual(selected, replacement)
            self.assertEqual(selector.read_keychain_account(paths.keychain_account_file), replacement)

    def test_mise_resolution_accepts_user_local_and_rejects_writable_binary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            mise = home / ".local/bin/mise"
            mise.parent.mkdir(parents=True)
            mise.write_text("#!/bin/sh\nexit 0\n")
            mise.chmod(0o700)
            self.assertEqual(installer.resolve_mise_binary(home, (mise,)), mise)
            mise.chmod(0o722)
            with self.assertRaises(relay.RelayConfigurationError):
                installer.resolve_mise_binary(home, (mise,))

    def test_private_token_rejects_broad_permissions_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            token_file = root / "token"
            token_file.write_text(f"{CALLER_TOKEN}\n")
            token_file.chmod(0o600)
            self.assertEqual(relay.read_private_token(token_file), CALLER_TOKEN)
            token_file.chmod(0o644)
            with self.assertRaises(relay.RelayConfigurationError):
                relay.read_private_token(token_file)
            token_file.chmod(0o600)
            symlink = root / "token-link"
            symlink.symlink_to(token_file)
            with self.assertRaises(relay.RelayConfigurationError):
                relay.read_private_token(symlink)

    def test_keychain_document_parser_handles_only_bounded_json_key(self) -> None:
        document = json.dumps({"OPENAI_API_KEY": DUMMY_PROVIDER_KEY}).encode()
        self.assertEqual(relay.parse_codex_auth_document(document), DUMMY_PROVIDER_KEY)
        for invalid in (b"not json", b"[]", b"{}", b'{"OPENAI_API_KEY":"a\\n"}'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(relay.ProviderCredentialError):
                    relay.parse_codex_auth_document(invalid)

    def test_keychain_account_reader_rejects_missing_loose_malformed_and_symlinked_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            private = root / "private"
            private.mkdir(mode=0o700)
            account_file = private / "maka-openai-keychain-account"
            with self.assertRaises(relay.RelayConfigurationError):
                relay.read_keychain_account(account_file)
            account_file.write_text(f"{KEYCHAIN_ACCOUNT}\n", encoding="ascii")
            account_file.chmod(0o600)
            self.assertEqual(relay.read_keychain_account(account_file), KEYCHAIN_ACCOUNT)
            account_file.chmod(0o644)
            with self.assertRaises(relay.RelayConfigurationError):
                relay.read_keychain_account(account_file)
            account_file.chmod(0o600)
            account_file.write_text("cli|bad account\n", encoding="ascii")
            with self.assertRaises(relay.RelayConfigurationError):
                relay.read_keychain_account(account_file)
            account_file.write_text(f"{KEYCHAIN_ACCOUNT}\n", encoding="ascii")
            symlink = private / "account-link"
            symlink.symlink_to(account_file)
            with self.assertRaises(relay.RelayConfigurationError):
                relay.read_keychain_account(symlink)

    def test_key_loader_uses_fixed_service_and_exact_selected_account(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"OPENAI_API_KEY": DUMMY_PROVIDER_KEY}).encode(),
            stderr=b"",
        )
        with mock.patch.object(relay.subprocess, "run", return_value=completed) as run:
            self.assertEqual(relay.KeychainOpenAIKey(KEYCHAIN_ACCOUNT)(), DUMMY_PROVIDER_KEY)
        arguments = run.call_args.args[0]
        self.assertEqual(
            arguments,
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                relay.KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
        )
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_key_loader_refuses_account_changes_until_installer_rebinds_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private = pathlib.Path(directory) / "private"
            account_file = private / "maka-openai-keychain-account"
            ready_file = private / "maka-api-key-relay-ready"
            selector.write_keychain_account(KEYCHAIN_ACCOUNT, account_file)
            selector.write_relay_ready(ready_file, account_file)
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"OPENAI_API_KEY": DUMMY_PROVIDER_KEY}).encode(),
                stderr=b"",
            )
            loader = relay.KeychainOpenAIKey(KEYCHAIN_ACCOUNT, account_file, ready_file)
            with mock.patch.object(relay.subprocess, "run", return_value=completed) as run:
                self.assertEqual(loader(), DUMMY_PROVIDER_KEY)
                ready_file.unlink()
                with self.assertRaises(relay.RelayConfigurationError):
                    loader()
                selector.write_relay_ready(ready_file, account_file)
                selector.write_keychain_account(
                    "cli|changed-account",
                    account_file,
                    replace=True,
                )
                with self.assertRaises(relay.ProviderCredentialError):
                    loader()
            run.assert_called_once()

    def test_openai_forwarder_hardcodes_origin_path_and_headers(self) -> None:
        captures: list[object] = []

        class FakeConnection:
            def __init__(self, host, port, **options):
                captures.append((host, port, options))

            def request(self, method, path, body, headers):
                captures.append((method, path, body, headers))

            def getresponse(self):
                return FakeResponse()

            def close(self):
                captures.append("closed")

        with mock.patch.object(relay.http.client, "HTTPSConnection", FakeConnection):
            exchange = relay.OpenAIForwarder().forward(
                b'{"offline":true}', DUMMY_PROVIDER_KEY
            )
            exchange.close()
        host, port, options = captures[0]
        self.assertEqual((host, port), (relay.OPENAI_HOST, relay.OPENAI_PORT))
        self.assertIsNotNone(options["context"])
        self.assertIsNone(options["context"].keylog_filename)
        method, path, body, headers = captures[1]
        self.assertEqual((method, path), ("POST", relay.OPENAI_PATH))
        self.assertEqual(body, b'{"offline":true}')
        self.assertEqual(headers["Authorization"], f"Bearer {DUMMY_PROVIDER_KEY}")
        self.assertEqual(set(headers), {"Authorization", "Content-Type", "Accept", "Connection"})

    def test_launch_agent_contains_no_key_or_token_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = default_paths(pathlib.Path(directory))
            document = build_launch_agent(paths, pathlib.Path("/usr/bin/python3"))
            encoded = plistlib.dumps(document)
            self.assertNotIn(CALLER_TOKEN.encode(), encoded)
            self.assertNotIn(DUMMY_PROVIDER_KEY.encode(), encoded)
            self.assertNotIn(b"OPENAI_API_KEY", encoded)
            self.assertNotIn(KEYCHAIN_ACCOUNT.encode(), encoded)
            self.assertIn(str(paths.token_file).encode(), encoded)
            self.assertIn(str(paths.keychain_account_file).encode(), encoded)
            self.assertIn(str(paths.relay_ready_file).encode(), encoded)
            self.assertNotIn(b"Keychain", encoded)
            arguments = document["ProgramArguments"]
            self.assertEqual(arguments[:2], ["/usr/bin/env", "-i"])
            self.assertIn("-I", arguments)
            self.assertIn("-S", arguments)
            self.assertIn("-B", arguments)
            self.assertIn(str(paths.attestation_file), arguments)
            self.assertIn("--keychain-account-file", arguments)
            self.assertIn("--relay-ready-file", arguments)

    def test_relay_imports_and_enters_serve_under_isolated_pinned_python(self) -> None:
        component = pathlib.Path(__file__).resolve().parent.parent
        python = pathlib.Path.home() / ".local/share/mise/installs/python/3.12.14/bin/python3.12"
        if not python.is_file():
            self.skipTest("pinned Python is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            code = """
import pathlib, sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
import maka_openai_relay as relay
called = []
relay.assert_current_runtime = lambda: called.append(True)
base = pathlib.Path(sys.argv[3])
try:
    relay.serve(base / 'missing-token', base / 'attestation', base / 'account', base / 'ready', 0)
except relay.RelayConfigurationError:
    pass
else:
    raise SystemExit('serve unexpectedly passed missing-token preflight')
if called != [True]:
    raise SystemExit('runtime assertion was not called')
"""
            completed = subprocess.run(
                [
                    str(python),
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    code,
                    str(component / "interactive"),
                    str(component / "scripts"),
                    directory,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env={
                    "HOME": str(pathlib.Path.home()),
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                },
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_eval_key_export_uses_isolated_pinned_python(self) -> None:
        source = (
            pathlib.Path(__file__).resolve().parent.parent / "scripts/harbor-smoke.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("/usr/bin/env -i PATH=/usr/bin:/bin", source)
        self.assertIn('harden_python_runtime.py" verify', source)
        self.assertIn('run_hardened_python.py" key-export', source)

    def test_relay_tasks_force_locked_runtime_before_exact_isolated_install(self) -> None:
        source = (
            pathlib.Path(__file__).resolve().parent.parent / "mise.toml"
        ).read_text(encoding="utf-8")
        self.assertIn("harden_python_runtime.py prepare", source)
        self.assertIn("run_hardened_python.py relay-installer", source)
        hardener = (
            pathlib.Path(__file__).resolve().parent.parent
            / "scripts/harden_python_runtime.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"--force",', hardener)
        self.assertIn('"--locked",', hardener)
        self.assertIn('f"python@{PYTHON_VERSION}"', hardener)


if __name__ == "__main__":
    unittest.main(verbosity=2)
