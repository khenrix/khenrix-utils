"""Disposable Codex custom-provider characterization at the Forge boundary."""
from __future__ import annotations

import importlib.util
import http.client
import itertools
import json
import queue
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CONNECT_TARGETS = {
    "chatgpt.com:443", "github.com:443", "ab.chatgpt.com:443",
}


@pytest.fixture
def native_policy():
    if sys.platform != "darwin":
        pytest.skip("native Forge boundary requires macOS sandbox-exec")
    spec = importlib.util.spec_from_file_location(
        "fanout_forge_fixtures", ROOT / "scripts" / "fanout_forge_fixtures.py"
    )
    assert spec and spec.loader
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    with tempfile.TemporaryDirectory(prefix="forge-codex-canary-", dir="/private/tmp") as raw:
        temporary = Path(raw)
        baseline = temporary / "baseline"
        consumer = temporary / "consumer"
        baseline.mkdir(mode=0o700)
        consumer.mkdir(mode=0o700)
        boundary = fixtures.open_blindness_boundary(
            temporary / "campaign", baseline, consumer, resume=False,
        )
        workspace = boundary.run_root / "cases" / "forge-01" / "seats" / "codex"
        workspace.mkdir(mode=0o700, parents=True)
        (workspace / "sentinel").write_text("seat\n", encoding="utf-8")
        seat_env = fixtures.seat_environment(boundary, workspace, "codex")
        state = Path(seat_env["HOME"])
        (state / ".codex").mkdir(mode=0o700)
        yield {
            "policy": fixtures._native_campaign_policy(boundary, workspace, state),
            "baseline": baseline,
            "workspace": workspace,
            "state": state,
            "seat_env": seat_env,
            "codex": dict(boundary.executable_paths)["codex"],
        }


def test_installed_codex_version_is_01590_under_native_policy(native_policy: dict) -> None:
    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", native_policy["policy"],
         str(native_policy["codex"]), "--version"],
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
             "HOME": str(native_policy["state"]),
             "CODEX_HOME": str(native_policy["state"] / ".codex")},
        capture_output=True, text=True, timeout=10,
    )
    _applied(result)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "codex-cli 0.159.0"


