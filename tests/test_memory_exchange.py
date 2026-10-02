from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import pathlib
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MEMORY_ROOT = ROOT / "components" / "memory"
sys.path.insert(0, str(MEMORY_ROOT))

import memory_exchange
import memoryctl


SCHEMA = "fanout-memory-exchange-v1"


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _save_request(*, text: str = "checkpoint α") -> dict[str, object]:
    return {
        "operation": "save",
        "project": "fanout/run/task/seat",
        "schema_version": SCHEMA,
        "text": text,
        "title": "fanout checkpoint sha256:abc",
    }


def _fetch_request(ids: list[int] | None = None) -> dict[str, object]:
    return {
        "ids": ids or [41, 42],
        "operation": "fetch",
        "schema_version": SCHEMA,
    }


def _observation(
    observation_id: int, *, observation_type: str = "discovery"
) -> dict[str, object]:
    return {
        "agent_id": None,
        "agent_type": None,
        "concepts": "[]",
        "content_hash": f"{observation_id:016x}",
        "created_at": "2026-09-23T00:00:00.000Z",
        "created_at_epoch": 1_795_000_000_000 + observation_id,
        "discovery_tokens": 0,
        "facts": "[]",
        "files_modified": "[]",
        "files_read": "[]",
        "generated_by_model": None,
        "id": observation_id,
        "memory_session_id": "manual-fanout-run-task-seat",
        "merged_into_project": None,
        "metadata": None,
        "narrative": f"checkpoint {observation_id}",
        "origin_device_id": None,
        "origin_local_id": None,
        "project": "fanout/run/task/seat",
        "prompt_number": None,
        "relevance_count": 0,
        "subtitle": "Manual memory",
        "sync_rev": "1",
        "synced_at": None,
        "text": None,
        "title": f"fanout checkpoint {observation_id}",
        "type": observation_type,
    }


Reply = tuple[int, bytes, dict[str, str]]
ReplyFactory = Callable[[dict[str, Any]], Reply]


