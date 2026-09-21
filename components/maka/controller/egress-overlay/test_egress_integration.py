#!/usr/bin/env python3
"""Exercise the pinned proxy with aligned and host-confused TLS requests."""

from __future__ import annotations

import datetime
import http.server
import json
import os
import pathlib
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

DUMMY = b"integration-dummy-secret"
STATE = pathlib.Path("/opt/maka-egress-state")
SOCKET = pathlib.Path("/run/maka-secret/key.sock")
valid_body = json.dumps({
    "model": "gpt-5.6-sol",
    "reasoning": {"effort": "xhigh", "summary": "auto"},
    "stream": True,
    "store": False,
    "parallel_tool_calls": True,
    "include": ["reasoning.encrypted_content"],
    "input": "synthetic integration prompt",
    "tools": [{
        "type": "function",
        "name": "Read",
        "description": "Read one synthetic path",
        "parameters": {"type": "object", "properties": {}},
    }, {"type": "apply_patch"}],
    "tool_choice": "auto",
    "prompt_cache_key": "maka:synthetic-integration",
}).encode()
received: list[dict[str, str]] = []


class Origin(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        received.append({
            "host": self.headers.get("host", ""),
            "authorization": self.headers.get("authorization", ""),
            "path": self.path,
        })
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


class QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        return


def certificate(directory: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "api.openai.com")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("api.openai.com"),
            x509.DNSName("attacker.test"),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path = directory / "origin.key"
    cert_path = directory / "origin.pem"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def broker(ready: threading.Event) -> None:
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(SOCKET))
    listener.listen(1)
    ready.set()
    connection, _ = listener.accept()
    with connection:
        connection.sendall(DUMMY)
    listener.close()
    SOCKET.unlink(missing_ok=True)


def receive_headers(connection: socket.socket) -> tuple[int, bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data and len(data) < 65536:
        chunk = connection.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    first = bytes(data).split(b"\r\n", 1)[0]
    parts = first.split()
    if len(parts) < 2:
        raise RuntimeError(f"invalid proxy response: {first!r}")
    return int(parts[1]), bytes(data)


def tunnel(
    target: str,
    connect_host: str,
    *,
    tls_sni: str | None = None,
    inner_host: str | None = None,
    websocket: bool = False,
) -> tuple[int, int | None, bytes]:
    raw = socket.create_connection(("127.0.0.1", 8080), timeout=5)
    raw.settimeout(5)
    raw.sendall(
        f"CONNECT {target}:443 HTTP/1.1\r\nHost: {connect_host}:443\r\n\r\n".encode()
    )
    connect_status, connect_data = receive_headers(raw)
    if connect_status != 200:
        raw.close()
        return connect_status, None, connect_data
    context = ssl.create_default_context(cafile=str(STATE / "mitmproxy-ca-cert.pem"))
    secure = context.wrap_socket(raw, server_hostname=tls_sni or target)
    host = inner_host or target
    if websocket:
        secure.sendall(
            b"GET /v1/responses HTTP/1.1\r\n"
            + f"Host: {host}\r\n".encode()
            + b"Authorization: Bearer maka-decoy-integration123\r\n"
            + b"Connection: Upgrade\r\n"
            + b"Upgrade: websocket\r\n"
            + b"Sec-WebSocket-Version: 13\r\n"
            + b"Sec-WebSocket-Key: MDEyMzQ1Njc4OWFiY2RlZg==\r\n"
            + b"OpenAI-Beta: responses_websockets=2026-02-06\r\n\r\n"
        )
    else:
        body = valid_body
        secure.sendall(
            b"POST /v1/responses HTTP/1.1\r\n"
            + f"Host: {host}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Authorization: Bearer maka-decoy\r\nConnection: close\r\n\r\n"
            + body
        )
    inner_status, response = receive_headers(secure)
    secure.close()
    return connect_status, inner_status, response


with tempfile.TemporaryDirectory() as temporary:
    temp = pathlib.Path(temporary)
    cert_path, key_path = certificate(temp)
    origin = QuietServer(("0.0.0.0", 443), Origin)
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(cert_path, key_path)
    origin.socket = tls_context.wrap_socket(origin.socket, server_side=True)
    origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
    origin_thread.start()

    shutil.rmtree(STATE, ignore_errors=True)
    STATE.mkdir(mode=0o700)
    broker_ready = threading.Event()
    broker_thread = threading.Thread(target=broker, args=(broker_ready,), daemon=True)
    broker_thread.start()
    assert broker_ready.wait(2)
    environment = dict(os.environ)
    environment["MAKA_EVAL_EGRESS_AUDIT"] = str(STATE / "hits.jsonl")
    environment["MAKA_EVAL_PROXY_TEST_VERBOSE"] = "1"
    proxy = subprocess.Popen(
        [
            "python", "/opt/maka-eval/keyed_mitmdump.py", "--ssl-insecure",
            "--set", "termlog_verbosity=debug",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proxy.poll() is not None:
                raise RuntimeError("proxy exited during integration startup")
            if (STATE / "mitmproxy-ca-cert.pem").is_file():
                try:
                    socket.create_connection(("127.0.0.1", 8080), timeout=0.2).close()
                    break
                except OSError:
                    pass
            time.sleep(0.1)
        else:
            raise RuntimeError("proxy did not become ready")

        ws_probe = tunnel("api.openai.com", "api.openai.com", websocket=True)
        assert ws_probe[:2] == (200, 403), ws_probe[:2]
        assert b"x-maka-eval-egress-rule: openai_websocket_disabled" in ws_probe[2].lower()
        assert received == []

        valid = tunnel("api.openai.com", "api.openai.com")
        assert valid[:2] == (200, 200), valid[:2]
        assert received == [{
            "host": "api.openai.com",
            "authorization": f"Bearer {DUMMY.decode()}",
            "path": "/v1/responses",
        }]

        connect_spoof = tunnel("attacker.test", "api.openai.com")
        assert connect_spoof[0] == 421, connect_spoof[0]
        inner_spoof = tunnel("attacker.test", "attacker.test", inner_host="api.openai.com")
        assert inner_spoof[:2] == (200, 421), inner_spoof[:2]
        sni_spoof = tunnel("api.openai.com", "api.openai.com", tls_sni="attacker.test")
        assert sni_spoof[0] in {200, 421}
        if sni_spoof[0] == 200:
            assert sni_spoof[1] == 421, sni_spoof[:2]
        assert len(received) == 1
    finally:
        active_error = sys.exc_info()[0] is not None
        proxy.terminate()
        try:
            output, errors = proxy.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
            output, errors = proxy.communicate(timeout=5)
        origin.shutdown()
        origin.server_close()
        broker_thread.join(timeout=2)
        if active_error and DUMMY not in errors:
            sys.stderr.write(output.decode("utf-8", errors="replace"))
            sys.stderr.write(errors.decode("utf-8", errors="replace"))

    audit = (STATE / "hits.jsonl").read_bytes() if (STATE / "hits.jsonl").exists() else b""
    assert DUMMY not in output
    assert DUMMY not in errors
    assert DUMMY not in audit
    assert b"attacker.test" not in audit
    records = [json.loads(line) for line in audit.splitlines()]
    assert records[0]["ruleId"] == "openai_websocket_disabled"
    assert records[0]["normalizedPath"] == "sequence=1"
    assert records[1]["ruleId"] == "openai_authorized"
    assert records[1]["normalizedPath"] == "sequence=1"

print("OpenAI proxy TLS host-confusion integration tests passed")
