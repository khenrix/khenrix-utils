"""Characterize installed agy model initialization with a disposable endpoint."""
from __future__ import annotations

import json
import importlib.util
import queue
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
import re


ROOT = Path(__file__).resolve().parents[1]
AGY = Path.home() / ".local" / "libexec" / "agy-bin"


def _policy(base: str, state: Path, workspace: Path,
            gateway_port: int, probe_port: int) -> str:
    rules = [base]
    for path in ("/Users", "/private/tmp", "/private/var/folders", "/Volumes"):
        rules.append(f"(deny file-read* (subpath {json.dumps(path)}))")
    rules.append(f"(allow file-read* (literal {json.dumps(str(AGY))}))")
    for path in (state, workspace):
        literal = json.dumps(str(path))
        rules.append(f"(allow file-read* (subpath {literal}))")
    rules.append("(deny network-outbound)")
    for port in (gateway_port, probe_port):
        rules.append(f'(allow network-outbound (remote ip "localhost:{port}"))')
    return " ".join(rules)


def _sandbox(policy: str, argv: list[str], *, env: dict[str, str],
             cwd: Path, input: str | None = None, timeout: int = 10,
             ) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", policy, *argv],
        cwd=cwd, env=env, input=input, capture_output=True, text=True, timeout=timeout,
    )


def _assert_boundary(policy: str, workspace: Path, protected: Path, port: int) -> None:
    source = (
        "import os,socket,sys\n"
        "s=socket.socket(); s.settimeout(1)\n"
        "print(s.connect_ex((sys.argv[1],int(sys.argv[2]))))\n"
        "try: open(sys.argv[3], 'rb').read(1)\n"
        "except PermissionError: print('read-denied')\n"
        "else: print('read-allowed')\n"
        "try: os.listdir(sys.argv[4])\n"
        "except PermissionError: print('home-denied')\n"
        "else: print('home-allowed')\n"
        "try: os.link(sys.argv[5],sys.argv[6])\n"
        "except PermissionError: print('link-denied')\n"
        "else: print('link-allowed')\n"
        "try: os.listdir('/private/tmp')\n"
        "except PermissionError: print('temp-denied')\n"
        "else: print('temp-allowed')\n"
        "try: os.listdir('/private/var/folders')\n"
        "except PermissionError: print('var-denied')\n"
        "else: print('var-allowed')\n"
        "try: open('/etc/hosts', 'rb').read(1)\n"
        "except PermissionError: print('hosts-denied')\n"
        "else: print('hosts-read')\n"
    )
    env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    for host, target_port, expected in (
        ("127.0.0.1", port, "0"), ("203.0.113.1", 443, "1"),
    ):
        result = _sandbox(
            policy, ["/usr/bin/python3", "-I", "-c", source, host,
                     str(target_port), str(protected), str(Path.home()),
                     str(workspace / "sentinel"), str(workspace / "hardlink")],
            env=env, cwd=workspace,
        )
        if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
            pytest.skip("nested tool sandbox cannot apply native Seatbelt")
        assert result.returncode == 0, f"Seatbelt probe exited {result.returncode}"
        assert result.stdout.splitlines() == [
            expected, "read-denied", "home-denied", "link-denied",
            "temp-denied", "var-denied", "hosts-read",
        ]


