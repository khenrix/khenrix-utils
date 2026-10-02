#!/usr/bin/env python3
"""Exchange exact fanout checkpoints through the local claude-mem gateway."""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import os
import pathlib
import re
import stat
import sys
import urllib.parse
from collections.abc import Mapping
from typing import Any, BinaryIO

import memoryctl


SCHEMA_VERSION = "fanout-memory-exchange-v1"
GATEWAY_PORT = 48175
MAX_IDS = 256
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_TEXT_BYTES + 64 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = MAX_RESPONSE_BYTES + 64 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_CONTENT_HASH_RE = re.compile(r"[0-9A-Fa-f]{16}\Z")
_REVISION_RE = re.compile(r"(?:0|[1-9][0-9]{0,19})\Z")
_OBSERVATION_TYPES = frozenset(
    {
        "bugfix",
        "change",
        "decision",
        "discovery",
        "feature",
        "refactor",
        "security_alert",
        "security_note",
        "sensitive",
    }
)
# Exact columns returned by SELECT o.* at the repository's claude-mem pin:
# 13.25.3 / 4520de9e0f8d6cdc20597520e383d8b51d93137f.
_OBSERVATION_FIELDS = frozenset(
    {
        "agent_id",
        "agent_type",
        "concepts",
        "content_hash",
        "created_at",
        "created_at_epoch",
        "discovery_tokens",
        "facts",
        "files_modified",
        "files_read",
        "generated_by_model",
        "id",
        "memory_session_id",
        "merged_into_project",
        "metadata",
        "narrative",
        "origin_device_id",
        "origin_local_id",
        "project",
        "prompt_number",
        "relevance_count",
        "subtitle",
        "sync_rev",
        "synced_at",
        "text",
        "title",
        "type",
    }
)


