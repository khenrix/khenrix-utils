"""Exact, content-bound fanout checkpoints over the local memory controller."""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence

from .artifacts import ArtifactRef, ArtifactStore, canonical_json
from .errors import (
    ArtifactError,
    ArtifactExistsError,
    MemoryDurabilityError,
    MemoryEquivocationError,
    MemoryError,
    MemoryIntegrityError,
    MemoryProtocolError,
    MemoryTransportError,
    MemoryValidationError,
    RunStateError,
)
from .process import ProcessCommand, ProcessResult, ProcessStatus, run_command
from .runstate import OwnerCapability, RunJournal


CONTROLLER_SCHEMA = "fanout-memory-exchange-v1"
CHECKPOINT_SCHEMA = "fanout-checkpoint-v1"
ENVELOPE_SCHEMA = "fanout-checkpoint-envelope-v1"
PUBLICATION_SCHEMA = "fanout-checkpoint-publication-v1"
RECEIPT_SCHEMA = "fanout-checkpoint-receipt-v1"
INTENT_SCHEMA = "fanout-checkpoint-intent-v1"
MAX_IDENTITY_BYTES = 96
MAX_ANSWER_BYTES = 3 * 1024 * 1024
MAX_CONTROLLER_BYTES = 4 * 1024 * 1024
MAX_CONTROLLER_OUTPUT_BYTES = 17 * 1024 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
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


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_positive_int(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_SAFE_INTEGER


def _bounded_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise MemoryValidationError(f"{label} must be a bounded identity")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise MemoryValidationError(f"{label} must be a bounded identity")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise MemoryValidationError(f"{label} must be valid UTF-8") from error
    if len(encoded) > MAX_IDENTITY_BYTES:
        raise MemoryValidationError(f"{label} must be a bounded identity")
    return value


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise MemoryValidationError(f"{label} must be bounded text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise MemoryValidationError(f"{label} must be bounded text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise MemoryValidationError(f"{label} must be valid UTF-8") from error
    if len(encoded) > maximum:
        raise MemoryValidationError(f"{label} must be bounded text")
    return value


def _segment(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).rstrip(b"=").decode("ascii")


def _strict_json(data: bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        return json.loads(
            data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise MemoryProtocolError("memory controller response is malformed") from error


@dataclass(frozen=True, slots=True)
class CheckpointIdentity:
    """The complete immutable identity of one provider seat turn."""

    run_id: str
    task_id: str
    seat_id: str
    attempt: int
    round: int
    provider_session_id: str

    def __post_init__(self) -> None:
        for name in ("run_id", "task_id", "seat_id", "provider_session_id"):
            _bounded_identity(getattr(self, name), name.replace("_", " "))
        if not _is_positive_int(self.attempt) or not _is_positive_int(self.round):
            raise MemoryValidationError("checkpoint attempt and round must be positive integers")
        if len(self.key.encode("ascii")) > 900:
            raise MemoryValidationError("checkpoint key exceeds its bounded size")
        if len(self.project.encode("ascii")) > 512:
            raise MemoryValidationError("checkpoint project exceeds its bounded size")

    @property
    def key(self) -> str:
        return "/".join(
            (
                "v1",
                _segment(self.run_id),
                _segment(self.task_id),
                _segment(self.seat_id),
                str(self.attempt),
                str(self.round),
                _segment(self.provider_session_id),
            )
        )

    @property
    def project(self) -> str:
        return ".".join(
            (
                "llm-fanout-v1",
                _segment(self.run_id),
                _segment(self.task_id),
                _segment(self.seat_id),
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "provider_session_id": self.provider_session_id,
            "round": self.round,
            "run_id": self.run_id,
            "seat_id": self.seat_id,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> "CheckpointIdentity":
        fields = {
            "attempt",
            "provider_session_id",
            "round",
            "run_id",
            "seat_id",
            "task_id",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise MemoryValidationError("checkpoint identity has unknown or missing fields")
        return cls(**{name: value[name] for name in fields})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True, init=False)
class Checkpoint:
    """One canonical answer payload and the exact surface saved to claude-mem."""

    identity: CheckpointIdentity
    answer: str = field(repr=False)
    payload_bytes: bytes = field(repr=False)
    digest: str
    title: str
    text: str = field(repr=False)
    project: str

    def __init__(self, identity: CheckpointIdentity, answer: str) -> None:
        if not isinstance(identity, CheckpointIdentity):
            raise MemoryValidationError("checkpoint identity is invalid")
        if not isinstance(answer, str) or not answer or "\x00" in answer:
            raise MemoryValidationError("checkpoint answer must be non-empty text")
        try:
            answer_bytes = answer.encode("utf-8")
        except UnicodeEncodeError as error:
            raise MemoryValidationError("checkpoint answer must be valid UTF-8") from error
        if len(answer_bytes) > MAX_ANSWER_BYTES:
            raise MemoryValidationError("checkpoint answer exceeds its bounded size")
        payload = {
            "answer": answer,
            "answer_sha256": _digest(answer_bytes),
            "identity": identity.to_dict(),
            "schema_version": CHECKPOINT_SCHEMA,
        }
        payload_bytes = canonical_json(payload)
        checkpoint_digest = _digest(payload_bytes)
        title = (
            f"llm-fanout checkpoint key={identity.key} sha256={checkpoint_digest}"
        )
        envelope = {
            "checkpoint_digest": checkpoint_digest,
            "checkpoint_key": identity.key,
            "payload": payload,
            "schema_version": ENVELOPE_SCHEMA,
        }
        text = canonical_json(envelope).decode("utf-8")[:-1]
        if len(title.encode("utf-8")) > 1024 or len(text.encode("utf-8")) > 4 * 1024 * 1024:
            raise MemoryValidationError("checkpoint save surface exceeds its bounded size")
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "answer", answer)
        object.__setattr__(self, "payload_bytes", payload_bytes)
        object.__setattr__(self, "digest", checkpoint_digest)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "project", identity.project)

    @classmethod
    def create(cls, identity: CheckpointIdentity, answer: str) -> "Checkpoint":
        return cls(identity, answer)


def _artifact_dict(ref: ArtifactRef) -> dict[str, object]:
    return {"digest": ref.digest, "path": ref.path, "size": ref.size}


def _artifact_ref(value: object) -> ArtifactRef:
    if not isinstance(value, Mapping) or set(value) != {"digest", "path", "size"}:
        raise MemoryValidationError("artifact reference has unknown or missing fields")
    path, digest, size = value["path"], value["digest"], value["size"]
    if not isinstance(path, str) or not path or "\\" in path:
        raise MemoryValidationError("artifact reference path is invalid")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise MemoryValidationError("artifact reference path is invalid")
    if not _is_digest(digest) or type(size) is not int or size < 0:
        raise MemoryValidationError("artifact reference identity is invalid")
    return ArtifactRef(path, digest, size)


def _artifact_prefix_for_key(key: str) -> str:
    return f"memory/{_digest(key.encode('ascii'))}"


@dataclass(frozen=True, slots=True)
class CheckpointPublication:
    """Durable evidence that one save returned an exact observation identity."""

    identity: CheckpointIdentity
    checkpoint_key: str
    checkpoint_digest: str
    project: str
    observation_id: int
    controller_sha256: str
    checkpoint_ref: ArtifactRef
    intent_ref: ArtifactRef
    publication_ref: ArtifactRef | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.identity, CheckpointIdentity):
            raise MemoryValidationError("publication identity is invalid")
        if self.checkpoint_key != self.identity.key or not _is_digest(self.checkpoint_digest):
            raise MemoryValidationError("publication checkpoint binding is invalid")
        if self.project != self.identity.project or not _is_positive_int(self.observation_id):
            raise MemoryValidationError("publication observation binding is invalid")
        if not _is_digest(self.controller_sha256):
            raise MemoryValidationError("publication controller binding is invalid")
        for ref in (self.checkpoint_ref, self.intent_ref):
            _artifact_ref(_artifact_dict(ref))
        if self.publication_ref is not None:
            _artifact_ref(_artifact_dict(self.publication_ref))

    def to_dict(self) -> dict[str, object]:
        return {
            "checkpoint_digest": self.checkpoint_digest,
            "checkpoint_key": self.checkpoint_key,
            "checkpoint_ref": _artifact_dict(self.checkpoint_ref),
            "controller_sha256": self.controller_sha256,
            "identity": self.identity.to_dict(),
            "intent_ref": _artifact_dict(self.intent_ref),
            "observation_id": self.observation_id,
            "project": self.project,
            "schema_version": PUBLICATION_SCHEMA,
        }

    @classmethod
    def from_dict(cls, value: object) -> "CheckpointPublication":
        fields = {
            "checkpoint_digest",
            "checkpoint_key",
            "checkpoint_ref",
            "controller_sha256",
            "identity",
            "intent_ref",
            "observation_id",
            "project",
            "schema_version",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != fields
            or value.get("schema_version") != PUBLICATION_SCHEMA
        ):
            raise MemoryValidationError("publication has unknown or missing fields")
        publication = cls(
            identity=CheckpointIdentity.from_dict(value["identity"]),
            checkpoint_key=value["checkpoint_key"],  # type: ignore[arg-type]
            checkpoint_digest=value["checkpoint_digest"],  # type: ignore[arg-type]
            project=value["project"],  # type: ignore[arg-type]
            observation_id=value["observation_id"],  # type: ignore[arg-type]
            controller_sha256=value["controller_sha256"],  # type: ignore[arg-type]
            checkpoint_ref=_artifact_ref(value["checkpoint_ref"]),
            intent_ref=_artifact_ref(value["intent_ref"]),
        )
        encoded = canonical_json(publication.to_dict())
        ref = ArtifactRef(
            f"{_artifact_prefix_for_key(publication.checkpoint_key)}/published.json",
            _digest(encoded),
            len(encoded),
        )
        return dataclasses.replace(publication, publication_ref=ref)


@dataclass(frozen=True, slots=True)
class CheckpointReceipt:
    """Verified observation evidence suitable for exact peer-packet recovery."""

    publication: CheckpointPublication
    memory_session_id: str
    observation_sha256: str
    verified: bool = True
    verification_ref: ArtifactRef | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.publication, CheckpointPublication):
            raise MemoryValidationError("receipt publication is invalid")
        _bounded_text(self.memory_session_id, "memory session id", 1024)
        if not _is_digest(self.observation_sha256) or self.verified is not True:
            raise MemoryValidationError("receipt verification binding is invalid")
        if self.verification_ref is not None:
            _artifact_ref(_artifact_dict(self.verification_ref))

    @property
    def identity(self) -> CheckpointIdentity:
        return self.publication.identity

    @property
    def checkpoint_key(self) -> str:
        return self.publication.checkpoint_key

    @property
    def checkpoint_digest(self) -> str:
        return self.publication.checkpoint_digest

    @property
    def observation_id(self) -> int:
        return self.publication.observation_id

    @property
    def checkpoint_ref(self) -> ArtifactRef:
        return self.publication.checkpoint_ref

    @property
    def intent_ref(self) -> ArtifactRef:
        return self.publication.intent_ref

    @property
    def publication_ref(self) -> ArtifactRef:
        if self.publication.publication_ref is None:
            raise MemoryDurabilityError("publication reference is missing")
        return self.publication.publication_ref

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_session_id": self.memory_session_id,
            "observation_sha256": self.observation_sha256,
            "publication": self.publication.to_dict(),
            "schema_version": RECEIPT_SCHEMA,
            "verified": True,
        }

    @classmethod
    def from_dict(cls, value: object) -> "CheckpointReceipt":
        fields = {
            "memory_session_id",
            "observation_sha256",
            "publication",
            "schema_version",
            "verified",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != fields
            or value.get("schema_version") != RECEIPT_SCHEMA
        ):
            raise MemoryValidationError("receipt has unknown or missing fields")
        receipt = cls(
            publication=CheckpointPublication.from_dict(value["publication"]),
            memory_session_id=value["memory_session_id"],  # type: ignore[arg-type]
            observation_sha256=value["observation_sha256"],  # type: ignore[arg-type]
            verified=value["verified"],  # type: ignore[arg-type]
        )
        encoded = canonical_json(receipt.to_dict())
        ref = ArtifactRef(
            f"{_artifact_prefix_for_key(receipt.checkpoint_key)}/verified.json",
            _digest(encoded),
            len(encoded),
        )
        return dataclasses.replace(receipt, verification_ref=ref)


class CheckpointBlockedError(MemoryError):
    """A durable memory block that requires exact recovery or owner action."""

    def __init__(
        self,
        stage: str,
        *,
        publication: CheckpointPublication | None = None,
    ) -> None:
        super().__init__("checkpoint exchange is blocked-memory")
        self.stage = stage
        self.publication = publication

    def __repr__(self) -> str:
        return (
            f"CheckpointBlockedError(stage={self.stage!r}, "
            "checkpoint=<redacted>, publication=<redacted>)"
        )


class MemoryController:
    """Pinned stdin/stdout client for Task 10's exact controller executable."""

    __slots__ = ("executable", "executable_sha256", "timeout", "_identity")

    def __init__(
        self,
        executable: Path | str,
        *,
        executable_sha256: str,
        timeout: float = 30.0,
    ) -> None:
        path = Path(executable)
        if not path.is_absolute():
            raise MemoryValidationError("memory controller path must be absolute")
        if path.name != "memory_exchange.py":
            raise MemoryValidationError("memory controller executable name is invalid")
        if not _is_digest(executable_sha256):
            raise MemoryValidationError("memory controller digest is invalid")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 120
        ):
            raise MemoryValidationError("memory controller timeout is invalid")
        self.executable = path
        self.executable_sha256 = executable_sha256
        self.timeout = float(timeout)
        self._identity = self._verify_executable()

    def _verify_executable(self) -> tuple[int, int, int]:
        try:
            if self.executable.resolve(strict=True) != self.executable:
                raise OSError("controller path contains a symlink")
            parent = self.executable.parent.lstat()
            entry = self.executable.lstat()
            fd = os.open(self.executable, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                opened = os.fstat(fd)
                if opened.st_size > MAX_CONTROLLER_BYTES:
                    raise OSError("controller exceeds its bounded size")
                data = bytearray()
                while len(data) < opened.st_size:
                    chunk = os.read(fd, min(64 * 1024, opened.st_size - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
            finally:
                os.close(fd)
        except OSError as error:
            raise MemoryValidationError("memory controller executable is unsafe") from error
        mode = stat.S_IMODE(opened.st_mode)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) & 0o022
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(entry.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or mode != 0o700
            or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)
            or len(data) != opened.st_size
            or _digest(bytes(data)) != self.executable_sha256
        ):
            raise MemoryValidationError("memory controller executable identity changed")
        return opened.st_dev, opened.st_ino, opened.st_size

    def save(self, checkpoint: Checkpoint) -> int:
        if not isinstance(checkpoint, Checkpoint):
            raise MemoryValidationError("save requires a checkpoint")
        response = self._invoke(
            {
                "operation": "save",
                "project": checkpoint.project,
                "schema_version": CONTROLLER_SCHEMA,
                "text": checkpoint.text,
                "title": checkpoint.title,
            },
            "save",
        )
        result = response["result"]
        fields = {"id", "message", "project", "success", "title"}
        if (
            not isinstance(result, Mapping)
            or set(result) != fields
            or result["success"] is not True
            or not _is_positive_int(result["id"])
            or result["project"] != checkpoint.project
            or result["title"] != checkpoint.title
            or result["message"] != f"Memory saved as observation #{result['id']}"
        ):
            raise MemoryProtocolError("memory save response does not bind the checkpoint")
        return result["id"]  # type: ignore[return-value]

    def preflight(self) -> bool:
        """Recheck the pinned controller executable without invoking external memory."""
        if self._verify_executable() != self._identity:
            raise MemoryValidationError("memory controller executable identity changed")
        return True

    def fetch(self, ids: Sequence[int]) -> tuple[dict[str, object], ...]:
        if (
            isinstance(ids, (str, bytes))
            or not isinstance(ids, Sequence)
            or not 1 <= len(ids) <= 256
            or any(not _is_positive_int(item) for item in ids)
            or len(set(ids)) != len(ids)
        ):
            raise MemoryValidationError("exact fetch ids are invalid")
        requested = tuple(ids)
        response = self._invoke(
            {
                "ids": list(requested),
                "operation": "fetch",
                "schema_version": CONTROLLER_SCHEMA,
            },
            "fetch",
        )
        result = response["result"]
        if not isinstance(result, list) or len(result) != len(requested):
            raise MemoryProtocolError("exact fetch response has incomplete membership")
        rows: list[dict[str, object]] = []
        seen: set[int] = set()
        for expected, row in zip(requested, result, strict=True):
            if not isinstance(row, dict) or not _is_positive_int(row.get("id")):
                raise MemoryProtocolError("exact fetch response contains an invalid id")
            observation_id = row["id"]  # exact integer established above
            if observation_id in seen:
                raise MemoryProtocolError("exact fetch response duplicated an id")
            seen.add(observation_id)
            if observation_id != expected:
                raise MemoryProtocolError("exact fetch response changed requested order")
            rows.append(row)
        return tuple(rows)

    def _invoke(self, request: Mapping[str, object], operation: str) -> dict[str, object]:
        before = self._verify_executable()
        if before != self._identity:
            raise MemoryValidationError("memory controller executable identity changed")
        command = ProcessCommand(
            argv=(sys.executable, str(self.executable)),
            stdin=canonical_json(request),
            cwd=self.executable.parent,
            timeout=self.timeout,
            environment={},
        )
        try:
            result = run_command(command, base_environment={})
        except Exception:
            raise MemoryTransportError("memory controller process boundary failed") from None
        if not isinstance(result, ProcessResult):
            raise MemoryProtocolError("memory controller process result is invalid")
        after = self._verify_executable()
        if after != self._identity:
            raise MemoryValidationError("memory controller executable identity changed")
        if len(result.stdout) > MAX_CONTROLLER_OUTPUT_BYTES or len(result.stderr) > MAX_CONTROLLER_OUTPUT_BYTES:
            raise MemoryProtocolError("memory controller output exceeds its bounded size")
        if result.stderr:
            raise MemoryProtocolError("memory controller contaminated stderr")
        if result.status is not ProcessStatus.EXIT:
            raise MemoryTransportError("memory controller did not terminate successfully")
        parsed = _strict_json(result.stdout)
        if not isinstance(parsed, dict) or canonical_json(parsed) != result.stdout:
            raise MemoryProtocolError("memory controller response is noncanonical")
        if result.returncode != 0:
            fields = {"error", "ok", "schema_version"}
            error = parsed.get("error")
            if (
                set(parsed) != fields
                or parsed.get("ok") is not False
                or parsed.get("schema_version") != CONTROLLER_SCHEMA
                or not isinstance(error, dict)
                or set(error) != {"code", "message"}
                or not isinstance(error.get("code"), str)
                or error.get("message") != "memory exchange failed"
            ):
                raise MemoryProtocolError("memory controller failure envelope is invalid")
            raise MemoryTransportError("memory controller reported a classified failure")
        fields = {"ok", "operation", "result", "schema_version"}
        if (
            set(parsed) != fields
            or parsed.get("ok") is not True
            or parsed.get("operation") != operation
            or parsed.get("schema_version") != CONTROLLER_SCHEMA
        ):
            raise MemoryProtocolError("memory controller success envelope is invalid")
        return parsed

    def __repr__(self) -> str:
        return (
            f"MemoryController(executable={str(self.executable)!r}, "
            f"sha256={self.executable_sha256!r}, credential=<redacted>)"
        )


class MemoryCheckpointExchange:
    """Publish, verify, and recover checkpoint evidence using exact IDs only."""

    __slots__ = ("controller", "artifacts")

    def __init__(self, controller: MemoryController, artifacts: ArtifactStore) -> None:
        if not isinstance(controller, MemoryController):
            raise TypeError("controller must be a MemoryController")
        if not isinstance(artifacts, ArtifactStore):
            raise TypeError("artifacts must be an ArtifactStore")
        self.controller = controller
        self.artifacts = artifacts

    def preflight(self) -> bool:
        """Authenticate this exact controller and artifact route before provider spend."""
        if self.controller.preflight() is not True:
            raise MemoryValidationError("memory controller preflight did not report healthy")
        descriptor = self.artifacts._root_fd_copy()
        os.close(descriptor)
        return True

    def publish(
        self,
        checkpoint: Checkpoint,
        *,
        journal: RunJournal,
        owner: OwnerCapability,
    ) -> CheckpointReceipt:
        self._validate_inputs(checkpoint, journal, owner)
        checkpoint_ref, intent_ref = self._persist_intent(checkpoint)
        phase = journal.state.seat_phase(
            checkpoint.identity.task_id,
            checkpoint.identity.seat_id,
            checkpoint.identity.attempt,
            checkpoint.identity.round,
        )
        if phase == "publication-intent":
            self._block(journal, checkpoint.identity, owner)
            raise CheckpointBlockedError("ambiguous-publication-intent")
        if phase != "artifacts-durable":
            raise MemoryDurabilityError("checkpoint seat is not ready for publication")
        journal.append(
            "publication-intent",
            task_id=checkpoint.identity.task_id,
            seat_id=checkpoint.identity.seat_id,
            attempt=checkpoint.identity.attempt,
            round=checkpoint.identity.round,
        )
        publication: CheckpointPublication | None = None
        try:
            observation_id = self.controller.save(checkpoint)
            publication = self._persist_publication(
                checkpoint, observation_id, checkpoint_ref, intent_ref
            )
            journal.append(
                "checkpoint-published",
                task_id=checkpoint.identity.task_id,
                seat_id=checkpoint.identity.seat_id,
                attempt=checkpoint.identity.attempt,
                round=checkpoint.identity.round,
                evidence_sha256=publication.publication_ref.digest,
            )
            observation = self.controller.fetch((observation_id,))[0]
            receipt = self._verify_and_persist(checkpoint, publication, observation)
            journal.append(
                "checkpoint-verified",
                task_id=checkpoint.identity.task_id,
                seat_id=checkpoint.identity.seat_id,
                attempt=checkpoint.identity.attempt,
                round=checkpoint.identity.round,
                evidence_sha256=receipt.verification_ref.digest,
            )
            return receipt
        except CheckpointBlockedError:
            raise
        except (MemoryError, ArtifactError, RunStateError) as error:
            self._block(journal, checkpoint.identity, owner)
            raise CheckpointBlockedError(
                "publication-failed", publication=publication
            ) from error

    def recover(
        self,
        checkpoint: Checkpoint,
        publication: CheckpointPublication,
        *,
        journal: RunJournal,
        owner: OwnerCapability,
    ) -> CheckpointReceipt:
        self._validate_inputs(checkpoint, journal, owner)
        if not isinstance(publication, CheckpointPublication):
            raise MemoryValidationError("recovery publication is invalid")
        phase = journal.state.seat_phase(
            checkpoint.identity.task_id,
            checkpoint.identity.seat_id,
            checkpoint.identity.attempt,
            checkpoint.identity.round,
        )
        if phase not in {
            "publication-intent",
            "checkpoint-published",
            "checkpoint-verified",
        }:
            raise MemoryDurabilityError("memory recovery seat phase is invalid")
        task_phase = journal.state.task_phases.get(checkpoint.identity.task_id)
        recovery_owner = (
            checkpoint.identity.seat_id,
            checkpoint.identity.attempt,
            checkpoint.identity.round,
        )
        active_owner = journal.state.memory_block_owners.get(
            checkpoint.identity.task_id
        )
        if phase == "checkpoint-verified":
            if task_phase not in {"blocked-memory", "memory-recovered"}:
                raise MemoryDurabilityError(
                    "verified memory recovery requires a durable recovery phase"
                )
        else:
            if task_phase in {None, "memory-recovered"}:
                self._block(journal, checkpoint.identity, owner)
                task_phase = "blocked-memory"
                active_owner = recovery_owner
            if task_phase != "blocked-memory":
                raise MemoryDurabilityError(
                    "memory recovery requires durable blocked-memory"
                )
            if active_owner != recovery_owner:
                raise MemoryDurabilityError(
                    "memory recovery block belongs to another seat turn"
                )
        try:
            self._verify_publication(checkpoint, publication)
            observation = self.controller.fetch((publication.observation_id,))[0]
            receipt = self._verify_and_persist(checkpoint, publication, observation)
        except (MemoryError, ArtifactError) as error:
            raise CheckpointBlockedError(
                "recovery-failed", publication=publication
            ) from error
        try:
            if phase == "publication-intent":
                journal.append(
                    "checkpoint-published",
                    task_id=checkpoint.identity.task_id,
                    seat_id=checkpoint.identity.seat_id,
                    attempt=checkpoint.identity.attempt,
                    round=checkpoint.identity.round,
                    evidence_sha256=publication.publication_ref.digest,
                )
            if phase != "checkpoint-verified":
                journal.append(
                    "checkpoint-verified",
                    task_id=checkpoint.identity.task_id,
                    seat_id=checkpoint.identity.seat_id,
                    attempt=checkpoint.identity.attempt,
                    round=checkpoint.identity.round,
                    evidence_sha256=receipt.verification_ref.digest,
                )
            if task_phase == "blocked-memory" and active_owner == recovery_owner:
                journal.append(
                    "memory-recovered",
                    task_id=checkpoint.identity.task_id,
                    seat_id=checkpoint.identity.seat_id,
                    attempt=checkpoint.identity.attempt,
                    round=checkpoint.identity.round,
                    owner=owner,
                )
        except RunStateError as error:
            raise MemoryDurabilityError("recovered checkpoint transitions were not durable") from error
        return receipt

    def verify_existing(
        self, checkpoint: Checkpoint, receipt: CheckpointReceipt
    ) -> CheckpointReceipt:
        if not isinstance(checkpoint, Checkpoint) or not isinstance(receipt, CheckpointReceipt):
            raise MemoryValidationError("verified replay inputs are invalid")
        self._verify_receipt_artifacts(checkpoint, receipt)
        observation = self.controller.fetch((receipt.observation_id,))[0]
        memory_session, observation_sha256 = self._verify_observation(
            checkpoint, receipt.observation_id, observation
        )
        if (
            memory_session != receipt.memory_session_id
            or observation_sha256 != receipt.observation_sha256
        ):
            raise MemoryIntegrityError("verified observation no longer matches its receipt")
        return receipt

    def fetch_verified(
        self,
        checkpoints: Iterable[tuple[Checkpoint, CheckpointReceipt]],
    ) -> tuple[CheckpointReceipt, ...]:
        pairs = tuple(checkpoints)
        if not pairs or len(pairs) > 256:
            raise MemoryValidationError("verified fetch batch is empty or oversized")
        ids: list[int] = []
        keys: set[str] = set()
        for checkpoint, receipt in pairs:
            if not isinstance(checkpoint, Checkpoint) or not isinstance(receipt, CheckpointReceipt):
                raise MemoryValidationError("verified fetch batch contains invalid inputs")
            if receipt.observation_id in ids or checkpoint.identity.key in keys:
                raise MemoryValidationError("verified fetch batch contains duplicate associations")
            ids.append(receipt.observation_id)
            keys.add(checkpoint.identity.key)
            self._verify_receipt_artifacts(checkpoint, receipt)
        observations = self.controller.fetch(tuple(ids))
        for (checkpoint, receipt), observation in zip(pairs, observations, strict=True):
            memory_session, observation_sha256 = self._verify_observation(
                checkpoint, receipt.observation_id, observation
            )
            if (
                memory_session != receipt.memory_session_id
                or observation_sha256 != receipt.observation_sha256
            ):
                raise MemoryIntegrityError("batch observation changed association")
        return tuple(receipt for _checkpoint, receipt in pairs)

    @staticmethod
    def _validate_inputs(
        checkpoint: Checkpoint, journal: RunJournal, owner: OwnerCapability
    ) -> None:
        if not isinstance(checkpoint, Checkpoint):
            raise MemoryValidationError("checkpoint is invalid")
        if not isinstance(journal, RunJournal):
            raise MemoryValidationError("run journal is invalid")
        if not isinstance(owner, OwnerCapability):
            raise MemoryValidationError("owner capability is invalid")
        if journal.inputs.run_id != checkpoint.identity.run_id:
            raise MemoryValidationError("checkpoint belongs to another run")

    @staticmethod
    def _prefix(checkpoint: Checkpoint) -> str:
        return _artifact_prefix_for_key(checkpoint.identity.key)

    def _write_exact(self, path: str, data: bytes, *, equivocation: bool = False) -> ArtifactRef:
        expected = ArtifactRef(path, _digest(data), len(data))
        try:
            return self.artifacts.write_bytes(path, data)
        except ArtifactExistsError:
            existing = self._read_private_path(path)
            if existing != data:
                if equivocation:
                    raise MemoryEquivocationError(
                        "checkpoint identity already has another digest"
                    )
                raise MemoryDurabilityError("durable memory evidence changed")
            return expected
        except ArtifactError as error:
            raise MemoryDurabilityError("durable memory evidence could not be written") from error

    def _persist_intent(self, checkpoint: Checkpoint) -> tuple[ArtifactRef, ArtifactRef]:
        prefix = self._prefix(checkpoint)
        checkpoint_ref = self._write_exact(
            f"{prefix}/checkpoint.json",
            checkpoint.text.encode("utf-8"),
            equivocation=True,
        )
        intent = {
            "checkpoint_digest": checkpoint.digest,
            "checkpoint_key": checkpoint.identity.key,
            "checkpoint_ref": _artifact_dict(checkpoint_ref),
            "controller_sha256": self.controller.executable_sha256,
            "identity": checkpoint.identity.to_dict(),
            "project": checkpoint.project,
            "schema_version": INTENT_SCHEMA,
            "title": checkpoint.title,
        }
        intent_ref = self._write_exact(
            f"{prefix}/intent.json", canonical_json(intent), equivocation=True
        )
        return checkpoint_ref, intent_ref

    def _intent_document(
        self, checkpoint: Checkpoint, checkpoint_ref: ArtifactRef
    ) -> dict[str, object]:
        return {
            "checkpoint_digest": checkpoint.digest,
            "checkpoint_key": checkpoint.identity.key,
            "checkpoint_ref": _artifact_dict(checkpoint_ref),
            "controller_sha256": self.controller.executable_sha256,
            "identity": checkpoint.identity.to_dict(),
            "project": checkpoint.project,
            "schema_version": INTENT_SCHEMA,
            "title": checkpoint.title,
        }

    def _persist_publication(
        self,
        checkpoint: Checkpoint,
        observation_id: int,
        checkpoint_ref: ArtifactRef,
        intent_ref: ArtifactRef,
    ) -> CheckpointPublication:
        publication = CheckpointPublication(
            identity=checkpoint.identity,
            checkpoint_key=checkpoint.identity.key,
            checkpoint_digest=checkpoint.digest,
            project=checkpoint.project,
            observation_id=observation_id,
            controller_sha256=self.controller.executable_sha256,
            checkpoint_ref=checkpoint_ref,
            intent_ref=intent_ref,
        )
        ref = self._write_exact(
            f"{self._prefix(checkpoint)}/published.json",
            canonical_json(publication.to_dict()),
        )
        return dataclasses.replace(publication, publication_ref=ref)

    def _verify_publication(
        self, checkpoint: Checkpoint, publication: CheckpointPublication
    ) -> None:
        if (
            publication.identity != checkpoint.identity
            or publication.checkpoint_key != checkpoint.identity.key
            or publication.checkpoint_digest != checkpoint.digest
            or publication.project != checkpoint.project
            or publication.controller_sha256 != self.controller.executable_sha256
            or publication.publication_ref is None
        ):
            raise MemoryEquivocationError("publication belongs to another checkpoint")
        prefix = self._prefix(checkpoint)
        if (
            publication.checkpoint_ref.path != f"{prefix}/checkpoint.json"
            or publication.intent_ref.path != f"{prefix}/intent.json"
            or publication.publication_ref.path != f"{prefix}/published.json"
        ):
            raise MemoryDurabilityError("publication artifact paths changed association")
        expected_checkpoint = checkpoint.text.encode("utf-8")
        if (
            publication.checkpoint_ref.digest != _digest(expected_checkpoint)
            or publication.checkpoint_ref.size != len(expected_checkpoint)
            or self._read_artifact(publication.checkpoint_ref) != expected_checkpoint
        ):
            raise MemoryDurabilityError("checkpoint artifact no longer verifies")
        expected_intent = canonical_json(
            self._intent_document(checkpoint, publication.checkpoint_ref)
        )
        if (
            publication.intent_ref.digest != _digest(expected_intent)
            or publication.intent_ref.size != len(expected_intent)
            or self._read_artifact(publication.intent_ref) != expected_intent
        ):
            raise MemoryDurabilityError("publication intent no longer verifies")
        expected_publication = canonical_json(publication.to_dict())
        if (
            publication.publication_ref.digest != _digest(expected_publication)
            or publication.publication_ref.size != len(expected_publication)
            or self._read_artifact(publication.publication_ref) != expected_publication
        ):
            raise MemoryDurabilityError("publication receipt no longer verifies")

    def _verify_and_persist(
        self,
        checkpoint: Checkpoint,
        publication: CheckpointPublication,
        observation: Mapping[str, object],
    ) -> CheckpointReceipt:
        self._verify_publication(checkpoint, publication)
        memory_session, observation_sha256 = self._verify_observation(
            checkpoint, publication.observation_id, observation
        )
        receipt = CheckpointReceipt(
            publication=publication,
            memory_session_id=memory_session,
            observation_sha256=observation_sha256,
        )
        ref = self._write_exact(
            f"{self._prefix(checkpoint)}/verified.json",
            canonical_json(receipt.to_dict()),
        )
        return dataclasses.replace(receipt, verification_ref=ref)

    def _verify_receipt_artifacts(
        self, checkpoint: Checkpoint, receipt: CheckpointReceipt
    ) -> None:
        self._verify_publication(checkpoint, receipt.publication)
        if receipt.verification_ref is None:
            raise MemoryDurabilityError("verification receipt reference is missing")
        if receipt.verification_ref.path != f"{self._prefix(checkpoint)}/verified.json":
            raise MemoryDurabilityError("verification receipt path changed association")
        expected = canonical_json(receipt.to_dict())
        if (
            receipt.verification_ref.digest != _digest(expected)
            or receipt.verification_ref.size != len(expected)
            or self._read_artifact(receipt.verification_ref) != expected
        ):
            raise MemoryDurabilityError("verification receipt no longer verifies")

    def _read_artifact(self, ref: ArtifactRef) -> bytes:
        data = self._read_private_path(ref.path)
        if len(data) != ref.size or _digest(data) != ref.digest:
            raise MemoryDurabilityError("durable memory evidence no longer verifies")
        return data

    def _read_private_path(self, path: str) -> bytes:
        """Read one artifact through private, stable descriptors and metadata."""
        try:
            parts = self.artifacts._parts(path)
            root_fd = self.artifacts._root_fd_copy()
        except (ArtifactError, OSError, ValueError) as error:
            raise MemoryDurabilityError("durable memory artifact path is unavailable") from error
        directory_fds = [root_fd]
        directory_links: list[tuple[int, str, int, tuple[int, int]]] = []
        file_fd: int | None = None
        try:
            self._assert_private_directory(root_fd)
            for part in parts[:-1]:
                parent_fd = directory_fds[-1]
                child_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                try:
                    self._assert_private_directory(child_fd)
                except BaseException:
                    os.close(child_fd)
                    raise
                child = os.fstat(child_fd)
                directory_links.append(
                    (parent_fd, part, child_fd, (child.st_dev, child.st_ino))
                )
                directory_fds.append(child_fd)
            directory_fd = directory_fds[-1]
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            before = os.fstat(file_fd)
            entry = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
            if not self._private_file(before) or not self._private_file(entry):
                raise MemoryDurabilityError("durable memory artifact is not owner-private")
            identity = (before.st_dev, before.st_ino)
            if (entry.st_dev, entry.st_ino) != identity:
                raise MemoryDurabilityError("durable memory artifact entry changed")
            if before.st_size > self.artifacts.limits.max_file_bytes:
                raise MemoryDurabilityError("durable memory artifact exceeds its bounded size")
            data = bytearray()
            while len(data) < before.st_size:
                chunk = os.read(
                    file_fd, min(64 * 1024, before.st_size - len(data))
                )
                if not chunk:
                    break
                data.extend(chunk)
            after = os.fstat(file_fd)
            final_entry = os.stat(
                parts[-1], dir_fd=directory_fd, follow_symlinks=False
            )
            for held_directory_fd in directory_fds:
                self._assert_private_directory(held_directory_fd)
            for parent_fd, name, child_fd, child_identity in directory_links:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or current.st_uid != os.getuid()
                    or stat.S_IMODE(current.st_mode) != 0o700
                    or (current.st_dev, current.st_ino) != child_identity
                ):
                    raise MemoryDurabilityError(
                        "durable memory artifact directory changed while reading"
                    )
            if (
                len(data) != before.st_size
                or (after.st_dev, after.st_ino) != identity
                or (final_entry.st_dev, final_entry.st_ino) != identity
                or not self._private_file(after)
                or not self._private_file(final_entry)
                or after.st_size != before.st_size
            ):
                raise MemoryDurabilityError("durable memory artifact changed while reading")
            return bytes(data)
        except MemoryDurabilityError:
            raise
        except (ArtifactError, OSError, ValueError) as error:
            raise MemoryDurabilityError("durable memory artifact is unavailable") from error
        finally:
            if file_fd is not None:
                os.close(file_fd)
            for directory_fd in reversed(directory_fds):
                os.close(directory_fd)

    @staticmethod
    def _assert_private_directory(fd: int) -> None:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise MemoryDurabilityError("durable memory artifact directory is unsafe")

    @staticmethod
    def _private_file(metadata: os.stat_result) -> bool:
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == 0o600
        )

    @staticmethod
    def _verify_observation(
        checkpoint: Checkpoint,
        observation_id: int,
        observation: Mapping[str, object],
    ) -> tuple[str, str]:
        if not isinstance(observation, Mapping) or set(observation) != _OBSERVATION_FIELDS:
            raise MemoryIntegrityError("observation schema does not match the pinned worker")
        expected_session = f"manual-{checkpoint.project}-claude"
        fixed = {
            "id": observation_id,
            "project": checkpoint.project,
            "memory_session_id": expected_session,
            "type": "discovery",
            "title": checkpoint.title,
            "subtitle": "Manual memory",
            "narrative": checkpoint.text,
            "text": None,
            "facts": "[]",
            "concepts": "[]",
            "files_read": "[]",
            "files_modified": "[]",
            "metadata": None,
            "prompt_number": None,
            "discovery_tokens": 0,
            "generated_by_model": None,
            "agent_type": None,
            "agent_id": None,
            "merged_into_project": None,
            "origin_device_id": None,
            "origin_local_id": None,
        }
        if any(observation.get(name) != value for name, value in fixed.items()):
            raise MemoryIntegrityError("observation is foreign or its checkpoint surface changed")
        expected_hash = hashlib.sha256(
            f"{expected_session}\0{checkpoint.title}\0{checkpoint.text}".encode("utf-8")
        ).hexdigest()[:16]
        if observation.get("content_hash") != expected_hash:
            raise MemoryIntegrityError("observation content hash does not bind the checkpoint")
        return expected_session, _digest(canonical_json(dict(observation)))

    @staticmethod
    def _block(
        journal: RunJournal,
        identity: CheckpointIdentity,
        owner: OwnerCapability,
    ) -> None:
        phase = journal.state.task_phases.get(identity.task_id)
        recovery_owner = (identity.seat_id, identity.attempt, identity.round)
        if phase == "blocked-memory":
            if journal.state.memory_block_owners.get(identity.task_id) != recovery_owner:
                raise MemoryDurabilityError(
                    "task is blocked by another memory recovery"
                )
            return
        if phase not in {None, "memory-recovered"}:
            raise MemoryDurabilityError("task cannot enter blocked-memory from its current phase")
        try:
            journal.append(
                "blocked-memory",
                task_id=identity.task_id,
                seat_id=identity.seat_id,
                attempt=identity.attempt,
                round=identity.round,
                owner=owner,
            )
        except RunStateError as error:
            raise MemoryDurabilityError("blocked-memory could not be made durable") from error