def test_agy_1_2_13_gemini_base_url_initial_reaches_fake_endpoint() -> None:
    """The documented Gemini API-key route must reach only our fake endpoint."""
    if sys.platform != "darwin" or not AGY.is_file():
        pytest.skip("installed macOS agy binary is required")

    accepted: queue.Queue[None] = queue.Queue()
    requests: queue.Queue[tuple[str, str, bool]] = queue.Queue()

    class LocalServer(ThreadingHTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            accepted.put(None)
            return connection, address

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def _respond(self) -> None:
            parsed = urlsplit(self.path)
            query = dict(parse_qsl(parsed.query))
            dummy_key = (
                self.headers.get("x-goog-api-key") == "dummy-canary-key"
                or query.get("key") == "dummy-canary-key"
            )
            requests.put((self.command, parsed.path, dummy_key))
            payload = b'{"error":{"message":"local canary stop"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _respond
        do_POST = _respond

    with tempfile.TemporaryDirectory(prefix="agy-tokenless-canary-", dir="/private/tmp") as raw:
        temporary = Path(raw)
        baseline = temporary / "baseline"
        consumer = temporary / "consumer"
        for path in (baseline, consumer):
            path.mkdir(mode=0o700)
        (baseline / "sentinel").write_text("public canary\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location(
            "fanout_forge_fixtures", ROOT / "scripts" / "fanout_forge_fixtures.py",
        )
        assert spec and spec.loader
        fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixtures)
        boundary = fixtures.open_blindness_boundary(
            temporary / "campaign", baseline, consumer, resume=False,
        )
        assert fixtures.open_blindness_boundary(
            temporary / "campaign", baseline, consumer, resume=True,
        ) == boundary
        workspace = boundary.run_root / "cases" / "forge-01" / "seats" / "agy"
        workspace.mkdir(mode=0o700, parents=True)
        state = Path(fixtures.seat_environment(boundary, workspace, "agy")["HOME"])
        assert dict(boundary.executable_paths)["agy"] == AGY
        (workspace / "sentinel").write_text("public workspace canary\n", encoding="utf-8")
        settings = state / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(mode=0o700, parents=True)
        settings.write_text(json.dumps({"modelProvider": "gemini"}), encoding="utf-8")
        settings.chmod(0o600)
        with socket.create_server(("127.0.0.1", 0)) as probe, \
                LocalServer(("127.0.0.1", 0), Gateway) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_port
                probe_port = probe.getsockname()[1]
                base = fixtures._native_campaign_policy(boundary, workspace, state)
                policy = _policy(base, state, workspace, port, probe_port)
                _assert_boundary(policy, workspace, baseline / "sentinel", probe_port)
                probe.settimeout(2)
                with probe.accept()[0]:
                    pass
                env = {
                    "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
                    "HOME": str(state), "XDG_CONFIG_HOME": str(state / ".config"),
                    "TMPDIR": str(state / "tmp"),
                    "GEMINI_API_KEY": "dummy-canary-key",
                    "GOOGLE_GEMINI_BASE_URL": f"http://127.0.0.1:{port}",
                    "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
                }
                version = _sandbox(policy, [str(AGY), "--version"],
                                   env=env, cwd=workspace)
                assert version.returncode == 0, version.stderr
                assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version.stdout.strip()), version.stdout
                prompt = json.dumps({"event": "user", "message": {"content": "canary"}}) + "\n"
                result = _sandbox(
                    policy, [str(AGY), "--new-project", "--mode", "plan",
                             "--input-format", "stream-json", "--output-format", "stream-json",
                             "--model", "gemini-3.8-flash-high", "--effort", "high",
                             "--log-file", str(state / "agy.log")],
                    env=env, cwd=workspace, input=prompt, timeout=12,
                )
                log_path = state / "agy.log"
                saw_model_panic = False
                if log_path.exists():
                    with log_path.open(errors="replace") as log_file:
                        saw_model_panic = any(
                            "panic: unknown model key MODEL_PLACEHOLDER_M50: model not found" in line
                            for line in log_file
                        )
            finally:
                server.shutdown()
                thread.join(timeout=5)
    assert not accepted.empty(), "Gemini base-URL initial route never reached the fake endpoint"
    observed = list(requests.queue)
    assert any(
        method == "POST"
        and path == "/v1beta/models/gemini-3.8-flash:streamGenerateContent"
        and dummy_key
        for method, path, dummy_key in observed
    ), f"fake Gemini model request absent: {observed!r}"
    assert not saw_model_panic, "obsolete gateway model-init panic recurred"
    assert result.returncode != 0, "fake HTTP 400 unexpectedly completed a provider turn"
