"""Durable, owner-controlled reconstruction of one fanout run.

The journal is deliberately independent of scheduling and providers.  It only
records evidence that has already happened, verifies that evidence on resume,
and refuses to turn an ambiguous process start into an automatic retry.
"""
from __future__ import annotations

import datetime as _datetime
import base64
import binascii
import errno
import fcntl
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import stat
import threading
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from .artifacts import ArtifactRef, ArtifactStore, canonical_json
from .errors import ArtifactError, ProviderError, RunAuthorizationError, RunLockError, RunStateError


_EVENT_SCHEMA = "fanout-run-event-v1"
_SNAPSHOT_SCHEMA = "fanout-run-snapshot-v1"
_BRANCH_SNAPSHOT_SCHEMA = "fanout-run-snapshot-v2"
_BRANCH_INTENT_SCHEMA = "fanout-branch-handover-intent-v2"
_BRANCH_TERMINAL_SCHEMA = "fanout-branch-handover-terminal-v2"
_BRANCH_INTENT_EVENT = "branch-handover-intent-v2"
_BRANCH_TERMINAL_EVENT = "branch-handover-terminal-v2"
_BRANCH_RECORD_EVENT = "branch-handover-record-v2"
_BRANCH_BLOCKED_EVENT = "branch-handover-blocked-v2"
_INPUTS_SCHEMA = "fanout-run-inputs-v1"
_PROFILED_INPUTS_SCHEMA = "fanout-run-inputs-v2"
_TARGET_INPUTS_SCHEMA = "fanout-run-inputs-v3"
_OWNER_SCHEMA = "fanout-run-owner-v1"
_AUTHORITY_SCHEMA = "fanout-run-authority-v1"
_MAX_AUTHORITY_BYTES = 256 * 1024
_MAX_AUTHORITY_RESPONSE_BYTES = 512 * 1024
_MAX_AUTHORITY_ADDRESSES = 32
_ZERO_CHECKSUM = "0" * 64
_ZERO_OID = "0" * 40
_EVENT_NAME = {
    "dispatch-intent", "process-started", "provider-terminal", "artifacts-durable",
    "publication-intent", "checkpoint-published", "checkpoint-verified",
    "reconciliation-pending", "reconciliation-verifying", "completed",
    "blocked-action", "blocked-memory", "action-complete", "uncertain-attempt",
    "duplicate-spend-accepted", "plan-amended", "amendment-accepted",
    "plan-amendment-intent", "plan-amendment-accepted", "plan-amendment-abandoned",
    "handover-intent", "handover-complete", "handover-rolled-back",
    "handover-conflict", "handover-not-mutated",
    "memory-recovered",
    "uncertainty-resolved",
    _BRANCH_INTENT_EVENT, _BRANCH_TERMINAL_EVENT, _BRANCH_RECORD_EVENT, _BRANCH_BLOCKED_EVENT,
}
_SEAT_EVENTS = {
    "dispatch-intent", "process-started", "provider-terminal", "artifacts-durable",
    "publication-intent", "checkpoint-published", "checkpoint-verified", "uncertain-attempt",
}
_OWNER_EVENTS = {
    "reconciliation-pending", "reconciliation-verifying", "completed", "blocked-action",
    "blocked-memory", "action-complete", "uncertain-attempt", "duplicate-spend-accepted",
    "plan-amended", "amendment-accepted", "plan-amendment-intent",
    "plan-amendment-accepted", "plan-amendment-abandoned", "handover-intent", "handover-complete",
    "handover-rolled-back", "handover-conflict", "handover-not-mutated",
    "memory-recovered",
    "uncertainty-resolved",
    _BRANCH_INTENT_EVENT, _BRANCH_TERMINAL_EVENT, _BRANCH_RECORD_EVENT, _BRANCH_BLOCKED_EVENT,
}
_SEAT_NEXT = {
    None: "dispatch-intent",
    "dispatch-intent": "process-started",
    "process-started": "provider-terminal",
    "provider-terminal": "artifacts-durable",
    "artifacts-durable": "publication-intent",
    "publication-intent": "checkpoint-published",
    "checkpoint-published": "checkpoint-verified",
}
_ACTIVE_LOCKS: set[tuple[int, int]] = set()
_ACTIVE_LOCKS_GUARD = threading.Lock()


def _frozen_json(value: Any) -> Any:
    """Return an immutable, JSON-only copy after canonical serialization."""
    copied = json.loads(canonical_json(value).decode("utf-8"))
    if isinstance(copied, dict):
        return MappingProxyType({key: _frozen_json(item) for key, item in copied.items()})
    if isinstance(copied, list):
        return tuple(_frozen_json(item) for item in copied)
    return copied


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mac(key: bytes, domain: bytes, value: Mapping[str, object]) -> str:
    return hmac.digest(key, domain + canonical_json(value), "sha256").hex()


def _owner_verifier(salt: str, capability: OwnerCapability) -> str:
    return hmac.digest(bytes.fromhex(salt), capability._secret.encode("ascii"), "sha256").hex()