@pytest.fixture
def fake_responses(request):
    requests: queue.Queue[dict] = queue.Queue()
    post_numbers = itertools.count(1)

    class FakeResponses(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def parse_request(self) -> bool:
            self._record = None
            self._last_status = None
            parsed = super().parse_request()
            if self.command is not None:
                if self.command == "CONNECT":
                    path = (self.path if self.path in EXPECTED_CONNECT_TARGETS
                            else "<unexpected CONNECT>")
                else:
                    path = ("/v1/responses" if self.path == "/v1/responses"
                            else "<unexpected path>")
                headers = self.headers if parsed else None
                self._record = {
                    "method": (self.command if self.command in {"POST", "CONNECT"}
                               else "<unexpected method>"),
                    "path": path,
                    "headers_parsed": parsed,
                    "has_authorization": (headers.get("Authorization") is not None
                                          if headers is not None else None),
                    "has_proxy_authorization": (headers.get("Proxy-Authorization") is not None
                                                if headers is not None else None),
                    "has_broker_header": (headers.get("X-Fanout-Seat") == "disposable-placeholder"
                                          if headers is not None else None),
                    "model_is_fake": None,
                    "body_fields": None,
                    "status": self._last_status,
                }
                requests.put(self._record)
            return parsed

        def send_response(self, code: int, message: str | None = None) -> None:
            self._last_status = code
            if getattr(self, "_record", None) is not None:
                self._record["status"] = code
            super().send_response(code, message)

        def _respond(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(body) if body else None
            except json.JSONDecodeError:
                payload = None
            self._record["model_is_fake"] = (isinstance(payload, dict)
                                              and payload.get("model") == "fake-canary-model")
            self._record["body_fields"] = sorted(payload) if isinstance(payload, dict) else None
            post_number = next(post_numbers) if self.command == "POST" else 0
            if (self.command == "POST" and post_number <= 2
                    and getattr(request, "param", None) == "complete"):
                label = "first" if post_number == 1 else "second"
                response_id = f"resp_fixture_{label}"
                events = (
                    {"type": "response.created", "response": {"id": response_id}},
                    {"type": "response.output_item.done", "item": {
                        "type": "message", "role": "assistant", "id": f"msg_fixture_{label}",
                        "content": [{"type": "output_text", "text": f"fixture-{label}"}],
                    }},
                    {"type": "response.completed", "response": {
                        "id": response_id,
                        "usage": {"input_tokens": 0, "input_tokens_details": None,
                                  "output_tokens": 0, "output_tokens_details": None,
                                  "total_tokens": 0},
                    }},
                )
                response = "".join(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                    for event in events
                ).encode()
                status = 200
                content_type = "text/event-stream"
            else:
                response = b'{"error":{"message":"fake canary response","type":"invalid_request_error"}}'
                status = 400
                content_type = "application/json"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        do_POST = _respond
        do_GET = _respond
        do_CONNECT = _respond

    with ThreadingHTTPServer(("127.0.0.1", 0), FakeResponses) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port, requests
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _run_codex(native_policy: dict, policy: str, port: int,
               *, timeout: int = 10, resume_id: str | None = None,
               prompt: str = "say canary", broker_marker: bool = False
               ) -> subprocess.CompletedProcess[str]:
    proxy = f"http://127.0.0.1:{port}"
    env = {
        "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
        "HOME": str(native_policy["state"]),
        "XDG_CONFIG_HOME": native_policy["seat_env"]["XDG_CONFIG_HOME"],
        "TMPDIR": native_policy["seat_env"]["TMPDIR"],
        "CODEX_HOME": str(native_policy["state"] / ".codex"),
        "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "ALL_PROXY": proxy,
        "NO_PROXY": "127.0.0.1,localhost",
    }
    provider_options = [
        "-c", "model_provider=fixture_canary",
        "-c", "model_providers.fixture_canary.name=FixtureCanary",
        "-c", f'model_providers.fixture_canary.base_url="{proxy}/v1"',
        "-c", "model_providers.fixture_canary.wire_api=responses",
        "-c", "model_providers.fixture_canary.requires_openai_auth=false",
        "-c", "model_providers.fixture_canary.request_max_retries=0",
        "-c", "model_providers.fixture_canary.stream_max_retries=0",
    ]
    if broker_marker:
        provider_options += [
            "-c",
            'model_providers.fixture_canary.http_headers={"X-Fanout-Seat"="disposable-placeholder"}',
        ]
    command = ["/usr/bin/sandbox-exec", "-p", policy,
               str(native_policy["codex"]), "exec"]
    if resume_id is None:
        command += [
            "--ignore-user-config", "--ignore-rules", "--strict-config",
            "--skip-git-repo-check", "--json", "-s", "read-only",
            "-C", str(native_policy["workspace"]), "-m", "fake-canary-model",
            *provider_options, prompt,
        ]
    else:
        command += [
            "resume", "--ignore-user-config", "--ignore-rules", "--strict-config",
            "--skip-git-repo-check", "--json", "-m", "fake-canary-model",
            "-c", 'sandbox_mode="read-only"', *provider_options, resume_id, prompt,
        ]
    return subprocess.run(
        command, cwd=native_policy["workspace"], env=env,
        capture_output=True, text=True, timeout=timeout,
    )


def _applied(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
        pytest.skip("nested tool sandbox cannot apply macOS sandbox-exec")


def _socket_errno(policy: str, host: str, port: int) -> int:
    probe = ("import socket,sys; s=socket.socket(); "
             "print(s.connect_ex((sys.argv[1],int(sys.argv[2]))))")
    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", policy, "/usr/bin/python3", "-I",
         "-c", probe, host, str(port)],
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        capture_output=True, text=True, timeout=10,
    )
    _applied(result)
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def _assert_fake_requests(requests: queue.Queue, *, posts: int,
                          post_status: int) -> None:
    observed = list(requests.queue)
    received_posts = 0
    connect_counts: dict[str, int] = {}
    assert observed
    for record in observed:
        assert record["headers_parsed"] is True, observed
        assert record["has_authorization"] is False, observed
        assert record["has_proxy_authorization"] is False, observed
        if record["method"] == "CONNECT":
            target = record["path"]
            assert target in EXPECTED_CONNECT_TARGETS, observed
            assert record["status"] == 400, observed
            # Optional startup CONNECTs vary by timing, but none may repeat more
            # than once per Codex invocation (one invocation per expected POST).
            connect_counts[target] = connect_counts.get(target, 0) + 1
            assert connect_counts[target] <= posts, observed
            continue
        assert (record["method"], record["path"]) == ("POST", "/v1/responses"), observed
        assert record["status"] == post_status, observed
        assert record["model_is_fake"] is True, observed
        received_posts += 1
    assert received_posts == posts, observed


def test_fake_server_records_and_rejects_unsupported_method(fake_responses) -> None:
    port, requests = fake_responses
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(
            "PATCH", "/v1/responses",
            headers={"Authorization": "fixture-auth-header",
                     "Proxy-Authorization": "fixture-proxy-header"},
        )
        response = connection.getresponse()
        response.read()
    finally:
        connection.close()
    assert response.status == 501
    observed = list(requests.queue)
    assert len(observed) == 1, observed
    assert observed[0]["method"] == "<unexpected method>"
    assert observed[0]["status"] == 501
    assert observed[0]["has_authorization"] is True
    assert observed[0]["has_proxy_authorization"] is True
    assert "fixture-auth-header" not in repr(observed)
    assert "fixture-proxy-header" not in repr(observed)
    assert "PATCH" not in repr(observed)
    with pytest.raises(AssertionError):
        _assert_fake_requests(requests, posts=0, post_status=200)


@pytest.mark.parametrize(
    "method,target,status,safe_method,safe_target",
    [
        ("X-PRIVATE-CANARY", "/v1/responses", 501,
         "<unexpected method>", "/v1/responses"),
        ("CONNECT", "private-canary.invalid:443", 400,
         "CONNECT", "<unexpected CONNECT>"),
    ],
)
def test_fake_server_sanitizes_unexpected_request_metadata(
    fake_responses, method: str, target: str, status: int,
    safe_method: str, safe_target: str,
) -> None:
    port, requests = fake_responses
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(method, target)
        response = connection.getresponse()
        response.read()
    finally:
        connection.close()
    assert response.status == status
    observed = list(requests.queue)
    assert len(observed) == 1, observed
    assert observed[0]["method"] == safe_method
    assert observed[0]["path"] == safe_target
    assert observed[0]["status"] == status
    assert observed[0]["has_authorization"] is False
    assert observed[0]["has_proxy_authorization"] is False
    assert "PRIVATE-CANARY" not in repr(observed)
    assert "private-canary.invalid" not in repr(observed)
    with pytest.raises(AssertionError):
        _assert_fake_requests(requests, posts=0, post_status=200)


def test_fake_only_policy_keeps_protected_baseline_unreadable(
    native_policy: dict, fake_responses,
) -> None:
    port, _requests = fake_responses
    sentinel = native_policy["baseline"] / "protected-sentinel"
    sentinel.write_bytes(b"disposable protected data\n")
    assert sentinel.read_bytes() == b"disposable protected data\n"
    policy = (native_policy["policy"] + " (deny network-outbound)"
              + f' (allow network-outbound (remote ip "localhost:{port}"))')
    probe = ("import sys\ntry: open(sys.argv[1], 'rb').read(1)\n"
             "except PermissionError: sys.exit(17)\nsys.exit(0)")
    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", policy,
         "/usr/bin/python3", "-I", "-c", probe, str(sentinel)],
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        capture_output=True, text=True, timeout=10,
    )
    _applied(result)
    assert result.returncode == 17, (result.returncode, result.stderr)


