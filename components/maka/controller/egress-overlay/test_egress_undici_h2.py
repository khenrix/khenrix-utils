#!/usr/bin/env python3
"""Exercise Maka's pinned WebSocket-to-Undici fallback against an H2 origin."""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import shutil
import socket
import socketserver
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
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, RequestReceived, StreamEnded

DUMMY = b"integration-dummy-secret"
STATE = pathlib.Path("/opt/maka-egress-state")
SOCKET = pathlib.Path("/run/maka-secret/key.sock")
CLIENT = "/opt/maka-eval/test_egress_undici_h2_client.mjs"
received: list[dict[str, str]] = []


class H2Origin(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        if self.request.selected_alpn_protocol() != "h2":
            self.request.settimeout(0.5)
            try:
                self.request.recv(1)
            except TimeoutError:
                pass
            return

        connection = H2Connection(
            config=H2Configuration(client_side=False, header_encoding="utf-8")
        )
        # Emit server SETTINGS immediately. This is the production ordering
        # that exposed the proxy's server-first next-layer race.
        connection.initiate_connection()
        self.request.sendall(connection.data_to_send())
        self.request.settimeout(5)
        headers_by_stream: dict[int, dict[str, str]] = {}
        while True:
            payload = self.request.recv(65535)
            if not payload:
                return
            completed_stream: int | None = None
            for event in connection.receive_data(payload):
                if isinstance(event, RequestReceived):
                    headers_by_stream[event.stream_id] = dict(event.headers)
                elif isinstance(event, DataReceived):
                    connection.acknowledge_received_data(
                        event.flow_controlled_length, event.stream_id
                    )
                elif isinstance(event, StreamEnded):
                    completed_stream = event.stream_id
            if completed_stream is not None:
                request_headers = headers_by_stream[completed_stream]
                received.append(request_headers)
                body = b'{"ok":true}'
                connection.send_headers(
                    completed_stream,
                    [
                        (":status", "200"),
                        ("content-type", "application/json"),
                        ("content-length", str(len(body))),
                    ],
                )
                connection.send_data(completed_stream, body, end_stream=True)
            pending = connection.data_to_send()
            if pending:
                self.request.sendall(pending)
            if completed_stream is not None:
                return


class H2Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

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
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("api.openai.com")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path = directory / "origin.key"
    cert_path = directory / "origin.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
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


with tempfile.TemporaryDirectory() as temporary:
    temp = pathlib.Path(temporary)
    cert_path, key_path = certificate(temp)
    origin = H2Server(("0.0.0.0", 443), H2Origin)
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(cert_path, key_path)
    tls_context.set_alpn_protocols(["h2"])
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
        ["python", "/opt/maka-eval/keyed_mitmdump.py", "--ssl-insecure"],
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

        node_environment = dict(environment)
        node_environment["NODE_EXTRA_CA_CERTS"] = str(
            STATE / "mitmproxy-ca-cert.pem"
        )
        node = subprocess.run(
            ["/opt/maka-node-toolchain/bin/node", CLIENT],
            text=True,
            capture_output=True,
            env=node_environment,
            timeout=20,
        )
    finally:
        active_error = sys.exc_info()[0] is not None
        proxy.kill()
        output, errors = proxy.communicate(timeout=5)
        origin.shutdown()
        origin.server_close()
        broker_thread.join(timeout=2)
        if active_error and DUMMY not in errors:
            sys.stderr.write(output.decode("utf-8", errors="replace"))
            sys.stderr.write(errors.decode("utf-8", errors="replace"))

    audit = (STATE / "hits.jsonl").read_bytes()
    assert node.returncode == 0, (node.stdout, node.stderr)
    assert node.stdout.strip() == (
        "Pinned Undici WebSocket-to-H2 fallback integration passed"
    )
    assert node.stderr == ""
    assert received == [
        {
            ":method": "POST",
            ":scheme": "https",
            ":path": "/v1/responses",
            ":authority": "api.openai.com",
            "authorization": f"Bearer {DUMMY.decode()}",
            "content-type": "application/json",
            "accept": "*/*",
            "accept-language": "*",
            "sec-fetch-mode": "cors",
            "user-agent": "undici",
            "accept-encoding": "br, gzip, deflate, zstd",
            "content-length": "430",
        }
    ]
    assert DUMMY not in output
    assert DUMMY not in errors
    assert DUMMY not in audit
    assert DUMMY not in node.stdout.encode()
    assert DUMMY not in node.stderr.encode()
    records = [json.loads(line) for line in audit.splitlines()]
    assert [record["ruleId"] for record in records] == [
        "openai_websocket_disabled",
        "openai_authorized",
    ]
    assert [record["normalizedPath"] for record in records] == [
        "sequence=1",
        "sequence=1",
    ]

print("Pinned Undici HTTP/2 egress integration tests passed")
