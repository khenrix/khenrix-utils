#!/usr/bin/env python3
"""Bearer-authenticated loopback gateway for local memory search and viewer access."""

from __future__ import annotations

import argparse
import http.client
import http.cookies
import http.server
import os
import pathlib
import secrets
import urllib.parse

from provider_relay import RelayError, read_private

MAX_BODY = 16 * 1024 * 1024


def make_handler(
    token_file: pathlib.Path,
    worker_port: int,
) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "khenrix-memory-gateway"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _token(self) -> str:
            return read_private(token_file, 256).decode("ascii").strip()

        def _authorized(self) -> bool:
            expected = self._token()
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer ") and secrets.compare_digest(header[7:], expected):
                return True
            cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            value = cookie.get("khenrix_memory")
            return bool(value and secrets.compare_digest(value.value, expected))

        def _json_error(self, status: int, message: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)

        def _proxy(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            supplied = query.get("token", [""])[0]
            if supplied and secrets.compare_digest(supplied, self._token()):
                self.send_response(302)
                self.send_header(
                    "Set-Cookie",
                    f"khenrix_memory={self._token()}; HttpOnly; SameSite=Strict; Path=/",
                )
                self.send_header("Location", parsed.path or "/")
                self.end_headers()
                return
            if not self._authorized():
                self._json_error(401, b'{"error":"unauthorized"}')
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY:
                self._json_error(413, b'{"error":"body_too_large"}')
                return
            body = self.rfile.read(length) if length else None
            connection = http.client.HTTPConnection("127.0.0.1", worker_port, timeout=60)
            try:
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in {"authorization", "cookie", "host", "content-length", "connection"}
                }
                if body is not None:
                    headers["Content-Length"] = str(len(body))
                connection.request(self.command, self.path, body=body, headers=headers)
                response = connection.getresponse()
                payload = response.read(MAX_BODY + 1)
                if len(payload) > MAX_BODY:
                    raise RelayError("upstream response too large")
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in {"connection", "content-length", "transfer-encoding", "set-cookie"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (OSError, http.client.HTTPException, RelayError):
                self._json_error(502, b'{"error":"memory_worker_unavailable"}')
            finally:
                connection.close()

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._json_error(200, b'{"service":"khenrix-memory-gateway","status":"ok"}')
            else:
                self._proxy()

        def do_POST(self) -> None:
            self._proxy()

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=pathlib.Path, required=True)
    parser.add_argument("--worker-port", type=int, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    read_private(args.token_file, 256)
    os.umask(0o077)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", args.port), make_handler(args.token_file, args.worker_port)
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