def test_codex_custom_provider_reaches_fake_responses_proxy(
    native_policy: dict, fake_responses,
) -> None:
    port, requests = fake_responses
    # The disposable protocol probe allows only the fake listener; it does not
    # change the production Forge policy measured in the other tests.
    policy = (native_policy["policy"] + " (deny network-outbound)"
              + f' (allow network-outbound (remote ip "localhost:{port}"))')
    assert _socket_errno(policy, "127.0.0.1", port) == 0
    assert _socket_errno(policy, "203.0.113.1", 1) == 1
    result = _run_codex(native_policy, policy, port)
    _applied(result)
    _assert_fake_requests(requests, posts=1, post_status=400)


def test_codex_custom_provider_can_send_fixed_tokenless_broker_header(
    native_policy: dict, fake_responses,
) -> None:
    port, requests = fake_responses
    policy = (native_policy["policy"] + " (deny network-outbound)"
              + f' (allow network-outbound (remote ip "localhost:{port}"))')
    result = _run_codex(native_policy, policy, port, broker_marker=True)
    _applied(result)
    _assert_fake_requests(requests, posts=1, post_status=400)
    assert all(record["has_broker_header"] is True for record in requests.queue
               if record["method"] == "POST")
    assert all("max_output_tokens" not in record["body_fields"] for record in requests.queue
               if record["method"] == "POST")


