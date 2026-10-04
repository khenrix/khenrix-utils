"""Finite, controller-owned HTTP request boundary for disposable provider probes.

This module does not fetch credentials. The controller must pin the listener,
trusted upstream transport, native profile, and permit separately. The broker
checks ordinary response reflections, but cannot make an untrusted upstream
safe against arbitrary secret encodings in free-form model output.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import socket
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from typing import Callable

from .container_routes import PreparedOutbound


class BrokerRejected(ValueError):
    """A probe attempted a route, spend, or disclosure outside its permit."""


_AGY_PATH = re.compile(
    r"/v1beta/models/([A-Za-z0-9._-]{1,128}):streamGenerateContent\?alt=sse\Z"
)
_AGY_AUXILIARY_PATH = "/v1beta/models/gemini-3.1-flash-lite-preview:streamGenerateContent?alt=sse"
_AGY_AUXILIARY_REFUSAL = "broker agy auxiliary model is intentionally refused"
_AUTH_HEADERS = frozenset({
    "authorization", "x-api-key", "x-goog-api-key", "proxy-authorization", "cookie",
})
_FORWARD_HEADERS = frozenset({
    "content-type", "accept", "anthropic-version", "anthropic-beta", "openai-beta",
})
_MAX_JSON_DEPTH = 64


@dataclass(frozen=True, slots=True)
class BrokerPermit:
    provider: str
    run_id: str
    task_id: str
    target_id: str
    seat_id: str
    route: str
    model: str
    placeholder_key: str = field(repr=False)
    max_requests: int
    max_request_bytes: int
    max_response_bytes: int
    max_output_tokens: int
    response_deadline: float = 300.0
    inputs_digest: str | None = None
    session_id: str | None = None
    clamp_output_tokens: bool = False
    upstream_route_digest: str | None = None
    upstream_origin: str | None = None
    upstream_model: str | None = None
    upstream_path: str | None = None

    def __post_init__(self) -> None:
        if self.provider not in {"claude", "codex", "agy"} or self.route not in {
            "initial", "resume",
        }:
            raise BrokerRejected("broker provider or route is unsupported")
        for name in ("run_id", "task_id", "target_id", "seat_id", "model"):
            value = getattr(self, name)
            if (not isinstance(value, str) or not value
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None):
                raise BrokerRejected(f"broker {name} is invalid")
        if (not isinstance(self.placeholder_key, str)
                or re.fullmatch(r"[A-Za-z0-9._-]{16,128}", self.placeholder_key) is None):
            raise BrokerRejected("broker placeholder is invalid")
        if self.inputs_digest is not None and (
            not isinstance(self.inputs_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.inputs_digest) is None
        ):
            raise BrokerRejected("broker inputs digest is invalid")
        if self.session_id is not None and (
            not isinstance(self.session_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", self.session_id) is None
        ):
            raise BrokerRejected("broker session identity is invalid")
        if self.route == "resume" and self.session_id is None:
            raise BrokerRejected("broker resume needs an exact session identity")
        upstream = (self.upstream_route_digest, self.upstream_origin,
                    self.upstream_model, self.upstream_path)
        if any(value is not None for value in upstream) and (
                not isinstance(self.upstream_route_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", self.upstream_route_digest) is None
                or not isinstance(self.upstream_origin, str)
                or re.fullmatch(r"(?:https://)?[a-z0-9.-]{1,253}", self.upstream_origin) is None
                or not isinstance(self.upstream_model, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.upstream_model) is None
                or not isinstance(self.upstream_path, str)
                or not self.upstream_path.startswith("/")
                or self.upstream_path.startswith("//")
                or len(self.upstream_path) > 512
                or any(not 33 <= ord(char) < 127 for char in self.upstream_path)):
            raise BrokerRejected("broker upstream route is invalid")
        if type(self.clamp_output_tokens) is not bool or (
            self.clamp_output_tokens and self.provider not in {"claude", "agy"}
        ):
            raise BrokerRejected("broker output clamp is invalid")
        for name, ceiling in (("max_requests", 32), ("max_request_bytes", 1024 * 1024),
                              ("max_response_bytes", 8 * 1024 * 1024),
                              ("max_output_tokens", 8192)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise BrokerRejected(f"broker {name} is invalid")
        if (type(self.response_deadline) not in (int, float)
                or not 0.1 <= self.response_deadline <= 600):
            raise BrokerRejected("broker response deadline is invalid")


@dataclass(frozen=True, slots=True)
class BrokerResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes | Iterable[bytes]


def _check_json_depth(value: object) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict, list)):
            if depth >= _MAX_JSON_DEPTH:
                raise BrokerRejected("broker JSON nesting exceeds its finite depth budget")
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)


def _contains_secret(value: object, credential: str) -> bool:
    pending = [(value, 0)]
    while pending:
        item, nested_depth = pending.pop()
        if isinstance(item, str):
            if credential in item:
                return True
            # Provider text can itself contain serialized JSON. Inspect that
            # common wrapper, but retain the pinned-upstream trust boundary.
            if item.lstrip().startswith(("{", "[", '"')):
                if nested_depth >= 4:
                    raise BrokerRejected("broker upstream response nesting is unsupported")
                try:
                    nested = json.loads(item, object_pairs_hook=_unique_json_pairs)
                    _check_json_depth(nested)
                    json.dumps(nested, allow_nan=False)
                except json.JSONDecodeError:
                    continue
                except (ValueError, RecursionError) as error:
                    raise BrokerRejected("broker upstream response is not safe JSON") from error
                pending.append((nested, nested_depth + 1))
        elif isinstance(item, list):
            pending.extend((child, nested_depth) for child in item)
        elif isinstance(item, dict):
            pending.extend((child, nested_depth) for pair in item.items() for child in pair)
    return False


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BrokerRejected("broker JSON has duplicate fields")
        result[key] = value
    return result


def _safe_response_body(body: bytes, mime: str, credential: str) -> bytes:
    if credential.encode() in body:
        raise BrokerRejected("broker upstream response contains a disallowed disclosure")
    if not body:
        return body
    try:
        decoded = body.decode("utf-8")
        if mime == "application/json":
            parsed = json.loads(decoded, object_pairs_hook=_unique_json_pairs)
            _check_json_depth(parsed)
            if _contains_secret(parsed, credential):
                raise BrokerRejected("broker upstream response contains a disallowed disclosure")
            safe = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False).encode()
        elif mime == "text/event-stream":
            frames = []
            for block in decoded.replace("\r\n", "\n").split("\n\n"):
                if not block:
                    continue
                event = None
                data_lines = []
                for line in block.splitlines():
                    if line.startswith("event: ") and event is None:
                        event = line[7:]
                        if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", event) is None:
                            raise BrokerRejected("broker upstream SSE event is invalid")
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip(" "))
                    elif line.startswith(":"):
                        continue
                    else:
                        raise BrokerRejected("broker upstream SSE frame is invalid")
                if not data_lines:
                    continue
                data = "\n".join(data_lines)
                if data != "[DONE]":
                    parsed = json.loads(data, object_pairs_hook=_unique_json_pairs)
                    _check_json_depth(parsed)
                    if _contains_secret(parsed, credential):
                        raise BrokerRejected("broker upstream response contains a disallowed disclosure")
                    data = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"),
                                      allow_nan=False)
                frames.append((f"event: {event}\n" if event else "") + f"data: {data}\n\n")
            safe = "".join(frames).encode()
        else:
            raise BrokerRejected("broker upstream response content type is unsupported")
    except BrokerRejected:
        raise
    except (ValueError, UnicodeError, RecursionError) as error:
        raise BrokerRejected("broker upstream response is not structured UTF-8") from error
    return safe


class _OwnedStream:
    def __init__(self, iterator: Iterator[bytes], source: Iterable[bytes]):
        self._iterator = iterator
        self._source = source
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        try:
            return next(self._iterator)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._iterator.close()
        finally:
            close = getattr(self._source, "close", None)
            if callable(close):
                try:
                    close()
                except (OSError, ValueError):
                    pass


def _safe_stream_body(chunks: Iterable[bytes], credential: str,
                      max_bytes: int, expires: float) -> Iterable[bytes]:
    return _OwnedStream(_safe_stream_chunks(chunks, credential, max_bytes, expires), chunks)


def _safe_stream_chunks(chunks: Iterable[bytes], credential: str,
                        max_bytes: int, expires: float) -> Iterator[bytes]:
    pending: Queue[tuple[str, object]] = Queue(maxsize=1)
    stopped = threading.Event()

    def publish(kind: str, value: object) -> bool:
        while not stopped.is_set():
            try:
                pending.put((kind, value), timeout=0.05)
                return True
            except Full:
                pass
        return False

    def produce() -> None:
        try:
            for chunk in chunks:
                if not publish("chunk", chunk):
                    return
        except Exception:
            publish("error", None)
        else:
            publish("end", None)

    worker = threading.Thread(target=produce, daemon=True)
    worker.start()
    raw_total = 0
    safe_total = 0
    buffer = b""
    text_tails: dict[tuple[str | int, ...], str] = {}

    def inspect_frame(raw: bytes) -> bytes:
        safe = _safe_response_body(raw, "text/event-stream", credential)
        if not safe:
            return b":\n\n" if raw.strip() else b""
        if len(credential) == 1:
            return safe
        data = next(line[6:] for line in safe.splitlines() if line.startswith(b"data: "))
        if data == b"[DONE]":
            return safe
        values: list[tuple[tuple[str | int, ...], object]] = [((), json.loads(data))]
        while values:
            path, item = values.pop()
            if isinstance(item, str):
                combined = text_tails.get(path, "") + item
                if _contains_secret(combined, credential):
                    raise BrokerRejected("broker upstream response contains a disallowed disclosure")
                text_tails[path] = combined[-(len(credential) - 1):]
            elif isinstance(item, list):
                values.extend((path + (index,), child) for index, child in enumerate(item))
            elif isinstance(item, dict):
                values.extend((path + (key,), child) for key, child in item.items())
        return safe

    try:
        while True:
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise BrokerRejected("broker upstream response deadline expired")
            try:
                kind, value = pending.get(timeout=remaining)
            except Empty:
                raise BrokerRejected("broker upstream response deadline expired") from None
            if time.monotonic() > expires:
                raise BrokerRejected("broker upstream response deadline expired")
            if kind == "error":
                raise BrokerRejected("broker upstream stream failed")
            if kind == "end":
                if buffer.strip():
                    raise BrokerRejected("broker upstream SSE frame is incomplete")
                return
            if not isinstance(value, bytes):
                raise BrokerRejected("broker upstream stream chunk is invalid")
            raw_total += len(value)
            if raw_total > max_bytes:
                raise BrokerRejected("broker upstream response exceeds the finite byte budget")
            buffer = (buffer + value).replace(b"\r\n", b"\n")
            while b"\n\n" in buffer:
                frame, buffer = buffer.split(b"\n\n", 1)
                if not frame:
                    continue
                safe = inspect_frame(frame + b"\n\n")
                safe_total += len(safe)
                if safe_total > max_bytes:
                    raise BrokerRejected("broker upstream response exceeds the finite byte budget")
                if safe:
                    yield safe
    finally:
        stopped.set()


def _unique_headers(headers: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        raise BrokerRejected("broker request headers are invalid")
    result: dict[str, str] = {}
    for key, value in headers.items():
        if (not isinstance(key, str) or not isinstance(value, str)
                or re.fullmatch(r"[A-Za-z0-9-]{1,64}", key) is None
                or "\r" in value or "\n" in value):
            raise BrokerRejected("broker request headers are invalid")
        folded = key.lower()
        if folded in result:
            raise BrokerRejected("broker request header is duplicated")
        result[folded] = value
    return result


def _json_object(body: bytes) -> dict[str, object]:
    try:
        value = json.loads(body, object_pairs_hook=_unique_json_pairs)
        _check_json_depth(value)
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, UnicodeError, RecursionError) as error:
        raise BrokerRejected("broker request body is not valid JSON") from error
    if not isinstance(value, dict):
        raise BrokerRejected("broker request body must be an object")
    return value


def _agy_wire_model(model: str) -> str:
    return "gemini-3.8-flash" if model == "gemini-3.8-flash-high" else model


def _validate_request(permit: BrokerPermit, method: str, path: str,
                      headers: Mapping[str, str], body: bytes,
                      ) -> tuple[str, dict[str, str], bytes, int]:
    if method != "POST" or not isinstance(path, str) or not isinstance(body, bytes):
        raise BrokerRejected("broker method or request is not permitted")
    if len(path) > 256:
        raise BrokerRejected("broker provider path is not permitted")
    if not body or len(body) > permit.max_request_bytes:
        raise BrokerRejected("broker request exceeds the finite byte budget")
    incoming = _unique_headers(headers)
    if incoming.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise BrokerRejected("broker content type is not JSON")
    seat_auth_name = {"claude": "x-api-key", "codex": "x-fanout-seat",
                      "agy": "x-goog-api-key"}[permit.provider]
    if incoming.get(seat_auth_name) != permit.placeholder_key or any(
        name in incoming for name in _AUTH_HEADERS - {seat_auth_name}
    ):
        raise BrokerRejected("broker seat authentication differs")
    if permit.provider == "claude":
        if path != "/v1/messages" and re.fullmatch(
            r"/v1/messages\?beta=[A-Za-z0-9._,-]{1,128}", path,
        ) is None:
            raise BrokerRejected("broker provider path is not permitted")
        path = "/v1/messages"
    elif permit.provider == "codex":
        if path != "/v1/responses":
            raise BrokerRejected("broker provider path is not permitted")
    else:
        wire_model = _agy_wire_model(permit.model)
        if wire_model == "gemini-3.8-flash" and path == _AGY_AUXILIARY_PATH:
            raise BrokerRejected(_AGY_AUXILIARY_REFUSAL)
        match = _AGY_PATH.fullmatch(path)
        if match is None or match.group(1) != wire_model:
            raise BrokerRejected("broker provider path is not permitted")
    document = _json_object(body)
    body_changed = False
    if permit.provider == "agy":
        if "model" in document and document["model"] != wire_model:
            raise BrokerRejected("broker model differs")
        generation = document.get("generationConfig")
        limit = generation.get("maxOutputTokens") if isinstance(generation, dict) else None
        if (permit.clamp_output_tokens and type(limit) is int
                and limit > permit.max_output_tokens):
            generation["maxOutputTokens"] = permit.max_output_tokens
            limit = permit.max_output_tokens
            body_changed = True
    else:
        if document.get("model") != permit.model:
            raise BrokerRejected("broker model differs")
        if permit.provider == "codex" and "max_output_tokens" not in document:
            document["max_output_tokens"] = permit.max_output_tokens
            limit = permit.max_output_tokens
            body_changed = True
        else:
            limit = document.get("max_tokens" if permit.provider == "claude"
                                 else "max_output_tokens")
        if (permit.provider == "claude" and permit.clamp_output_tokens
                and type(limit) is int and limit > permit.max_output_tokens):
            document["max_tokens"] = permit.max_output_tokens
            limit = permit.max_output_tokens
            body_changed = True
    if type(limit) is not int or not 1 <= limit <= permit.max_output_tokens:
        raise BrokerRejected("broker generation exceeds the finite token budget")
    if body_changed:
        try:
            body = json.dumps(document, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError, UnicodeError, RecursionError) as error:
            raise BrokerRejected("broker request body is not safe JSON") from error
        if len(body) > permit.max_request_bytes:
            raise BrokerRejected("broker request exceeds the finite byte budget")
    return (path, {name: value for name, value in incoming.items()
                   if name in _FORWARD_HEADERS}, body, limit)


def is_authenticated_agy_auxiliary(permit: BrokerPermit, method: str, path: str,
                                   headers: Mapping[str, str], body: bytes) -> bool:
    if (permit.provider != "agy" or _agy_wire_model(permit.model) != "gemini-3.8-flash"
            or path != _AGY_AUXILIARY_PATH):
        return False
    try:
        _validate_request(permit, method, path, headers, body)
    except BrokerRejected as error:
        if error.args == (_AGY_AUXILIARY_REFUSAL,):
            return True
        raise
    return False


class Broker:
    """A single-seat permit; ``send`` must be pinned to a trusted provider."""

    def __init__(self, permit: BrokerPermit, *, credential: str,
                 send: Callable[[str, str, Mapping[str, str], bytes], BrokerResponse]) -> None:
        if not isinstance(permit, BrokerPermit) or not callable(send):
            raise BrokerRejected("broker permit or upstream transport is invalid")
        if not isinstance(credential, str) or not credential:
            raise BrokerRejected("broker controller credential is unavailable")
        if credential == permit.placeholder_key:
            raise BrokerRejected("broker seat placeholder must differ from the credential")
        self.permit = permit
        self._credential = credential
        self._send = send
        self._used = 0
        self._effective_limits: list[int] = []
        self._refused_agy_auxiliary = 0
        self._lock = threading.Lock()

    @property
    def effective_output_tokens(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._effective_limits)

    @property
    def refused_agy_auxiliary_requests(self) -> int:
        with self._lock:
            return self._refused_agy_auxiliary

    def validate_request(self, method: str, path: str, headers: Mapping[str, str],
                         body: bytes) -> tuple[str, dict[str, str], bytes, int]:
        if isinstance(path, str) and (self.permit.placeholder_key in path
                                      or self._credential in path):
            raise BrokerRejected("broker provider path contains a credential marker")
        return _validate_request(self.permit, method, path, headers, body)

    def preview_upstream(self, method: str, path: str, headers: Mapping[str, str],
                         body: bytes) -> PreparedOutbound:
        incoming = _unique_headers(headers)
        auth_name = {"claude": "x-api-key", "codex": "x-fanout-seat",
                     "agy": "x-goog-api-key"}[self.permit.provider]
        if auth_name not in incoming:
            candidate = {**incoming, auth_name: self.permit.placeholder_key}
            normalized = self.validate_request(method, path, candidate, body)
            if normalized[:3] != (path, incoming, body):
                raise BrokerRejected("broker preview request differs from validation")
        else:
            path, incoming, body, _ = self.validate_request(method, path, headers, body)
        preview = getattr(self._send, "preview", None)
        if not callable(preview):
            raise BrokerRejected("broker upstream preview is unavailable")
        prepared = preview(method, path, incoming, body)
        if (not isinstance(prepared, PreparedOutbound)
                or prepared.route_digest != self.permit.upstream_route_digest
                or prepared.origin != self.permit.upstream_origin
                or prepared.path != self.permit.upstream_path
                or prepared.effective_model != self.permit.upstream_model
                or not isinstance(prepared.body, bytes)
                or not prepared.body or len(prepared.body) > self.permit.max_request_bytes):
            raise BrokerRejected("broker upstream preview differs from permit")
        return prepared

    def dispatch(self, method: str, path: str, headers: Mapping[str, str],
                 body: bytes, *, reservation: object | None = None) -> BrokerResponse:
        try:
            path, outgoing, body, limit = self.validate_request(method, path, headers, body)
        except BrokerRejected as error:
            if error.args == (_AGY_AUXILIARY_REFUSAL,):
                with self._lock:
                    self._refused_agy_auxiliary += 1
            raise
        ledger = None
        if self.permit.upstream_route_digest is not None:
            from .container_broker import OutboundRequestIdentity, PermitLedger

            if (not isinstance(reservation, tuple) or len(reservation) != 5
                    or type(reservation[0]) is not PermitLedger
                    or not isinstance(reservation[4], OutboundRequestIdentity)):
                raise BrokerRejected("broker upstream reservation is required")
            ledger, permit_id, epoch, request_id, reserved_identity = reservation
            prepared = self.preview_upstream(method, path, outgoing, body)
            wire_model = (_agy_wire_model(self.permit.model)
                          if self.permit.provider == "agy" else _json_object(body)["model"])
            identity = OutboundRequestIdentity(
                method, path, hashlib.sha256(body).hexdigest(), limit, wire_model,
                prepared.route_digest, prepared.origin, prepared.path,
                hashlib.sha256(prepared.body).hexdigest(), prepared.effective_model)
            if identity != reserved_identity:
                raise BrokerRejected("broker upstream reservation request differs")
        elif reservation is not None:
            raise BrokerRejected("broker legacy request has no reservation")
        with self._lock:
            if self._used >= self.permit.max_requests:
                raise BrokerRejected("broker request budget is exhausted")
            if ledger is not None:
                ledger.claim_send(permit_id, epoch, request_id, self.permit, identity)
            self._used += 1
            self._effective_limits.append(limit)
        auth_name = {"claude": "x-api-key", "codex": "authorization",
                     "agy": "x-goog-api-key"}[self.permit.provider]
        outgoing[auth_name] = (f"Bearer {self._credential}" if self.permit.provider == "codex"
                               else self._credential)
        expires = time.monotonic() + self.permit.response_deadline
        result: Queue[tuple[str, object]] = Queue(maxsize=1)

        def send() -> None:
            try:
                result.put(("response", self._send(method, path, outgoing, body)))
            except Exception:
                result.put(("error", None))

        threading.Thread(target=send, daemon=True).start()
        try:
            kind, response = result.get(timeout=max(0, expires - time.monotonic()))
        except Empty:
            raise BrokerRejected("broker upstream response deadline expired") from None
        if time.monotonic() > expires:
            raise BrokerRejected("broker upstream response deadline expired")
        if kind == "error":
            raise BrokerRejected("broker upstream result is uncertain")
        if not isinstance(response, BrokerResponse) or type(response.status) is not int:
            raise BrokerRejected("broker upstream response is invalid")
        if not 200 <= response.status <= 599 or 300 <= response.status < 400:
            raise BrokerRejected("broker upstream response is unsafe")
        returned = _unique_headers(response.headers)
        if ("location" in returned or "content-encoding" in returned
                or any(name in returned for name in _AUTH_HEADERS)
                or any(self._credential in value for value in returned.values())):
            raise BrokerRejected("broker upstream response contains a disallowed disclosure")
        mime = returned.get("content-type", "").split(";", 1)[0].strip().lower()
        if isinstance(response.body, bytes):
            if len(response.body) > self.permit.max_response_bytes:
                raise BrokerRejected("broker upstream response exceeds the finite byte budget")
            safe_body = (b"".join(_safe_stream_body((response.body,), self._credential,
                         self.permit.max_response_bytes, expires))
                         if mime == "text/event-stream" else
                         _safe_response_body(response.body, mime, self._credential))
            if len(safe_body) > self.permit.max_response_bytes:
                raise BrokerRejected("broker upstream response exceeds the finite byte budget")
            if time.monotonic() > expires:
                raise BrokerRejected("broker upstream response deadline expired")
        elif (mime == "text/event-stream" and isinstance(response.body, Iterable)
              and not isinstance(response.body, (str, bytearray, Mapping))):
            safe_body = _safe_stream_body(response.body, self._credential,
                                          self.permit.max_response_bytes,
                                          expires)
        else:
            raise BrokerRejected("broker upstream response body is invalid")
        return BrokerResponse(response.status,
                              {"content-type": mime} if mime else {},
                              safe_body)


class _BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 4

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]):
        self._slots = threading.BoundedSemaphore(4)
        super().__init__(address, handler)

    def process_request(self, request: object, client_address: object) -> None:
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: object, client_address: object) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, _request: object, _client_address: object) -> None:
        # Never log untrusted request lines or headers from a probe seat.
        pass


class BrokerListener:
    """A loopback-only probe endpoint; all model traffic remains in ``Broker``."""

    def __init__(self, broker: Broker, *, request_deadline: float = 5.0,
                 ledger_binding: tuple[object, str, int] | None = None) -> None:
        if not isinstance(broker, Broker):
            raise BrokerRejected("broker listener requires an exact permit")
        if (type(request_deadline) not in (int, float)
                or not 0.1 <= request_deadline <= 10):
            raise BrokerRejected("broker request deadline is invalid")
        self._broker = broker
        if ledger_binding is not None:
            from .container_broker import PermitLedger
            if (not isinstance(ledger_binding, tuple) or len(ledger_binding) != 3
                    or type(ledger_binding[0]) is not PermitLedger
                    or broker.permit.upstream_route_digest is None):
                raise BrokerRejected("native listener ledger binding is invalid")
            ledger_binding[0].require_permit(ledger_binding[1], ledger_binding[2], broker.permit)
        self._ledger_binding = ledger_binding
        self._native_deliveries: list[dict[str, object]] = []
        self._native_failed = False
        self._delivery_lock = threading.Lock()
        self._request_deadline = float(request_deadline)
        self._server: _BoundedHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._binding_id: str | None = None
        self.host = "127.0.0.1"
        self.port = 0

    def active_binding(self) -> tuple[str, int, str, BrokerPermit]:
        """Return the held socket and permit identity only while listening."""
        server = self._server
        if (server is None or self._thread is None or not self._thread.is_alive()
                or server.socket.fileno() < 0 or self._binding_id is None
                or server.server_address != (self.host, self.port)):
            raise BrokerRejected("broker listener is not active")
        return self.host, self.port, self._binding_id, self._broker.permit

    @property
    def effective_output_tokens(self) -> tuple[int, ...]:
        return self._broker.effective_output_tokens

    @property
    def refused_agy_auxiliary_requests(self) -> int:
        return self._broker.refused_agy_auxiliary_requests

    @property
    def native_deliveries(self) -> tuple[dict[str, object], ...]:
        with self._delivery_lock:
            return tuple(dict(item) for item in self._native_deliveries)

    @property
    def native_failed(self) -> bool:
        with self._delivery_lock:
            return self._native_failed

    def _fence_native(self) -> None:
        if self._ledger_binding is not None:
            ledger, permit_id, epoch = self._ledger_binding
            with self._delivery_lock:
                self._native_failed = True
            ledger.fence_epoch(permit_id, epoch)

    def __enter__(self) -> "BrokerListener":
        if self._server is not None:
            raise BrokerRejected("broker listener is already active")
        broker = self._broker
        listener = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                self.request.settimeout(min(1.5, listener._request_deadline))
                super().setup()
                self._read_lock = threading.Lock()
                self._read_complete = False
                self._read_expired = False
                self._delivery_started = False

                def expire() -> None:
                    with self._read_lock:
                        if self._read_complete:
                            return
                        self._read_expired = True
                        try:
                            self.request.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

                self._read_timer = threading.Timer(listener._request_deadline, expire)
                self._read_timer.daemon = True
                self._read_timer.start()

            def finish(self) -> None:
                with self._read_lock:
                    self._read_complete = True
                self._read_timer.cancel()
                super().finish()

            def log_message(self, _format: str, *_args: object) -> None:
                # Request paths and headers are untrusted and must not reach logs.
                pass

            def _reply(self, status: int, body: bytes,
                       headers: Mapping[str, str] | None = None) -> None:
                self.close_connection = True
                self._delivery_started = True
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                    self.wfile.flush()

            def _reply_stream(self, response: BrokerResponse) -> bool:
                started = False
                stream = iter(response.body)
                try:
                    for frame in stream:
                        if not started:
                            self.close_connection = True
                            self._delivery_started = True
                            self.send_response(response.status)
                            self.send_header("Content-Type", "text/event-stream")
                            self.send_header("Transfer-Encoding", "chunked")
                            self.send_header("Connection", "close")
                            self.end_headers()
                            started = True
                        self.wfile.write(f"{len(frame):X}\r\n".encode() + frame + b"\r\n")
                        self.wfile.flush()
                    if not started:
                        self.close_connection = True
                        self._delivery_started = True
                        self.send_response(response.status)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Transfer-Encoding", "chunked")
                        self.send_header("Connection", "close")
                        self.end_headers()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    return True
                except (BrokerRejected, OSError):
                    if not started:
                        raise
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return False
                finally:
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()

            def _refuse(self) -> None:
                self._reply(403, b"broker request refused")

            def do_POST(self) -> None:
                try:
                    if (self.headers.get("Host") != f"{listener.host}:{listener.port}"
                            or len(self.headers.get_all("Content-Length", [])) != 1
                            or self.headers.get("Transfer-Encoding") is not None):
                        raise BrokerRejected("broker framing differs")
                    raw_length = self.headers.get("Content-Length", "")
                    if re.fullmatch(r"[0-9]{1,8}", raw_length) is None:
                        raise BrokerRejected("broker request length differs")
                    length = int(raw_length)
                    if not 0 < length <= broker.permit.max_request_bytes:
                        raise BrokerRejected("broker request length differs")
                    headers: dict[str, str] = {}
                    for name, value in self.headers.items():
                        folded = name.lower()
                        if folded in headers:
                            raise BrokerRejected("broker request header is duplicated")
                        headers[folded] = value
                    body = self.rfile.read(length)
                    with self._read_lock:
                        self._read_complete = True
                        expired = self._read_expired
                    self._read_timer.cancel()
                    if expired or len(body) != length:
                        raise BrokerRejected("broker request read deadline expired")
                    native = listener._ledger_binding
                    request_id = None
                    if native is None:
                        response = broker.dispatch("POST", self.path, headers, body)
                    else:
                        from .container_broker import RelayRequest, dispatch_once
                        ledger, permit_id, epoch = native
                        ledger.record_attempt(permit_id, epoch)
                        request_id = secrets.token_hex(16)
                        response = dispatch_once(RelayRequest(permit_id, epoch, request_id,
                            "POST", self.path, headers, body), ledger, broker)
                    if native is not None and response.headers.get("content-type") == "application/json":
                        self._reply(response.status, b"".join(response.body), response.headers)
                    elif isinstance(response.body, bytes):
                        self._reply(response.status, response.body, response.headers)
                    else:
                        if not self._reply_stream(response):
                            listener._fence_native()
                            return
                    if native is not None:
                        # Native socket completion is deliberately separate from
                        # the guest ACK used by HostRelay/container certification.
                        digest = response.body.digest
                        ledger.settle(request_id, digest)
                        with listener._delivery_lock:
                            listener._native_deliveries.append({"request_id": request_id,
                                "status": response.status, "content_type": response.headers.get("content-type"),
                                "body_sha256": digest, "delivery": "native-http-complete"})
                except BrokerRejected:
                    listener._fence_native()
                    if self._delivery_started:
                        self.close_connection = True
                    else:
                        self._refuse()
                except Exception:
                    listener._fence_native()
                    if self._delivery_started:
                        self.close_connection = True
                    else:
                        self._reply(500, b"broker internal failure")

            do_GET = do_PUT = do_DELETE = do_CONNECT = do_OPTIONS = do_HEAD = _refuse

        self._server = _BoundedHTTPServer((self.host, 0), Handler)
        self.port = self._server.server_address[1]
        self._binding_id = secrets.token_hex(16)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None
        self._binding_id = None
        self.port = 0
