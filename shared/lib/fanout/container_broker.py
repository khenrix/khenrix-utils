"""Controller-held spend ledger and finite provider frames for isolated seats.

The caller owns the pinned SSH stdio process. This module only handles its
already authenticated binary pipes; no credential or host path enters frames.
"""
from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import select
import stat
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Mapping

from .artifacts import canonical_json
from .broker import (Broker, BrokerPermit, BrokerRejected, BrokerResponse,
                     is_authenticated_agy_auxiliary)
from .controller import LifecycleController, assert_controller


MAX_FRAME_BYTES = 2_000_000
_MAX_LEDGER_BYTES = 1024 * 1024
_CHUNK_BYTES = 48 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SCHEMA = "fanout-container-spend-v4"
_REQUEST_FIELDS = frozenset({
    "type", "permit_id", "epoch", "request_id", "method", "provider_path",
    "headers", "body_b64",
})


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BrokerRejected("relay JSON has duplicate fields")
        result[key] = value
    return result


def _digest(permit: BrokerPermit) -> str:
    return hashlib.sha256(canonical_json(asdict(permit))).hexdigest()


def _seat_scope(permit: BrokerPermit) -> str:
    return hashlib.sha256(canonical_json({
        "run_id": permit.run_id, "task_id": permit.task_id,
        "target_id": permit.target_id, "seat_id": permit.seat_id,
    })).hexdigest()


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def auxiliary_attempt_limit(permit: BrokerPermit | Mapping[str, object]) -> int:
    """Only the measured agy High auxiliary path receives a refusal allowance."""
    provider = permit["provider"] if isinstance(permit, Mapping) else permit.provider
    model = permit["model"] if isinstance(permit, Mapping) else permit.model
    return 3 if provider == "agy" and model == "gemini-3.8-flash-high" else 0


def attempt_limit(permit: BrokerPermit | Mapping[str, object]) -> int:
    """Finite guest/host attempt allowance; the broker spend cap is separate."""
    maximum = permit["max_requests"] if isinstance(permit, Mapping) else permit.max_requests
    return maximum + auxiliary_attempt_limit(permit)


@dataclass(frozen=True, slots=True)
class OutboundRequestIdentity:
    method: str
    path: str
    body_sha256: str
    effective_output_tokens: int
    wire_model: str
    upstream_route_digest: str | None = None
    upstream_origin: str | None = None
    upstream_path: str | None = None
    upstream_body_sha256: str | None = None
    upstream_model: str | None = None

    def __post_init__(self) -> None:
        if (self.method != "POST" or not isinstance(self.path, str)
                or not self.path.startswith("/") or self.path.startswith("//")
                or len(self.path) > 256 or any(not 32 <= ord(char) < 127 for char in self.path)
                or not isinstance(self.body_sha256, str) or _SHA.fullmatch(self.body_sha256) is None
                or type(self.effective_output_tokens) is not int
                or not 1 <= self.effective_output_tokens <= 8192
                or not _valid_id(self.wire_model)):
            raise BrokerRejected("broker outbound identity is invalid")
        upstream = (self.upstream_route_digest, self.upstream_origin,
                    self.upstream_path, self.upstream_body_sha256,
                    self.upstream_model)
        if any(value is not None for value in upstream) and (
                not isinstance(self.upstream_route_digest, str)
                or _SHA.fullmatch(self.upstream_route_digest) is None
                or not isinstance(self.upstream_origin, str)
                or re.fullmatch(r"(?:https://)?[a-z0-9.-]{1,253}", self.upstream_origin) is None
                or not isinstance(self.upstream_path, str)
                or not self.upstream_path.startswith("/")
                or self.upstream_path.startswith("//")
                or len(self.upstream_path) > 512
                or any(not 33 <= ord(char) < 127 for char in self.upstream_path)
                or not isinstance(self.upstream_body_sha256, str)
                or _SHA.fullmatch(self.upstream_body_sha256) is None
                or not _valid_id(self.upstream_model)):
            raise BrokerRejected("broker outbound upstream identity is invalid")