def test_codex_custom_provider_cannot_reach_fake_proxy_under_fixture_policy(
    native_policy: dict, fake_responses,
) -> None:
    port, requests = fake_responses
    assert _socket_errno(native_policy["policy"], "127.0.0.1", port) == 1
    try:
        result = _run_codex(native_policy, native_policy["policy"], port, timeout=5)
    except subprocess.TimeoutExpired:
        pass
    else:
        _applied(result)
        assert result.returncode != 0
    assert requests.empty(), list(requests.queue)


@pytest.mark.parametrize("fake_responses", ["complete"], indirect=True)
def test_codex_completes_from_fake_sse_without_model_or_auth(
    native_policy: dict, fake_responses,
) -> None:
    port, requests = fake_responses
    policy = (native_policy["policy"] + " (deny network-outbound)"
              + f' (allow network-outbound (remote ip "localhost:{port}"))')
    result = _run_codex(native_policy, policy, port)
    _applied(result)
    assert result.returncode == 0, (result.stdout[-2000:], result.stderr[-2000:])
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(event["type"] == "turn.completed" for event in events)
    assert any(event["type"] == "item.completed"
               and event["item"].get("text") == "fixture-first"
               for event in events)
    _assert_fake_requests(requests, posts=1, post_status=200)


@pytest.mark.parametrize("fake_responses", ["complete"], indirect=True)
def test_codex_resumes_exact_private_session_from_fake_sse(
    native_policy: dict, fake_responses,
) -> None:
    port, requests = fake_responses
    policy = (native_policy["policy"] + " (deny network-outbound)"
              + f' (allow network-outbound (remote ip "localhost:{port}"))')
    first = _run_codex(native_policy, policy, port, prompt="first canary")
    _applied(first)
    assert first.returncode == 0, (first.stdout[-2000:], first.stderr[-2000:])
    first_events = [json.loads(line) for line in first.stdout.splitlines()]
    thread_id = next(event["thread_id"] for event in first_events
                     if event["type"] == "thread.started")
    assert list((native_policy["state"] / ".codex" / "sessions").rglob("*.jsonl"))

    second = _run_codex(native_policy, policy, port,
                        resume_id=thread_id, prompt="second canary")
    _applied(second)
    assert second.returncode == 0, (second.stdout[-2000:], second.stderr[-2000:])
    second_events = [json.loads(line) for line in second.stdout.splitlines()]
    assert next(event["thread_id"] for event in second_events
                if event["type"] == "thread.started") == thread_id
    assert any(event["type"] == "item.completed"
               and event["item"].get("text") == "fixture-second"
               for event in second_events)
    _assert_fake_requests(requests, posts=2, post_status=200)