class _Gateway(http.server.ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    reply: Reply | ReplyFactory


@contextlib.contextmanager
def _gateway(reply: Reply | ReplyFactory) -> Iterator[_Gateway]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            captured = {
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "path": self.path,
                "body": self.rfile.read(length),
            }
            self.server.requests.append(captured)  # type: ignore[attr-defined]
            selected = self.server.reply(captured) if callable(self.server.reply) else self.server.reply  # type: ignore[attr-defined]
            status, body, headers = selected
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

    server = _Gateway(("127.0.0.1", 0), Handler)
    server.requests = []
    server.reply = reply
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def private_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("AGENTIC_MEMORY_HOME", str(home))
    return home


@pytest.fixture
def token_path(private_home: pathlib.Path) -> pathlib.Path:
    path = private_home / ".config" / "agentic-memory" / "gateway-token"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("t" * 43 + "\n", encoding="ascii")
    path.chmod(0o600)
    memoryctl._atomic_private_json(
        memoryctl.install_receipt_path(), memoryctl.expected_install_receipt()
    )
    return path


def _run(
    raw: bytes,
    gateway: _Gateway | None = None,
    *,
    token_path: pathlib.Path | None = None,
) -> tuple[int, bytes, dict[str, object]]:
    stdin = io.BytesIO(raw)
    stdout = io.BytesIO()
    endpoint = None if gateway is None else f"http://127.0.0.1:{gateway.server_address[1]}"
    status = memory_exchange.run_once(
        stdin,
        stdout,
        endpoint=endpoint,
        token_path=token_path,
    )
    encoded = stdout.getvalue()
    return status, encoded, json.loads(encoded)


def test_controller_installs_and_inventories_memory_exchange_without_secrets(
    private_home: pathlib.Path,
) -> None:
    checkpoint = "checkpoint text must not become inventory"
    token = "t" * 43
    token_file = memoryctl.gateway_token_path()
    token_file.parent.mkdir(parents=True, mode=0o700)
    token_file.write_text(token + "\n", encoding="ascii")
    token_file.chmod(0o600)

    controller = memoryctl.install_controller()
    inventory = memoryctl.controller_inventory()

    assert (controller / "memory_exchange.py").is_file()
    assert inventory["complete"] is True
    assert "memory_exchange.py" in [item["name"] for item in inventory["files"]]
    assert memoryctl.health_document(require_running=False)["controller"] == inventory
    rendered = json.dumps(inventory, sort_keys=True)
    assert token not in rendered
    assert checkpoint not in rendered


def test_executable_sends_checkpoint_only_in_authenticated_stdin_body(
    private_home: pathlib.Path,
    token_path: pathlib.Path,
) -> None:
    checkpoint = "private checkpoint body"

    def saved(captured: dict[str, Any]) -> Reply:
        request = json.loads(captured["body"])
        response = {
            "success": True,
            "id": 41,
            "title": request["title"],
            "project": request["project"],
            "message": "Memory saved as observation #41",
        }
        return 200, json.dumps(response, separators=(",", ":")).encode(), {}

    with _gateway(saved) as gateway:
        controller = memoryctl.install_controller()
        command = [sys.executable, str(controller / "memory_exchange.py")]
        environment = {
            **os.environ,
            "AGENTIC_MEMORY_HOME": str(private_home),
            "KHENRIX_MEMORY_GATEWAY_PORT": str(gateway.server_address[1]),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            command,
            input=_canonical(_save_request(text=checkpoint)),
            capture_output=True,
            env=environment,
            timeout=10,
            check=False,
        )

    assert result.returncode == 0
    assert result.stderr == b""
    assert checkpoint not in " ".join(command)
    assert "t" * 43 not in " ".join(command)
    assert len(gateway.requests) == 1
    sent = gateway.requests[0]
    assert sent["path"] == "/api/memory/save"
    assert sent["authorization"] == f"Bearer {'t' * 43}"
    assert json.loads(sent["body"]) == {
        "project": "fanout/run/task/seat",
        "text": checkpoint,
        "title": "fanout checkpoint sha256:abc",
    }
    response = json.loads(result.stdout)
    assert response["result"]["id"] == 41
    assert result.stdout == _canonical(response)


def test_exchange_rejects_mismatched_install_receipt_before_network(
    token_path: pathlib.Path,
) -> None:
    stale = memoryctl.expected_install_receipt()
    stale["version"] = "13.25.1"
    memoryctl._atomic_private_json(memoryctl.install_receipt_path(), stale)
    with _gateway((200, b"[]", {})) as gateway:
        status, _, response = _run(_canonical(_fetch_request()), gateway, token_path=token_path)

    assert status != 0
    assert response["error"]["code"] == "install-unverified"  # type: ignore[index]
    assert gateway.requests == []


def test_fetch_normalizes_pinned_date_desc_rows_to_requested_id_order(
    token_path: pathlib.Path,
) -> None:
    body = json.dumps([_observation(42), _observation(41)], separators=(",", ":")).encode()
    with _gateway((200, body, {})) as gateway:
        status, encoded, response = _run(
            _canonical(_fetch_request()), gateway, token_path=token_path
        )

    assert status == 0
    assert encoded == _canonical(response)
    assert response == {
        "ok": True,
        "operation": "fetch",
        "result": [_observation(41), _observation(42)],
        "schema_version": SCHEMA,
    }
    assert len(gateway.requests) == 1
    sent = gateway.requests[0]
    assert sent["path"] == "/api/observations/batch"
    assert json.loads(sent["body"]) == {"ids": [41, 42]}
    assert "/api/search" not in sent["path"]
    assert "/context/inject" not in sent["path"]


@pytest.mark.parametrize(
    "observation_type",
    [
        "decision",
        "bugfix",
        "feature",
        "refactor",
        "discovery",
        "change",
        "security_alert",
        "security_note",
        "sensitive",
    ],
)
def test_all_pinned_observation_types_are_accepted(
    token_path: pathlib.Path, observation_type: str
) -> None:
    observation = _observation(41, observation_type=observation_type)
    body = json.dumps([observation], separators=(",", ":")).encode()
    with _gateway((200, body, {})) as gateway:
        status, _, response = _run(
            _canonical(_fetch_request([41])), gateway, token_path=token_path
        )

    assert status == 0
    assert response["result"] == [observation]


def test_unknown_observation_type_fails_closed(token_path: pathlib.Path) -> None:
    body = json.dumps(
        [_observation(41, observation_type="unknown")], separators=(",", ":")
    ).encode()
    with _gateway((200, body, {})) as gateway:
        status, _, response = _run(
            _canonical(_fetch_request([41])), gateway, token_path=token_path
        )

    assert status != 0
    assert response["error"]["code"] == "invalid-worker-response"


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json\n",
        b"{}\n{}\n",
        b'{"operation":"fetch","operation":"save","schema_version":"fanout-memory-exchange-v1"}\n',
        json.dumps(_fetch_request()).encode() + b"\n",
        _canonical({**_fetch_request(), "unknown": 1}),
        _canonical({**_fetch_request(), "ids": [True]}),
        _canonical({**_fetch_request(), "ids": [41, 41]}),
        _canonical({**_fetch_request(), "ids": []}),
        _canonical({**_save_request(), "metadata": {"secret": "x"}}),
        _canonical({**_save_request(), "text": " padded "}),
    ],
)
def test_invalid_or_noncanonical_requests_fail_before_token_or_network(raw: bytes) -> None:
    status, encoded, response = _run(raw)
    assert status != 0
    assert encoded == _canonical(response)
    assert response == {
        "error": {"code": "invalid-request", "message": "memory exchange failed"},
        "ok": False,
        "schema_version": SCHEMA,
    }