def _lock_identity(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _is_int(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _is_oid(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{40}", value))


def _branch_artifact_ref(value: object) -> ArtifactRef:
    if isinstance(value, ArtifactRef):
        ref = value
    elif isinstance(value, Mapping) and set(value) == {"path", "digest", "size"}:
        ref = ArtifactRef(value["path"], value["digest"], value["size"])
    else:
        raise RunStateError("branch handover artifact reference is invalid")
    try:
        parts = ArtifactStore._parts(ref.path)
    except ArtifactError as error:
        raise RunStateError("branch handover artifact path is invalid") from error
    if ("/".join(parts) != ref.path or parts[0] == ArtifactStore._TEMP_DIR
            or not _is_digest(ref.digest) or not _is_int(ref.size)):
        raise RunStateError("branch handover artifact reference is invalid")
    return ref


def _branch_artifact_dict(ref: ArtifactRef) -> dict[str, object]:
    return {"path": ref.path, "digest": ref.digest, "size": ref.size}


def _validate_branch_intent_binding(intent: BranchHandoverIntentV2, inputs: RunInputs,
                                    amendments: Mapping[int, RunAmendment]) -> None:
    if inputs.targets is None:
        raise RunStateError("branch intent requires v3 target inputs")
    binding = inputs.targets.get(intent.target_id)
    if binding is None or (
        intent.run_id != inputs.run_id
        or (intent.plan_revision == 1 and (
            intent.plan_sha256 != inputs.compiled_plan_sha256
            or intent.inputs_digest != inputs.digest
        ))
        or (intent.plan_revision > 1 and (
            (amendment := amendments.get(intent.plan_revision)) is None
            or amendment.phase != "plan-amendment-accepted"
            or intent.plan_sha256 != amendment.new_plan_sha256
            or intent.inputs_digest != amendment.new_inputs_digest
        ))
        or intent.repository != binding.spec.repository
        or intent.branch_ref != binding.spec.branch_ref
        or intent.base_oid != binding.base_oid
        or intent.old_ref_oid != (binding.branch_oid or _ZERO_OID)
        or intent.expected_head_oid != binding.base_oid
    ):
        raise RunStateError("branch intent differs from run, plan, target, or ref binding")


def _identity(value: object, label: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 128
            or any(not (char.isalnum() or char in "._-/") for char in value)):
        raise RunStateError(f"{label} must be a bounded non-whitespace identity")
    return value


def _handover_root(value: object) -> str:
    if not isinstance(value, str):
        raise RunStateError("handover transaction root must be an absolute path")
    path = Path(value)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RunStateError("handover transaction root must be valid UTF-8") from error
    if (
        not path.is_absolute()
        or len(encoded) > 4096
        or any(part in {"", ".", ".."} for part in path.parts[1:])
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or os.fspath(path) != value
    ):
        raise RunStateError("handover transaction root must be a canonical absolute path")
    return value


@dataclass(frozen=True, slots=True)
class RunInputs:
    """Immutable pre-dispatch binding of all external run inputs."""

    run_id: str
    compiled_plan_sha256: str
    source_sha256: str
    draft_sha256: str
    compiler_sha256: str
    parser_sha256: str
    provider_profiles: Mapping[str, str]
    skill_manifests: Mapping[str, object]
    repo_baseline_sha256: str | None = None
    profile_shape: str = "flat"
    targets: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        _identity(self.run_id, "run id")
        for name in ("compiled_plan_sha256", "source_sha256", "draft_sha256", "compiler_sha256", "parser_sha256"):
            if not _is_digest(getattr(self, name)):
                raise RunStateError(f"{name} must be a lower-case SHA-256 digest")
        if self.repo_baseline_sha256 is not None and not _is_digest(self.repo_baseline_sha256):
            raise RunStateError("repo_baseline_sha256 must be a lower-case SHA-256 digest or None")
        if self.profile_shape not in {"flat", "class-tier"}:
            raise RunStateError("run input profile shape is unsupported")
        if self.targets is not None:
            from .targets import TargetBinding
            if (self.profile_shape != "class-tier" or self.repo_baseline_sha256 is not None
                    or not isinstance(self.targets, Mapping) or not self.targets
                    or any(not isinstance(key, str) or not isinstance(binding, TargetBinding)
                           or key != binding.spec.id or binding.baseline_sha256 is None
                           for key, binding in self.targets.items())):
                raise RunStateError("v3 target inputs require complete target bindings")
            object.__setattr__(self, "targets", MappingProxyType(dict(sorted(self.targets.items()))))
        profiles = _plain_json(self.provider_profiles)
        manifests = _plain_json(self.skill_manifests)
        if not isinstance(profiles, dict) or not profiles:
            raise RunStateError("provider profiles must be a non-empty mapping")
        if not all(_is_public_digest_binding(key, value, "provider profile") for key, value in profiles.items()):
            raise RunStateError("provider profiles must map safe IDs to SHA-256 digests")
        if self.profile_shape == "class-tier":
            from .providers import parse_profile_binding_key
            try:
                for key in profiles:
                    parse_profile_binding_key(key)
            except ProviderError as error:
                raise RunStateError("class-tier provider profiles contain a mixed or invalid key") from error
        if not isinstance(manifests, dict) or not all(_is_public_digest_binding(key, value, "skill manifest") for key, value in manifests.items()):
            raise RunStateError("skill manifests must map safe IDs to SHA-256 digests")
        object.__setattr__(self, "provider_profiles", _frozen_json(profiles))
        object.__setattr__(self, "skill_manifests", _frozen_json(manifests))

    def to_dict(self) -> dict[str, object]:
        if self.targets is not None:
            return {
                "schema_version": _TARGET_INPUTS_SCHEMA,
                "run_id": self.run_id,
                "compiled_plan_sha256": self.compiled_plan_sha256,
                "source_sha256": self.source_sha256,
                "draft_sha256": self.draft_sha256,
                "compiler_sha256": self.compiler_sha256,
                "parser_sha256": self.parser_sha256,
                "provider_profiles": _plain_json(self.provider_profiles),
                "skill_manifests": _plain_json(self.skill_manifests),
                "profile_shape": "class-tier",
                "targets": {key: self.targets[key].to_dict() for key in sorted(self.targets)},
            }
        value: dict[str, object] = {
            "schema_version": (_PROFILED_INPUTS_SCHEMA if self.profile_shape == "class-tier"
                               else _INPUTS_SCHEMA),
            "run_id": self.run_id,
            "compiled_plan_sha256": self.compiled_plan_sha256,
            "source_sha256": self.source_sha256,
            "draft_sha256": self.draft_sha256,
            "compiler_sha256": self.compiler_sha256,
            "parser_sha256": self.parser_sha256,
            "provider_profiles": _plain_json(self.provider_profiles),
            "skill_manifests": _plain_json(self.skill_manifests),
            "repo_baseline_sha256": self.repo_baseline_sha256,
        }
        if self.profile_shape == "class-tier":
            value["profile_shape"] = self.profile_shape
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RunInputs":
        allowed = {
            "schema_version", "run_id", "compiled_plan_sha256", "source_sha256", "draft_sha256",
            "compiler_sha256", "parser_sha256", "provider_profiles", "skill_manifests",
            "repo_baseline_sha256",
        }
        if not isinstance(value, Mapping):
            raise RunStateError("run inputs have unknown or missing fields")
        schema = value.get("schema_version")
        if schema == _TARGET_INPUTS_SCHEMA:
            from .errors import PlanValidationError, RepositoryValidationError
            from .targets import TargetBinding
            expected = (allowed - {"repo_baseline_sha256"}) | {"profile_shape", "targets"}
            if (set(value) != expected or value.get("profile_shape") != "class-tier"
                    or not isinstance(value.get("targets"), dict)):
                raise RunStateError("run inputs have unknown or missing fields")
            try:
                targets = {key: TargetBinding.from_dict(binding)
                           for key, binding in value["targets"].items()}
                return cls(**{key: value[key] for key in allowed - {
                    "schema_version", "repo_baseline_sha256"}},
                    profile_shape="class-tier", targets=targets)  # type: ignore[arg-type]
            except (TypeError, ValueError, PlanValidationError,
                    RepositoryValidationError) as error:
                raise RunStateError("run inputs have invalid target bindings") from error
        if schema == _INPUTS_SCHEMA and set(value) == allowed:
            return cls(**{key: value[key] for key in allowed - {"schema_version"}})  # type: ignore[arg-type]
        if (schema == _PROFILED_INPUTS_SCHEMA and set(value) == allowed | {"profile_shape"}
                and value.get("profile_shape") == "class-tier"):
            return cls(**{key: value[key] for key in allowed - {"schema_version"}},
                       profile_shape="class-tier")  # type: ignore[arg-type]
        raise RunStateError("run inputs have unknown or missing fields")

    @property
    def digest(self) -> str:
        return _sha256(canonical_json(self.to_dict()))


def _is_public_digest_binding(key: object, value: object, label: str) -> bool:
    try:
        _identity(key, label)
    except RunStateError:
        return False
    return _is_digest(value)


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Bounded recovery limits for one journal and snapshot."""

    max_events: int = 20_000
    max_journal_bytes: int = 32 * 1024 * 1024
    max_snapshot_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_events < 1 or self.max_journal_bytes < 1024 or self.max_snapshot_bytes < 1024:
            raise ValueError("run-state limits must be positive bounded values")


@dataclass(frozen=True, slots=True)
class AnchorRevision:
    """One immutable value returned by an external monotonic authority."""

    revision: int
    value: bytes


class _AnchorAuthority:
    """Nominal internal boundary shared by production and explicit test clients."""


class LocalAnchorAuthority(_AnchorAuthority):
    """Owner-local monotonic CAS authority, independent of the run and repo roots.

    Bootstrap is explicit, and reopening never repairs missing or damaged state.
    The declared repository root must already be a directory so separation can
    be checked before bootstrap creates any path on a case-insensitive volume.
    A persistent lock serializes processes while record replacement keeps readers
    on either complete revision. This detects accidental rollback of a run root;
    restoring both roots under the same account is outside its threat model.
    """

    __slots__ = ("_root", "_run_root", "_identity", "_root_identity", "_lock_identity",
                 "_target_registry_sha256", "_sealed")
    _META = "authority.json"
    _LOCK = ".authority.lock"
    _SCHEMA = "fanout-local-authority-v1"
    _TARGET_SCHEMA = "fanout-local-authority-v2"
    _RECORD_SCHEMA = "fanout-local-anchor-v1"

    def __init__(self, root: Path | str, *, run_root: Path | str, repo_root: Path | str,
                 target_bindings: Mapping[str, object] | None = None) -> None:
        root_path, run_path = _local_authority_paths(root, run_root, repo_root)
        registry_digest = _local_target_registry_digest(target_bindings, root_path, run_path)
        object.__setattr__(self, "_root", root_path)
        object.__setattr__(self, "_run_root", run_path)
        object.__setattr__(self, "_target_registry_sha256", registry_digest)
        root_fd = self._open_root()
        try:
            metadata = self._read_metadata(root_fd)
            lock_fd = self._open_lock(root_fd, metadata)
            os.close(lock_fd)
            object.__setattr__(self, "_identity", metadata["authority_id"])
            object.__setattr__(self, "_root_identity", (metadata["root_device"], metadata["root_inode"]))
            object.__setattr__(self, "_lock_identity", (metadata["lock_device"], metadata["lock_inode"]))
        finally:
            os.close(root_fd)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("LocalAnchorAuthority is immutable")
        object.__setattr__(self, name, value)

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("LocalAnchorAuthority is final")

    @classmethod
    def bootstrap(cls, root: Path | str, *, run_root: Path | str,
                  repo_root: Path | str,
                  target_bindings: Mapping[str, object] | None = None) -> "LocalAnchorAuthority":
        """Exclusively create an empty authority; a partial bootstrap is refused."""
        root_path, run_path = _local_authority_paths(root, run_root, repo_root)
        registry_digest = _local_target_registry_digest(target_bindings, root_path, run_path)
        root_fd = _open_private_directory(root_path, create=True, exclusive=True)
        try:
            if stat.S_IMODE(os.fstat(root_fd).st_mode) != 0o700:
                raise RunStateError("local authority root must have mode 0700")
            _write_new_private(root_fd, cls._LOCK, b"")
            lock_info = os.stat(cls._LOCK, dir_fd=root_fd, follow_symlinks=False)
            root_info = os.fstat(root_fd)
            metadata = {
                "schema_version": (cls._SCHEMA if registry_digest is None else cls._TARGET_SCHEMA),
                "authority_id": f"local-{secrets.token_hex(32)}",
                "root_path": str(root_path), "root_device": root_info.st_dev,
                "root_inode": root_info.st_ino, "lock_device": lock_info.st_dev,
                "lock_inode": lock_info.st_ino,
            }
            if registry_digest is not None:
                metadata["target_registry_sha256"] = registry_digest
            _write_new_private(root_fd, cls._META, canonical_json(metadata))
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        return cls(root_path, run_root=run_root, repo_root=repo_root,
                   target_bindings=target_bindings)

    @property
    def identity(self) -> str:
        return self._identity

    def check_run_root(self, root: Path | str) -> None:
        if _local_absolute_path(root, "run root") != self._run_root:
            raise RunStateError("local authority is bound to a different run root")

    def probe(self) -> str:
        """Check bootstrap files and every present record before owner dispatch."""
        with self._locked_root() as root_fd:
            for name in os.listdir(root_fd):
                if name in {self._META, self._LOCK}:
                    continue
                if re.fullmatch(r"\.anchor-tmp-[0-9a-f]{32}", name):
                    try:
                        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    except OSError as error:
                        raise RunStateError("local authority temporary entry is unsafe") from error
                    if not _private_stat(info) or info.st_size > _MAX_AUTHORITY_RESPONSE_BYTES:
                        raise RunStateError("local authority temporary entry is unsafe")
                    continue
                if not re.fullmatch(r"anchor-[0-9a-f]{64}\.json", name):
                    raise RunStateError("local authority contains an unexpected entry")
                self._read_record(root_fd, name)
        return self._identity

    def create(self, key: str, value: bytes) -> AnchorRevision:
        key, value = _local_request(key, value)
        name = _local_record_name(key)
        with self._locked_root() as root_fd:
            try:
                os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RunStateError("local anchor already exists")
            try:
                _write_new_private(root_fd, name, self._record_bytes(key, 1, value))
                os.fsync(root_fd)
            except OSError as error:
                raise RunStateError("local anchor create failed") from error
        return AnchorRevision(1, value)

    def read(self, key: str) -> AnchorRevision:
        name = _local_record_name(_identity(key, "anchor key"))
        with self._locked_root() as root_fd:
            stored_key, result = self._read_record(root_fd, name)
            if stored_key != key:
                raise RunStateError("local anchor key does not match its record")
            return result

    def compare_and_set(self, key: str, expected_revision: int, value: bytes) -> AnchorRevision:
        key, value = _local_request(key, value)
        if not _is_int(expected_revision, minimum=1):
            raise RunStateError("local anchor expected revision is invalid")
        name = _local_record_name(key)
        with self._locked_root() as root_fd:
            stored_key, current = self._read_record(root_fd, name)
            if stored_key != key:
                raise RunStateError("local anchor key does not match its record")
            if current.revision != expected_revision:
                raise RunStateError("local anchor revision conflict")
            next_revision = expected_revision + 1
            temporary = f".anchor-tmp-{secrets.token_hex(16)}"
            replaced = False
            try:
                _write_new_private(root_fd, temporary, self._record_bytes(key, next_revision, value))
                os.replace(temporary, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                replaced = True
                os.fsync(root_fd)
            except OSError as error:
                raise RunStateError("local anchor compare-and-set failed") from error
            finally:
                if not replaced:
                    try:
                        os.unlink(temporary, dir_fd=root_fd)
                    except FileNotFoundError:
                        pass
            return AnchorRevision(next_revision, value)

    def _open_root(self) -> int:
        try:
            root_fd = _open_private_directory(self._root, create=False, exclusive=False)
        except (OSError, RunStateError) as error:
            raise RunStateError("local authority root is unavailable or unsafe") from error
        if stat.S_IMODE(os.fstat(root_fd).st_mode) != 0o700:
            os.close(root_fd)
            raise RunStateError("local authority root must have mode 0700")
        return root_fd

    def _read_metadata(self, root_fd: int) -> dict[str, object]:
        data = _local_read_file(root_fd, self._META, 4096)
        try:
            metadata = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RunStateError("local authority metadata is malformed") from error
        required = {"schema_version", "authority_id", "root_path", "root_device", "root_inode",
                    "lock_device", "lock_inode"}
        if self._target_registry_sha256 is not None:
            required.add("target_registry_sha256")
            if isinstance(metadata, dict) and metadata.get("target_registry_sha256") != self._target_registry_sha256:
                raise RunStateError("local authority target registry changed")
        if (not isinstance(metadata, dict) or set(metadata) != required
                or canonical_json(metadata) != data
                or metadata["schema_version"] != (self._SCHEMA if self._target_registry_sha256 is None
                                                  else self._TARGET_SCHEMA)
                or (self._target_registry_sha256 is not None and
                    metadata["target_registry_sha256"] != self._target_registry_sha256)
                or not isinstance(metadata["authority_id"], str)
                or not re.fullmatch(r"local-[0-9a-f]{64}", metadata["authority_id"])
                or metadata["root_path"] != str(self._root)
                or any(not _is_int(metadata[name], minimum=1) for name in (
                    "root_device", "root_inode", "lock_device", "lock_inode"))):
            raise RunStateError("local authority metadata is invalid")
        info = os.fstat(root_fd)
        if (info.st_dev, info.st_ino) != (metadata["root_device"], metadata["root_inode"]):
            raise RunStateError("local authority root identity changed")
        return metadata

    def _open_lock(self, root_fd: int, metadata: Mapping[str, object]) -> int:
        try:
            lock_fd = os.open(self._LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
        except OSError as error:
            raise RunStateError("local authority lock is missing or unsafe") from error
        try:
            expected = (metadata["lock_device"], metadata["lock_inode"])
            _assert_current_entry(root_fd, self._LOCK, lock_fd, expected, "local authority lock")
            return lock_fd
        except BaseException:
            os.close(lock_fd)
            raise

    @contextmanager
    def _locked_root(self) -> Iterator[int]:
        root_fd = self._open_root()
        lock_fd: int | None = None
        try:
            metadata = self._read_metadata(root_fd)
            if (metadata["authority_id"] != self._identity
                    or (metadata["root_device"], metadata["root_inode"]) != self._root_identity
                    or (metadata["lock_device"], metadata["lock_inode"]) != self._lock_identity):
                raise RunStateError("local authority identity changed")
            lock_fd = self._open_lock(root_fd, metadata)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except OSError as error:
                raise RunStateError("local authority lock is unavailable") from error
            self._read_metadata(root_fd)
            _assert_current_entry(root_fd, self._LOCK, lock_fd, self._lock_identity,
                                  "local authority lock")
            yield root_fd
        finally:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            os.close(root_fd)

    def _record_bytes(self, key: str, revision: int, value: bytes) -> bytes:
        record = {
            "schema_version": self._RECORD_SCHEMA, "authority_id": self._identity,
            "key": key, "revision": revision, "value_base64": base64.b64encode(value).decode("ascii"),
            "value_sha256": _sha256(value),
        }
        return canonical_json(record)

    def _read_record(self, root_fd: int, name: str) -> tuple[str, AnchorRevision]:
        data = _local_read_file(root_fd, name, _MAX_AUTHORITY_RESPONSE_BYTES, durable=True)
        try:
            record = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RunStateError("local anchor record is malformed") from error
        required = {"schema_version", "authority_id", "key", "revision", "value_base64", "value_sha256"}
        if (not isinstance(record, dict) or set(record) != required or canonical_json(record) != data
                or record["schema_version"] != self._RECORD_SCHEMA
                or record["authority_id"] != self._identity
                or not _is_int(record["revision"], minimum=1)
                or not _is_digest(record["value_sha256"])):
            raise RunStateError("local anchor record has an invalid schema")
        key = _identity(record["key"], "anchor key")
        if _local_record_name(key) != name:
            raise RunStateError("local anchor key does not match its record path")
        try:
            value = base64.b64decode(record["value_base64"], validate=True)
        except (TypeError, ValueError, binascii.Error) as error:
            raise RunStateError("local anchor value is malformed") from error
        if len(value) > _MAX_AUTHORITY_BYTES or _sha256(value) != record["value_sha256"]:
            raise RunStateError("local anchor value is damaged or oversized")
        return key, AnchorRevision(record["revision"], value)


def _local_absolute_path(value: Path | str, label: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise RunStateError(f"{label} must be a canonical absolute path")
    path = Path(value)
    if (not path.is_absolute() or any(part in {".", ".."} for part in path.parts)
            or str(path) != os.fspath(value)):
        raise RunStateError(f"{label} must be a canonical absolute path")
    return path


def _local_target_registry_digest(
    bindings: Mapping[str, object] | None, authority_root: Path, run_root: Path,
) -> str | None:
    if bindings is None:
        return None
    from .errors import RepositoryValidationError
    from .repo import revalidate_target_bindings
    from .targets import TargetBinding

    if not isinstance(bindings, Mapping) or not bindings:
        raise RunStateError("local authority target registry is invalid")
    if any(not isinstance(key, str) or not isinstance(binding, TargetBinding)
           or key != binding.spec.id or binding.baseline_sha256 is None
           for key, binding in bindings.items()):
        raise RunStateError("local authority target registry is invalid")
    try:
        revalidate_target_bindings(bindings, writable_ids=set())
        paths = [binding.root for binding in bindings.values()]
        for path in paths:
            _local_authority_paths(authority_root, run_root, path)
        for index, left in enumerate(paths):
            for right in paths[index + 1:]:
                if left == right or left in right.parents or right in left.parents:
                    raise RunStateError("local authority target roots must be disjoint")
    except (OSError, ValueError, RepositoryValidationError) as error:
        raise RunStateError("local authority target registry is invalid") from error
    return _sha256(canonical_json({key: bindings[key].to_dict() for key in sorted(bindings)}))


def _local_authority_paths(root: Path | str, run_root: Path | str,
                           repo_root: Path | str) -> tuple[Path, Path]:
    root_path = _local_absolute_path(root, "local authority root")
    run_path = _local_absolute_path(run_root, "run root")
    repo_path = _local_absolute_path(repo_root, "repository root")
    code_root = Path(__file__).resolve().parents[3]
    for separate in (run_path, repo_path, code_root):
        if root_path == separate or separate in root_path.parents or root_path in separate.parents:
            raise RunStateError("local authority root must be outside the run and repository roots")
    try:
        repo_info = os.stat(repo_path)
    except OSError as error:
        raise RunStateError("repository root must exist as a directory") from error
    if not stat.S_ISDIR(repo_info.st_mode):
        raise RunStateError("repository root must exist as a directory")
    for separate in (run_path, repo_path, code_root):
        if (any(_local_same_directory(ancestor, separate) for ancestor in (root_path, *root_path.parents))
                or any(_local_same_directory(root_path, ancestor) for ancestor in (separate, *separate.parents))):
            raise RunStateError("local authority root must be outside the run and repository roots")
    return root_path, run_path


def _local_same_directory(left: Path, right: Path) -> bool:
    try:
        left_info, right_info = os.stat(left), os.stat(right)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RunStateError("local authority path identity is unavailable") from error
    return (stat.S_ISDIR(left_info.st_mode) and stat.S_ISDIR(right_info.st_mode)
            and (left_info.st_dev, left_info.st_ino) == (right_info.st_dev, right_info.st_ino))


def _local_record_name(key: str) -> str:
    return f"anchor-{_sha256(key.encode('utf-8'))}.json"


def _local_request(key: str, value: bytes) -> tuple[str, bytes]:
    key = _identity(key, "anchor key")
    if not isinstance(value, bytes) or len(value) > _MAX_AUTHORITY_BYTES:
        raise RunStateError("local anchor value is malformed or oversized")
    return key, value


def _local_read_file(root_fd: int, name: str, limit: int, *, durable: bool = False) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    except OSError as error:
        raise RunStateError(f"local authority file is missing or unsafe: {name}") from error
    try:
        data = _read_private_fd(fd, limit, f"local authority file {name}")
        identity = _lock_identity(fd)
        _assert_current_entry(root_fd, name, fd, identity, f"local authority file {name}")
        if durable:
            try:
                os.fsync(fd)
                os.fsync(root_fd)
            except OSError as error:
                raise RunStateError("local anchor durability check failed") from error
            _assert_current_entry(root_fd, name, fd, identity, f"local authority file {name}")
        return data
    finally:
        os.close(fd)


class RemoteAnchorAuthority(_AnchorAuthority):
    """Pinned HTTPS client for an independently durable monotonic CAS service.

    The server response wire schema is canonical JSON containing
    ``schema_version``, ``operation``, ``authority_id``, ``nonce``, ``key``,
    ``revision``, ``value_base64``, and ``attestation``. The attestation is
    HMAC-SHA-256 over the other fields with domain ``fanout-anchor-response-v1``.
    Every request uses a fresh nonce, and every response must echo its exact
    operation, authority identity, key, nonce, revision, and value. TLS verifies
    the remote endpoint; the pinned attestation key verifies the authority.

    Construction is deliberately nominal and final. RunJournal never accepts
    structural lookalikes or an injected transport. The explicit nominal local
    authority is the separate owner-local production option.
    """

    __slots__ = (
        "_endpoint", "_host", "_port", "_base_path", "_literal_ip",
        "_authority_id", "_credential", "_attestation_key", "_identity",
        "_timeout", "_sealed",
    )

    def __init__(self, *, endpoint: str, authority_id: str, credential: str,
        attestation_key: bytes, timeout: float = 10.0) -> None:
        normalized, host, port, base_path, literal_ip = _normalize_authority_endpoint(endpoint)
        authority_id = _identity(authority_id, "remote authority id")
        if (not isinstance(credential, str) or not 32 <= len(credential) <= 4096
                or any(ord(char) < 33 or ord(char) > 126 for char in credential)):
            raise RunStateError("remote anchor authority credential is malformed")
        if not isinstance(attestation_key, bytes) or not 32 <= len(attestation_key) <= 4096:
            raise RunStateError("remote anchor authority attestation key is malformed")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise RunStateError("remote anchor authority timeout is invalid")
        _resolve_public_authority(host, port, literal_ip)
        descriptor = {
            "schema_version": "fanout-anchor-client-v1", "endpoint": normalized,
            "authority_id": authority_id, "attestation_key_sha256": _sha256(attestation_key),
        }
        self._endpoint = normalized
        self._host = host
        self._port = port
        self._base_path = base_path
        self._literal_ip = literal_ip
        self._authority_id = authority_id
        self._credential = credential
        self._attestation_key = attestation_key
        self._identity = f"remote-{_sha256(canonical_json(descriptor))}"
        self._timeout = float(timeout)
        self._sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("RemoteAnchorAuthority is immutable")
        object.__setattr__(self, name, value)

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("RemoteAnchorAuthority is final")

    @property
    def identity(self) -> str:
        return self._identity

    def create(self, key: str, value: bytes) -> AnchorRevision:
        return self._request("create", key, value=value)

    def read(self, key: str) -> AnchorRevision:
        return self._request("read", key)

    def compare_and_set(self, key: str, expected_revision: int, value: bytes) -> AnchorRevision:
        if not _is_int(expected_revision, minimum=1):
            raise RunStateError("remote anchor expected revision is invalid")
        return self._request("compare-and-set", key, value=value, expected_revision=expected_revision)

    def _request(self, operation: str, key: str, *, value: bytes | None = None,
                 expected_revision: int | None = None) -> AnchorRevision:
        key = _identity(key, "anchor key")
        if value is not None and (not isinstance(value, bytes) or len(value) > _MAX_AUTHORITY_BYTES):
            raise RunStateError("remote anchor authority request value is malformed")
        nonce = secrets.token_hex(32)
        payload: dict[str, object] = {
            "schema_version": "fanout-anchor-request-v1", "operation": operation,
            "authority_id": self._authority_id, "nonce": nonce, "key": key,
        }
        if value is not None:
            payload["value_base64"] = base64.b64encode(value).decode("ascii")
        if expected_revision is not None:
            payload["expected_revision"] = expected_revision
        targets = _resolve_public_authority(self._host, self._port, self._literal_ip)
        body = canonical_json(payload)
        try:
            status, data = _authority_https_post(
                self._host, self._port, f"{self._base_path}/v1/anchors/{operation}",
                targets, body, self._credential, self._timeout,
            )
        except RunStateError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as error:
            raise RunStateError("remote anchor authority transport failed") from error
        if status != 200:
            raise RunStateError("remote anchor authority returned a non-success status")
        if len(data) > _MAX_AUTHORITY_RESPONSE_BYTES:
            raise RunStateError("remote anchor authority response exceeds its bounded size")
        return self._verify_response(data, operation=operation, nonce=nonce, key=key)

    def _verify_response(self, data: bytes, *, operation: str, nonce: str, key: str) -> AnchorRevision:
        try:
            response = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RunStateError("remote anchor authority response is malformed") from error
        allowed = {"schema_version", "operation", "authority_id", "nonce", "key", "revision",
                   "value_base64", "attestation"}
        if (not isinstance(response, dict) or set(response) != allowed
                or response.get("schema_version") != "fanout-anchor-response-v1"
                or canonical_json(response) != data):
            raise RunStateError("remote anchor authority response is noncanonical or has an invalid schema")
        unsigned = {name: item for name, item in response.items() if name != "attestation"}
        expected = hmac.digest(
            self._attestation_key, b"fanout-anchor-response-v1" + canonical_json(unsigned), "sha256",
        ).hex()
        if (response["operation"] != operation or response["authority_id"] != self._authority_id
                or response["nonce"] != nonce or response["key"] != key
                or not _is_int(response["revision"], minimum=1)
                or not _is_digest(response["attestation"])
                or not hmac.compare_digest(response["attestation"], expected)):
            raise RunStateError("remote anchor authority response attestation is invalid")
        try:
            value = base64.b64decode(response["value_base64"], validate=True)
        except (TypeError, ValueError, binascii.Error) as error:
            raise RunStateError("remote anchor authority response value is malformed") from error
        if len(value) > _MAX_AUTHORITY_BYTES:
            raise RunStateError("remote anchor authority response value exceeds its bounded size")
        return AnchorRevision(response["revision"], value)

    def __repr__(self) -> str:
        return f"RemoteAnchorAuthority(identity={self._identity!r}, credential=<redacted>)"


def _normalize_authority_endpoint(
        endpoint: str) -> tuple[str, str, int, str, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise RunStateError("remote anchor authority endpoint is malformed")
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        port = parsed.port or 443
    except ValueError as error:
        raise RunStateError("remote anchor authority endpoint is malformed") from error
    hostname = parsed.hostname
    if (parsed.scheme != "https" or not parsed.netloc or hostname is None or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or parsed.netloc.endswith(":")
            or not 1 <= port <= 65535):
        raise RunStateError("remote anchor authority endpoint must be an absolute credential-free HTTPS URL")
    if ((parsed.path and not parsed.path.startswith("/")) or "\\" in parsed.path
            or re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path)
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in parsed.path)):
        raise RunStateError("remote anchor authority endpoint path is malformed")
    for segment in parsed.path.split("/"):
        decoded = urllib.parse.unquote(segment)
        if decoded in {".", ".."} or "/" in decoded or "\\" in decoded or "\x00" in decoded:
            raise RunStateError("remote anchor authority endpoint path is unsafe")

    trailing_dot = hostname.endswith(".")
    raw_host = hostname.rstrip(".").lower()
    if not raw_host or raw_host == "localhost" or raw_host.endswith((".localhost", ".local")):
        raise RunStateError("remote anchor authority endpoint hostname is not public")
    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        literal = ipaddress.ip_address(raw_host)
    except ValueError:
        literal = None
        try:
            socket.inet_aton(raw_host)
        except (OSError, OverflowError, ValueError):
            pass
        else:
            raise RunStateError("remote anchor authority endpoint uses ambiguous numeric IPv4 syntax")
        if ":" in raw_host or "%" in raw_host:
            raise RunStateError("remote anchor authority endpoint IP literal is malformed")
        try:
            host = raw_host.encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise RunStateError("remote anchor authority endpoint hostname is malformed") from error
        labels = host.split(".")
        if (len(host) > 253 or len(labels) < 2 or not any(char.isalpha() for char in labels[-1])
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                       for label in labels)):
            raise RunStateError("remote anchor authority endpoint requires a public DNS hostname")
    else:
        if isinstance(literal, ipaddress.IPv6Address) and literal.ipv4_mapped is not None:
            raise RunStateError("remote anchor authority endpoint may not use IPv4-mapped IPv6")
        host = str(literal)
        if trailing_dot or raw_host != host or not _is_global_authority_ip(literal):
            raise RunStateError("remote anchor authority endpoint requires a canonical global IP")

    base_path = parsed.path.rstrip("/")
    display_host = f"[{host}]" if literal is not None and literal.version == 6 else host
    netloc = display_host if port == 443 else f"{display_host}:{port}"
    normalized = urllib.parse.urlunsplit(("https", netloc, base_path, "", ""))
    return normalized, host, port, base_path, literal


def _is_global_authority_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return False
    return (address.is_global and not address.is_private and not address.is_loopback
            and not address.is_unspecified and not address.is_link_local
            and not address.is_multicast and not address.is_reserved)


def _resolve_public_authority(
        host: str, port: int,
        literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None,
        ) -> tuple[tuple[int, int, int, tuple[object, ...]], ...]:
    try:
        answers = socket.getaddrinfo(
            host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP,
        )
    except (socket.gaierror, OSError, UnicodeError) as error:
        raise RunStateError("remote anchor authority endpoint resolution failed") from error
    if not answers or len(answers) > _MAX_AUTHORITY_ADDRESSES:
        raise RunStateError("remote anchor authority endpoint resolved to an invalid number of addresses")
    targets: list[tuple[int, int, int, tuple[object, ...]]] = []
    seen: set[tuple[int, tuple[object, ...]]] = set()
    for answer in answers:
        if not isinstance(answer, tuple) or len(answer) != 5:
            raise RunStateError("remote anchor authority endpoint resolver returned malformed data")
        family, socktype, proto, _canonname, sockaddr = answer
        if (family not in {socket.AF_INET, socket.AF_INET6} or socktype != socket.SOCK_STREAM
                or proto not in {0, socket.IPPROTO_TCP} or not isinstance(sockaddr, tuple)
                or len(sockaddr) < 2 or sockaddr[1] != port):
            raise RunStateError("remote anchor authority endpoint resolver returned an unsafe target")
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except (TypeError, ValueError) as error:
            raise RunStateError("remote anchor authority endpoint resolver returned an invalid address") from error
        if ((family == socket.AF_INET and address.version != 4)
                or (family == socket.AF_INET6 and address.version != 6)
                or not _is_global_authority_ip(address)
                or (literal is not None and address != literal)):
            raise RunStateError("remote anchor authority endpoint resolved to a non-global address")
        if family == socket.AF_INET6 and (len(sockaddr) != 4 or sockaddr[3] != 0):
            raise RunStateError("remote anchor authority endpoint resolved to a scoped IPv6 address")
        normalized_sockaddr: tuple[object, ...]
        if family == socket.AF_INET:
            normalized_sockaddr = (str(address), port)
        else:
            normalized_sockaddr = (str(address), port, int(sockaddr[2]), 0)
        marker = (family, normalized_sockaddr)
        if marker not in seen:
            seen.add(marker)
            targets.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, normalized_sockaddr))
    if not targets:
        raise RunStateError("remote anchor authority endpoint resolved to no usable global address")
    return tuple(targets)


class _ResolvedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that uses one already validated address set without re-resolving."""

    def __init__(self, host: str, port: int,
                 targets: tuple[tuple[int, int, int, tuple[object, ...]], ...],
                 timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._targets = targets

    def connect(self) -> None:
        last_error: OSError | None = None
        for family, socktype, proto, sockaddr in self._targets:
            raw = socket.socket(family, socktype, proto)
            try:
                raw.settimeout(self.timeout)
                raw.connect(sockaddr)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except OSError as error:
                last_error = error
                raw.close()
        raise OSError("all validated remote anchor authority addresses failed") from last_error


def _authority_https_post(
        host: str, port: int, path: str,
        targets: tuple[tuple[int, int, int, tuple[object, ...]], ...],
        body: bytes, credential: str, timeout: float) -> tuple[int, bytes]:
    connection = _ResolvedHTTPSConnection(host, port, targets, timeout)
    try:
        connection.request(
            "POST", path, body=body,
            headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        try:
            return response.status, response.read(_MAX_AUTHORITY_RESPONSE_BYTES + 1)
        finally:
            response.close()
    finally:
        connection.close()


class _TestAnchorAuthority(_AnchorAuthority):
    """Explicit test-only nominal wrapper; never admitted by production APIs."""

    __slots__ = ("_backend", "_identity")

    def __init__(self, backend: object) -> None:
        try:
            identity = backend.identity  # type: ignore[attr-defined]
        except Exception as error:
            raise RunStateError("test anchor backend identity is unavailable") from error
        self._backend = backend
        self._identity = f"test-only/{_identity(identity, 'test anchor backend identity')}"

    @property
    def identity(self) -> str:
        return self._identity

    def create(self, key: str, value: bytes) -> AnchorRevision:
        return self._backend.create(key, value)  # type: ignore[attr-defined,no-any-return]

    def read(self, key: str) -> AnchorRevision:
        return self._backend.read(key)  # type: ignore[attr-defined,no-any-return]

    def compare_and_set(self, key: str, expected_revision: int, value: bytes) -> AnchorRevision:
        return self._backend.compare_and_set(key, expected_revision, value)  # type: ignore[attr-defined,no-any-return]


def _test_anchor_authority(backend: object) -> _TestAnchorAuthority:
    """Build a nominal test adapter for the private RunJournal test entrypoints."""
    return _TestAnchorAuthority(backend)


class OwnerCapability:
    """Opaque in-memory owner proof.  Its plaintext is never serialized or repr'd."""

    __slots__ = ("_secret",)

    def __init__(self, secret: str) -> None:
        if not isinstance(secret, str) or len(secret) < 32:
            raise RunAuthorizationError("owner capability is malformed")
        self._secret = secret

    @classmethod
    def from_token(cls, token: str) -> "OwnerCapability":
        """Import a capability recovered from owner-controlled external storage."""
        return cls(token)

    def export_token(self) -> str:
        """Return the one-time token for external owner-controlled storage only."""
        return self._secret

    def __repr__(self) -> str:
        return "OwnerCapability(<redacted>)"

    __str__ = __repr__

    def _mac_key(self) -> bytes:
        return hmac.digest(self._secret.encode("ascii"), b"fanout-runstate-mac-v2", "sha256")


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Non-mutating information about the journal tail ignored at recovery."""

    torn_tail_bytes: int = 0


@dataclass(frozen=True, slots=True)
class RunAmendment:
    """One revision-keyed immutable plan, input, and profile transition."""

    revision: int
    phase: str
    old_plan_sha256: str
    old_inputs_digest: str
    new_plan_sha256: str
    new_inputs_digest: str
    old_profiles_sha256: str
    new_profiles_sha256: str

    @property
    def binding(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.old_plan_sha256,
            self.old_inputs_digest,
            self.new_plan_sha256,
            self.new_inputs_digest,
            self.old_profiles_sha256,
            self.new_profiles_sha256,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "new_inputs_digest": self.new_inputs_digest,
            "new_plan_sha256": self.new_plan_sha256,
            "old_inputs_digest": self.old_inputs_digest,
            "old_plan_sha256": self.old_plan_sha256,
            "old_profiles_sha256": self.old_profiles_sha256,
            "new_profiles_sha256": self.new_profiles_sha256,
            "phase": self.phase,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class BranchHandoverIntentV2:
    """Exact target, ref, and verified candidate selected for a branch handover."""

    run_id: str
    plan_revision: int
    plan_sha256: str
    inputs_digest: str
    task_id: str
    target_id: str
    repository: str
    branch_ref: str
    base_oid: str
    old_ref_oid: str
    candidate_ref: ArtifactRef
    verification_ref: ArtifactRef
    expected_head_oid: str
    expected_index_sha256: str
    expected_tree_oid: str

    def __post_init__(self) -> None:
        _identity(self.run_id, "branch run id")
        _identity(self.task_id, "branch task id")
        _identity(self.target_id, "branch target id")
        if not _is_int(self.plan_revision, minimum=1):
            raise RunStateError("branch intent plan revision is invalid")
        if any(not _is_digest(value) for value in (
            self.plan_sha256, self.inputs_digest, self.expected_index_sha256,
        )):
            raise RunStateError("branch intent digest is invalid")
        if any(not _is_oid(value) for value in (
            self.base_oid, self.old_ref_oid, self.expected_head_oid, self.expected_tree_oid,
        )) or self.base_oid == _ZERO_OID or self.expected_tree_oid == _ZERO_OID:
            raise RunStateError("branch intent OID is invalid")
        if (not isinstance(self.repository, str) or not self.repository
                or not isinstance(self.branch_ref, str)
                or not self.branch_ref.startswith("refs/heads/")):
            raise RunStateError("branch intent repository or ref is invalid")
        _branch_artifact_ref(self.candidate_ref)
        _branch_artifact_ref(self.verification_ref)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _BRANCH_INTENT_SCHEMA,
            "run_id": self.run_id, "plan_revision": self.plan_revision,
            "plan_sha256": self.plan_sha256, "inputs_digest": self.inputs_digest,
            "task_id": self.task_id, "target_id": self.target_id,
            "repository": self.repository, "branch_ref": self.branch_ref,
            "base_oid": self.base_oid, "old_ref_oid": self.old_ref_oid,
            "candidate_ref": _branch_artifact_dict(self.candidate_ref),
            "verification_ref": _branch_artifact_dict(self.verification_ref),
            "expected_head_oid": self.expected_head_oid,
            "expected_index_sha256": self.expected_index_sha256,
            "expected_tree_oid": self.expected_tree_oid,
        }

    @property
    def sha256(self) -> str:
        return _sha256(canonical_json(self.to_dict()))

    @classmethod
    def from_dict(cls, value: object) -> "BranchHandoverIntentV2":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(value, Mapping) or set(value) != fields | {"schema_version"} or value.get("schema_version") != _BRANCH_INTENT_SCHEMA:
            raise RunStateError("branch intent payload is malformed")
        return cls(**{
            key: _branch_artifact_ref(value[key]) if key in {"candidate_ref", "verification_ref"} else value[key]
            for key in fields
        })  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class HandoverTerminalV2:
    """One durable commit result bound to exactly one branch intent."""

    intent_sha256: str
    commit_oid: str
    evidence: ArtifactRef
    candidate_sha256: str
    terminal_sha256: str

    def __post_init__(self) -> None:
        if not _is_digest(self.intent_sha256) or not _is_digest(self.candidate_sha256):
            raise RunStateError("branch terminal digest is invalid")
        if not _is_oid(self.commit_oid) or self.commit_oid == _ZERO_OID:
            raise RunStateError("branch terminal commit OID is invalid")
        _branch_artifact_ref(self.evidence)
        if self.terminal_sha256 != _sha256(canonical_json(self._unsigned_dict())):
            raise RunStateError("branch terminal checksum is invalid")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": _BRANCH_TERMINAL_SCHEMA,
            "intent_sha256": self.intent_sha256, "commit_oid": self.commit_oid,
            "evidence": _branch_artifact_dict(self.evidence),
            "candidate_sha256": self.candidate_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._unsigned_dict(), "terminal_sha256": self.terminal_sha256}

    @classmethod
    def from_dict(cls, value: object) -> "HandoverTerminalV2":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(value, Mapping) or set(value) != fields | {"schema_version"} or value.get("schema_version") != _BRANCH_TERMINAL_SCHEMA:
            raise RunStateError("branch terminal payload is malformed")
        return cls(**{
            key: _branch_artifact_ref(value[key]) if key == "evidence" else value[key]
            for key in fields
        })  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class RunHandover:
    """Exact Task 14 transaction bound to one execution handover cycle."""

    task_id: str
    phase: str
    transaction_root: str
    transaction_sha256: str
    disposition_sha256: str | None = None

    def __post_init__(self) -> None:
        _identity(self.task_id, "handover task id")
        _handover_root(self.transaction_root)
        if (
            self.phase not in {
                "handover-intent",
                "handover-complete",
                "handover-rolled-back",
                "handover-conflict",
                "handover-not-mutated",
            }
            or not _is_digest(self.transaction_sha256)
            or (
                self.phase == "handover-intent"
                and self.disposition_sha256 is not None
            )
            or (
                self.phase != "handover-intent"
                and not _is_digest(self.disposition_sha256)
            )
        ):
            raise RunStateError("handover transaction binding is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "disposition_sha256": self.disposition_sha256,
            "phase": self.phase,
            "task_id": self.task_id,
            "transaction_root": self.transaction_root,
            "transaction_sha256": self.transaction_sha256,
        }


@dataclass(slots=True)
class RunState:
    """The exact state reconstructed from valid journal events."""

    inputs_digest: str
    seq: int = 0
    head_checksum: str = _ZERO_CHECKSUM
    seat_phases: dict[tuple[str, str, int, int], str] = field(default_factory=dict)
    task_phases: dict[str, str] = field(default_factory=dict)
    memory_block_owners: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    accepted_retries: set[tuple[str, str, int, int]] = field(default_factory=set)
    seat_evidence: dict[tuple[str, str, int, int, str], str] = field(default_factory=dict)
    uncertainty_resolutions: dict[tuple[str, str, int, int], str] = field(default_factory=dict)
    amendments: dict[int, RunAmendment] = field(default_factory=dict)
    handovers: dict[str, RunHandover] = field(default_factory=dict)
    branch_handovers: dict[str, tuple[BranchHandoverIntentV2, HandoverTerminalV2 | None]] = field(default_factory=dict)
    branch_records: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    branch_blocked: dict[str, tuple[str, str]] = field(default_factory=dict)

    def seat_phase(self, task_id: str, seat_id: str, attempt: int, round: int) -> str | None:
        return self.seat_phases.get((task_id, seat_id, attempt, round))

    def evidence_digest(
        self, event_type: str, task_id: str, seat_id: str, attempt: int, round: int,
    ) -> str | None:
        return self.seat_evidence.get((task_id, seat_id, attempt, round, event_type))

    def to_dict(self) -> dict[str, object]:
        return {
            "inputs_digest": self.inputs_digest,
            "seq": self.seq,
            "head_checksum": self.head_checksum,
            "seat_phases": [
                {"task_id": key[0], "seat_id": key[1], "attempt": key[2], "round": key[3], "phase": phase}
                for key, phase in sorted(self.seat_phases.items())
            ],
            "task_phases": [{"task_id": key, "phase": value} for key, value in sorted(self.task_phases.items())],
            "memory_block_owners": [
                {
                    "task_id": task_id,
                    "seat_id": owner[0],
                    "attempt": owner[1],
                    "round": owner[2],
                }
                for task_id, owner in sorted(self.memory_block_owners.items())
            ],
            "accepted_retries": [
                {"task_id": key[0], "seat_id": key[1], "attempt": key[2], "round": key[3]}
                for key in sorted(self.accepted_retries)
            ],
            "seat_evidence": [
                {
                    "task_id": key[0], "seat_id": key[1], "attempt": key[2],
                    "round": key[3], "event_type": key[4], "sha256": digest,
                }
                for key, digest in sorted(self.seat_evidence.items())
            ],
            "uncertainty_resolutions": [
                {
                    "task_id": key[0], "seat_id": key[1], "attempt": key[2],
                    "round": key[3], "disposition": disposition,
                }
                for key, disposition in sorted(self.uncertainty_resolutions.items())
            ],
            "amendments": [
                amendment.to_dict()
                for _revision, amendment in sorted(self.amendments.items())
            ],
            "handovers": [
                handover.to_dict()
                for _task_id, handover in sorted(self.handovers.items())
            ],
            "branch_handovers": [
                {"task_id": task_id, "intent": intent.to_dict(),
                 "terminal": None if terminal is None else terminal.to_dict()}
                for task_id, (intent, terminal) in sorted(self.branch_handovers.items())
            ],
            "branch_records": [
                {"task_id": task_id, "kind": kind, "name": ref[0], "digest": ref[1]}
                for task_id, records in sorted(self.branch_records.items())
                for kind, ref in sorted(records.items())
            ],
            "branch_blocked": [
                {"task_id": task_id, "intent_sha256": value[0], "reason": value[1]}
                for task_id, value in sorted(self.branch_blocked.items())
            ],
        }


@dataclass(frozen=True, slots=True)
class RunInspection:
    """Detached, authenticated replay evidence without controller ownership."""

    inputs: RunInputs
    state: RunState
    recovery: RecoveryReport
    pending_authority: bool
    authority_revision: int
    journal_sha256: str
    authority_sha256: str


class RunJournal:
    """An exclusively controlled append-only journal rooted at a pinned directory FD."""

    _EVENT_FILE = "events.jsonl"
    _INPUTS_FILE = "inputs.json"
    _OWNER_FILE = "owner.json"
    _LOCK_FILE = ".controller.lock"
    _SNAPSHOT_FILE = "snapshot.json"

    def __init__(self, root: Path, root_fd: int, lock_fd: int, inputs: RunInputs, *, limits: RunLimits,
                 owner_digest: str, state: RunState, journal_offset: int, recovery: RecoveryReport,
                 anchor_store: _AnchorAuthority, anchor_store_id: str, anchor_key: str,
                 anchor_revision: int) -> None:
        self.root = root
        self._root_fd = root_fd
        self._lock_fd = lock_fd
        self.inputs = inputs
        self.limits = limits
        self._owner_digest = owner_digest
        self._owner_salt = ""
        self._mac_key = b""
        self._lock_identity: tuple[int, int] = (0, 0)
        self._journal_identity: tuple[int, int] = (0, 0)
        self._anchor_store = anchor_store
        self._anchor_store_id = anchor_store_id
        self._anchor_key = anchor_key
        self._anchor_revision = anchor_revision
        self._loaded_journal_sha256 = ""
        self._loaded_authority_sha256 = ""
        self._pending_authority: dict[str, object] | None = None
        self._failed = False
        self.state = state
        self._journal_offset = journal_offset
        self.recovery = recovery
        self._events_fd: int | None = None

    @classmethod
    def create(cls, root: Path | str, inputs: RunInputs, *, limits: RunLimits | None = None,
               anchor_store: LocalAnchorAuthority | RemoteAnchorAuthority | None = None,
               owner_capability: OwnerCapability | None = None) -> tuple["RunJournal", OwnerCapability]:
        """Create a private run, optionally using an owner capability escrowed first."""
        return cls._create_impl(root, inputs, limits=limits, anchor_store=anchor_store,
                                owner_capability=owner_capability, allow_test=False)

    @classmethod
    def _create_for_test(cls, root: Path | str, inputs: RunInputs, *, limits: RunLimits | None = None,
                         anchor_store: _TestAnchorAuthority,
                         owner_capability: OwnerCapability | None = None) -> tuple["RunJournal", OwnerCapability]:
        """Private deterministic entrypoint; production callers cannot admit this adapter."""
        return cls._create_impl(root, inputs, limits=limits, anchor_store=anchor_store,
                                owner_capability=owner_capability, allow_test=True)

    @classmethod
    def _create_impl(cls, root: Path | str, inputs: RunInputs, *, limits: RunLimits | None,
                     anchor_store: _AnchorAuthority | None,
                     owner_capability: OwnerCapability | None,
                     allow_test: bool) -> tuple["RunJournal", OwnerCapability]:
        if not isinstance(inputs, RunInputs):
            raise TypeError("inputs must be RunInputs")
        if owner_capability is not None:
            if not isinstance(owner_capability, OwnerCapability):
                raise TypeError("owner_capability must be an OwnerCapability")
            try:
                owner_capability.export_token().encode("ascii")
            except UnicodeEncodeError as error:
                raise RunAuthorizationError("owner capability is malformed") from error
        store_id = _require_anchor_store(anchor_store, allow_test=allow_test)
        assert anchor_store is not None
        limits = limits or RunLimits()
        path = Path(root).absolute()
        if type(anchor_store) is LocalAnchorAuthority:
            anchor_store.check_run_root(path)
        root_fd = _open_private_directory(path, create=True, exclusive=True)
        lock_fd: int | None = None
        event_fd: int | None = None
        try:
            lock_fd = _acquire_lock(root_fd)
            capability = owner_capability or OwnerCapability(secrets.token_urlsafe(32))
            salt = secrets.token_hex(16)
            digest = _owner_verifier(salt, capability)
            lock_identity = _lock_identity(lock_fd)
            _write_new_private(root_fd, cls._INPUTS_FILE, canonical_json(inputs.to_dict()))
            os.fsync(root_fd)
            _write_new_private(root_fd, cls._EVENT_FILE, b"")
            event_fd = _open_event_journal(root_fd)
            journal_identity = _lock_identity(event_fd)
            anchor_key = f"fanout/{inputs.run_id}/{secrets.token_hex(16)}"
            initial_anchor = _authority_record(
                store_id, anchor_key, 1, inputs.run_id, journal_identity,
                0, _ZERO_CHECKSUM, 0, None, capability._mac_key(),
            )
            initial = _authority_create(anchor_store, store_id, anchor_key, initial_anchor)
            _write_new_private(root_fd, cls._OWNER_FILE, canonical_json({
                "schema_version": _OWNER_SCHEMA, "capability_salt": salt,
                "capability_verifier": digest, "lock_device": lock_identity[0],
                "lock_inode": lock_identity[1], "inputs_digest": inputs.digest,
                "journal_device": journal_identity[0], "journal_inode": journal_identity[1],
                "anchor_store_id": store_id, "anchor_key": anchor_key,
                "anchor_initial_revision": initial.revision,
                "mac": _mac(capability._mac_key(), b"owner", {
                    "schema_version": _OWNER_SCHEMA, "capability_salt": salt,
                    "capability_verifier": digest, "lock_device": lock_identity[0],
                    "lock_inode": lock_identity[1], "inputs_digest": inputs.digest,
                    "journal_device": journal_identity[0], "journal_inode": journal_identity[1],
                    "anchor_store_id": store_id, "anchor_key": anchor_key,
                    "anchor_initial_revision": initial.revision,
                }),
            }))
            os.fsync(root_fd)
            _assert_current_entry(root_fd, cls._EVENT_FILE, event_fd, journal_identity, "event journal")
            result = cls(path, root_fd, lock_fd, inputs, limits=limits, owner_digest=digest,
                         state=RunState(inputs.digest), journal_offset=0, recovery=RecoveryReport(),
                         anchor_store=anchor_store, anchor_store_id=store_id, anchor_key=anchor_key,
                         anchor_revision=initial.revision)
            result._mac_key = capability._mac_key()
            result._lock_identity = lock_identity
            result._journal_identity = journal_identity
            result._owner_salt = salt
            result._events_fd = event_fd
            event_fd = None
            return result, capability
        except BaseException:
            if event_fd is not None:
                try:
                    os.close(event_fd)
                except OSError:
                    pass
            if lock_fd is not None:
                try:
                    _release_lock(lock_fd)
                except OSError:
                    pass
            try:
                os.close(root_fd)
            except OSError:
                pass
            raise

    @classmethod
    def resume(cls, root: Path | str, expected_inputs: RunInputs, capability: OwnerCapability,
               *, limits: RunLimits | None = None,
               anchor_store: LocalAnchorAuthority | RemoteAnchorAuthority | None = None,
               expected_inspection: RunInspection | None = None) -> "RunJournal":
        """Lock and independently reconstruct a run, rejecting any changed input evidence."""
        return cls._resume_impl(
            root, expected_inputs, capability, limits=limits, anchor_store=anchor_store, allow_test=False,
            expected_inspection=expected_inspection,
        )

    @classmethod
    def inspect(cls, root: Path | str, expected_inputs: RunInputs, capability: OwnerCapability,
                *, limits: RunLimits | None = None,
                anchor_store: LocalAnchorAuthority | RemoteAnchorAuthority | None = None) -> RunInspection:
        """Authenticate and replay a run without repairing or committing pending authority."""
        return cls._inspect_impl(
            root, expected_inputs, capability, limits=limits, anchor_store=anchor_store, allow_test=False,
        )

    @classmethod
    def _inspect_for_test(cls, root: Path | str, expected_inputs: RunInputs, capability: OwnerCapability,
                          *, limits: RunLimits | None = None,
                          anchor_store: _TestAnchorAuthority) -> RunInspection:
        """Private deterministic inspection entrypoint for contract tests."""
        return cls._inspect_impl(
            root, expected_inputs, capability, limits=limits, anchor_store=anchor_store, allow_test=True,
        )

    @classmethod
    def _inspect_impl(cls, root: Path | str, expected_inputs: RunInputs, capability: OwnerCapability,
                      *, limits: RunLimits | None, anchor_store: _AnchorAuthority | None,
                      allow_test: bool) -> RunInspection:
        with cls._resume_impl(
            root, expected_inputs, capability, limits=limits, anchor_store=anchor_store,
            allow_test=allow_test, commit_pending=False,
        ) as journal:
            return RunInspection(
                journal.inputs, _copy_state(journal.state), journal.recovery,
                journal._pending_authority is not None, journal._anchor_revision,
                journal._loaded_journal_sha256, journal._loaded_authority_sha256,
            )

    @classmethod
    def _resume_for_test(cls, root: Path | str, expected_inputs: RunInputs, capability: OwnerCapability,
                         *, limits: RunLimits | None = None,
                         anchor_store: _TestAnchorAuthority,
                         expected_inspection: RunInspection | None = None) -> "RunJournal":
        """Private deterministic entrypoint; production callers cannot admit this adapter."""
        return cls._resume_impl(
            root, expected_inputs, capability, limits=limits, anchor_store=anchor_store, allow_test=True,
            expected_inspection=expected_inspection,
        )

    @classmethod
    def _resume_impl(cls, root: Path | str, expected_inputs: RunInputs, capability: OwnerCapability,
                     *, limits: RunLimits | None, anchor_store: _AnchorAuthority | None,
                     allow_test: bool, commit_pending: bool = True,
                     expected_inspection: RunInspection | None = None) -> "RunJournal":
        if not isinstance(expected_inputs, RunInputs):
            raise TypeError("expected_inputs must be RunInputs")
        if expected_inspection is not None and not isinstance(expected_inspection, RunInspection):
            raise TypeError("expected_inspection must be a RunInspection")
        store_id = _require_anchor_store(anchor_store, allow_test=allow_test)
        assert anchor_store is not None
        limits = limits or RunLimits()
        path = Path(root).absolute()
        if type(anchor_store) is LocalAnchorAuthority:
            anchor_store.check_run_root(path)
        root_fd = _open_private_directory(path, create=False, exclusive=False)
        lock_fd: int | None = None
        event_fd: int | None = None
        try:
            stored = RunInputs.from_dict(_read_json_file(root_fd, cls._INPUTS_FILE, limits.max_snapshot_bytes))
            if not hmac.compare_digest(stored.digest, expected_inputs.digest):
                raise RunStateError("run inputs do not match immutable preflight evidence")
            owner = _read_json_file(root_fd, cls._OWNER_FILE, limits.max_snapshot_bytes)
            if not _validate_owner(owner, capability, stored.digest):
                raise RunStateError("owner capability record is malformed")
            if owner["anchor_store_id"] != store_id:
                raise RunStateError("anchor store identity does not match owner metadata")
            lock_fd = _acquire_lock(root_fd, (owner["lock_device"], owner["lock_inode"]),
                                    readonly=not commit_pending)
            journal_identity = (owner["journal_device"], owner["journal_inode"])
            event_fd = _open_event_journal(root_fd, expected=journal_identity,
                                           readonly=not commit_pending)
            data = _read_private_fd(event_fd, limits.max_journal_bytes, "event journal")
            _assert_current_entry(root_fd, cls._EVENT_FILE, event_fd, journal_identity, "event journal")
            records, offset, torn = _parse_events(data, limits, capability._mac_key())
            state = _replay(records, stored.digest, stored.run_id, inputs=stored)
            _verify_branch_artifacts(path, state, stored)
            anchor_revision, anchor_state = _authority_read(
                anchor_store, store_id, owner["anchor_key"], capability._mac_key(), stored.run_id,
                (owner["journal_device"], owner["journal_inode"]),
            )
            journal_sha256 = _sha256(data)
            authority_sha256 = _sha256(anchor_revision.value)
            if expected_inspection is not None and (
                expected_inspection.inputs.digest != stored.digest
                or expected_inspection.state.seq != state.seq
                or expected_inspection.state.head_checksum != state.head_checksum
                or expected_inspection.recovery.torn_tail_bytes != torn
                or expected_inspection.pending_authority != (anchor_state["pending"] is not None)
                or expected_inspection.authority_revision != anchor_revision.revision
                or expected_inspection.journal_sha256 != journal_sha256
                or expected_inspection.authority_sha256 != authority_sha256
            ):
                raise RunStateError("run changed since authenticated inspection")
            pending: dict[str, object] | None = None
            if anchor_state["pending"] is not None:
                pending = anchor_state
                record = anchor_state["pending"]
                base_count = anchor_state["seq"]
                if base_count > len(records):
                    raise RunStateError("journal is older than the pending authority base")
                base_records = records[:base_count]
                base_state = _replay(base_records, stored.digest, stored.run_id, inputs=stored)
                base_offset = sum(len(canonical_json(item)) for item in base_records)
                if base_records and base_records[-1]["authority_revision"] > anchor_revision.revision:
                    raise RunStateError("pending authority revision predates its committed journal base")
                if (base_state.seq, base_state.head_checksum, base_offset) != (
                        anchor_state["seq"], anchor_state["head"], anchor_state["offset"]):
                    raise RunStateError("pending authority base does not match the journal prefix")
                prospective = _copy_state(base_state)
                _apply_event(prospective, record["type"], record["payload"])
                encoded = canonical_json(record)
                suffix = data[base_offset:]
                if suffix == encoded:
                    if torn or offset != base_offset + len(encoded) or not records or records[-1] != record:
                        raise RunStateError("journal does not contain the exact pending authority event")
                    if commit_pending:
                        committed = _authority_record(
                            store_id, owner["anchor_key"], anchor_revision.revision + 1, stored.run_id,
                            (owner["journal_device"], owner["journal_inode"]), state.seq,
                            state.head_checksum, offset, None, capability._mac_key(),
                        )
                        anchor_revision = _authority_cas(
                            anchor_store, store_id, owner["anchor_key"], anchor_revision.revision, committed,
                        )
                        anchor_state = committed
                        pending = None
                elif (state.seq, state.head_checksum, offset) != (
                        anchor_state["seq"], anchor_state["head"], base_offset):
                    raise RunStateError("journal diverges from the pending authority base")
                elif not encoded.startswith(suffix):
                    raise RunStateError("journal tail is not the pending authority event")
            elif (anchor_state["seq"], anchor_state["head"], anchor_state["offset"]) != (
                    state.seq, state.head_checksum, offset):
                raise RunStateError("journal does not match external monotonic authority")
            elif records and records[-1]["authority_revision"] > anchor_revision.revision:
                raise RunStateError("external authority revision predates the journal head")
            snapshot = _read_optional_json_file(root_fd, cls._SNAPSHOT_FILE, limits.max_snapshot_bytes)
            if snapshot is not None:
                _verify_snapshot(
                    snapshot, data, records, stored.digest, stored.run_id, capability._mac_key(),
                    store_id, owner["anchor_key"], (owner["journal_device"], owner["journal_inode"]),
                    anchor_revision.revision, inputs=stored,
                )
            _assert_current_entry(root_fd, cls._EVENT_FILE, event_fd, journal_identity, "event journal")
            result = cls(path, root_fd, lock_fd, stored, limits=limits,
                       owner_digest=owner["capability_verifier"], state=state,
                       journal_offset=offset, recovery=RecoveryReport(torn),
                       anchor_store=anchor_store, anchor_store_id=store_id,
                       anchor_key=owner["anchor_key"], anchor_revision=anchor_revision.revision)
            result._owner_digest = owner["capability_verifier"]
            result._owner_salt = owner["capability_salt"]
            result._mac_key = capability._mac_key()
            result._lock_identity = (owner["lock_device"], owner["lock_inode"])
            result._journal_identity = (owner["journal_device"], owner["journal_inode"])
            result._pending_authority = pending
            result._loaded_journal_sha256 = journal_sha256
            result._loaded_authority_sha256 = authority_sha256
            result._events_fd = event_fd
            event_fd = None
            return result
        except BaseException:
            if event_fd is not None:
                try:
                    os.close(event_fd)
                except OSError:
                    pass
            if lock_fd is not None:
                try:
                    _release_lock(lock_fd)
                except OSError:
                    pass
            try:
                os.close(root_fd)
            except OSError:
                pass
            raise

    def close(self) -> None:
        """Release controller ownership deterministically; lock release also happens on process exit."""
        failure: OSError | None = None
        events_fd, self._events_fd = self._events_fd, None
        if events_fd is not None:
            try:
                os.close(events_fd)
            except OSError as error:
                failure = error
        lock_fd, self._lock_fd = self._lock_fd, -1
        if lock_fd >= 0:
            try:
                _release_lock(lock_fd)
            except OSError as error:
                failure = failure or error
        root_fd, self._root_fd = self._root_fd, -1
        if root_fd >= 0:
            try:
                os.close(root_fd)
            except OSError as error:
                failure = failure or error
        if failure is not None:
            raise failure

    def __enter__(self) -> "RunJournal":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    def append(self, event_type: str, *, task_id: str, seat_id: str | None = None,
               attempt: int | None = None, round: int | None = None, owner: OwnerCapability | None = None,
               timestamp: str | None = None, evidence_sha256: str | None = None,
               transaction_root: str | None = None,
               transaction_sha256: str | None = None,
               disposition_sha256: str | None = None) -> None:
        """Validate then durably append one canonical event; invalid calls write nothing."""
        self._assert_open()
        self._assert_lock_intact()
        self._assert_journal_intact()
        if self.recovery.torn_tail_bytes:
            raise RunStateError("journal has a torn final record; owner repair is required before append")
        if event_type not in _EVENT_NAME:
            raise RunStateError(f"unknown run event type: {event_type}")
        if event_type in _OWNER_EVENTS:
            self._authorize(owner)
        payload = _event_payload(
            event_type, task_id, seat_id, attempt, round,
            evidence_sha256=evidence_sha256,
            transaction_root=transaction_root,
            transaction_sha256=transaction_sha256,
            disposition_sha256=disposition_sha256,
        )
        prospective = _copy_state(self.state)
        _apply_event(prospective, event_type, payload)
        record = {
            "schema_version": _EVENT_SCHEMA,
            "version": 1,
            "run_id": self.inputs.run_id,
            "seq": self.state.seq + 1,
            "previous_checksum": self.state.head_checksum,
            "timestamp": _timestamp() if timestamp is None else _validate_timestamp(timestamp),
            "type": event_type,
            "payload": payload,
            "payload_sha256": _sha256(canonical_json(payload)),
            "authority_revision": self._anchor_revision + 2,
        }
        record["checksum"] = _checksum(record)
        record["mac"] = _mac(self._mac_key, b"event", record)
        encoded = canonical_json(record)
        if len(encoded) > self.limits.max_journal_bytes:
            raise RunStateError("event exceeds journal byte limit")
        if self.state.seq >= self.limits.max_events:
            raise RunStateError("journal event limit exceeded")
        current = self._event_fd()
        size = os.fstat(current).st_size
        if size != self._journal_offset:
            raise RunStateError("journal changed while controller lock was held")
        if size + len(encoded) > self.limits.max_journal_bytes:
            raise RunStateError("journal byte limit exceeded")
        self._anchor_pending(record, size + len(encoded))
        _write_all(current, encoded)
        os.fsync(current)
        os.fsync(self._root_fd)
        self._assert_journal_intact()
        self._anchor_commit(record, size + len(encoded))
        self._assert_journal_intact()
        prospective.seq = record["seq"]
        prospective.head_checksum = record["checksum"]
        self.state = prospective
        self._journal_offset += len(encoded)

    def authorize_owner(self, owner: OwnerCapability) -> None:
        """Authenticate the owner and recheck the exclusive controller lock without mutating state."""
        self._assert_open()
        self._authorize(owner)
        self._assert_lock_intact()
        self._assert_journal_intact()

    def recover_uncertain(self, owner: OwnerCapability) -> int:
        """Durably classify started-but-not-terminal attempts without retrying them."""
        self._authorize(owner)
        self._assert_lock_intact()
        self._assert_journal_intact()
        converted = 0
        for (task_id, seat_id, attempt, round), phase in sorted(tuple(self.state.seat_phases.items())):
            if phase == "process-started":
                self.append("uncertain-attempt", task_id=task_id, seat_id=seat_id, attempt=attempt,
                            round=round, owner=owner)
                converted += 1
        return converted

    def resolve_uncertain(
        self, owner: OwnerCapability, *, task_id: str, seat_id: str,
        attempt: int, round: int, disposition: str = "exclude",
    ) -> None:
        """Durably record the owner's explicit disposition of one uncertain turn."""
        self._authorize(owner)
        if disposition != "exclude":
            raise RunStateError("uncertain disposition must be exclude")
        payload = {
            "task_id": _identity(task_id, "task id"),
            "seat_id": _identity(seat_id, "seat id"),
            "attempt": attempt,
            "round": round,
            "disposition": disposition,
        }
        prospective = _copy_state(self.state)
        _apply_event(prospective, "uncertainty-resolved", payload)
        self._append_prepared("uncertainty-resolved", payload, prospective)

    def accept_possible_duplicate(self, owner: OwnerCapability, *, task_id: str, seat_id: str,
                                  prior_attempt: int, next_attempt: int, round: int) -> None:
        """Require explicit owner acknowledgement before a possibly billable retry."""
        self._authorize(owner)
        if not _is_int(prior_attempt, minimum=1) or not _is_int(next_attempt, minimum=1) or not _is_int(round, minimum=1):
            raise RunStateError("attempt and round identities must be integers")
        payload = {
            "task_id": _identity(task_id, "task id"), "seat_id": _identity(seat_id, "seat id"),
            "prior_attempt": prior_attempt, "next_attempt": next_attempt, "round": round,
        }
        prospective = _copy_state(self.state)
        _apply_event(prospective, "duplicate-spend-accepted", payload)
        self._append_prepared("duplicate-spend-accepted", payload, prospective)

    def append_amendment(
        self,
        event_type: str,
        *,
        revision: int,
        old_plan_sha256: str,
        old_inputs_digest: str,
        new_plan_sha256: str,
        new_inputs_digest: str,
        old_profiles_sha256: str,
        new_profiles_sha256: str,
        owner: OwnerCapability,
    ) -> None:
        """Append one typed amendment transition outside the task namespace."""
        self._authorize(owner)
        payload = {
            "new_inputs_digest": new_inputs_digest,
            "new_plan_sha256": new_plan_sha256,
            "old_inputs_digest": old_inputs_digest,
            "old_plan_sha256": old_plan_sha256,
            "old_profiles_sha256": old_profiles_sha256,
            "new_profiles_sha256": new_profiles_sha256,
            "revision": revision,
        }
        prospective = _copy_state(self.state)
        _apply_event(prospective, event_type, payload)
        self._append_prepared(event_type, payload, prospective)

    def append_branch_intent(self, intent: BranchHandoverIntentV2, *, owner: OwnerCapability) -> None:
        """Journal one v3 target's exact handover decision after its artifacts are durable."""
        self.authorize_owner(owner)
        if not isinstance(intent, BranchHandoverIntentV2) or self.inputs.targets is None:
            raise RunStateError("branch intent requires v3 target inputs")
        _validate_branch_intent_binding(intent, self.inputs, self.state.amendments)
        self._read_branch_evidence(intent)
        prospective = _copy_state(self.state)
        _apply_event(prospective, _BRANCH_INTENT_EVENT, intent.to_dict(), inputs=self.inputs)
        self._append_prepared(_BRANCH_INTENT_EVENT, intent.to_dict(), prospective)

    def append_branch_terminal(self, task_id: str, terminal: HandoverTerminalV2, *, owner: OwnerCapability) -> None:
        """Require a stored terminal artifact before the fsynced event and authority CAS."""
        self.authorize_owner(owner)
        if not isinstance(terminal, HandoverTerminalV2):
            raise RunStateError("branch terminal is invalid")
        active = self.state.branch_handovers.get(task_id)
        if active is not None and terminal.candidate_sha256 != self._read_branch_evidence(active[0]):
            raise RunStateError("branch terminal candidate digest differs from intent")
        self._read_branch_artifact(terminal.evidence)
        payload = {"task_id": _identity(task_id, "branch task id"), "terminal": terminal.to_dict()}
        prospective = _copy_state(self.state)
        _apply_event(prospective, _BRANCH_TERMINAL_EVENT, payload)
        self._append_prepared(_BRANCH_TERMINAL_EVENT, payload, prospective)

    def append_branch_record(self, task_id: str, kind: str, name: str, digest: str,
                             *, owner: OwnerCapability) -> None:
        """Index one controller-sealed association or prepared commit under its intent."""
        self.authorize_owner(owner)
        payload = {"task_id": task_id, "kind": kind, "name": name, "digest": digest}
        prospective = _copy_state(self.state)
        _apply_event(prospective, _BRANCH_RECORD_EVENT, payload)
        self._append_prepared(_BRANCH_RECORD_EVENT, payload, prospective)

    def branch_record_for(self, task_id: str, kind: str) -> tuple[str, str] | None:
        self._assert_open()
        self._assert_lock_intact()
        self._assert_journal_intact()
        return self.state.branch_records.get(task_id, {}).get(kind)

    def append_branch_blocked(self, task_id: str, reason: str, *, owner: OwnerCapability) -> None:
        self.authorize_owner(owner)
        intent, _terminal = self.branch_handover_state(task_id)
        payload = {"task_id": task_id, "intent_sha256": intent.sha256, "reason": reason}
        prospective = _copy_state(self.state)
        _apply_event(prospective, _BRANCH_BLOCKED_EVENT, payload)
        self._append_prepared(_BRANCH_BLOCKED_EVENT, payload, prospective)

    def branch_handover_state(self, task_id: str) -> tuple[BranchHandoverIntentV2, HandoverTerminalV2 | None]:
        """Read the authenticated branch state and recheck its immutable evidence bytes."""
        self._assert_open()
        self._assert_lock_intact()
        self._assert_journal_intact()
        state = self.state.branch_handovers[_identity(task_id, "branch task id")]
        candidate_sha256 = self._read_branch_evidence(state[0])
        if state[1] is not None:
            if state[1].candidate_sha256 != candidate_sha256:
                raise RunStateError("branch terminal candidate digest differs from intent")
            self._read_branch_artifact(state[1].evidence)
        return state

    def _read_branch_evidence(self, intent: BranchHandoverIntentV2) -> str:
        try:
            with ArtifactStore.open_existing(self.root / "artifacts") as store:
                digest = _branch_candidate_digest(store, intent, self.inputs)
                _branch_verification_digest(store, intent, self.inputs, digest)
                return digest
        except (ArtifactError, OSError, ValueError) as error:
            raise RunStateError("branch handover candidate or verification evidence is missing or changed") from error

    def _read_branch_artifact(self, ref: ArtifactRef) -> bytes:
        # The run's existing artifacts directory is the sole v3 evidence store.
        # Opening it existing-only prevents a missing store from becoming evidence.
        try:
            with ArtifactStore.open_existing(self.root / "artifacts") as store:
                return store.read_bytes(ref)
        except (ArtifactError, OSError, ValueError) as error:
            raise RunStateError("branch handover artifact evidence is missing or changed") from error

    def snapshot(self, owner: OwnerCapability) -> None:
        """Atomically replace a fsynced snapshot tied to an exact journal byte prefix."""
        self._authorize(owner)
        self._assert_open()
        self._assert_lock_intact()
        self._assert_journal_intact()
        if self.recovery.torn_tail_bytes:
            raise RunStateError("journal has a torn final record; owner repair is required before snapshot")
        snapshot_schema = _BRANCH_SNAPSHOT_SCHEMA if self.inputs.targets is not None else _SNAPSHOT_SCHEMA
        snapshot_state = self.state.to_dict()
        if snapshot_schema == _SNAPSHOT_SCHEMA:
            del snapshot_state["branch_handovers"]
            del snapshot_state["branch_records"]
            del snapshot_state["branch_blocked"]
        snapshot = {
            "schema_version": snapshot_schema,
            "journal_offset": self._journal_offset,
            "seq": self.state.seq,
            "head_checksum": self.state.head_checksum,
            "anchor_store_id": self._anchor_store_id,
            "anchor_key": self._anchor_key,
            "authority_revision": self._anchor_revision,
            "journal_device": self._journal_identity[0],
            "journal_inode": self._journal_identity[1],
            "state": snapshot_state,
        }
        snapshot["mac"] = _mac(self._mac_key, b"snapshot", snapshot)
        encoded = canonical_json(snapshot)
        if len(encoded) > self.limits.max_snapshot_bytes:
            raise RunStateError("snapshot byte limit exceeded")
        name = f".snapshot-{secrets.token_hex(16)}.tmp"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._root_fd)
        try:
            _write_all(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(name, self._SNAPSHOT_FILE, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
            os.fsync(self._root_fd)
            self._assert_journal_intact()
        except BaseException:
            try:
                os.unlink(name, dir_fd=self._root_fd)
            except FileNotFoundError:
                pass
            raise

    def repair_torn_tail(self, owner: OwnerCapability) -> int:
        """Owner-only truncation of only the final malformed, non-evidence tail."""
        self._authorize(owner)
        self._assert_lock_intact()
        self._assert_journal_intact()
        if not self.recovery.torn_tail_bytes:
            return 0
        fd = self._event_fd()
        previous = os.fstat(fd).st_size
        if previous != self._journal_offset + self.recovery.torn_tail_bytes:
            raise RunStateError("journal changed while controller lock was held")
        os.ftruncate(fd, self._journal_offset)
        os.fsync(fd)
        os.fsync(self._root_fd)
        self._assert_journal_intact()
        removed = self.recovery.torn_tail_bytes
        self.recovery = RecoveryReport()
        return removed

    def recover_pending(self, owner: OwnerCapability) -> str:
        """Resolve an externally durable pending append without guessing provider work.

        Exact complete events are committed during ``resume``. An absent or
        partial dispatch intent is explicitly discarded. Any later seat event
        is durably replaced by ``uncertain-attempt`` for the same exact
        task/seat/attempt/round before dispatch can resume.
        """
        self._authorize(owner)
        self._assert_lock_intact()
        self._assert_journal_intact()
        pending_state = self._pending_authority
        if pending_state is None:
            return "none"
        pending = pending_state["pending"]
        assert isinstance(pending, dict)
        fd = self._event_fd()
        current_size = os.fstat(fd).st_size
        base_offset = pending_state["offset"]
        encoded = canonical_json(pending)
        if current_size < base_offset or current_size > base_offset + len(encoded):
            raise RunStateError("pending journal tail has an impossible size")
        journal_bytes = _read_private_fd(fd, self.limits.max_journal_bytes, "event journal")
        self._assert_journal_intact()
        suffix = journal_bytes[base_offset:]
        if not encoded.startswith(suffix):
            raise RunStateError("journal tail is not the exact pending authority event")
        if suffix == encoded:
            prospective = _copy_state(self.state)
            _apply_event(prospective, pending["type"], pending["payload"])
            committed = _authority_record(
                self._anchor_store_id, self._anchor_key, self._anchor_revision + 1,
                self.inputs.run_id, self._journal_identity, pending["seq"],
                pending["checksum"], base_offset + len(encoded), None, self._mac_key,
            )
            self._assert_journal_intact()
            revision = _authority_cas(
                self._anchor_store, self._anchor_store_id, self._anchor_key,
                self._anchor_revision, committed,
            )
            prospective.seq = pending["seq"]  # type: ignore[assignment]
            prospective.head_checksum = pending["checksum"]  # type: ignore[assignment]
            self.state = prospective
            self._journal_offset = base_offset + len(encoded)
            self._anchor_revision = revision.revision
            self._pending_authority = None
            self.recovery = RecoveryReport()
            self._failed = False
            return "committed"
        if current_size != base_offset:
            os.ftruncate(fd, base_offset)
            os.fsync(fd)
            os.fsync(self._root_fd)
            self.recovery = RecoveryReport()
            self._assert_journal_intact()
        if pending["type"] == "dispatch-intent" or pending["type"] not in _SEAT_EVENTS:
            committed = _authority_record(
                self._anchor_store_id, self._anchor_key, self._anchor_revision + 1,
                self.inputs.run_id, self._journal_identity, self.state.seq,
                self.state.head_checksum, self._journal_offset, None, self._mac_key,
            )
            self._assert_journal_intact()
            revision = _authority_cas(
                self._anchor_store, self._anchor_store_id, self._anchor_key,
                self._anchor_revision, committed,
            )
            self._anchor_revision = revision.revision
            self._pending_authority = None
            self._failed = False
            return "discarded-intent" if pending["type"] == "dispatch-intent" else "discarded-control"

        payload = pending["payload"]
        assert isinstance(payload, dict)
        prospective = _copy_state(self.state)
        _apply_event(prospective, "uncertain-attempt", payload)
        uncertain = self._make_record("uncertain-attempt", payload, self._anchor_revision + 2)
        self._replace_pending_and_append(uncertain, prospective)
        return "uncertain-attempt"

    def _append_prepared(self, event_type: str, payload: dict[str, object], prospective: RunState) -> None:
        """Append a prevalidated non-seat event without re-applying its transition."""
        self._assert_open()
        self._assert_lock_intact()
        self._assert_journal_intact()
        if self.recovery.torn_tail_bytes:
            raise RunStateError("journal has a torn final record; owner repair is required before append")
        record = {
            "schema_version": _EVENT_SCHEMA, "version": 1, "run_id": self.inputs.run_id,
            "seq": self.state.seq + 1,
            "previous_checksum": self.state.head_checksum, "timestamp": _timestamp(), "type": event_type,
            "payload": payload, "payload_sha256": _sha256(canonical_json(payload)),
            "authority_revision": self._anchor_revision + 2,
        }
        record["checksum"] = _checksum(record)
        record["mac"] = _mac(self._mac_key, b"event", record)
        encoded = canonical_json(record)
        if self.state.seq >= self.limits.max_events:
            raise RunStateError("journal event limit exceeded")
        fd = self._event_fd()
        if os.fstat(fd).st_size != self._journal_offset or self._journal_offset + len(encoded) > self.limits.max_journal_bytes:
            raise RunStateError("journal byte limit exceeded or journal changed while locked")
        self._anchor_pending(record, self._journal_offset + len(encoded))
        _write_all(fd, encoded)
        os.fsync(fd)
        os.fsync(self._root_fd)
        self._assert_journal_intact()
        self._anchor_commit(record, self._journal_offset + len(encoded))
        self._assert_journal_intact()
        prospective.seq = record["seq"]
        prospective.head_checksum = record["checksum"]
        self.state = prospective
        self._journal_offset += len(encoded)

    def _anchor_pending(self, record: Mapping[str, object], offset: int) -> None:
        try:
            pending = _authority_record(
                self._anchor_store_id, self._anchor_key, self._anchor_revision + 1,
                self.inputs.run_id, self._journal_identity, self.state.seq,
                self.state.head_checksum, self._journal_offset, record, self._mac_key,
            )
            revision = _authority_cas(
                self._anchor_store, self._anchor_store_id, self._anchor_key,
                self._anchor_revision, pending,
            )
            self._anchor_revision = revision.revision
            self._pending_authority = pending
        except RunStateError:
            self._failed = True
            raise

    def _anchor_commit(self, record: Mapping[str, object], offset: int) -> None:
        try:
            committed = _authority_record(
                self._anchor_store_id, self._anchor_key, self._anchor_revision + 1,
                self.inputs.run_id, self._journal_identity, record["seq"],
                record["checksum"], offset, None, self._mac_key,
            )
            revision = _authority_cas(
                self._anchor_store, self._anchor_store_id, self._anchor_key,
                self._anchor_revision, committed,
            )
            if revision.revision != record["authority_revision"]:
                raise RunStateError("authority commit revision does not match the journal event")
            self._anchor_revision = revision.revision
            self._pending_authority = None
        except RunStateError:
            self._failed = True
            raise

    def _make_record(self, event_type: str, payload: Mapping[str, object], authority_revision: int) -> dict[str, object]:
        record: dict[str, object] = {
            "schema_version": _EVENT_SCHEMA, "version": 1, "run_id": self.inputs.run_id,
            "seq": self.state.seq + 1, "previous_checksum": self.state.head_checksum,
            "timestamp": _timestamp(), "type": event_type, "payload": dict(payload),
            "payload_sha256": _sha256(canonical_json(payload)),
            "authority_revision": authority_revision,
        }
        record["checksum"] = _checksum(record)
        record["mac"] = _mac(self._mac_key, b"event", record)
        return record

    def _replace_pending_and_append(self, record: Mapping[str, object], prospective: RunState) -> None:
        encoded = canonical_json(record)
        pending = _authority_record(
            self._anchor_store_id, self._anchor_key, self._anchor_revision + 1,
            self.inputs.run_id, self._journal_identity, self.state.seq,
            self.state.head_checksum, self._journal_offset, record, self._mac_key,
        )
        revision = _authority_cas(
            self._anchor_store, self._anchor_store_id, self._anchor_key,
            self._anchor_revision, pending,
        )
        self._anchor_revision = revision.revision
        self._pending_authority = pending
        fd = self._event_fd()
        _write_all(fd, encoded)
        os.fsync(fd)
        os.fsync(self._root_fd)
        self._assert_journal_intact()
        self._anchor_commit(record, self._journal_offset + len(encoded))
        self._assert_journal_intact()
        prospective.seq = record["seq"]  # type: ignore[assignment]
        prospective.head_checksum = record["checksum"]  # type: ignore[assignment]
        self.state = prospective
        self._journal_offset += len(encoded)
        self.recovery = RecoveryReport()
        self._failed = False

    def _authorize(self, capability: OwnerCapability | None) -> None:
        if not isinstance(capability, OwnerCapability):
            raise RunAuthorizationError("owner capability is required")
        supplied = _owner_verifier(self._owner_salt, capability)
        if not hmac.compare_digest(supplied, self._owner_digest):
            raise RunAuthorizationError("owner capability is invalid")

    def _assert_lock_intact(self) -> None:
        expected = self._lock_identity
        if _lock_identity(self._lock_fd) != expected:
            raise RunLockError("controller lock descriptor changed")
        try:
            entry = os.stat(self._LOCK_FILE, dir_fd=self._root_fd, follow_symlinks=False)
        except OSError as error:
            raise RunLockError("controller lock entry is missing") from error
        if not _private_stat(entry) or (entry.st_dev, entry.st_ino) != expected:
            raise RunLockError("controller lock entry was replaced")

    def _assert_journal_intact(self) -> None:
        fd = self._event_fd()
        _assert_current_entry(
            self._root_fd, self._EVENT_FILE, fd, self._journal_identity, "event journal",
        )

    def _event_fd(self) -> int:
        if self._events_fd is None:
            self._events_fd = _open_event_journal(self._root_fd, expected=self._journal_identity)
        return self._events_fd

    def _assert_open(self) -> None:
        if self._root_fd < 0 or self._lock_fd < 0:
            raise ValueError("run journal is closed")
        if self._failed:
            raise RunStateError("run journal requires owner recovery")
        if self._pending_authority is not None:
            raise RunStateError("external authority has a pending event; owner recovery is required")


def _event_payload(event_type: str, task_id: str, seat_id: str | None, attempt: int | None,
                   round: int | None, *, evidence_sha256: str | None = None,
                   transaction_root: str | None = None,
                   transaction_sha256: str | None = None,
                   disposition_sha256: str | None = None) -> dict[str, object]:
    task_id = _identity(task_id, "task id")
    handover_events = {
        "handover-intent", "handover-complete", "handover-rolled-back",
        "handover-conflict", "handover-not-mutated",
    }
    if event_type in handover_events:
        if any(value is not None for value in (seat_id, attempt, round, evidence_sha256)):
            raise RunStateError("handover events may only carry transaction evidence")
        root = _handover_root(transaction_root)
        if not _is_digest(transaction_sha256):
            raise RunStateError("handover transaction digest is invalid")
        if event_type == "handover-intent":
            if disposition_sha256 is not None:
                raise RunStateError("handover intent cannot carry a disposition")
        elif not _is_digest(disposition_sha256):
            raise RunStateError("handover terminal disposition digest is invalid")
        return {
            "disposition_sha256": disposition_sha256,
            "task_id": task_id,
            "transaction_root": root,
            "transaction_sha256": transaction_sha256,
        }
    if any(
        value is not None
        for value in (transaction_root, transaction_sha256, disposition_sha256)
    ):
        raise RunStateError("transaction evidence is only valid for handover events")
    if evidence_sha256 is not None and event_type not in {
        "dispatch-intent", "provider-terminal", "checkpoint-published", "checkpoint-verified",
    }:
        raise RunStateError("event evidence digest is invalid")
    if event_type in _SEAT_EVENTS:
        if not isinstance(seat_id, str) or not _is_int(attempt, minimum=1) or not _is_int(round, minimum=1):
            raise RunStateError("seat events require task, seat, attempt, and round identities")
        payload: dict[str, object] = {
            "task_id": task_id, "seat_id": _identity(seat_id, "seat id"),
            "attempt": attempt, "round": round,
        }
        if evidence_sha256 is not None:
            if event_type not in {
                "dispatch-intent", "provider-terminal", "checkpoint-published", "checkpoint-verified",
            } or not _is_digest(evidence_sha256):
                raise RunStateError("event evidence digest is invalid")
            payload["evidence_sha256"] = evidence_sha256
        return payload
    if event_type in {"blocked-memory", "memory-recovered"}:
        if seat_id is None and attempt is None and round is None:
            return {"task_id": task_id}
        if isinstance(seat_id, str) and _is_int(attempt, minimum=1) and _is_int(round, minimum=1):
            return {
                "task_id": task_id,
                "seat_id": _identity(seat_id, "seat id"),
                "attempt": attempt,
                "round": round,
            }
        raise RunStateError(
            "memory recovery events require either no owner or an exact seat, attempt, and round"
        )
    if event_type in {"reconciliation-pending", "reconciliation-verifying", "completed", "blocked-action",
                      "action-complete", "plan-amended", "amendment-accepted", "handover-intent",
                      "handover-complete", "handover-rolled-back", "handover-conflict",
                      "handover-not-mutated"}:
        if any(value is not None for value in (seat_id, attempt, round)):
            raise RunStateError("task control events may not carry seat identities")
        return {"task_id": task_id}
    raise RunStateError(f"event needs a dedicated payload: {event_type}")


def _apply_event(state: RunState, event_type: str, payload: Mapping[str, object],
                 *, inputs: RunInputs | None = None) -> None:
    if event_type == _BRANCH_RECORD_EVENT:
        if set(payload) != {"task_id", "kind", "name", "digest"}:
            raise RunStateError("branch record payload is malformed")
        task_id = _identity(payload["task_id"], "branch task id")
        kind = payload["kind"]
        name = payload["name"]
        digest = payload["digest"]
        active = state.branch_handovers.get(task_id)
        records = state.branch_records.setdefault(task_id, {})
        if (active is None or active[1] is not None or task_id in state.branch_blocked
                or kind not in {"association", "prepared-commit"} or kind in records
                or kind == "prepared-commit" and "association" not in records
                or not _is_digest(digest) or name != f"{digest}.json"):
            raise RunStateError("branch record is duplicate or lacks its intent")
        records[kind] = (name, digest)
        return
    if event_type == _BRANCH_BLOCKED_EVENT:
        if set(payload) != {"task_id", "intent_sha256", "reason"}:
            raise RunStateError("branch blocked payload is malformed")
        task_id = _identity(payload["task_id"], "branch task id")
        active = state.branch_handovers.get(task_id)
        reason = payload["reason"]
        if (active is None or task_id in state.branch_blocked
                or payload["intent_sha256"] != active[0].sha256
                or reason not in {"association-missing", "association-changed", "pre-cas-interrupted",
                                  "prepared-commit-missing", "source-changed", "target-changed",
                                  "ref-changed", "checkout-changed", "git-proof-invalid", "delivery-failed"}):
            raise RunStateError("branch blocked disposition is invalid")
        state.branch_blocked[task_id] = (active[0].sha256, reason)
        return
    if event_type == _BRANCH_INTENT_EVENT:
        intent = BranchHandoverIntentV2.from_dict(payload)
        expected_digest = (state.inputs_digest if intent.plan_revision == 1 else
                           state.amendments.get(intent.plan_revision).new_inputs_digest
                           if intent.plan_revision in state.amendments else None)
        if intent.inputs_digest != expected_digest or intent.task_id in state.branch_handovers:
            raise RunStateError("branch intent is duplicate or differs from run inputs")
        if inputs is not None:
            _validate_branch_intent_binding(intent, inputs, state.amendments)
        state.branch_handovers[intent.task_id] = (intent, None)
        return
    if event_type == _BRANCH_TERMINAL_EVENT:
        if set(payload) != {"task_id", "terminal"}:
            raise RunStateError("branch terminal payload is malformed")
        task_id = _identity(payload["task_id"], "branch task id")
        terminal = HandoverTerminalV2.from_dict(payload["terminal"])
        active = state.branch_handovers.get(task_id)
        if (active is None or active[1] is not None or task_id in state.branch_blocked
                or terminal.intent_sha256 != active[0].sha256):
            raise RunStateError("branch terminal is duplicate or lacks its exact intent")
        state.branch_handovers[task_id] = (active[0], terminal)
        return
    if event_type in {
        "handover-intent", "handover-complete", "handover-rolled-back",
        "handover-conflict", "handover-not-mutated",
    }:
        expected = {
            "disposition_sha256", "task_id", "transaction_root",
            "transaction_sha256",
        }
        if set(payload) != expected:
            raise RunStateError("handover transaction payload is malformed")
        task_id = _identity(payload["task_id"], "task id")
        transaction_root = _handover_root(payload["transaction_root"])
        transaction_sha256 = payload["transaction_sha256"]
        disposition_sha256 = payload["disposition_sha256"]
        if not _is_digest(transaction_sha256):
            raise RunStateError("handover transaction digest is invalid")
        previous = state.task_phases.get(task_id)
        active = state.handovers.get(task_id)
        if event_type == "handover-intent":
            if (
                disposition_sha256 is not None
                or previous
                not in {"completed", "handover-rolled-back", "handover-not-mutated"}
            ):
                raise RunStateError(
                    f"illegal task transition from {previous!r} to {event_type!r}"
                )
        elif (
            not _is_digest(disposition_sha256)
            or previous != "handover-intent"
            or active is None
            or active.phase != "handover-intent"
            or active.transaction_root != transaction_root
            or active.transaction_sha256 != transaction_sha256
        ):
            raise RunStateError("handover terminal changed transaction association")
        state.handovers[task_id] = RunHandover(
            task_id,
            event_type,
            transaction_root,
            transaction_sha256,
            disposition_sha256,
        )
        state.task_phases[task_id] = event_type
        return

    if event_type in {
        "plan-amendment-intent", "plan-amendment-accepted",
        "plan-amendment-abandoned",
    }:
        expected = {
            "revision", "old_plan_sha256", "old_inputs_digest",
            "new_plan_sha256", "new_inputs_digest", "old_profiles_sha256",
            "new_profiles_sha256",
        }
        if set(payload) != expected:
            raise RunStateError("amendment payload has unknown or missing fields")
        revision = payload["revision"]
        digests = (
            payload["old_plan_sha256"], payload["old_inputs_digest"],
            payload["new_plan_sha256"], payload["new_inputs_digest"],
            payload["old_profiles_sha256"], payload["new_profiles_sha256"],
        )
        if not _is_int(revision, minimum=2) or any(not _is_digest(item) for item in digests):
            raise RunStateError("amendment binding is invalid")
        current = state.amendments.get(revision)
        if event_type == "plan-amendment-intent":
            if current is not None:
                raise RunStateError("amendment revision already exists")
        elif (
            current is None
            or current.phase != "plan-amendment-intent"
            or current.binding != digests
        ):
            raise RunStateError("amendment terminal lacks its exact durable intent")
        if event_type == "plan-amendment-abandoned":
            del state.amendments[revision]
            return
        state.amendments[revision] = RunAmendment(
            revision,
            event_type,
            *digests,
        )
        return

    if event_type in _SEAT_EVENTS:
        expected = {"task_id", "seat_id", "attempt", "round"}
        with_evidence = expected | {"evidence_sha256"}
        if frozenset(payload) not in {frozenset(expected), frozenset(with_evidence)}:
            raise RunStateError("seat event payload has unknown or missing fields")
        task_id, seat_id = _identity(payload["task_id"], "task id"), _identity(payload["seat_id"], "seat id")
        attempt, round = payload["attempt"], payload["round"]
        if not _is_int(attempt, minimum=1) or not _is_int(round, minimum=1):
            raise RunStateError("seat identity is invalid")
        key = (task_id, seat_id, attempt, round)
        previous = state.seat_phases.get(key)
        if event_type == "dispatch-intent":
            if previous is not None:
                raise RunStateError("illegal duplicate or regressive dispatch-intent")
            earlier = [(a, r, phase) for (task, seat, a, r), phase in state.seat_phases.items()
                       if task == task_id and seat == seat_id]
            if attempt > 1 and key not in state.accepted_retries:
                prior = state.seat_phase(task_id, seat_id, attempt - 1, round)
                if prior != "dispatch-intent":
                    raise RunStateError("uncertain or settled attempt cannot retry without owner duplicate-spend acceptance")
            if round > 1 and not any(a == attempt and r < round and phase == "checkpoint-verified" for a, r, phase in earlier):
                raise RunStateError("later round requires a checkpoint-verified prior round")
        elif event_type == "uncertain-attempt":
            if previous not in {
                "dispatch-intent", "process-started", "provider-terminal", "artifacts-durable",
                "publication-intent", "checkpoint-published",
            }:
                raise RunStateError("illegal uncertain-attempt transition")
        elif _SEAT_NEXT.get(previous) != event_type:
            raise RunStateError(f"illegal seat transition from {previous!r} to {event_type!r}")
        state.seat_phases[key] = event_type
        evidence = payload.get("evidence_sha256")
        if evidence is not None:
            if event_type not in {
                "dispatch-intent", "provider-terminal", "checkpoint-published", "checkpoint-verified",
            } or not _is_digest(evidence):
                raise RunStateError("seat evidence digest is invalid")
            state.seat_evidence[(*key, event_type)] = evidence
        return

    if event_type == "uncertainty-resolved":
        expected = {"task_id", "seat_id", "attempt", "round", "disposition"}
        if set(payload) != expected:
            raise RunStateError("uncertainty resolution payload has unknown or missing fields")
        task_id = _identity(payload["task_id"], "task id")
        seat_id = _identity(payload["seat_id"], "seat id")
        attempt, round = payload["attempt"], payload["round"]
        if (
            not _is_int(attempt, minimum=1) or not _is_int(round, minimum=1)
            or payload["disposition"] != "exclude"
        ):
            raise RunStateError("uncertainty resolution is invalid")
        key = (task_id, seat_id, attempt, round)
        if state.seat_phases.get(key) != "uncertain-attempt":
            raise RunStateError("uncertainty resolution requires an uncertain attempt")
        if key in state.uncertainty_resolutions:
            raise RunStateError("uncertain attempt is already resolved")
        state.uncertainty_resolutions[key] = "exclude"
        return

    if event_type == "duplicate-spend-accepted":
        expected = {"task_id", "seat_id", "prior_attempt", "next_attempt", "round"}
        if set(payload) != expected:
            raise RunStateError("duplicate-spend payload has unknown or missing fields")
        task_id, seat_id = _identity(payload["task_id"], "task id"), _identity(payload["seat_id"], "seat id")
        prior, next_attempt, round = payload["prior_attempt"], payload["next_attempt"], payload["round"]
        if not all(_is_int(value, minimum=1) for value in (prior, next_attempt, round)) or next_attempt != prior + 1:
            raise RunStateError("duplicate-spend acceptance must authorize exactly the next positive attempt")
        if state.seat_phase(task_id, seat_id, prior, round) != "uncertain-attempt":
            raise RunStateError("duplicate-spend acceptance requires an uncertain prior attempt")
        key = (task_id, seat_id, next_attempt, round)
        if key in state.accepted_retries or key in state.seat_phases:
            raise RunStateError("duplicate-spend acceptance is already consumed")
        state.accepted_retries.add(key)
        return

    if event_type in {"blocked-memory", "memory-recovered"}:
        fields = set(payload)
        legacy = {"task_id"}
        owned = {"task_id", "seat_id", "attempt", "round"}
        if fields != legacy and fields != owned:
            raise RunStateError("memory recovery payload has unknown or missing fields")
        task_id = _identity(payload["task_id"], "task id")
        cycle: tuple[str, int, int] | None = None
        if fields == owned:
            seat_id = _identity(payload["seat_id"], "seat id")
            attempt, round = payload["attempt"], payload["round"]
            if not _is_int(attempt, minimum=1) or not _is_int(round, minimum=1):
                raise RunStateError("memory block owner identity is invalid")
            cycle = (seat_id, attempt, round)
        previous = state.task_phases.get(task_id)
        if event_type == "blocked-memory":
            if previous not in {None, "memory-recovered"}:
                raise RunStateError(
                    f"illegal task transition from {previous!r} to {event_type!r}"
                )
            state.task_phases[task_id] = event_type
            if cycle is None:
                state.memory_block_owners.pop(task_id, None)
            else:
                state.memory_block_owners[task_id] = cycle
            return
        if previous != "blocked-memory":
            raise RunStateError(
                f"illegal task transition from {previous!r} to {event_type!r}"
            )
        active = state.memory_block_owners.get(task_id)
        if active != cycle:
            raise RunStateError("memory block owner does not match the recovery cycle")
        state.task_phases[task_id] = event_type
        state.memory_block_owners.pop(task_id, None)
        return

    expected = {"task_id"}
    if set(payload) != expected:
        raise RunStateError("task control event payload has unknown or missing fields")
    task_id = _identity(payload["task_id"], "task id")
    previous = state.task_phases.get(task_id)
    legal = {
        "reconciliation-pending": {None, "memory-recovered", "action-complete", "amendment-accepted"}, "reconciliation-verifying": {"reconciliation-pending"},
        "completed": {"reconciliation-verifying"}, "blocked-action": {None},
        "action-complete": {"blocked-action"},
        "plan-amended": {None}, "amendment-accepted": {"plan-amended"},
    }
    if event_type not in legal or previous not in legal[event_type]:
        raise RunStateError(f"illegal task transition from {previous!r} to {event_type!r}")
    state.task_phases[task_id] = event_type


def _copy_state(state: RunState) -> RunState:
    return RunState(state.inputs_digest, state.seq, state.head_checksum, dict(state.seat_phases),
                    dict(state.task_phases), dict(state.memory_block_owners),
                    set(state.accepted_retries), dict(state.seat_evidence),
                    dict(state.uncertainty_resolutions), dict(state.amendments),
                    dict(state.handovers), dict(state.branch_handovers),
                    {key: dict(value) for key, value in state.branch_records.items()},
                    dict(state.branch_blocked))


def _branch_target(intent: BranchHandoverIntentV2, inputs: RunInputs) -> dict[str, object]:
    if inputs.targets is None or intent.target_id not in inputs.targets:
        raise RunStateError("branch candidate target is absent from run inputs")
    binding = inputs.targets[intent.target_id]
    return {
        "run_id": intent.run_id, "task_id": intent.task_id,
        "target_id": intent.target_id, "repository": intent.repository,
        "branch_ref": intent.branch_ref, "base_oid": intent.base_oid,
        "baseline_sha256": binding.baseline_sha256,
    }


def _branch_wrapper(store: ArtifactStore, ref: ArtifactRef, *, schema: str,
                    fields: set[str], target: dict[str, object]) -> dict[str, object]:
    data = store.read_bytes(ref)
    try:
        wrapper = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunStateError("branch handover evidence wrapper is invalid") from error
    if (not isinstance(wrapper, dict) or set(wrapper) != fields
            or wrapper["schema_version"] != schema
            or wrapper["target"] != target or canonical_json(wrapper) != data):
        raise RunStateError("branch handover evidence wrapper differs from intent")
    return wrapper


def _branch_candidate_digest(store: ArtifactStore, intent: BranchHandoverIntentV2,
                             inputs: RunInputs) -> str:
    """Bind Task 6's wrapper to its stored v1 manifest digest, without reissuing its seal."""
    target = _branch_target(intent, inputs)
    binding = inputs.targets[intent.target_id]
    wrapper = _branch_wrapper(
        store, intent.candidate_ref, schema="fanout-target-candidate-v1",
        fields={"schema_version", "target", "candidate", "controller_evidence"},
        target=target,
    )
    candidate_ref = _branch_artifact_ref(wrapper["candidate"])
    manifest = store.read_bytes(candidate_ref)
    from .errors import CandidateValidationError
    from .verification import CandidateBundle
    try:
        candidate = CandidateBundle.from_manifest(manifest)
    except CandidateValidationError as error:
        raise RunStateError("branch candidate manifest is invalid") from error
    if candidate.baseline_digest != binding.baseline_sha256 or candidate.digest != candidate_ref.digest:
        raise RunStateError("branch candidate manifest differs from target baseline")
    return candidate_ref.digest


def _branch_verification_digest(store: ArtifactStore, intent: BranchHandoverIntentV2,
                                inputs: RunInputs, candidate_sha256: str) -> None:
    wrapper = _branch_wrapper(
        store, intent.verification_ref, schema="fanout-target-verification-v1",
        fields={"schema_version", "target", "candidate", "candidate_evidence", "controller_evidence"},
        target=_branch_target(intent, inputs),
    )
    result = _branch_artifact_ref(wrapper["candidate"])
    if (_branch_artifact_ref(wrapper["candidate_evidence"]) != intent.candidate_ref
            or result.digest != candidate_sha256):
        raise RunStateError("branch verification candidate differs from intent")
    from .verification import candidate_result_path
    parts = result.path.split("/")
    if (len(parts) != 4 or not _is_digest(parts[-2])
            or result.path != candidate_result_path(intent.task_id, candidate_sha256, parts[-2])):
        raise RunStateError("branch verification result lacks exact receipt path")
    store.read_bytes(result)


def _verify_branch_artifacts(root: Path, state: RunState, inputs: RunInputs) -> None:
    if not state.branch_handovers:
        return
    try:
        with ArtifactStore.open_existing(root / "artifacts") as store:
            for intent, terminal in state.branch_handovers.values():
                candidate_sha256 = _branch_candidate_digest(store, intent, inputs)
                _branch_verification_digest(store, intent, inputs, candidate_sha256)
                if terminal is not None:
                    if terminal.candidate_sha256 != candidate_sha256:
                        raise RunStateError("branch terminal candidate digest differs from intent")
                    store.read_bytes(terminal.evidence)
    except (ArtifactError, OSError, ValueError) as error:
        raise RunStateError("branch handover artifact evidence is missing or changed") from error


def _checksum(record: Mapping[str, object]) -> str:
    unsigned = {key: value for key, value in record.items() if key not in {"checksum", "mac"}}
    return _sha256(canonical_json(unsigned))


def _timestamp() -> str:
    return _datetime.datetime.now(tz=_datetime.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RunStateError("event timestamp must be an RFC3339 UTC Z string")
    try:
        _datetime.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise RunStateError("event timestamp is invalid") from error
    return value


def _parse_events(data: bytes, limits: RunLimits, mac_key: bytes) -> tuple[list[dict[str, object]], int, int]:
    if len(data) > limits.max_journal_bytes:
        raise RunStateError("journal byte limit exceeded during recovery")
    full, tail = data, b""
    if data and not data.endswith(b"\n"):
        cut = data.rfind(b"\n") + 1
        full, tail = data[:cut], data[cut:]
    records: list[dict[str, object]] = []
    position = 0
    for line in full.splitlines(keepends=True):
        position += len(line)
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RunStateError("journal has an invalid interior record") from error
        if not isinstance(record, dict) or canonical_json(record) != line:
            raise RunStateError("journal record is not canonical JSON")
        _validate_record(record, mac_key)
        records.append(record)
        if len(records) > limits.max_events:
            raise RunStateError("journal event limit exceeded during recovery")
    return records, position, len(tail)


def _validate_record(record: Mapping[str, object], mac_key: bytes) -> None:
    allowed = {"schema_version", "version", "run_id", "seq", "previous_checksum", "timestamp", "type", "payload", "payload_sha256", "authority_revision", "checksum", "mac"}
    if set(record) != allowed or record.get("schema_version") != _EVENT_SCHEMA or record.get("version") != 1:
        raise RunStateError("journal event has unknown or missing fields")
    _identity(record["run_id"], "event run id")
    if (not _is_int(record["seq"], minimum=1) or not _is_digest(record["previous_checksum"])
            or not _is_int(record["authority_revision"], minimum=1)):
        raise RunStateError("journal event sequence or predecessor is invalid")
    _validate_timestamp(record["timestamp"])
    if record["type"] not in _EVENT_NAME or not isinstance(record["payload"], dict):
        raise RunStateError("journal event type or payload is invalid")
    if not _is_digest(record["payload_sha256"]) or not _is_digest(record["checksum"]) or not _is_digest(record["mac"]):
        raise RunStateError("journal event digest is invalid")
    if not hmac.compare_digest(record["payload_sha256"], _sha256(canonical_json(record["payload"]))):
        raise RunStateError("journal payload checksum mismatch")
    if not hmac.compare_digest(record["checksum"], _checksum(record)):
        raise RunStateError("journal event checksum mismatch")
    unsigned = {key: value for key, value in record.items() if key != "mac"}
    if not hmac.compare_digest(record["mac"], _mac(mac_key, b"event", unsigned)):
        raise RunStateError("journal event MAC mismatch")


def _replay(records: list[dict[str, object]], inputs_digest: str, run_id: str,
            *, inputs: RunInputs | None = None) -> RunState:
    state = RunState(inputs_digest)
    authority_revision = 1
    for record in records:
        if record["run_id"] != run_id:
            raise RunStateError("journal event belongs to another run")
        if record["seq"] != state.seq + 1 or not hmac.compare_digest(record["previous_checksum"], state.head_checksum):
            raise RunStateError("journal sequence or previous checksum is reordered or forged")
        if record["authority_revision"] <= authority_revision:
            raise RunStateError("journal authority revisions are not strictly monotonic")
        _apply_event(state, record["type"], record["payload"], inputs=inputs)
        state.seq = record["seq"]
        state.head_checksum = record["checksum"]
        authority_revision = record["authority_revision"]
    return state


def _verify_snapshot(snapshot: Mapping[str, object], journal: bytes, records: list[dict[str, object],],
                     inputs_digest: str, run_id: str, mac_key: bytes, store_id: str, anchor_key: str,
                     journal_identity: tuple[int, int], current_authority_revision: int,
                     *, inputs: RunInputs | None = None) -> None:
    allowed = {"schema_version", "journal_offset", "seq", "head_checksum", "anchor_store_id",
               "anchor_key", "authority_revision", "journal_device", "journal_inode", "state", "mac"}
    if set(snapshot) != allowed or snapshot.get("schema_version") not in {_SNAPSHOT_SCHEMA, _BRANCH_SNAPSHOT_SCHEMA}:
        raise RunStateError("snapshot has unknown or missing fields")
    if not _is_digest(snapshot.get("mac")):
        raise RunStateError("snapshot MAC is invalid")
    unsigned = {key: value for key, value in snapshot.items() if key != "mac"}
    if not hmac.compare_digest(snapshot["mac"], _mac(mac_key, b"snapshot", unsigned)):
        raise RunStateError("snapshot MAC mismatch")
    offset = snapshot["journal_offset"]
    if (not _is_int(offset) or offset > len(journal) or not _is_int(snapshot["seq"])
            or not _is_digest(snapshot["head_checksum"])
            or snapshot["anchor_store_id"] != store_id or snapshot["anchor_key"] != anchor_key
            or (snapshot["journal_device"], snapshot["journal_inode"]) != journal_identity
            or not _is_int(snapshot["authority_revision"], minimum=1)
            or snapshot["authority_revision"] > current_authority_revision):
        raise RunStateError("snapshot journal offset is invalid")
    snapshot_state = _normalized_snapshot_state(snapshot["state"], snapshot["schema_version"])
    prefix: list[dict[str, object]] = []
    consumed = 0
    if offset == 0:
        reconstructed = RunState(inputs_digest)
        if snapshot["seq"] != 0 or snapshot["head_checksum"] != _ZERO_CHECKSUM:
            raise RunStateError("empty snapshot has non-empty head")
        if snapshot_state != reconstructed.to_dict():
            raise RunStateError("snapshot state has invalid types or contents")
        if snapshot["authority_revision"] < 1:
            raise RunStateError("empty snapshot has an invalid authority revision")
        return
    for record in records:
        size = len(canonical_json(record))
        if consumed + size > offset:
            raise RunStateError("snapshot points inside an event")
        prefix.append(record)
        consumed += size
        if consumed == offset:
            break
    if consumed != offset:
        raise RunStateError("snapshot offset is not a journal record boundary")
    reconstructed = _replay(prefix, inputs_digest, run_id, inputs=inputs)
    if snapshot["seq"] != reconstructed.seq or snapshot["head_checksum"] != reconstructed.head_checksum or snapshot_state != reconstructed.to_dict():
        raise RunStateError("snapshot does not match its journal prefix")
    if records[snapshot["seq"] - 1]["authority_revision"] > snapshot["authority_revision"]:
        raise RunStateError("snapshot authority revision predates its journal head")


def _normalized_snapshot_state(value: object, schema: str) -> dict[str, object]:
    legacy_fields = {
        "inputs_digest", "seq", "head_checksum", "seat_phases", "task_phases",
        "accepted_retries",
    }
    evidence_fields = {"seat_evidence", "uncertainty_resolutions"}
    optional_fields = {"memory_block_owners", "amendments", "handovers"} | evidence_fields
    fields = set(value) if isinstance(value, dict) else set()
    if (
        not isinstance(value, dict)
        or not legacy_fields.issubset(fields)
        or bool(fields & evidence_fields) != evidence_fields.issubset(fields)
        or fields - legacy_fields - optional_fields - ({"branch_handovers", "branch_records", "branch_blocked"} if schema == _BRANCH_SNAPSHOT_SCHEMA else set())
        or (schema == _BRANCH_SNAPSHOT_SCHEMA and "branch_handovers" not in fields)
    ):
        raise RunStateError("snapshot state has unknown or missing fields")
    if not _is_digest(value["inputs_digest"]) or not _is_int(value["seq"]) or not _is_digest(value["head_checksum"]):
        raise RunStateError("snapshot state has invalid scalar types")
    for key in ("seat_phases", "task_phases", "accepted_retries"):
        if not isinstance(value[key], list):
            raise RunStateError("snapshot state has invalid collection types")
    owners = value.get("memory_block_owners", [])
    evidence = value.get("seat_evidence", [])
    resolutions = value.get("uncertainty_resolutions", [])
    amendments = value.get("amendments", [])
    handovers = value.get("handovers", [])
    branch_handovers = value.get("branch_handovers", [])
    branch_records = value.get("branch_records", [])
    branch_blocked = value.get("branch_blocked", [])
    if not all(
        isinstance(item, list)
        for item in (owners, evidence, resolutions, amendments, handovers, branch_handovers, branch_records, branch_blocked)
    ):
        raise RunStateError("snapshot state has invalid collection types")
    for item in value["seat_phases"]:
        if not isinstance(item, dict) or set(item) != {"task_id", "seat_id", "attempt", "round", "phase"}:
            raise RunStateError("snapshot seat state is malformed")
        _identity(item["task_id"], "task id"); _identity(item["seat_id"], "seat id")
        if not _is_int(item["attempt"], minimum=1) or not _is_int(item["round"], minimum=1) or item["phase"] not in _SEAT_EVENTS:
            raise RunStateError("snapshot seat state has invalid values")
    for item in value["task_phases"]:
        if not isinstance(item, dict) or set(item) != {"task_id", "phase"}:
            raise RunStateError("snapshot task state is malformed")
        _identity(item["task_id"], "task id")
        if item["phase"] not in _EVENT_NAME:
            raise RunStateError("snapshot task phase is invalid")
    for item in owners:
        if not isinstance(item, dict) or set(item) != {"task_id", "seat_id", "attempt", "round"}:
            raise RunStateError("snapshot memory block owner is malformed")
        _identity(item["task_id"], "task id"); _identity(item["seat_id"], "seat id")
        if not _is_int(item["attempt"], minimum=1) or not _is_int(item["round"], minimum=1):
            raise RunStateError("snapshot memory block owner has invalid values")
    for item in value["accepted_retries"]:
        if not isinstance(item, dict) or set(item) != {"task_id", "seat_id", "attempt", "round"}:
            raise RunStateError("snapshot retry state is malformed")
        _identity(item["task_id"], "task id"); _identity(item["seat_id"], "seat id")
        if not _is_int(item["attempt"], minimum=1) or not _is_int(item["round"], minimum=1):
            raise RunStateError("snapshot retry state has invalid values")
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {
            "task_id", "seat_id", "attempt", "round", "event_type", "sha256",
        }:
            raise RunStateError("snapshot seat evidence is malformed")
        _identity(item["task_id"], "task id"); _identity(item["seat_id"], "seat id")
        if (
            not _is_int(item["attempt"], minimum=1)
            or not _is_int(item["round"], minimum=1)
            or item["event_type"] not in {
                "dispatch-intent", "provider-terminal", "checkpoint-published", "checkpoint-verified",
            }
            or not _is_digest(item["sha256"])
        ):
            raise RunStateError("snapshot seat evidence has invalid values")
    for item in resolutions:
        if not isinstance(item, dict) or set(item) != {
            "task_id", "seat_id", "attempt", "round", "disposition",
        }:
            raise RunStateError("snapshot uncertainty resolution is malformed")
        _identity(item["task_id"], "task id"); _identity(item["seat_id"], "seat id")
        if (
            not _is_int(item["attempt"], minimum=1)
            or not _is_int(item["round"], minimum=1)
            or item["disposition"] != "exclude"
        ):
            raise RunStateError("snapshot uncertainty resolution has invalid values")
    seen_revisions: set[int] = set()
    for item in amendments:
        expected = {
            "revision", "phase", "old_plan_sha256", "old_inputs_digest",
            "new_plan_sha256", "new_inputs_digest", "old_profiles_sha256",
            "new_profiles_sha256",
        }
        if not isinstance(item, dict) or set(item) != expected:
            raise RunStateError("snapshot amendment state is malformed")
        revision = item["revision"]
        if (
            not _is_int(revision, minimum=2)
            or revision in seen_revisions
            or item["phase"] not in {
                "plan-amendment-intent", "plan-amendment-accepted",
            }
            or any(
                not _is_digest(item[name])
                for name in (
                    "old_plan_sha256", "old_inputs_digest",
                    "new_plan_sha256", "new_inputs_digest",
                    "old_profiles_sha256", "new_profiles_sha256",
                )
            )
        ):
            raise RunStateError("snapshot amendment state has invalid values")
        seen_revisions.add(revision)
    seen_handover_tasks: set[str] = set()
    for item in handovers:
        expected = {
            "disposition_sha256", "phase", "task_id", "transaction_root",
            "transaction_sha256",
        }
        if not isinstance(item, dict) or set(item) != expected:
            raise RunStateError("snapshot handover state is malformed")
        task_id = _identity(item["task_id"], "handover task id")
        if task_id in seen_handover_tasks:
            raise RunStateError("snapshot handover task is duplicate")
        RunHandover(
            task_id,
            item["phase"],
            _handover_root(item["transaction_root"]),
            item["transaction_sha256"],
            item["disposition_sha256"],
        )
        seen_handover_tasks.add(task_id)
    seen_branch_tasks: set[str] = set()
    for item in branch_handovers:
        if not isinstance(item, dict) or set(item) != {"task_id", "intent", "terminal"}:
            raise RunStateError("snapshot branch handover state is malformed")
        task_id = _identity(item["task_id"], "branch task id")
        intent = BranchHandoverIntentV2.from_dict(item["intent"])
        if task_id in seen_branch_tasks or task_id != intent.task_id:
            raise RunStateError("snapshot branch handover task differs or is duplicate")
        if item["terminal"] is not None:
            terminal = HandoverTerminalV2.from_dict(item["terminal"])
            if terminal.intent_sha256 != intent.sha256:
                raise RunStateError("snapshot branch terminal differs from intent")
        seen_branch_tasks.add(task_id)
    normalized = dict(value)
    normalized["memory_block_owners"] = owners
    normalized["seat_evidence"] = evidence
    normalized["uncertainty_resolutions"] = resolutions
    normalized["amendments"] = amendments
    normalized["handovers"] = handovers
    normalized["branch_handovers"] = branch_handovers
    normalized["branch_records"] = branch_records
    normalized["branch_blocked"] = branch_blocked
    return normalized


def _require_anchor_store(store: _AnchorAuthority | None, *, allow_test: bool = False) -> str:
    if store is None:
        raise RunStateError("an external monotonic anchor authority is required")
    if type(store) in {LocalAnchorAuthority, RemoteAnchorAuthority}:
        return store.identity
    if allow_test and type(store) is _TestAnchorAuthority:
        return store.identity
    raise RunStateError("a trusted remote or local anchor authority client is required")


def _authority_record(store_id: str, anchor_key: str, revision: int, run_id: str,
                      journal: tuple[int, int], seq: int, head: str, offset: int,
                      pending: Mapping[str, object] | None, key: bytes) -> dict[str, object]:
    pending_value = dict(pending) if pending is not None else None
    record: dict[str, object] = {
        "schema_version": _AUTHORITY_SCHEMA, "anchor_store_id": store_id,
        "anchor_key": anchor_key, "revision": revision, "run_id": run_id,
        "journal_device": journal[0], "journal_inode": journal[1], "seq": seq,
        "head": head, "offset": offset,
        "pending": pending_value,
        "pending_offset": offset + len(canonical_json(pending_value)) if pending_value is not None else None,
    }
    record["mac"] = _mac(key, b"authority", record)
    return record


def _authority_create(store: _AnchorAuthority, store_id: str, anchor_key: str,
                      record: Mapping[str, object]) -> AnchorRevision:
    encoded = _bounded_authority_bytes(record)
    try:
        result = store.create(anchor_key, encoded)
    except Exception as error:
        raise RunStateError("anchor authority create failed") from error
    return _validate_authority_result(store, store_id, anchor_key, result, 1, encoded)


def _authority_cas(store: _AnchorAuthority, store_id: str, anchor_key: str, expected_revision: int,
                   record: Mapping[str, object]) -> AnchorRevision:
    encoded = _bounded_authority_bytes(record)
    try:
        result = store.compare_and_set(anchor_key, expected_revision, encoded)
    except Exception as error:
        raise RunStateError("anchor authority compare-and-set failed") from error
    return _validate_authority_result(store, store_id, anchor_key, result, expected_revision + 1, encoded)


def _authority_read(store: _AnchorAuthority, store_id: str, anchor_key: str, key: bytes, run_id: str,
                    journal: tuple[int, int]) -> tuple[AnchorRevision, dict[str, object]]:
    try:
        result = store.read(anchor_key)
    except Exception as error:
        raise RunStateError("anchor authority read failed") from error
    result = _validate_authority_result(store, store_id, anchor_key, result, None, None)
    record = _parse_authority_value(result.value, key, store_id, anchor_key, run_id, journal, result.revision)
    return result, record


def _validate_authority_result(store: _AnchorAuthority, store_id: str, anchor_key: str,
                               result: object, expected_revision: int | None,
                               expected_value: bytes | None) -> AnchorRevision:
    if type(store) not in {LocalAnchorAuthority, RemoteAnchorAuthority, _TestAnchorAuthority} or store.identity != store_id:
        raise RunStateError("anchor authority identity changed")
    if (not isinstance(result, AnchorRevision) or not _is_int(result.revision, minimum=1)
            or not isinstance(result.value, bytes) or len(result.value) > _MAX_AUTHORITY_BYTES):
        raise RunStateError("anchor authority returned a malformed revision")
    if expected_revision is not None and result.revision != expected_revision:
        raise RunStateError("anchor authority returned a stale or skipped revision")
    if expected_value is not None and result.value != expected_value:
        raise RunStateError("anchor authority did not retain the exact canonical value")
    _identity(anchor_key, "anchor key")
    return result


def _bounded_authority_bytes(record: Mapping[str, object]) -> bytes:
    encoded = canonical_json(record)
    if len(encoded) > _MAX_AUTHORITY_BYTES:
        raise RunStateError("anchor authority value exceeds its bounded size")
    return encoded


def _parse_authority_value(data: bytes, key: bytes, store_id: str, anchor_key: str, run_id: str,
                           journal: tuple[int, int], revision: int) -> dict[str, object]:
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunStateError("anchor authority value is malformed") from error
    allowed = {"schema_version", "anchor_store_id", "anchor_key", "revision", "run_id",
               "journal_device", "journal_inode", "seq", "head", "offset", "pending",
               "pending_offset", "mac"}
    if (not isinstance(record, dict) or set(record) != allowed
            or record.get("schema_version") != _AUTHORITY_SCHEMA or canonical_json(record) != data):
        raise RunStateError("anchor authority value is noncanonical or has an invalid schema")
    unsigned = {name: value for name, value in record.items() if name != "mac"}
    if (record.get("anchor_store_id") != store_id or record.get("anchor_key") != anchor_key
            or record.get("revision") != revision or record.get("run_id") != run_id
            or (record.get("journal_device"), record.get("journal_inode")) != journal
            or not _is_int(record.get("seq")) or not _is_int(record.get("offset"))
            or not _is_digest(record.get("head")) or not _is_digest(record.get("mac"))
            or not hmac.compare_digest(record["mac"], _mac(key, b"authority", unsigned))):
        raise RunStateError("anchor authority value does not authenticate this run and revision")
    pending = record["pending"]
    if pending is not None:
        if not isinstance(pending, dict):
            raise RunStateError("anchor authority pending event is malformed")
        _validate_record(pending, key)
        expected_offset = record["offset"] + len(canonical_json(pending))
        if (pending["run_id"] != run_id or pending["seq"] != record["seq"] + 1
                or pending["previous_checksum"] != record["head"]
                or pending["authority_revision"] != revision + 1
                or record["pending_offset"] != expected_offset
                or expected_offset > 32 * 1024 * 1024):
            raise RunStateError("anchor authority pending event does not extend its committed base")
    elif record["pending_offset"] is not None:
        raise RunStateError("committed authority value may not carry a pending offset")
    return record


def _open_private_directory(path: Path, *, create: bool, exclusive: bool) -> int:
    parts = path.parts[1:] if path.is_absolute() else path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise RunStateError("run root path is unsafe")
    fd = os.open(os.sep if path.is_absolute() else ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            made = False
            try:
                if create:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    made = True
                elif final:
                    pass
            except FileExistsError:
                if final and exclusive:
                    raise RunStateError("run root already exists")
            if made:
                os.fsync(fd)
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.ENOENT}:
                    raise RunStateError("run root is not a contained directory") from error
                raise
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RunStateError("run root must be owner-private")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _assert_private_regular(fd: int, label: str) -> None:
    info = os.fstat(fd)
    if not _private_stat(info):
        raise RunStateError(f"{label} is not a private regular file")


def _private_stat(info: os.stat_result) -> bool:
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600)


def _write_new_private(root_fd: int, name: str, data: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_event_journal(root_fd: int, expected: tuple[int, int] | None = None,
                        *, readonly: bool = False) -> int:
    try:
        flags = os.O_RDONLY if readonly else os.O_RDWR | os.O_APPEND
        fd = os.open("events.jsonl", flags | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError as error:
        raise RunStateError("event journal is missing or unsafe") from error
    try:
        _assert_private_regular(fd, "event journal")
        identity = _lock_identity(fd)
        if expected is not None and identity != expected:
            raise RunStateError("event journal does not match authenticated owner metadata")
        _assert_current_entry(root_fd, "events.jsonl", fd, identity, "event journal")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _assert_current_entry(root_fd: int, name: str, fd: int, expected: tuple[int, int], label: str) -> None:
    try:
        descriptor = os.fstat(fd)
        entry = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except OSError as error:
        raise RunStateError(f"{label} entry is missing or unsafe") from error
    if (not _private_stat(descriptor) or not _private_stat(entry)
            or (descriptor.st_dev, descriptor.st_ino) != expected
            or (entry.st_dev, entry.st_ino) != expected):
        raise RunStateError(f"{label} entry or descriptor was replaced or mutated")


def _read_private_fd(fd: int, limit: int, label: str) -> bytes:
    _assert_private_regular(fd, label)
    before = os.fstat(fd)
    if before.st_size > limit:
        raise RunStateError(f"{label} exceeds its bounded read limit")
    data = bytearray()
    while len(data) < before.st_size:
        chunk = os.pread(fd, min(64 * 1024, before.st_size - len(data)), len(data))
        if not chunk:
            break
        data.extend(chunk)
    after = os.fstat(fd)
    if (len(data) != before.st_size or after.st_size != before.st_size
            or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)):
        raise RunStateError(f"{label} changed while reading")
    return bytes(data)


def _read_private_file(root_fd: int, name: str, limit: int) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError as error:
        raise RunStateError(f"required run-state file is missing or unsafe: {name}") from error
    try:
        return _read_private_fd(fd, limit, name)
    finally:
        os.close(fd)


def _read_json_file(root_fd: int, name: str, limit: int) -> dict[str, object]:
    data = _read_private_file(root_fd, name, limit)
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunStateError(f"run-state JSON file is malformed: {name}") from error
    if not isinstance(parsed, dict) or canonical_json(parsed) != data:
        raise RunStateError(f"run-state JSON file is noncanonical: {name}")
    return parsed


def _read_optional_json_file(root_fd: int, name: str, limit: int) -> dict[str, object] | None:
    try:
        return _read_json_file(root_fd, name, limit)
    except RunStateError as error:
        try:
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        raise error


def _validate_owner(owner: Mapping[str, object], capability: OwnerCapability, inputs_digest: str) -> bool:
    allowed = {"schema_version", "capability_salt", "capability_verifier", "lock_device", "lock_inode",
               "journal_device", "journal_inode", "inputs_digest", "anchor_store_id", "anchor_key",
               "anchor_initial_revision", "mac"}
    if set(owner) != allowed or owner.get("schema_version") != _OWNER_SCHEMA:
        return False
    salt, verifier = owner.get("capability_salt"), owner.get("capability_verifier")
    if (not isinstance(salt, str) or len(salt) != 32 or any(char not in "0123456789abcdef" for char in salt)
            or not _is_digest(verifier) or not _is_int(owner.get("lock_device"), minimum=1)
            or not _is_int(owner.get("lock_inode"), minimum=1) or not _is_int(owner.get("journal_device"), minimum=1)
            or not _is_int(owner.get("journal_inode"), minimum=1) or owner.get("inputs_digest") != inputs_digest
            or not _is_int(owner.get("anchor_initial_revision"), minimum=1)
            or not _is_digest(owner.get("mac"))):
        return False
    try:
        _identity(owner.get("anchor_store_id"), "anchor store identity")
        _identity(owner.get("anchor_key"), "anchor key")
    except RunStateError:
        return False
    if not hmac.compare_digest(verifier, _owner_verifier(salt, capability)):
        return False
    unsigned = {key: value for key, value in owner.items() if key != "mac"}
    return hmac.compare_digest(owner["mac"], _mac(capability._mac_key(), b"owner", unsigned))


def _acquire_lock(root_fd: int, expected: tuple[int, int] | None = None,
                  *, readonly: bool = False) -> int:
    try:
        flags = (os.O_RDONLY if readonly else os.O_RDWR) | os.O_NOFOLLOW
        if expected is None:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(".controller.lock", flags, 0o600, dir_fd=root_fd)
    except OSError as error:
        raise RunLockError("cannot safely open controller lock") from error
    try:
        try:
            _assert_private_regular(fd, "controller lock")
        except RunStateError as error:
            raise RunLockError("controller lock entry is unsafe") from error
        identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
        if expected is not None and identity != expected:
            raise RunLockError("controller lock entry does not match owner metadata")
        if expected is None:
            os.fsync(root_fd)
        with _ACTIVE_LOCKS_GUARD:
            if identity in _ACTIVE_LOCKS:
                raise RunLockError("controller lock is already held in this process")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RunLockError("controller lock is held by another process") from error
            _ACTIVE_LOCKS.add(identity)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _release_lock(fd: int) -> None:
    try:
        identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
        with _ACTIVE_LOCKS_GUARD:
            _ACTIVE_LOCKS.discard(identity)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    sent = 0
    while sent < len(data):
        written = os.write(fd, data[sent:])
        if written <= 0:
            raise OSError("short write to durable run state")
        sent += written