def _identity_matches_permit(identity: OutboundRequestIdentity,
                             permit: Mapping[str, object]) -> bool:
    expected_model = ("gemini-3.8-flash" if permit["provider"] == "agy"
                      and permit["model"] == "gemini-3.8-flash-high"
                      else permit["model"])
    expected_path = {"claude": "/v1/messages", "codex": "/v1/responses"}.get(
        permit["provider"],
        f"/v1beta/models/{expected_model}:streamGenerateContent?alt=sse")
    return (identity.wire_model == expected_model and identity.path == expected_path
            and identity.upstream_route_digest == permit["upstream_route_digest"]
            and identity.upstream_origin == permit["upstream_origin"]
            and identity.upstream_path == permit["upstream_path"]
            and identity.upstream_model == permit["upstream_model"]
            and ((identity.upstream_body_sha256 is None) ==
                 (permit["upstream_route_digest"] is None))
            and identity.effective_output_tokens <= permit["max_output_tokens"])


def _read_exact(reader: BinaryIO, size: int, deadline: float) -> bytes:
    result = bytearray()
    while len(result) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrokerRejected("relay frame read deadline expired")
        try:
            fd = reader.fileno()
        except (AttributeError, io.UnsupportedOperation):
            fd = None
        if fd is not None:
            if not select.select([fd], [], [], remaining)[0]:
                raise BrokerRejected("relay frame read deadline expired")
            piece = os.read(fd, size - len(result))
        else:
            piece = reader.read(size - len(result))
        if not piece:
            raise BrokerRejected("relay frame is truncated")
        result.extend(piece)
    return bytes(result)


class FrameStream:
    """Length-prefixed JSON over one already authenticated duplex pipe."""

    def __init__(self, reader: BinaryIO, writer: BinaryIO, *, timeout: float = 600.0,
                 write_timeout: float = 10.0):
        if not 0.1 <= timeout <= 600 or not 0.01 <= write_timeout <= 60:
            raise BrokerRejected("relay frame deadline is invalid")
        self.reader = reader
        self.writer = writer
        self.timeout = timeout
        self.write_timeout = write_timeout

    def recv(self) -> dict[str, object]:
        deadline = time.monotonic() + self.timeout
        size = int.from_bytes(_read_exact(self.reader, 4, deadline), "big")
        if not 0 < size <= MAX_FRAME_BYTES:
            raise BrokerRejected("relay frame exceeds the finite byte budget")
        raw = _read_exact(self.reader, size, deadline)
        try:
            frame = json.loads(raw, object_pairs_hook=_pairs)
        except BrokerRejected:
            raise
        except (UnicodeError, ValueError) as error:
            raise BrokerRejected("relay frame is invalid JSON") from error
        if not isinstance(frame, dict):
            raise BrokerRejected("relay frame must be an object")
        return frame

    def send(self, frame: Mapping[str, object]) -> None:
        try:
            raw = canonical_json(dict(frame)).rstrip(b"\n")
        except (TypeError, ValueError, UnicodeError) as error:
            raise BrokerRejected("relay frame is invalid") from error
        if not 0 < len(raw) <= MAX_FRAME_BYTES:
            raise BrokerRejected("relay frame exceeds the finite byte budget")
        try:
            pending = memoryview(len(raw).to_bytes(4, "big") + raw)
            try:
                fd = self.writer.fileno()
            except (AttributeError, io.UnsupportedOperation):
                fd = None
            if fd is None:
                while pending:
                    written = self.writer.write(pending)
                    if not isinstance(written, int) or written <= 0:
                        raise BrokerRejected("relay frame write failed")
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
                            raise BrokerRejected("relay frame write deadline expired")
                        try:
                            written = os.write(fd, pending[:_CHUNK_BYTES])
                        except BlockingIOError:
                            continue
                        if written <= 0:
                            raise BrokerRejected("relay frame write failed")
                        pending = pending[written:]
                finally:
                    os.set_blocking(fd, was_blocking)
        except OSError as error:
            raise BrokerRejected("relay frame write failed") from error


@dataclass(frozen=True, slots=True)
class RelayRequest:
    permit_id: str
    epoch: int
    request_id: str
    method: str
    provider_path: str
    headers: dict[str, str]
    body: bytes