def test_request_limits_are_enforced_before_network() -> None:
    oversized_ids = list(range(1, memory_exchange.MAX_IDS + 2))
    status, _, response = _run(_canonical(_fetch_request(oversized_ids)))
    assert status != 0
    assert response["error"]["code"] == "invalid-request"  # type: ignore[index]

    oversized_text = "x" * (memory_exchange.MAX_TEXT_BYTES + 1)
    status, _, response = _run(_canonical(_save_request(text=oversized_text)))
    assert status != 0
    assert response["error"]["code"] == "invalid-request"  # type: ignore[index]


@pytest.mark.parametrize(
    ("status_code", "body", "headers"),
    [
        (500, b'{"error":"checkpoint body echoed by worker"}', {}),
        (302, b"", {"Location": "http://example.com/context/inject"}),
        (200, b"not-json", {}),
    ],
)
def test_non_success_redirect_and_malformed_worker_responses_are_safe_and_not_retried(
    token_path: pathlib.Path,
    status_code: int,
    body: bytes,
    headers: dict[str, str],
) -> None:
    checkpoint = "checkpoint body echoed by worker"
    token = "t" * 43
    with _gateway((status_code, body, headers)) as gateway:
        status, encoded, response = _run(
            _canonical(_save_request(text=checkpoint)), gateway, token_path=token_path
        )

    assert status != 0
    assert len(gateway.requests) == 1
    assert encoded == _canonical(response)
    assert response["error"]["message"] == "memory exchange failed"  # type: ignore[index]
    assert checkpoint.encode() not in encoded
    assert token.encode() not in encoded