class ExchangeError(RuntimeError):
    """A classified, secret-safe controller failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def __repr__(self) -> str:
        return f"ExchangeError(code={self.code!r}, message=<redacted>)"


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _strict_json(data: bytes, *, code: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise ExchangeError(code, "JSON is malformed") from error


def _is_int(value: object, *, minimum: int = 0) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= MAX_SAFE_INTEGER
    )


def _bounded_string(
    value: object,
    *,
    maximum: int,
    allow_empty: bool = False,
    trimmed: bool = False,
    controls: bool = True,
) -> bool:
    if not isinstance(value, str):
        return False
    if not allow_empty and not value:
        return False
    if trimmed and value.strip() != value:
        return False
    if not controls and any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return False
    if "\x00" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        return False


def _read_request(stdin: BinaryIO) -> dict[str, Any]:
    raw = stdin.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ExchangeError("invalid-request", "request exceeds its bounded size")
    value = _strict_json(raw, code="invalid-request")
    if not isinstance(value, dict):
        raise ExchangeError("invalid-request", "request must be an object")
    try:
        canonical = canonical_json(value)
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise ExchangeError("invalid-request", "request is not canonical JSON") from error
    if raw != canonical:
        raise ExchangeError("invalid-request", "request is not canonical JSON")
    _validate_request(value)
    return value


def _validate_request(value: dict[str, Any]) -> None:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ExchangeError("invalid-request", "request schema is unsupported")
    operation = value.get("operation")
    if operation == "save":
        if set(value) != {"schema_version", "operation", "project", "title", "text"}:
            raise ExchangeError("invalid-request", "save request schema is invalid")
        if not _bounded_string(value["project"], maximum=512, trimmed=True, controls=False):
            raise ExchangeError("invalid-request", "save project is invalid")
        if "," in value["project"]:
            raise ExchangeError("invalid-request", "save project is invalid")
        if not _bounded_string(value["title"], maximum=1024, trimmed=True, controls=False):
            raise ExchangeError("invalid-request", "save title is invalid")
        if not _bounded_string(value["text"], maximum=MAX_TEXT_BYTES, trimmed=True):
            raise ExchangeError("invalid-request", "save text is invalid")
        return
    if operation == "fetch":
        if set(value) != {"schema_version", "operation", "ids"}:
            raise ExchangeError("invalid-request", "fetch request schema is invalid")
        ids = value["ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_IDS:
            raise ExchangeError("invalid-request", "fetch ids are invalid")
        if any(not _is_int(item, minimum=1) for item in ids) or len(set(ids)) != len(ids):
            raise ExchangeError("invalid-request", "fetch ids are invalid")
        return
    raise ExchangeError("invalid-request", "request operation is unsupported")


def _default_token_path() -> pathlib.Path:
    raw_home = os.environ.get("AGENTIC_MEMORY_HOME")
    home = pathlib.Path(raw_home).expanduser() if raw_home else pathlib.Path.home()
    return home / ".config" / "agentic-memory" / "gateway-token"


def _read_gateway_token(path: pathlib.Path) -> str:
    parent_fd: int | None = None
    token_fd: int | None = None
    try:
        parent = path.parent
        parent_metadata = parent.lstat()
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or stat.S_ISLNK(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.getuid()
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        ):
            raise OSError("unsafe gateway token directory")
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened_parent = os.fstat(parent_fd)
        if (opened_parent.st_dev, opened_parent.st_ino) != (
            parent_metadata.st_dev,
            parent_metadata.st_ino,
        ):
            raise OSError("gateway token directory changed")
        token_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        metadata = os.fstat(token_fd)
        entry = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(entry.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (entry.st_dev, entry.st_ino)
            or metadata.st_size > 128
        ):
            raise OSError("unsafe gateway token file")
        chunks: list[bytes] = []
        remaining = 129
        while remaining:
            chunk = os.read(token_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    except OSError as error:
        raise ExchangeError("gateway-unavailable", "owner-private gateway token is unavailable") from error
    finally:
        if token_fd is not None:
            os.close(token_fd)
        if parent_fd is not None:
            os.close(parent_fd)
    try:
        token_bytes = payload[:-1] if payload.endswith(b"\n") else payload
        token = token_bytes.decode("ascii")
    except UnicodeDecodeError as error:
        raise ExchangeError("gateway-unavailable", "owner-private gateway token is malformed") from error
    if not _TOKEN_RE.fullmatch(token):
        raise ExchangeError("gateway-unavailable", "owner-private gateway token is malformed")
    return token


def _default_endpoint() -> str:
    value = os.environ.get("KHENRIX_MEMORY_GATEWAY_PORT", str(GATEWAY_PORT))
    if not value.isascii() or not value.isdigit() or value != str(int(value)):
        raise ExchangeError("gateway-unavailable", "memory gateway port is invalid")
    port = int(value)
    if not 1 <= port <= 65535:
        raise ExchangeError("gateway-unavailable", "memory gateway port is invalid")
    return f"http://127.0.0.1:{port}"


class GatewayClient:
    """A no-redirect client sealed to one literal loopback gateway."""

    __slots__ = ("_host", "_port", "_token", "_timeout")

    def __init__(self, endpoint: str, token: str, *, timeout: float = 30.0) -> None:
        try:
            parsed = urllib.parse.urlsplit(endpoint)
            port = parsed.port
            address = ipaddress.ip_address(parsed.hostname or "")
        except (ValueError, TypeError) as error:
            raise ExchangeError("gateway-unavailable", "gateway target must be literal loopback HTTP") from error
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not address.is_loopback
            or port is None
            or not 1 <= port <= 65535
        ):
            raise ExchangeError("gateway-unavailable", "gateway target must be literal loopback HTTP")
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise ExchangeError("gateway-unavailable", "gateway credential is malformed")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise ExchangeError("gateway-unavailable", "gateway timeout is invalid")
        self._host = str(address)
        self._port = port
        self._token = token
        self._timeout = float(timeout)

    def post(self, path: str, body: bytes) -> bytes:
        if path not in {"/api/memory/save", "/api/observations/batch"}:
            raise ExchangeError("gateway-unavailable", "gateway path is not admitted")
        connection = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    declared = int(length)
                except ValueError as error:
                    raise ExchangeError("invalid-worker-response", "worker response length is invalid") from error
                if declared < 0 or declared > MAX_RESPONSE_BYTES:
                    raise ExchangeError("invalid-worker-response", "worker response exceeds its bounded size")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ExchangeError("invalid-worker-response", "worker response exceeds its bounded size")
            if response.status != 200:
                raise ExchangeError("gateway-unavailable", "memory worker returned a non-success status")
            if response.getheader("Content-Encoding"):
                raise ExchangeError("invalid-worker-response", "encoded worker responses are unsupported")
            media_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                raise ExchangeError("invalid-worker-response", "worker response media type is invalid")
            return payload
        except ExchangeError:
            raise
        except (OSError, http.client.HTTPException, TimeoutError) as error:
            raise ExchangeError("gateway-unavailable", "memory gateway transport failed") from error
        finally:
            connection.close()

    def __repr__(self) -> str:
        return f"GatewayClient(endpoint='http://{self._host}:{self._port}', token=<redacted>)"


def _validate_save_response(value: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    fields = {"success", "id", "title", "project", "message"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ExchangeError("invalid-worker-response", "save response schema drifted")
    observation_id = value["id"]
    if (
        value["success"] is not True
        or not _is_int(observation_id, minimum=1)
        or value["title"] != request["title"]
        or value["project"] != request["project"]
        or value["message"] != f"Memory saved as observation #{observation_id}"
    ):
        raise ExchangeError("invalid-worker-response", "save response values are invalid")
    return value


def _nullable_string(value: object, *, maximum: int) -> bool:
    return value is None or _bounded_string(value, maximum=maximum, allow_empty=True)


def _validate_embedded_list(value: object) -> bool:
    if value is None:
        return True
    if not _bounded_string(value, maximum=1024 * 1024, allow_empty=False):
        return False
    try:
        parsed = _strict_json(value.encode("utf-8"), code="invalid-worker-response")
    except ExchangeError:
        return False
    return (
        isinstance(parsed, list)
        and len(parsed) <= 4096
        and all(_bounded_string(item, maximum=64 * 1024, allow_empty=True) for item in parsed)
    )


def _validate_embedded_metadata(value: object) -> bool:
    if value is None:
        return True
    if not _bounded_string(value, maximum=64 * 1024, allow_empty=False):
        return False
    try:
        parsed = _strict_json(value.encode("utf-8"), code="invalid-worker-response")
    except ExchangeError:
        return False
    if not isinstance(parsed, dict):
        return False
    remaining = 1024

    def walk(item: object, depth: int) -> bool:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 8:
            return False
        if item is None or isinstance(item, bool) or isinstance(item, str):
            return not isinstance(item, str) or _bounded_string(item, maximum=64 * 1024, allow_empty=True)
        if isinstance(item, int) and not isinstance(item, bool):
            return -MAX_SAFE_INTEGER <= item <= MAX_SAFE_INTEGER
        if isinstance(item, float):
            return math.isfinite(item)
        if isinstance(item, list):
            return all(walk(member, depth + 1) for member in item)
        if isinstance(item, dict):
            return all(
                _bounded_string(key, maximum=1024, allow_empty=False)
                and walk(member, depth + 1)
                for key, member in item.items()
            )
        return False

    return walk(parsed, 0)


def _validate_observation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _OBSERVATION_FIELDS:
        raise ExchangeError("invalid-worker-response", "observation response schema drifted")
    if not _is_int(value["id"], minimum=1):
        raise ExchangeError("invalid-worker-response", "observation id is invalid")
    if not _bounded_string(value["memory_session_id"], maximum=1024):
        raise ExchangeError("invalid-worker-response", "observation session is invalid")
    if not _bounded_string(value["project"], maximum=512):
        raise ExchangeError("invalid-worker-response", "observation project is invalid")
    if value["type"] not in _OBSERVATION_TYPES:
        raise ExchangeError("invalid-worker-response", "observation type is invalid")
    if not _nullable_string(value["text"], maximum=MAX_TEXT_BYTES):
        raise ExchangeError("invalid-worker-response", "observation text is invalid")
    if not _nullable_string(value["title"], maximum=1024):
        raise ExchangeError("invalid-worker-response", "observation title is invalid")
    if not _nullable_string(value["subtitle"], maximum=4096):
        raise ExchangeError("invalid-worker-response", "observation subtitle is invalid")
    if not _nullable_string(value["narrative"], maximum=MAX_TEXT_BYTES):
        raise ExchangeError("invalid-worker-response", "observation narrative is invalid")
    if not all(_validate_embedded_list(value[name]) for name in ("facts", "concepts", "files_read", "files_modified")):
        raise ExchangeError("invalid-worker-response", "observation list field is invalid")
    for name in ("prompt_number", "synced_at"):
        if value[name] is not None and not _is_int(value[name], minimum=0):
            raise ExchangeError("invalid-worker-response", f"observation {name} is invalid")
    for name in ("created_at_epoch", "discovery_tokens", "relevance_count"):
        if not _is_int(value[name], minimum=0):
            raise ExchangeError("invalid-worker-response", f"observation {name} is invalid")
    if not _bounded_string(value["created_at"], maximum=128):
        raise ExchangeError("invalid-worker-response", "observation timestamp is invalid")
    if not isinstance(value["content_hash"], str) or not _CONTENT_HASH_RE.fullmatch(value["content_hash"]):
        raise ExchangeError("invalid-worker-response", "observation hash is invalid")
    for name in ("generated_by_model", "merged_into_project", "agent_type", "agent_id", "origin_device_id", "origin_local_id"):
        if not _nullable_string(value[name], maximum=1024):
            raise ExchangeError("invalid-worker-response", f"observation {name} is invalid")
    if not _validate_embedded_metadata(value["metadata"]):
        raise ExchangeError("invalid-worker-response", "observation metadata is invalid")
    revision = value["sync_rev"]
    if (
        not isinstance(revision, str)
        or not _REVISION_RE.fullmatch(revision)
        or not 1 <= int(revision) <= 2**64 - 1
    ):
        raise ExchangeError("invalid-worker-response", "observation revision is invalid")
    return value


def _validate_fetch_response(value: Any, requested_ids: list[int]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(requested_ids):
        raise ExchangeError("invalid-worker-response", "exact observation batch is incomplete")
    observations = [_validate_observation(item) for item in value]
    observations_by_id: dict[int, dict[str, Any]] = {}
    for observation in observations:
        observation_id = observation["id"]
        if observation_id in observations_by_id:
            raise ExchangeError(
                "invalid-worker-response", "exact observation batch duplicated an id"
            )
        observations_by_id[observation_id] = observation
    if set(observations_by_id) != set(requested_ids):
        raise ExchangeError(
            "invalid-worker-response", "exact observation batch changed id membership"
        )
    return [observations_by_id[observation_id] for observation_id in requested_ids]


def _exchange(request: dict[str, Any], client: GatewayClient) -> dict[str, Any]:
    operation = request["operation"]
    if operation == "save":
        body = canonical_json(
            {name: request[name] for name in ("project", "text", "title")}
        )
        raw = client.post("/api/memory/save", body)
        result = _validate_save_response(
            _strict_json(raw, code="invalid-worker-response"), request
        )
    else:
        body = canonical_json({"ids": request["ids"]})
        raw = client.post("/api/observations/batch", body)
        result = _validate_fetch_response(
            _strict_json(raw, code="invalid-worker-response"), request["ids"]
        )
    return {
        "ok": True,
        "operation": operation,
        "result": result,
        "schema_version": SCHEMA_VERSION,
    }


def _error_response(code: str) -> dict[str, object]:
    return {
        "error": {"code": code, "message": "memory exchange failed"},
        "ok": False,
        "schema_version": SCHEMA_VERSION,
    }


def _write_response(stdout: BinaryIO, response: Mapping[str, object]) -> bool:
    valid = True
    try:
        encoded = canonical_json(response)
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        encoded = canonical_json(_error_response("internal-error"))
        valid = False
    if len(encoded) > MAX_OUTPUT_BYTES:
        encoded = canonical_json(_error_response("invalid-worker-response"))
        valid = False
    stdout.write(encoded)
    stdout.flush()
    return valid


def run_once(
    stdin: BinaryIO,
    stdout: BinaryIO,
    *,
    endpoint: str | None = None,
    token_path: pathlib.Path | None = None,
) -> int:
    """Process exactly one request and emit exactly one response object."""
    try:
        request = _read_request(stdin)
        if memoryctl.install_receipt_problem() is not None:
            raise ExchangeError("install-unverified", "memory install receipt is unverified")
        token = _read_gateway_token(token_path or _default_token_path())
        client = GatewayClient(endpoint if endpoint is not None else _default_endpoint(), token)
        response = _exchange(request, client)
    except ExchangeError as error:
        response = _error_response(error.code)
        _write_response(stdout, response)
        return 1
    except Exception:
        response = _error_response("internal-error")
        _write_response(stdout, response)
        return 1
    return 0 if _write_response(stdout, response) else 1


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        _write_response(sys.stdout.buffer, _error_response("invalid-request"))
        return 1
    return run_once(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
