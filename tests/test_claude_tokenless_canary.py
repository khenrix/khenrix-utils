"""Characterize Claude's no-key auth gate inside a disposable native boundary."""
from __future__ import annotations

import importlib.util
import json
import queue
import socketserver
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLAUDE = Path.home() / ".local" / "share" / "claude" / "versions" / "2.1.284"
SESSION_ID = "fd6f840b-4b79-4fd2-91d0-14d3d8fba655"


@pytest.fixture
def native_boundary():
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        pytest.skip("Claude native canary requires macOS sandbox-exec")
    if not CLAUDE.is_file():
        pytest.skip("installed Claude 2.1.284 is unavailable")
    spec = importlib.util.spec_from_file_location(
        "fanout_forge_fixtures", ROOT / "scripts" / "fanout_forge_fixtures.py"
    )
    assert spec and spec.loader
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    with tempfile.TemporaryDirectory(prefix="forge-claude-canary-", dir="/private/tmp") as raw:
        temporary = Path(raw)
        baseline = temporary / "baseline"
        consumer = temporary / "consumer"
        baseline.mkdir(mode=0o700)
        consumer.mkdir(mode=0o700)
        protected_marker = baseline / "protected-marker"
        protected_marker.write_text("private fixture\n", encoding="utf-8")
        boundary = fixtures.open_blindness_boundary(
            temporary / "campaign", baseline, consumer, resume=False,
        )
        workspace = boundary.run_root / "cases" / "forge-01" / "seats" / "claude"
        workspace.mkdir(mode=0o700, parents=True)
        (workspace / "sentinel").write_text("seat\n", encoding="utf-8")
        seat_env = fixtures.seat_environment(boundary, workspace, "claude")
        state = Path(seat_env["HOME"])
        policy = (fixtures._native_campaign_policy(boundary, workspace, state)
                  + " (deny network-outbound)")
        yield {"workspace": workspace, "state": state, "seat_env": seat_env,
               "policy": policy, "protected_marker": protected_marker}


def _sandbox_run(boundary: dict, program: str, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", boundary["policy"],
         "/usr/bin/python3", "-I", "-c", program, *args],
        cwd=boundary["workspace"],
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
             **boundary["seat_env"]},
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
        pytest.skip("nested tool sandbox cannot apply macOS sandbox-exec")
    return result


@pytest.fixture
def fake_anthropic(native_boundary: dict):
    accepted: queue.Queue[str] = queue.Queue()
    methods: queue.Queue[str] = queue.Queue()
    socket_path = native_boundary["state"] / "anthropic.sock"

    class FakeAnthropic(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def parse_request(self) -> bool:
            parsed = super().parse_request()
            if parsed:
                methods.put(self.command)
            return parsed

        def _reject(self) -> None:
            response = b'{"type":"error","error":{"type":"invalid_request_error","message":"fake endpoint only"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        do_POST = do_GET = do_CONNECT = _reject

    class UnixHTTPServer(socketserver.UnixStreamServer):
        allow_reuse_address = True

        def get_request(self):
            connection = super().get_request()
            accepted.put("accepted")
            return connection

    with UnixHTTPServer(str(socket_path), FakeAnthropic) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield socket_path, accepted, methods
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _probe_policy(boundary: dict, socket_path: Path) -> None:
    read = _sandbox_run(
        boundary,
        "import sys\ntry: open(sys.argv[1], 'rb').read(1)\n"
        "except PermissionError: print('denied')\nelse: print('readable')",
        str(boundary["protected_marker"]),
    )
    assert read.returncode == 0 and read.stdout.strip() == "denied", read
    network = _sandbox_run(
        boundary,
        "import socket\ntry: socket.create_connection(('203.0.113.1',443), .2)\n"
        "except OSError as error: print(error.errno)\nelse: print('connected')",
    )
    assert network.returncode == 0 and network.stdout.strip() in {"1", "13"}, network
    local = _sandbox_run(
        boundary,
        "import socket,sys\ns=socket.socket(socket.AF_UNIX)\n"
        "try: s.connect(sys.argv[1])\n"
        "except PermissionError: print('denied')\n"
        "else: print('connected')",
        str(socket_path),
    )
    assert local.returncode == 0 and local.stdout.strip() == "denied", local


def _run_claude(boundary: dict, *options: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
           **boundary["seat_env"],
           "CLAUDE_CODE_TMPDIR": str(boundary["state"] / "tmp"),
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    command = ["/usr/bin/sandbox-exec", "-p", boundary["policy"], str(CLAUDE),
               *options]
    return subprocess.run(command, cwd=boundary["workspace"], env=env,
                          capture_output=True, text=True, timeout=20)


def test_claude_bare_without_key_stops_before_transport(
    native_boundary: dict, fake_anthropic,
) -> None:
    socket_path, accepted, methods = fake_anthropic
    _probe_policy(native_boundary, socket_path)
    version = _run_claude(native_boundary, "--version")
    assert version.returncode == 0, (version.stdout, version.stderr)
    assert version.stdout.strip() == "2.1.284 (Claude Code)"
    initial = _run_claude(
        native_boundary,
        "--bare", "--restricted", "--strict-mcp-config", "--tools", "",
        "--permission-mode", "plan", "--permission-prompts", "none",
        "--print", "--output-format", "json", "--model", "claude-sonnet-4-5",
        "--session-id", SESSION_ID, "first canary",
    )
    assert initial.returncode == 1, (initial.stdout[-1000:], initial.stderr[-1000:])
    assert initial.stdout, initial.stderr[-1000:]
    result = json.loads(initial.stdout)
    assert result["session_id"] == SESSION_ID
    assert "Not logged in" in result["result"]
    assert accepted.empty(), list(accepted.queue)
    assert methods.empty(), list(methods.queue)