@pytest.mark.parametrize(
    "response",
    [
        {
            "success": True,
            "id": 41,
            "title": "fanout checkpoint sha256:abc",
            "project": "fanout/run/task/seat",
            "message": "Memory saved as observation #41",
            "extra": "drift",
        },
        {
            "success": True,
            "id": True,
            "title": "fanout checkpoint sha256:abc",
            "project": "fanout/run/task/seat",
            "message": "Memory saved as observation #True",
        },
        {
            "success": True,
            "id": 41,
            "title": "another checkpoint",
            "project": "fanout/run/task/seat",
            "message": "Memory saved as observation #41",
        },
    ],
)
def test_save_response_shape_drift_fails_closed(
    token_path: pathlib.Path,
    response: dict[str, object],
) -> None:
    body = json.dumps(response, separators=(",", ":")).encode()
    with _gateway((200, body, {})) as gateway:
        status, _, result = _run(_canonical(_save_request()), gateway, token_path=token_path)
    assert status != 0
    assert result["error"]["code"] == "invalid-worker-response"  # type: ignore[index]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: [{**rows[0], "extra": "drift"}, rows[1]],
        lambda rows: [{key: value for key, value in rows[0].items() if key != "sync_rev"}, rows[1]],
        lambda rows: [{**rows[0], "id": True}, rows[1]],
        lambda rows: [{**rows[0], "facts": '["ok",1]'}, rows[1]],
        lambda rows: [rows[0]],
        lambda rows: [rows[0], rows[0]],
        lambda rows: [rows[0], {**rows[1], "id": 43}],
    ],
)
def test_worker_shape_or_exact_id_drift_fails_closed(
    token_path: pathlib.Path,
    mutate: Callable[[list[dict[str, object]]], list[dict[str, object]]],
) -> None:
    rows = mutate([_observation(41), _observation(42)])
    with _gateway((200, json.dumps(rows, separators=(",", ":")).encode(), {})) as gateway:
        status, _, response = _run(_canonical(_fetch_request()), gateway, token_path=token_path)
    assert status != 0
    assert response["error"]["code"] == "invalid-worker-response"  # type: ignore[index]


def test_oversized_worker_response_fails_closed(token_path: pathlib.Path) -> None:
    body = b" " * (memory_exchange.MAX_RESPONSE_BYTES + 1)
    with _gateway((200, body, {})) as gateway:
        status, _, response = _run(_canonical(_fetch_request([41])), gateway, token_path=token_path)
    assert status != 0
    assert response["error"]["code"] == "invalid-worker-response"  # type: ignore[index]


@pytest.mark.parametrize(
    "unsafe",
    ["http://192.0.2.1:48175", "https://127.0.0.1:48175", "http://localhost:48175"],
)
def test_gateway_target_must_be_literal_loopback(unsafe: str) -> None:
    with pytest.raises(memory_exchange.ExchangeError, match="loopback"):
        memory_exchange.GatewayClient(unsafe, "t" * 43)


def test_gateway_client_repr_redacts_owner_token() -> None:
    client = memory_exchange.GatewayClient("http://127.0.0.1:48175", "t" * 43)
    assert "t" * 43 not in repr(client)
    assert "<redacted>" in repr(client)


@pytest.mark.parametrize("kind", ["world-readable", "symlink", "hardlink"])
def test_gateway_token_must_be_owner_private_and_single_link(
    private_home: pathlib.Path,
    kind: str,
) -> None:
    memoryctl._atomic_private_json(
        memoryctl.install_receipt_path(), memoryctl.expected_install_receipt()
    )
    path = private_home / ".config" / "agentic-memory" / "gateway-token"
    path.parent.mkdir(parents=True, mode=0o700)
    real = private_home / "real-token"
    real.write_text("t" * 43 + "\n", encoding="ascii")
    real.chmod(0o600)
    if kind == "world-readable":
        real.chmod(0o644)
        path = real
    elif kind == "symlink":
        path.symlink_to(real)
    else:
        os.link(real, path)

    status, encoded, response = _run(_canonical(_fetch_request([41])), token_path=path)
    assert status != 0
    assert response["error"]["code"] == "gateway-unavailable"  # type: ignore[index]
    assert b"t" * 43 not in encoded
