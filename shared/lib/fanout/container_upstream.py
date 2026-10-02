"""Host-only, permit-pinned HTTPS sender for the existing fanout broker."""
from __future__ import annotations

import http.client
import hashlib
import json
import math
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path

from .broker import BrokerPermit, BrokerRejected, BrokerResponse, _json_object, _safe_response_body, _safe_stream_body
from .container_routes import PreparedOutbound, RouteAdmission, RouteRejected, _validate_profile
from .container_transfer_support import canonical_json


_AUTH = {"claude": "x-api-key", "codex": "authorization", "agy": "x-goog-api-key"}
_AUTH_NAMES = frozenset(_AUTH.values())
_FORWARD_NAMES = frozenset({
    "content-type", "accept", "anthropic-version", "anthropic-beta", "openai-beta",
})
_READ_BYTES = 64 * 1024
_FAILURE_PHASES = frozenset({"tls_setup", "connect", "request_write", "response_headers", "error_body", "json_body", "sse_body"})
_FAILURE_CATEGORIES = frozenset({"transport_failure", "deadline_expired", "non2xx", "invalid_status", "redirect_or_location", "unsafe_encoding", "unsupported_media", "response_budget"})
_FAILURE_OMISSIONS = frozenset({"unsafe_body", "wrong_shape", "body_budget", "body_read_failure", "invalid_status", "redirect_or_location", "unsafe_encoding", "unsupported_media"})


def _error_projection(body: bytes, credential: str) -> tuple[dict[str, object] | None, str | None]:
    try:
        safe = _safe_response_body(body, "application/json", credential)
        document = _json_object(safe)
        json.dumps(document, allow_nan=False)
    except (BrokerRejected, ValueError, UnicodeError, RecursionError):
        return None, "unsafe_body"
    error = document.get("error")
    if type(error) is not dict:
        return None, "wrong_shape"
    projected = {}
    for key in ("code", "type", "status", "message"):
        value = error.get(key)
        if key == "code" and type(value) is int and len(canonical_json(value)) <= 128:
            projected[key] = value
        elif type(value) is str:
            limit = 4096 if key == "message" else 128
            if len(value.encode("utf-8")) <= limit and (key == "message" or value.isascii()):
                projected[key] = value
    return (projected, None) if projected else (None, "wrong_shape")


def _system_tls_context() -> ssl.SSLContext:
    defaults = ssl.get_default_verify_paths()
    cafile = defaults.openssl_cafile
    capath = defaults.openssl_capath
    cafile = cafile if cafile and Path(cafile).is_file() else None
    capath = capath if capath and Path(capath).is_dir() else None
    if cafile is None and capath is None:
        raise BrokerRejected("host upstream TLS trust is unavailable")
    # Explicit OpenSSL defaults ignore SSL_CERT_FILE and SSL_CERT_DIR overrides.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=cafile, capath=capath)
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise BrokerRejected("host upstream TLS verification is unavailable")
    return context


