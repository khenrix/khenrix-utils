"""Credential-free guest socket relay and seat-local loopback HTTP bridge.

The host opens and authenticates the SSH stdio session. This program accepts
only its inherited pipes and the one fixed per-seat Unix socket; the bridge
inside a networkless seat connects only to its mounted broker socket.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import http.client
import json
import os
import re
import secrets
import select
import socket
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import UnixStreamServer
from typing import Callable


GUEST_SOCKET_ROOT = "/run/fanout"
BROKER_SOCKET = "/run/fanout/broker.sock"
MAX_FRAME_BYTES = 2_000_000
REQUEST_DEADLINE = 10.0
RESPONSE_DEADLINE = 600.0
_CHUNK_BYTES = 48 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class RelayRejected(ValueError):
    pass


def seat_socket_path(seat_id: str) -> str:
    if not isinstance(seat_id, str) or _ID.fullmatch(seat_id) is None or len(seat_id) > 90:
        raise ValueError("relay seat identity is invalid")
    return f"{GUEST_SOCKET_ROOT}/{seat_id}.sock"


def _safe_path(path: str) -> bool:
    return (isinstance(path, str) and path.startswith("/") and not path.startswith("//")
            and len(path) <= 256 and "://" not in path
            and all(32 <= ord(char) < 127 for char in path))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RelayRejected("relay frame has duplicate fields")
        result[key] = value
    return result


class FrameStream:
    def __init__(self, reader, writer, *, timeout=600.0, write_timeout=10.0):
        if not 0.1 <= timeout <= 600 or not 0.01 <= write_timeout <= 60:
            raise RelayRejected("relay frame deadline is invalid")
        self.reader = reader
        self.writer = writer
        self.timeout = timeout
        self.write_timeout = write_timeout

    def _read(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RelayRejected("relay frame deadline expired")
            try:
                fd = self.reader.fileno()
            except (AttributeError, OSError):
                fd = None
            if fd is not None:
                if not select.select([fd], [], [], remaining)[0]:
                    raise RelayRejected("relay frame deadline expired")
                piece = os.read(fd, size - len(data))
            else:
                piece = self.reader.read(size - len(data))
            if not piece:
                raise RelayRejected("relay frame closed")
            data.extend(piece)
        return bytes(data)

    def recv(self, *, deadline=None):
        limit = time.monotonic() + self.timeout
        if deadline is not None:
            limit = min(limit, deadline)
        size = int.from_bytes(self._read(4, limit), "big")
        if not 0 < size <= MAX_FRAME_BYTES:
            raise RelayRejected("relay frame exceeds limit")
        try:
            frame = json.loads(self._read(size, limit), object_pairs_hook=_pairs)
        except (UnicodeError, ValueError) as error:
            raise RelayRejected("relay frame is invalid") from error
        if not isinstance(frame, dict):
            raise RelayRejected("relay frame is invalid")
        return frame

    def send(self, frame):
        raw = json.dumps(frame, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
        if not 0 < len(raw) <= MAX_FRAME_BYTES:
            raise RelayRejected("relay frame exceeds limit")
        pending = memoryview(len(raw).to_bytes(4, "big") + raw)
        try:
            try:
                fd = self.writer.fileno()
            except (AttributeError, OSError):
                fd = None
            if fd is None:
                while pending:
                    written = self.writer.write(pending)
                    if not isinstance(written, int) or written <= 0:
                        raise RelayRejected("relay frame write failed")
                    pending = pending[written:]
                self.writer.flush()
            else:
                was_blocking = os.get_blocking(fd)
                os.set_blocking(fd, False)
                deadline = time.monotonic() + self.write_timeout
                try:
                    while pending:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                            raise RelayRejected("relay frame write deadline expired")
                        try:
                            written = os.write(fd, pending[:_CHUNK_BYTES])
                        except BlockingIOError:
                            continue
                        if written <= 0:
                            raise RelayRejected("relay frame write failed")
                        pending = pending[written:]
                finally:
                    os.set_blocking(fd, was_blocking)
        except OSError as error:
            raise RelayRejected("relay frame write failed") from error


def _request_headers(handler: BaseHTTPRequestHandler, max_bytes: int) -> tuple[dict[str, str], bytes]:
    deadline = handler._request_deadline
    if handler.headers.get("Transfer-Encoding") is not None:
        raise RelayRejected("relay request framing is invalid")
    lengths = handler.headers.get_all("Content-Length", [])
    if len(lengths) != 1 or re.fullmatch(r"[0-9]{1,8}", lengths[0]) is None:
        raise RelayRejected("relay request framing is invalid")
    length = int(lengths[0])
    if not 0 < length <= max_bytes:
        raise RelayRejected("relay request exceeds limit")
    headers = {}
    for name, value in handler.headers.items():
        name = name.lower()
        if (name in headers or len(headers) >= 32 or len(name) > 64
                or len(value) > 4096 or "\r" in value or "\n" in value):
            raise RelayRejected("relay request headers are invalid")
        headers[name] = value
    body = bytearray()
    while len(body) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RelayRejected("relay request deadline expired")
        handler.connection.settimeout(min(10, remaining))
        chunk = handler.rfile.read1(min(_CHUNK_BYTES, length - len(body)))
        if not chunk:
            raise RelayRejected("relay request body is incomplete")
        body.extend(chunk)
    if time.monotonic() > deadline:
        raise RelayRejected("relay request deadline expired")
    handler._request_timer.cancel()
    return headers, bytes(body)


def _shutdown_socket(connection: socket.socket) -> None:
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _start_request_timer(handler: BaseHTTPRequestHandler) -> None:
    handler._request_deadline = time.monotonic() + REQUEST_DEADLINE
    handler._request_timer = threading.Timer(REQUEST_DEADLINE + 0.1,
                                              _shutdown_socket, args=(handler.request,))
    handler._request_timer.daemon = True
    handler._request_timer.start()


def _refuse(handler: BaseHTTPRequestHandler):
    handler.close_connection = True
    handler.send_response(403)
    handler.send_header("Content-Length", "0")
    handler.send_header("Connection", "close")
    handler.end_headers()


def _reply_start(handler: BaseHTTPRequestHandler, status: int, mime: str):
    handler.close_connection = True
    handler.send_response(status)
    if mime:
        handler.send_header("Content-Type", mime)
    handler.send_header("Transfer-Encoding", "chunked")
    handler.send_header("Connection", "close")
    handler.end_headers()


def _reply_chunk(handler: BaseHTTPRequestHandler, chunk: bytes):
    if chunk:
        handler.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
        handler.wfile.flush()


class GuestRelay(UnixStreamServer):
    """One guest-local Unix socket, serialized against one SSH stdio stream."""

    allow_reuse_address = False

    def handle_error(self, _request, _client_address):
        pass

    def __init__(self, seat_id: str, permit_id: str, epoch: int, frames: FrameStream,
                 *, max_requests: int, max_request_bytes: int, max_response_bytes: int):
        if (not isinstance(permit_id, str) or _SHA.fullmatch(permit_id) is None
                or type(epoch) is not int or epoch < 1
                or type(max_requests) is not int or not 1 <= max_requests <= 34
                or type(max_request_bytes) is not int or not 1 <= max_request_bytes <= 1024 * 1024
                or type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 8 * 1024 * 1024):
            raise RelayRejected("relay permit budget is invalid")
        self.seat_id = seat_id
        self.permit_id = permit_id
        self.epoch = epoch
        self.frames = frames
        self.max_requests = max_requests
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.used = 0
        path = seat_socket_path(seat_id)
        if os.path.lexists(path):
            raise RelayRejected("relay seat socket already exists")
        super().__init__(path, _GuestHandler, bind_and_activate=True)
        os.chmod(path, 0o600)
        info = os.lstat(path)
        self._bound_identity = (info.st_dev, info.st_ino)
        self.timeout = 600.0

    def serve(self):
        try:
            self.frames.send({"type": "ready", "seat_id": self.seat_id,
                              "permit_id": self.permit_id, "epoch": self.epoch})
            if self.frames.recv() != {"type": "ready_ack"}:
                raise RelayRejected("relay readiness was not admitted")
            # Keep the exhausted socket attested until one final local refusal.
            while self.used <= self.max_requests:
                prior = self.used
                self.handle_request()
                if self.used == prior:
                    break
            self.frames.send({"type": "close"})
            if self.frames.recv() != {"type": "closed"}:
                raise RelayRejected("relay closure was not acknowledged")
        finally:
            self.server_close()
            try:
                info = os.lstat(self.server_address)
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISSOCK(info.st_mode) and (
                        info.st_dev, info.st_ino) == self._bound_identity:
                    os.unlink(self.server_address)


class _GuestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.request.settimeout(10)
        super().setup()
        _start_request_timer(self)

    def finish(self):
        self._request_timer.cancel()
        try:
            super().finish()
        except OSError:
            pass

    def log_message(self, *_args):
        pass

    def do_POST(self):
        relay: GuestRelay = self.server
        if relay.used >= relay.max_requests:
            _refuse(self)
            return
        relay.used += 1
        try:
            if not _safe_path(self.path):
                raise RelayRejected("relay path is invalid")
            headers, body = _request_headers(self, relay.max_request_bytes)
            if not re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", headers.get("host", "")):
                raise RelayRejected("relay Host differs")
            request_id = secrets.token_hex(16)
            relay.frames.send({"type": "request", "permit_id": relay.permit_id,
                               "epoch": relay.epoch, "request_id": request_id,
                               "method": "POST", "provider_path": self.path,
                               "headers": headers, "body_b64": base64.b64encode(body).decode()})
            deadline = time.monotonic() + RESPONSE_DEADLINE
            start = relay.frames.recv(deadline=deadline)
            if start == {"type": "reject", "status": 403}:
                _refuse(self)
                return
            if (start.keys() != {"type", "request_id", "status", "headers"}
                    or start["type"] != "response_start" or start["request_id"] != request_id
                    or type(start["status"]) is not int or not 200 <= start["status"] <= 599
                    or 300 <= start["status"] < 400 or not isinstance(start["headers"], dict)
                    or start["headers"].keys() - {"content-type"}):
                raise RelayRejected("relay response start is invalid")
            mime = start["headers"].get("content-type", "")
            if not isinstance(mime, str) or mime not in {"", "application/json", "text/event-stream"}:
                raise RelayRejected("relay response MIME is invalid")
            _reply_start(self, start["status"], mime)
            count = 0
            digest = hashlib.sha256()
            while True:
                frame = relay.frames.recv(deadline=deadline)
                if (frame.keys() == {"type", "request_id", "response_digest"}
                        and frame["type"] == "response_end"
                        and frame["request_id"] == request_id):
                    if frame["response_digest"] != digest.hexdigest():
                        raise RelayRejected("relay response digest differs")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    if time.monotonic() > deadline:
                        raise RelayRejected("relay response deadline expired")
                    relay.frames.send({"type": "response_ack", "request_id": request_id,
                                       "response_digest": digest.hexdigest()})
                    return
                if (frame.keys() != {"type", "request_id", "body_b64"}
                        or frame["type"] != "response_chunk" or frame["request_id"] != request_id
                        or not isinstance(frame["body_b64"], str)
                        or len(frame["body_b64"]) > _CHUNK_BYTES * 4 // 3 + 8):
                    raise RelayRejected("relay response chunk is invalid")
                try:
                    chunk = base64.b64decode(frame["body_b64"], validate=True)
                except (binascii.Error, ValueError) as error:
                    raise RelayRejected("relay response chunk is invalid") from error
                count += len(chunk)
                if count > relay.max_response_bytes:
                    raise RelayRejected("relay response exceeds limit")
                digest.update(chunk)
                _reply_chunk(self, chunk)
                if time.monotonic() > deadline:
                    raise RelayRejected("relay response deadline expired")
        except (RelayRejected, OSError, TimeoutError):
            if not self.close_connection:
                _refuse(self)
            self.close_connection = True

    do_GET = do_PUT = do_DELETE = do_CONNECT = do_OPTIONS = do_HEAD = _refuse


def _connect_broker(path: str) -> socket.socket:
    if path != BROKER_SOCKET:
        raise RelayRejected("relay broker socket differs")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(10)
    connection.connect(path)
    return connection


class LoopbackBridge(HTTPServer):
    """Seat-side HTTP endpoint with exactly one mounted Unix destination."""

    allow_reuse_address = False

    def handle_error(self, _request, _client_address):
        pass

    def __init__(self, *, port: int, connector: Callable[[str], socket.socket] = _connect_broker,
                 max_request_bytes: int = 1024 * 1024,
                 max_response_bytes: int = 8 * 1024 * 1024):
        if (type(port) is not int or not 0 <= port <= 65535
                or type(max_request_bytes) is not int or not 1 <= max_request_bytes <= 1024 * 1024
                or type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 8 * 1024 * 1024):
            raise RelayRejected("relay bridge budget is invalid")
        self.broker_socket_path = BROKER_SOCKET
        self.connector = connector
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        super().__init__(("127.0.0.1", port), _BridgeHandler)


class _BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.request.settimeout(10)
        super().setup()
        _start_request_timer(self)

    def finish(self):
        self._request_timer.cancel()
        try:
            super().finish()
        except OSError:
            pass

    def log_message(self, *_args):
        pass

    def do_POST(self):
        bridge: LoopbackBridge = self.server
        try:
            if not _safe_path(self.path):
                raise RelayRejected("relay path is invalid")
            headers, body = _request_headers(self, bridge.max_request_bytes)
            if headers.get("host") != f"127.0.0.1:{bridge.server_address[1]}":
                raise RelayRejected("relay Host differs")
            connection = bridge.connector(bridge.broker_socket_path)
            response_timer = None
            try:
                connection.settimeout(10)
                safe_headers = {name: value for name, value in headers.items()
                                if name not in {"connection", "transfer-encoding"}}
                request = (f"POST {self.path} HTTP/1.1\r\n" +
                           "".join(f"{name}: {value}\r\n" for name, value in safe_headers.items()) +
                           "Connection: close\r\n\r\n").encode("ascii") + body
                connection.sendall(request)
                upstream = http.client.HTTPResponse(connection)
                deadline = time.monotonic() + RESPONSE_DEADLINE
                response_timer = threading.Timer(RESPONSE_DEADLINE + 0.1,
                                                  _shutdown_socket, args=(connection,))
                response_timer.daemon = True
                response_timer.start()
                connection.settimeout(max(0.01, deadline - time.monotonic()))
                upstream.begin()
                if time.monotonic() > deadline:
                    raise RelayRejected("relay response deadline expired")
                if (not 200 <= upstream.status <= 599 or 300 <= upstream.status < 400
                        or upstream.getheader("Content-Encoding") is not None
                        or upstream.getheader("Location") is not None):
                    raise RelayRejected("relay response is invalid")
                mime = upstream.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
                if mime not in {"", "application/json", "text/event-stream"}:
                    raise RelayRejected("relay response MIME is invalid")
                _reply_start(self, upstream.status, mime)
                count = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RelayRejected("relay response deadline expired")
                    connection.settimeout(remaining)
                    chunk = upstream.read1(_CHUNK_BYTES)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > bridge.max_response_bytes:
                        raise RelayRejected("relay response exceeds limit")
                    _reply_chunk(self, chunk)
                    if time.monotonic() > deadline:
                        raise RelayRejected("relay response deadline expired")
                if time.monotonic() > deadline:
                    raise RelayRejected("relay response deadline expired")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            finally:
                if response_timer is not None:
                    response_timer.cancel()
                connection.close()
        except (RelayRejected, OSError, TimeoutError, http.client.HTTPException, UnicodeError):
            if not self.close_connection:
                _refuse(self)
            self.close_connection = True

    do_GET = do_PUT = do_DELETE = do_CONNECT = do_OPTIONS = do_HEAD = _refuse


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Finite container broker relay")
    modes = parser.add_subparsers(dest="mode", required=True)
    guest = modes.add_parser("guest-stdio")
    guest.add_argument("--seat-id", required=True)
    guest.add_argument("--permit-id", required=True)
    guest.add_argument("--epoch", required=True, type=int)
    guest.add_argument("--max-requests", required=True, type=int)
    guest.add_argument("--max-request-bytes", required=True, type=int)
    guest.add_argument("--max-response-bytes", required=True, type=int)
    bridge = modes.add_parser("bridge")
    bridge.add_argument("--port", required=True, type=int)
    bridge.add_argument("--max-request-bytes", required=True, type=int)
    bridge.add_argument("--max-response-bytes", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        if args.mode == "guest-stdio":
            if os.getuid() != 65532 or Path(GUEST_SOCKET_ROOT).is_symlink():
                raise RelayRejected("relay guest identity is invalid")
            frames = FrameStream(sys.stdin.buffer, sys.stdout.buffer)
            GuestRelay(args.seat_id, args.permit_id, args.epoch, frames,
                       max_requests=args.max_requests,
                       max_request_bytes=args.max_request_bytes,
                       max_response_bytes=args.max_response_bytes).serve()
        else:
            with LoopbackBridge(port=args.port,
                                max_request_bytes=args.max_request_bytes,
                                max_response_bytes=args.max_response_bytes) as server:
                server.serve_forever()
    except (RelayRejected, OSError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