def parse_request_frame(frame: object) -> RelayRequest:
    if not isinstance(frame, dict) or frame.keys() != _REQUEST_FIELDS or frame.get("type") != "request":
        raise BrokerRejected("relay request frame has unsupported fields")
    permit_id = frame["permit_id"]
    request_id = frame["request_id"]
    epoch = frame["epoch"]
    path = frame["provider_path"]
    headers = frame["headers"]
    encoded = frame["body_b64"]
    if (not isinstance(permit_id, str) or _SHA.fullmatch(permit_id) is None
            or not _valid_id(request_id) or type(epoch) is not int or epoch < 1
            or frame["method"] != "POST"
            or not isinstance(path, str) or not path.startswith("/")
            or path.startswith("//") or len(path) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or "://" in path):
        raise BrokerRejected("relay provider path or identity is invalid")
    if (not isinstance(headers, dict) or len(headers) > 32
            or any(not isinstance(key, str) or not isinstance(value, str)
                   or len(key) > 64 or len(value) > 4096
                   for key, value in headers.items())):
        raise BrokerRejected("relay request headers are invalid")
    if not isinstance(encoded, str) or len(encoded) > (1024 * 1024 * 4 // 3 + 8):
        raise BrokerRejected("relay request frame exceeds the finite byte budget")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise BrokerRejected("relay request body encoding is invalid") from error
    if not body or len(body) > 1024 * 1024:
        raise BrokerRejected("relay request body exceeds the finite byte budget")
    return RelayRequest(permit_id, epoch, request_id, "POST", path, headers, body)


class PermitLedger:
    """Atomic, fsynced, controller-authenticated one-seat spend state."""

    def __init__(self, root: Path, controller: LifecycleController):
        assert_controller(controller)
        self.root = Path(root)
        self.controller = controller
        if not self.root.is_absolute():
            self.root = self.root.absolute()
        try:
            self.root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise BrokerRejected("broker ledger root is unavailable") from error
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.getuid()):
            raise BrokerRejected("broker ledger root is unsafe")
        self._record = self.root / "ledger.json"
        self._lock = self.root / "ledger.lock"

    @contextmanager
    def _locked(self) -> Iterator[dict[str, object]]:
        assert_controller(self.controller)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        try:
            fd = os.open(self._lock, flags, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise BrokerRejected("broker ledger lock is unsafe")
            fcntl.flock(fd, fcntl.LOCK_EX)
            state = self._load()
            yield state
        finally:
            if "fd" in locals():
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _mac(self, payload: dict[str, object]) -> str:
        return hmac.new(self.controller._secret.encode("ascii"),
                        b"fanout-container-ledger\0" + canonical_json(payload),
                        hashlib.sha256).hexdigest()

    def _load(self) -> dict[str, object]:
        try:
            fd = os.open(self._record, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"schema_version": _SCHEMA, "controller_id": self.controller.controller_id,
                    "permits": {}, "requests": {}, "send_claims": {}}
        except OSError as error:
            raise BrokerRejected("broker ledger is unavailable") from error
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > _MAX_LEDGER_BYTES):
                raise BrokerRejected("broker ledger is unsafe")
            raw = os.read(fd, _MAX_LEDGER_BYTES + 1)
        finally:
            os.close(fd)
        try:
            record = json.loads(raw, object_pairs_hook=_pairs)
        except (UnicodeError, ValueError) as error:
            raise BrokerRejected("broker ledger is invalid") from error
        if not isinstance(record, dict) or record.keys() != {"payload", "mac"}:
            raise BrokerRejected("broker ledger is invalid")
        payload = record["payload"]
        if (not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA
                or payload.keys() != {"schema_version", "controller_id", "permits",
                                      "requests", "send_claims"}
                or not isinstance(payload["permits"], dict)
                or not isinstance(payload["requests"], dict)
                or not isinstance(payload["send_claims"], dict)):
            raise BrokerRejected("broker ledger is invalid")
        if payload["controller_id"] != self.controller.controller_id:
            raise BrokerRejected("broker ledger belongs to another controller")
        if not isinstance(record["mac"], str) or not hmac.compare_digest(
            record["mac"], self._mac(payload)):
            raise BrokerRejected("broker ledger integrity differs")
        self._validate_state(payload)
        return payload

    @staticmethod
    def _validate_state(payload: dict[str, object]) -> None:
        permits = payload["permits"]
        requests = payload["requests"]
        send_claims = payload["send_claims"]
        permit_fields = {"permit_digest", "seat_scope", "epoch", "provider", "model",
                         "upstream_route_digest", "upstream_origin", "upstream_model",
                         "upstream_path",
                         "max_requests", "max_response_bytes", "max_output_tokens",
                         "attempts", "auxiliary_attempts", "auxiliary_refusals",
                         "used", "fenced"}
        request_fields = {"permit_id", "epoch", "identity", "state", "response_digest"}
        identity_fields = set(OutboundRequestIdentity.__dataclass_fields__)
        used = {permit_id: 0 for permit_id in permits}
        for permit_id, entry in permits.items():
            if (not isinstance(permit_id, str) or _SHA.fullmatch(permit_id) is None
                    or not isinstance(entry, dict) or entry.keys() != permit_fields
                    or any(not isinstance(entry[name], str) or _SHA.fullmatch(entry[name]) is None
                           for name in ("permit_digest", "seat_scope"))
                    or type(entry["epoch"]) is not int or entry["epoch"] < 1
                    or not isinstance(entry["provider"], str)
                    or entry["provider"] not in {"claude", "codex", "agy"}
                    or not _valid_id(entry["model"])
                    or any(entry[name] is not None and not isinstance(entry[name], str)
                           for name in ("upstream_route_digest", "upstream_origin",
                                        "upstream_model", "upstream_path"))
                    or ((entry["upstream_route_digest"] is None) !=
                        (entry["upstream_origin"] is None))
                    or ((entry["upstream_route_digest"] is None) !=
                        (entry["upstream_model"] is None))
                    or ((entry["upstream_route_digest"] is None) !=
                        (entry["upstream_path"] is None))
                    or (entry["upstream_route_digest"] is not None and
                        (_SHA.fullmatch(entry["upstream_route_digest"]) is None
                         or re.fullmatch(r"(?:https://)?[a-z0-9.-]{1,253}",
                                         entry["upstream_origin"]) is None
                         or not _valid_id(entry["upstream_model"])
                         or not entry["upstream_path"].startswith("/")
                         or entry["upstream_path"].startswith("//")
                         or len(entry["upstream_path"]) > 512
                         or any(not 33 <= ord(char) < 127
                                for char in entry["upstream_path"])))
                    or type(entry["max_requests"]) is not int
                    or not 1 <= entry["max_requests"] <= 32
                    or type(entry["max_response_bytes"]) is not int
                    or not 1 <= entry["max_response_bytes"] <= 8 * 1024 * 1024
                    or type(entry["max_output_tokens"]) is not int
                    or not 1 <= entry["max_output_tokens"] <= 8192
                    or type(entry["fenced"]) is not bool):
                raise BrokerRejected("broker ledger permit entry is invalid")
            maximum = attempt_limit(entry)
            if (any(type(entry[name]) is not int for name in
                    ("attempts", "auxiliary_attempts", "auxiliary_refusals", "used"))
                    or not 0 <= entry["used"] <= entry["max_requests"]
                    or not 0 <= entry["attempts"] <= maximum
                    or not 0 <= entry["auxiliary_attempts"] <= entry["attempts"]
                    or entry["auxiliary_attempts"] > auxiliary_attempt_limit(entry)
                    or entry["auxiliary_refusals"] != min(1, entry["auxiliary_attempts"])
                    or (entry["auxiliary_attempts"] > 0 and maximum == entry["max_requests"])):
                raise BrokerRejected("broker ledger attempt entry is invalid")
        for request_id, entry in requests.items():
            if (not _valid_id(request_id) or not isinstance(entry, dict)
                    or entry.keys() != request_fields
                    or not isinstance(entry["permit_id"], str)
                    or entry["permit_id"] not in permits
                    or type(entry["epoch"]) is not int
                    or entry["epoch"] != permits[entry["permit_id"]]["epoch"]
                    or not isinstance(entry["identity"], dict)
                    or entry["identity"].keys() != identity_fields
                    or not isinstance(entry["state"], str)
                    or entry["state"] not in {"uncertain", "settled"}
                    or (entry["state"] == "uncertain" and entry["response_digest"] is not None)
                    or (entry["state"] == "settled" and
                        (not isinstance(entry["response_digest"], str)
                         or _SHA.fullmatch(entry["response_digest"]) is None))):
                raise BrokerRejected("broker ledger request entry is invalid")
            try:
                identity = OutboundRequestIdentity(**entry["identity"])
            except (BrokerRejected, TypeError) as error:
                raise BrokerRejected("broker ledger request identity is invalid") from error
            if not _identity_matches_permit(identity, permits[entry["permit_id"]]):
                raise BrokerRejected("broker ledger request identity differs")
            used[entry["permit_id"]] += 1
        if any(entry["used"] != used[permit_id]
               or entry["attempts"] < used[permit_id] + entry["auxiliary_attempts"]
               for permit_id, entry in permits.items()):
            raise BrokerRejected("broker ledger attempt or spend count is invalid")
        if any(not _valid_id(request_id) or claimed is not True
               or request_id not in requests
               for request_id, claimed in send_claims.items()):
            raise BrokerRejected("broker ledger send claim is invalid")

    def _save(self, payload: dict[str, object]) -> None:
        self._validate_state(payload)
        data = canonical_json({"payload": payload, "mac": self._mac(payload)})
        if len(data) > _MAX_LEDGER_BYTES:
            raise BrokerRejected("broker ledger exceeds the finite byte budget")
        name = ".ledger-" + secrets.token_hex(8)
        fd = os.open(self.root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(self.root / name, self._record)
            dir_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            try:
                os.unlink(self.root / name)
            except FileNotFoundError:
                pass
            raise

    def register(self, permit: BrokerPermit, epoch: int) -> str:
        if not isinstance(permit, BrokerPermit) or type(epoch) is not int or epoch < 1:
            raise BrokerRejected("broker permit or epoch is invalid")
        with self._locked() as state:
            permit_digest = _digest(permit)
            seat_scope = _seat_scope(permit)
            prior = [entry for entry in state["permits"].values()
                     if entry["seat_scope"] == seat_scope]
            if any(entry["epoch"] >= epoch for entry in prior):
                raise BrokerRejected("broker seat epoch is already registered or stale")
            if any(not entry["fenced"] for entry in prior):
                raise BrokerRejected("broker prior seat epoch must be fenced")
            permit_id = hashlib.sha256(canonical_json({
                "controller_id": self.controller.controller_id,
                "permit_digest": permit_digest, "epoch": epoch,
                "nonce": secrets.token_hex(16),
            })).hexdigest()
            state["permits"][permit_id] = {
                "permit_digest": permit_digest, "seat_scope": seat_scope,
                "epoch": epoch, "provider": permit.provider, "model": permit.model,
                "upstream_route_digest": permit.upstream_route_digest,
                "upstream_origin": permit.upstream_origin,
                "upstream_model": permit.upstream_model,
                "upstream_path": permit.upstream_path,
                "max_requests": permit.max_requests,
                "max_response_bytes": permit.max_response_bytes,
                "max_output_tokens": permit.max_output_tokens,
                "used": 0, "attempts": 0, "auxiliary_attempts": 0,
                "auxiliary_refusals": 0, "fenced": False,
            }
            self._save(state)
        return permit_id

    def require_permit(self, permit_id: str, epoch: int, permit: BrokerPermit) -> None:
        if (not isinstance(permit, BrokerPermit) or not isinstance(permit_id, str)
                or _SHA.fullmatch(permit_id) is None or type(epoch) is not int or epoch < 1):
            raise BrokerRejected("broker permit is invalid")
        with self._locked() as state:
            entry = state["permits"].get(permit_id)
            if not isinstance(entry, dict) or entry.get("permit_digest") != _digest(permit):
                raise BrokerRejected("broker permit identity differs")
            if entry.get("epoch") != epoch:
                raise BrokerRejected("broker permit epoch differs")
            if entry.get("fenced") is not False:
                raise BrokerRejected("broker permit epoch is fenced")

    def record_attempt(self, permit_id: str, epoch: int) -> None:
        if (not isinstance(permit_id, str) or _SHA.fullmatch(permit_id) is None
                or type(epoch) is not int or epoch < 1):
            raise BrokerRejected("broker permit epoch is invalid")
        with self._locked() as state:
            entry = state["permits"].get(permit_id)
            if not isinstance(entry, dict) or entry["epoch"] != epoch:
                raise BrokerRejected("broker permit epoch differs")
            if entry["fenced"]:
                raise BrokerRejected("broker permit epoch is fenced")
            if entry["attempts"] >= attempt_limit(entry):
                raise BrokerRejected("broker request attempt budget is exhausted")
            entry["attempts"] += 1
            self._save(state)

    def attempt_counts(self, permit_id: str, epoch: int) -> tuple[int, int]:
        if (not isinstance(permit_id, str) or _SHA.fullmatch(permit_id) is None
                or type(epoch) is not int or epoch < 1):
            raise BrokerRejected("broker permit epoch is invalid")
        with self._locked() as state:
            entry = state["permits"].get(permit_id)
            if not isinstance(entry, dict) or entry["epoch"] != epoch:
                raise BrokerRejected("broker permit epoch differs")
            return entry["attempts"], entry["auxiliary_attempts"]

    def reserve(self, permit_id: str, epoch: int, request_id: str,
                identity: OutboundRequestIdentity) -> None:
        if (not isinstance(permit_id, str) or _SHA.fullmatch(permit_id) is None
                or type(epoch) is not int or epoch < 1 or not _valid_id(request_id)
                or not isinstance(identity, OutboundRequestIdentity)):
            raise BrokerRejected("broker reservation identity is invalid")
        with self._locked() as state:
            entry = state["permits"].get(permit_id)
            if not isinstance(entry, dict):
                raise BrokerRejected("broker permit identity differs")
            if entry["epoch"] != epoch:
                raise BrokerRejected("broker permit epoch differs")
            if entry["fenced"]:
                raise BrokerRejected("broker permit epoch is fenced")
            prior = state["requests"].get(request_id)
            if prior is not None:
                raise BrokerRejected("broker request is uncertain or settled; replay refused")
            if entry["used"] >= entry["max_requests"]:
                raise BrokerRejected("broker request budget is exhausted")
            if entry["attempts"] <= entry["used"] + entry["auxiliary_attempts"]:
                raise BrokerRejected("broker request attempt is missing")
            if not _identity_matches_permit(identity, entry):
                raise BrokerRejected("broker reservation identity differs")
            entry["used"] += 1
            state["requests"][request_id] = {
                "permit_id": permit_id, "epoch": epoch, "identity": asdict(identity),
                "state": "uncertain", "response_digest": None,
            }
            self._save(state)

    def claim_send(self, permit_id: str, epoch: int, request_id: str,
                   permit: BrokerPermit, identity: OutboundRequestIdentity) -> None:
        """Durably consume one exact reservation before an upstream model send."""
        if (not isinstance(permit_id, str) or _SHA.fullmatch(permit_id) is None
                or type(epoch) is not int or epoch < 1 or not _valid_id(request_id)
                or not isinstance(permit, BrokerPermit)
                or permit.upstream_route_digest is None
                or not isinstance(identity, OutboundRequestIdentity)):
            raise BrokerRejected("broker send reservation is invalid")
        with self._locked() as state:
            entry = state["permits"].get(permit_id)
            request = state["requests"].get(request_id)
            if (not isinstance(entry, dict)
                    or entry["permit_digest"] != _digest(permit)
                    or entry["epoch"] != epoch or entry["fenced"]
                    or not isinstance(request, dict)
                    or request["permit_id"] != permit_id
                    or request["epoch"] != epoch
                    or request["state"] != "uncertain"
                    or request["identity"] != asdict(identity)):
                raise BrokerRejected("broker send reservation identity differs")
            if request_id in state["send_claims"]:
                raise BrokerRejected("broker send reservation is already claimed; replay refused")
            state["send_claims"][request_id] = True
            self._save(state)

    def record_auxiliary_refusal(self, permit_id: str, epoch: int) -> None:
        if not isinstance(permit_id, str) or _SHA.fullmatch(permit_id) is None or type(epoch) is not int or epoch < 1:
            raise BrokerRejected("broker permit epoch is invalid")
        with self._locked() as state:
            entry = state["permits"].get(permit_id)
            if not isinstance(entry, dict) or entry.get("epoch") != epoch:
                raise BrokerRejected("broker permit epoch differs")
            if entry["fenced"]:
                raise BrokerRejected("broker permit epoch is fenced")
            if (entry["provider"] != "agy" or entry["model"] != "gemini-3.8-flash-high"
                    or entry["auxiliary_attempts"] >= auxiliary_attempt_limit(entry)
                    or entry["auxiliary_attempts"] >= entry["attempts"] - entry["used"]):
                raise BrokerRejected("broker auxiliary attempt is unavailable")
            entry["auxiliary_attempts"] += 1
            entry["auxiliary_refusals"] = 1
            self._save(state)

    def auxiliary_refusal_count(self, permit_id: str, epoch: int) -> int:
        if not isinstance(permit_id, str) or _SHA.fullmatch(permit_id) is None or type(epoch) is not int or epoch < 1:
            raise BrokerRejected("broker permit epoch is invalid")
        with self._locked() as state:
            entry = state["permits"].get(permit_id)
            if not isinstance(entry, dict) or entry.get("epoch") != epoch:
                raise BrokerRejected("broker permit epoch differs")
            return entry.get("auxiliary_refusals", 0)

    def request_records(self, permit_id: str, epoch: int) -> tuple[dict[str, object], ...]:
        if (not isinstance(permit_id, str) or _SHA.fullmatch(permit_id) is None
                or type(epoch) is not int or epoch < 1):
            raise BrokerRejected("broker permit epoch is invalid")
        with self._locked() as state:
            entry = state["permits"].get(permit_id)
            if not isinstance(entry, dict) or entry["epoch"] != epoch:
                raise BrokerRejected("broker permit epoch differs")
            return tuple({"request_id": request_id, **request}
                         for request_id, request in sorted(state["requests"].items())
                         if request["permit_id"] == permit_id)

    def settle(self, request_id: str, response_digest: str) -> None:
        if (not _valid_id(request_id) or not isinstance(response_digest, str)
                or _SHA.fullmatch(response_digest) is None):
            raise BrokerRejected("broker settlement identity is invalid")
        with self._locked() as state:
            entry = state["requests"].get(request_id)
            if not isinstance(entry, dict) or entry.get("state") != "uncertain":
                raise BrokerRejected("broker settlement has no uncertain reservation")
            entry["state"] = "settled"
            entry["response_digest"] = response_digest
            self._save(state)

    def disposition(self, request_id: str) -> tuple[str, str | None]:
        with self._locked() as state:
            entry = state["requests"].get(request_id)
            if not isinstance(entry, dict):
                raise BrokerRejected("broker reservation identity differs")
            return entry["state"], entry["response_digest"]

    def response_limit(self, request_id: str) -> int:
        with self._locked() as state:
            request = state["requests"].get(request_id)
            if not isinstance(request, dict) or request.get("state") != "uncertain":
                raise BrokerRejected("broker reservation is unavailable")
            return state["permits"][request["permit_id"]]["max_response_bytes"]

    def fence_epoch(self, permit_id: str, epoch: int) -> None:
        if (not isinstance(permit_id, str) or _SHA.fullmatch(permit_id) is None
                or type(epoch) is not int or epoch < 1):
            raise BrokerRejected("broker permit epoch is invalid")
        with self._locked() as state:
            entry = state["permits"].get(permit_id)
            if not isinstance(entry, dict) or entry.get("epoch") != epoch:
                raise BrokerRejected("broker permit epoch differs")
            if not entry["fenced"]:
                entry["fenced"] = True
                self._save(state)


class _DeliveryBody:
    """Single-pass bounded body, committed only after the guest's exact ACK."""

    def __init__(self, source: bytes | Iterable[bytes], ledger: PermitLedger,
                 request_id: str, limit: int):
        if isinstance(source, bytes) and len(source) > limit:
            raise BrokerRejected("broker response exceeds the finite byte budget")
        self._source = source
        self._ledger = ledger
        self._request_id = request_id
        self._limit = limit
        self._started = False
        self._closed = False
        self._source_closed = False
        self._complete = False
        self._digest: str | None = None
        self._iterator: Iterator[bytes] | None = None

    def __iter__(self) -> Iterator[bytes]:
        if self._started:
            raise BrokerRejected("broker response stream cannot be replayed")
        self._started = True
        self._iterator = self._consume()
        return self._iterator

    def _consume(self) -> Iterator[bytes]:
        source = (self._source,) if isinstance(self._source, bytes) else iter(self._source)
        digest = hashlib.sha256()
        count = 0
        try:
            for chunk in source:
                if not isinstance(chunk, bytes):
                    raise BrokerRejected("broker response stream chunk is invalid")
                count += len(chunk)
                if count > self._limit:
                    raise BrokerRejected("broker response exceeds the finite byte budget")
                digest.update(chunk)
                yield chunk
            self._digest = digest.hexdigest()
            self._complete = True
        finally:
            try:
                if source is not self._source:
                    close = getattr(source, "close", None)
                    if callable(close):
                        close()
            finally:
                self._close_source()

    @property
    def digest(self) -> str:
        if not self._complete or self._digest is None:
            raise BrokerRejected("broker response delivery is incomplete")
        return self._digest

    def acknowledge(self, digest: str) -> None:
        if digest != self.digest:
            raise BrokerRejected("relay response acknowledgment differs")
        self._ledger.settle(self._request_id, digest)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._started = True
        try:
            if self._iterator is not None:
                self._iterator.close()
        finally:
            self._close_source()

    def _close_source(self) -> None:
        if self._source_closed:
            return
        self._source_closed = True
        close = getattr(self._source, "close", None)
        if callable(close):
            close()


def stream_and_settle(response: BrokerResponse, ledger: PermitLedger,
                      request_id: str) -> BrokerResponse:
    """Prepare one bounded delivery; settlement awaits a guest completion ACK."""
    body = _DeliveryBody(response.body, ledger, request_id,
                         ledger.response_limit(request_id))
    return BrokerResponse(response.status, response.headers, body)


def dispatch_once(request: RelayRequest, ledger: PermitLedger, broker: Broker) -> BrokerResponse:
    if not isinstance(request, RelayRequest) or not isinstance(broker, Broker):
        raise BrokerRejected("broker relay request is invalid")
    ledger.require_permit(request.permit_id, request.epoch, broker.permit)
    if is_authenticated_agy_auxiliary(
        broker.permit, request.method, request.provider_path, request.headers, request.body,
    ):
        ledger.record_auxiliary_refusal(request.permit_id, request.epoch)
        raise BrokerRejected("broker agy auxiliary model is intentionally refused")
    path, outgoing, body, limit = broker.validate_request(
        request.method, request.provider_path, request.headers, request.body)
    document = json.loads(body)
    wire_model = (path.split(":", 1)[0].removeprefix("/v1beta/models/")
                  if broker.permit.provider == "agy" else document["model"])
    prepared = (broker.preview_upstream(request.method, path, outgoing, body)
                if broker.permit.upstream_route_digest is not None else None)
    identity = OutboundRequestIdentity(
        request.method, path, hashlib.sha256(body).hexdigest(), limit, wire_model,
        upstream_route_digest=(prepared.route_digest if prepared else None),
        upstream_origin=(prepared.origin if prepared else None),
        upstream_path=(prepared.path if prepared else None),
        upstream_body_sha256=(hashlib.sha256(prepared.body).hexdigest()
                              if prepared else None),
        upstream_model=(prepared.effective_model if prepared else None))
    ledger.reserve(request.permit_id, request.epoch, request.request_id, identity)
    reservation = ((ledger, request.permit_id, request.epoch,
                    request.request_id, identity)
                   if prepared is not None else None)
    response = broker.dispatch(request.method, request.provider_path,
                               request.headers, request.body,
                               reservation=reservation)
    return stream_and_settle(response, ledger, request.request_id)


class HostRelay:
    """Serve one serialized seat over host-opened pinned SSH stdio frames."""

    def __init__(self, ledger: PermitLedger, broker: Broker):
        self.ledger = ledger
        self.broker = broker

    def serve(self, frames: FrameStream) -> None:
        ready = frames.recv()
        if (not isinstance(ready, dict) or ready.keys() != {"type", "seat_id", "permit_id", "epoch"}
                or ready["type"] != "ready" or ready["seat_id"] != self.broker.permit.seat_id):
            raise BrokerRejected("relay seat readiness differs")
        self.ledger.require_permit(ready["permit_id"], ready["epoch"], self.broker.permit)
        frames.send({"type": "ready_ack"})
        permit = self.broker.permit
        max_attempts = attempt_limit(permit)
        while True:
            frame = frames.recv()
            if frame == {"type": "close"}:
                frames.send({"type": "closed"})
                return
            if self.ledger.attempt_counts(ready["permit_id"], ready["epoch"])[0] >= max_attempts:
                raise BrokerRejected("relay request attempt budget is exhausted")
            self.ledger.record_attempt(ready["permit_id"], ready["epoch"])
            try:
                request = parse_request_frame(frame)
                if request.permit_id != ready["permit_id"] or request.epoch != ready["epoch"]:
                    raise BrokerRejected("relay permit identity differs")
                response = dispatch_once(request, self.ledger, self.broker)
            except BrokerRejected:
                frames.send({"type": "reject", "status": 403})
                continue
            body = response.body
            try:
                frames.send({"type": "response_start", "request_id": request.request_id,
                             "status": response.status, "headers": dict(response.headers)})
                for chunk in body:
                    for offset in range(0, len(chunk), _CHUNK_BYTES):
                        frames.send({"type": "response_chunk", "request_id": request.request_id,
                                     "body_b64": base64.b64encode(
                                         chunk[offset:offset + _CHUNK_BYTES]).decode()})
                digest = body.digest
                frames.send({"type": "response_end", "request_id": request.request_id,
                             "response_digest": digest})
                ack = frames.recv()
                if ack != {"type": "response_ack", "request_id": request.request_id,
                           "response_digest": digest}:
                    raise BrokerRejected("relay response acknowledgment differs")
                body.acknowledge(digest)
            finally:
                body.close()