def _remaining(connection: http.client.HTTPSConnection, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BrokerRejected("host upstream deadline expired")
    if connection.sock is not None:
        connection.sock.settimeout(remaining)


def _read_piece(connection: http.client.HTTPSConnection,
                response: http.client.HTTPResponse, deadline: float,
                amount: int, *, incremental: bool = False) -> bytes:
    _remaining(connection, deadline)
    piece = response.read1(amount) if incremental else response.read(amount)
    if time.monotonic() >= deadline:
        raise BrokerRejected("host upstream deadline expired")
    if not isinstance(piece, bytes):
        raise BrokerRejected("host upstream response is invalid")
    return piece


def _close(resource: object) -> None:
    try:
        resource.close()
    except Exception:
        pass


class _TransportLease:
    """Close both socket and detached response when the absolute deadline arrives."""

    def __init__(self, connection: http.client.HTTPSConnection, deadline: float):
        self.connection = connection
        self._response: http.client.HTTPResponse | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._timer = threading.Timer(max(0, deadline - time.monotonic()), self.close)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def attach(self, response: http.client.HTTPResponse) -> None:
        with self._lock:
            if self._closed:
                _close(response)
                raise BrokerRejected("host upstream deadline expired")
            self._response = response

    def close(self) -> None:
        with self._lock:
            self._closed = True
            response = self._response
            self._response = None
        self._timer.cancel()
        if response is not None:
            _close(response)
        # A timed-out DNS lookup can assign a socket after the timer fires.
        # The caller's final close handles that late socket too.
        _close(self.connection)


class _SSEBody:
    """Own the connection until the stream ends or its caller closes it."""

    def __init__(self, lease: _TransportLease,
                 response: http.client.HTTPResponse, limit: int, deadline: float,
                 failure: Callable[[str], None] | None = None,
                 success: Callable[[bytes], None] | None = None):
        self._lease = lease
        self._response = response
        self._limit = limit
        self._deadline = deadline
        self._total = 0
        self._closed = False
        self._failure = failure
        self._success = success
        self._captured_body = bytearray()

    def __iter__(self) -> "_SSEBody":
        return self

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        try:
            piece = _read_piece(self._lease.connection, self._response, self._deadline,
                                min(_READ_BYTES, self._limit - self._total + 1),
                                incremental=True)
            if not piece:
                if self._response.length not in (None, 0):
                    raise BrokerRejected("host upstream response framing is incomplete")
                if self._success is not None:
                    self._success(bytes(self._captured_body))
                self.close()
                raise StopIteration
            self._total += len(piece)
            if self._total > self._limit:
                if self._failure is not None: self._failure("response_budget")
                raise BrokerRejected("host upstream response exceeds byte budget")
            if self._success is not None:
                self._captured_body.extend(piece)
            return piece
        except StopIteration:
            raise
        except BrokerRejected:
            self.close()
            if self._failure is not None:
                self._failure("deadline_expired" if time.monotonic() >= self._deadline else "transport_failure")
            raise
        except Exception:
            self.close()
            if self._failure is not None:
                self._failure("deadline_expired" if time.monotonic() >= self._deadline else "transport_failure")
            raise BrokerRejected("host upstream stream failed") from None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._lease.close()

    def __del__(self) -> None:
        self.close()


class HostUpstream:
    """Callable transport for one sealed provider permit."""

    _failure_fields = frozenset({"method", "provider_path", "normalized_body_sha256", "upstream_body_sha256", "requested_model", "upstream_model", "upstream_route_digest", "upstream_origin", "upstream_path", "http_status", "phase", "category", "structured_error", "omission"})

    def __init__(self, permit: BrokerPermit, route: RouteAdmission, *, max_response_bytes: int,
                 deadline_seconds: float, failure_sink: Callable[[dict[str, object]], None] | None = None,
                 success_sink: Callable[[dict[str, object]], None] | None = None):
        if failure_sink is not None and not callable(failure_sink):
            raise BrokerRejected("host upstream failure sink is invalid")
        if success_sink is not None and not callable(success_sink):
            raise BrokerRejected("host upstream success sink is invalid")
        if not isinstance(permit, BrokerPermit):
            raise BrokerRejected("host upstream permit is invalid")
        if not isinstance(route, RouteAdmission):
            raise BrokerRejected("host upstream route is invalid")
        profile = route.profile
        try:
            descriptor = asdict(profile)
            descriptor["execution_classes"] = list(profile.execution_classes)
            _validate_profile(descriptor)
        except (RouteRejected, TypeError, ValueError):
            raise BrokerRejected("host upstream route is invalid") from None
        if profile.auth_family not in {"vertex-adc", "openai-keychain", "openai-1password"}:
            raise BrokerRejected("host upstream route has no direct-key sender")
        if (permit.provider != profile.provider or permit.model != profile.requested_model
                or permit.upstream_route_digest != route.digest
                or permit.upstream_origin != profile.origin
                or permit.upstream_model != profile.effective_model):
            raise BrokerRejected("host upstream route differs from permit")
        if (type(max_response_bytes) is not int or not 1 <= max_response_bytes
                <= permit.max_response_bytes):
            raise BrokerRejected("host upstream response budget is invalid")
        if (type(deadline_seconds) not in (int, float)
                or not math.isfinite(deadline_seconds)
                or not 0.1 <= deadline_seconds <= permit.response_deadline):
            raise BrokerRejected("host upstream deadline is invalid")
        self._permit = permit
        self._route = route
        self._max_response_bytes = max_response_bytes
        self._deadline_seconds = float(deadline_seconds)
        self._failure_sink = failure_sink
        self._success_sink = success_sink

    def _prepare(self, method: str, path: str, headers: Mapping[str, str],
                 body: bytes) -> tuple[PreparedOutbound, dict[str, str]]:
        expected_auth = _AUTH[self._permit.provider]
        if (method != "POST" or not isinstance(path, str) or not path.startswith("/")
                or path.startswith("//") or len(path) > 256 or "://" in path
                or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or not isinstance(body, bytes) or not body
                or len(body) > self._permit.max_request_bytes
                or not isinstance(headers, Mapping)):
            raise BrokerRejected("host upstream request is invalid")
        outgoing: dict[str, str] = {}
        for name, value in headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise BrokerRejected("host upstream headers are invalid")
            folded = name.lower()
            if (folded in outgoing or folded not in _FORWARD_NAMES | {expected_auth}
                    or "\r" in value or "\n" in value):
                raise BrokerRejected("host upstream headers are invalid")
            outgoing[folded] = value
        if outgoing.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise BrokerRejected("host upstream request is not JSON")
        profile = self._route.profile
        document = _json_object(body)
        try:
            json.dumps(document, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise BrokerRejected("host upstream request body is not safe JSON") from None
        if profile.provider == "claude":
            if path != "/v1/messages" or document.get("model") != profile.requested_model:
                raise BrokerRejected("host upstream route model differs")
            if (type(document.get("max_tokens")) is not int
                    or not 1 <= document["max_tokens"] <= self._permit.max_output_tokens):
                raise BrokerRejected("host upstream output budget differs")
            document.pop("model")
            document["anthropic_version"] = "vertex-2023-10-16"
            action = "streamRawPredict" if document.get("stream") is True else "rawPredict"
            target = (f"/v1/projects/{profile.project}/locations/{profile.location}"
                      f"/publishers/anthropic/models/{profile.effective_model}:{action}")
            body = canonical_json(document)
        elif profile.provider == "agy":
            expected = (f"/v1beta/models/{profile.wire_model}:streamGenerateContent?alt=sse")
            if path != expected or document.get("model", profile.wire_model) != profile.wire_model:
                raise BrokerRejected("host upstream route model differs")
            generation = document.get("generationConfig")
            limit = generation.get("maxOutputTokens") if isinstance(generation, dict) else None
            if type(limit) is not int or not 1 <= limit <= self._permit.max_output_tokens:
                raise BrokerRejected("host upstream output budget differs")
            if "model" in document:
                document.pop("model")
                body = canonical_json(document)
            target = (f"/v1/projects/{profile.project}/locations/{profile.location}"
                      f"/publishers/google/models/{profile.effective_model}"
                      ":streamGenerateContent?alt=sse")
        else:
            if path != "/v1/responses" or document.get("model") != profile.requested_model:
                raise BrokerRejected("host upstream route model differs")
            limit = document.get("max_output_tokens")
            if type(limit) is not int or not 1 <= limit <= self._permit.max_output_tokens:
                raise BrokerRejected("host upstream output budget differs")
            target = "/v1/responses"
        if target != self._permit.upstream_path:
            raise BrokerRejected("host upstream path differs from permit")
        if len(body) > self._permit.max_request_bytes:
            raise BrokerRejected("host upstream transformed request exceeds byte budget")
        return PreparedOutbound(profile.origin, target, body, profile.effective_model,
                                self._route.digest), outgoing

    def preview(self, method: str, path: str, headers: Mapping[str, str],
                body: bytes) -> PreparedOutbound:
        """Expose the exact effective request identity without resolving a credential."""
        return self._prepare(method, path, headers, body)[0]

    def __call__(self, method: str, path: str, headers: Mapping[str, str],
                 body: bytes) -> BrokerResponse:
        prepared, outgoing = self._prepare(method, path, headers, body)
        expected_auth = _AUTH[self._permit.provider]
        credential = outgoing.pop(expected_auth, "")
        if (not credential or credential in {self._permit.placeholder_key,
                                             f"Bearer {self._permit.placeholder_key}"}
                or any(name in outgoing for name in _AUTH_NAMES)
                or (expected_auth == "authorization" and (
                    not credential.startswith("Bearer ") or not credential[7:].strip()))
                or any(ord(char) < 32 or ord(char) == 127 for char in credential)):
            raise BrokerRejected("host upstream authentication differs from permit")
        if self._route.profile.auth_family == "vertex-adc":
            outgoing["authorization"] = f"Bearer {credential}"
            outgoing["x-goog-user-project"] = self._route.profile.project
        else:
            outgoing["authorization"] = credential
            outgoing["openai-project"] = self._route.profile.project
        deadline = time.monotonic() + self._deadline_seconds
        connection = None
        lease = None
        stream_returned = False
        phase = "tls_setup"
        status = None
        captured = False
        def capture_success(response_body: bytes, mime: str) -> None:
            if self._success_sink is None:
                return
            marker = credential[7:] if expected_auth == "authorization" else credential
            _safe_response_body(body, "application/json", marker)
            _safe_response_body(prepared.body, "application/json", marker)
            delivered = (b"".join(_safe_stream_body((response_body,), marker, self._max_response_bytes, deadline))
                         if mime == "text/event-stream" else _safe_response_body(response_body, mime, marker))
            if len(delivered) > self._max_response_bytes or time.monotonic() >= deadline:
                raise BrokerRejected("host upstream success exceeds proof budget")
            try:
                self._success_sink({
                    "method": method, "provider_path": path,
                    "normalized_body": body, "effective_body": prepared.body,
                    "upstream_origin": prepared.origin, "upstream_path": prepared.path,
                    "upstream_model": prepared.effective_model,
                    "upstream_route_digest": prepared.route_digest,
                    "http_status": status, "content_type": mime,
                    "response_body": response_body, "delivered_body": delivered,
                })
            except BaseException:
                raise BrokerRejected("host upstream success capture failed") from None
        def capture(category, *, at_phase=None, error=None, omission=None):
            nonlocal captured
            if captured or self._failure_sink is None: return
            captured = True
            payload = {"method": method, "provider_path": path,
                       "normalized_body_sha256": hashlib.sha256(body).hexdigest(),
                       "upstream_body_sha256": hashlib.sha256(prepared.body).hexdigest(),
                       "requested_model": self._permit.model, "upstream_model": prepared.effective_model,
                       "upstream_route_digest": prepared.route_digest, "upstream_origin": prepared.origin,
                       "upstream_path": prepared.path, "http_status": status,
                       "phase": at_phase or phase, "category": category,
                       "structured_error": error, "omission": omission}
            try: self._failure_sink(payload)
            except BaseException:
                raise BrokerRejected("host upstream diagnostic capture failed") from None
        try:
            connection = http.client.HTTPSConnection(
                prepared.origin.removeprefix("https://"), 443,
                timeout=self._deadline_seconds, context=_system_tls_context(),
            )
            connection.set_debuglevel(0)
            connection.auto_open = False
            _remaining(connection, deadline)
            lease = _TransportLease(connection, deadline)
            lease.start()
            phase = "connect"
            connection.connect()
            _remaining(connection, deadline)
            phase = "request_write"
            connection.request(method, prepared.path, body=prepared.body, headers=outgoing)
            _remaining(connection, deadline)
            phase = "response_headers"
            response = connection.getresponse()
            if type(response.status) is int: status = response.status
            lease.attach(response)
            _remaining(connection, deadline)
            if (type(response.status) is not int or not 200 <= response.status < 300
                    or response.getheader("location") is not None
                    or response.getheader("content-encoding") not in (None, "identity")):
                category = "non2xx"; error = None; omission = None
                if type(response.status) is not int:
                    category = omission = "invalid_status"
                elif 300 <= response.status < 400 or response.getheader("location") is not None:
                    category = omission = "redirect_or_location"
                elif response.getheader("content-encoding") not in (None, "identity"):
                    category = omission = "unsafe_encoding"
                elif (response.getheader("content-type") or "").split(";", 1)[0].strip().lower() != "application/json":
                    omission = "unsupported_media"
                elif self._failure_sink is not None:
                    chunks = []; total = 0; cap = min(16384, self._max_response_bytes)
                    phase = "error_body"
                    try:
                        while True:
                            piece = _read_piece(connection, response, deadline, min(_READ_BYTES, cap - total + 1))
                            if not piece:
                                # Bounded HTTPResponse.read does not raise on premature length-framed EOF.
                                if response.length not in (None, 0): omission = "body_read_failure"
                                break
                            total += len(piece)
                            if total > cap:
                                omission = "body_budget"; break
                            chunks.append(piece)
                        if omission is None:
                            marker = credential[7:] if expected_auth == "authorization" else credential
                            error, omission = _error_projection(b"".join(chunks), marker)
                    except Exception:
                        omission = "body_read_failure"
                capture(category, at_phase="error_body" if omission == "body_read_failure" else "response_headers", error=error, omission=omission)
                raise BrokerRejected("host upstream response is unsafe")
            mime = (response.getheader("content-type") or "").split(";", 1)[0].strip().lower()
            if mime not in {"application/json", "text/event-stream"}:
                capture("unsupported_media", omission="unsupported_media")
                raise BrokerRejected("host upstream response type is unsupported")
            if mime == "text/event-stream":
                stream = _SSEBody(lease, response, self._max_response_bytes, deadline,
                                  lambda category: capture(category, at_phase="sse_body"),
                                  (lambda data: capture_success(data, mime))
                                  if self._success_sink is not None else None)
                stream_returned = True
                return BrokerResponse(response.status, {"content-type": mime}, stream)
            chunks = []
            total = 0
            phase = "json_body"
            while True:
                piece = _read_piece(connection, response, deadline,
                                    min(_READ_BYTES, self._max_response_bytes - total + 1))
                if not piece:
                    if response.length not in (None, 0):
                        raise BrokerRejected("host upstream response framing is incomplete")
                    break
                total += len(piece)
                if total > self._max_response_bytes:
                    capture("response_budget")
                    raise BrokerRejected("host upstream response exceeds byte budget")
                chunks.append(piece)
            response_body = b"".join(chunks)
            capture_success(response_body, mime)
            return BrokerResponse(response.status, {"content-type": mime}, response_body)
        except BrokerRejected:
            capture("deadline_expired" if time.monotonic() >= deadline else "transport_failure")
            raise
        except Exception:
            capture("deadline_expired" if time.monotonic() >= deadline else "transport_failure")
            if time.monotonic() >= deadline:
                raise BrokerRejected("host upstream deadline expired") from None
            raise BrokerRejected("host upstream request failed") from None
        finally:
            if lease is not None and not stream_returned:
                lease.close()
            elif connection is not None and lease is None:
                _close(connection)
