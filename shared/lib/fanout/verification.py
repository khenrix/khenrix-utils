"""Immutable repository candidate bundles and fresh-workspace verification."""
from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import stat
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Mapping, Sequence

from .artifacts import ArtifactExistsError, ArtifactRef, ArtifactStore, canonical_json
from .controller import (
    LifecycleController,
    assert_controller,
    controller_directory,
    read_evidence,
    write_evidence,
)
from .errors import ArtifactError, CandidateValidationError, CandidateVerificationError
from .plan import FanoutPlanV2, PlanCheckV1
from .process import ProcessCommand, ProcessStatus, run_command
from .repo import RepositoryBaseline, RepositoryEntry, SeatWorkspace, _git, create_seat_workspace
from .runstate import RunInputs
from .targets import TargetBinding


_CANDIDATE_SCHEMA = "fanout-candidate-v1"
_SHA256_SIZE = 64
_READ_CHUNK = 64 * 1024
_RECEIPT_ISSUER = object()
_ANSWER_RECEIPT_ISSUER = object()
_TARGET_EVIDENCE_SCHEMA = "fanout-target-evidence-v1"
_TARGET_EVIDENCE_MAX_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CandidateEntry:
    """One complete changed file or symlink carried directly by a candidate."""

    path: str
    kind: str
    mode: int
    data: bytes = field(repr=False)
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        path = _path(self.path, "candidate entry path")
        if self.kind not in {"file", "symlink", "directory"}:
            raise CandidateValidationError("candidate entry kind must be file, symlink, or directory")
        if isinstance(self.mode, bool) or not isinstance(self.mode, int) or not 0 <= self.mode <= 0o777:
            raise CandidateValidationError("candidate entry mode is invalid")
        if not isinstance(self.data, bytes):
            raise CandidateValidationError("candidate entry data must be bytes")
        if self.kind == "symlink":
            _link_target(path, self.data)
        elif self.kind == "directory" and self.data:
            raise CandidateValidationError("candidate directory entry data must be empty")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "digest", hashlib.sha256(self.data).hexdigest())

    def to_manifest(self) -> dict[str, object]:
        """Return the complete, JSON-safe representation hashed by the bundle."""
        return {
            "path": self.path,
            "kind": self.kind,
            "mode": self.mode,
            "size": len(self.data),
            "data_sha256": self.digest,
            "data_b64": base64.b64encode(self.data).decode("ascii"),
        }


@dataclass(frozen=True, slots=True)
class CandidateBundle:
    """A canonical full-byte delta from one immutable repository baseline."""

    baseline_digest: str
    entries: tuple[CandidateEntry, ...]
    deleted_paths: tuple[str, ...]
    source_candidate_digests: tuple[str, ...] = ()
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        _digest(self.baseline_digest, "candidate baseline digest")
        entries = tuple(self.entries)
        deleted_paths = tuple(self.deleted_paths)
        sources = tuple(self.source_candidate_digests)
        if any(not isinstance(entry, CandidateEntry) for entry in entries):
            raise CandidateValidationError("candidate entries must be CandidateEntry values")
        if any(not isinstance(path, str) for path in deleted_paths):
            raise CandidateValidationError("candidate deleted paths must be strings")
        deleted = tuple(_path(path, "candidate deleted path") for path in deleted_paths)
        if len({entry.path for entry in entries}) != len(entries):
            raise CandidateValidationError("candidate entry paths must be unique")
        if len(set(deleted)) != len(deleted):
            raise CandidateValidationError("candidate deleted paths must be unique")
        _no_entry_overlap(entries)
        if any(not isinstance(value, str) for value in sources):
            raise CandidateValidationError("source candidate digests must be strings")
        for value in sources:
            _digest(value, "source candidate digest")
        object.__setattr__(self, "entries", tuple(sorted(entries, key=lambda item: _path_bytes(item.path))))
        object.__setattr__(self, "deleted_paths", tuple(sorted(deleted, key=_path_bytes)))
        object.__setattr__(self, "source_candidate_digests", sources)
        object.__setattr__(self, "digest", hashlib.sha256(self.manifest_bytes).hexdigest())

    def to_manifest(self) -> dict[str, object]:
        """Return a new plain mapping, never a mutable association used by verification."""
        return {
            "schema_version": _CANDIDATE_SCHEMA,
            "baseline_sha256": self.baseline_digest,
            "entries": [entry.to_manifest() for entry in self.entries],
            "deleted_paths": list(self.deleted_paths),
            "source_candidate_digests": list(self.source_candidate_digests),
        }

    @property
    def manifest_bytes(self) -> bytes:
        """The one canonical byte spelling whose digest identifies this candidate."""
        return canonical_json(self.to_manifest())

    @classmethod
    def from_manifest(cls, value: bytes | bytearray | memoryview | Mapping[str, object]) -> "CandidateBundle":
        """Parse a complete candidate manifest while rechecking every byte binding."""
        if isinstance(value, (bytes, bytearray, memoryview)):
            encoded = bytes(value)
            try:
                decoded = json.loads(encoded.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CandidateValidationError("candidate manifest is not valid UTF-8 JSON") from error
        elif isinstance(value, Mapping):
            decoded = dict(value)
        else:
            raise CandidateValidationError("candidate manifest must be bytes or an object")
        if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
            raise CandidateValidationError("candidate manifest must be an object")
        required = {
            "schema_version", "baseline_sha256", "entries", "deleted_paths", "source_candidate_digests",
        }
        if set(decoded) != required:
            raise CandidateValidationError("candidate manifest fields are invalid")
        if decoded["schema_version"] != _CANDIDATE_SCHEMA:
            raise CandidateValidationError("candidate manifest schema is invalid")
        entries_value = decoded["entries"]
        deleted_value = decoded["deleted_paths"]
        sources_value = decoded["source_candidate_digests"]
        if not isinstance(entries_value, list) or not isinstance(deleted_value, list) or not isinstance(sources_value, list):
            raise CandidateValidationError("candidate manifest collections are invalid")
        entries: list[CandidateEntry] = []
        for index, item in enumerate(entries_value):
            if not isinstance(item, dict) or any(not isinstance(key, str) for key in item):
                raise CandidateValidationError("candidate manifest entry is invalid")
            expected = {"path", "kind", "mode", "size", "data_sha256", "data_b64"}
            if set(item) != expected:
                raise CandidateValidationError("candidate manifest entry fields are invalid")
            if isinstance(item["size"], bool) or not isinstance(item["size"], int) or item["size"] < 0:
                raise CandidateValidationError("candidate manifest entry size is invalid")
            if not isinstance(item["data_b64"], str):
                raise CandidateValidationError("candidate manifest entry bytes are invalid")
            try:
                data = base64.b64decode(item["data_b64"].encode("ascii"), validate=True)
            except (UnicodeEncodeError, ValueError) as error:
                raise CandidateValidationError("candidate manifest entry bytes are invalid") from error
            if len(data) != item["size"]:
                raise CandidateValidationError("candidate manifest entry size does not match bytes")
            entry = CandidateEntry(item["path"], item["kind"], item["mode"], data)
            if item["data_sha256"] != entry.digest:
                raise CandidateValidationError(f"candidate manifest entry {index} digest does not match bytes")
            entries.append(entry)
        candidate = cls(
            decoded["baseline_sha256"],
            tuple(entries),
            tuple(deleted_value),
            tuple(sources_value),
        )
        if isinstance(value, (bytes, bytearray, memoryview)) and candidate.manifest_bytes != encoded:
            raise CandidateValidationError("candidate manifest is not canonical JSON")
        return candidate


@dataclass(frozen=True, slots=True)
class TargetEvidenceEnvelope:
    """Versioned identity around one controller-staged target artifact."""

    run_id: str
    task_id: str
    target_id: str
    repository: str
    branch_ref: str
    base_oid: str
    baseline_sha256: str
    evidence_kind: str
    payload: ArtifactRef
    schema_version: str = _TARGET_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != _TARGET_EVIDENCE_SCHEMA:
            raise CandidateValidationError("target evidence schema is invalid")
        for name in ("run_id", "task_id", "target_id", "repository", "branch_ref"):
            _answer_identity(getattr(self, name), f"target evidence {name}")
        if not isinstance(self.base_oid, str) or len(self.base_oid) != 40 or any(
            character not in "0123456789abcdef" for character in self.base_oid
        ):
            raise CandidateValidationError("target evidence base OID is invalid")
        _digest(self.baseline_sha256, "target evidence baseline digest")
        if self.evidence_kind not in {"candidate", "verification", "check", "skill-load"}:
            raise CandidateValidationError("target evidence kind is invalid")
        _answer_artifact_ref(self.payload)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version, "run_id": self.run_id,
            "task_id": self.task_id, "target_id": self.target_id,
            "repository": self.repository, "branch_ref": self.branch_ref,
            "base_oid": self.base_oid, "baseline_sha256": self.baseline_sha256,
            "evidence_kind": self.evidence_kind,
            "payload": {"path": self.payload.path, "digest": self.payload.digest,
                        "size": self.payload.size},
        }

    @classmethod
    def from_dict(cls, value: object) -> "TargetEvidenceEnvelope":
        fields = {
            "schema_version", "run_id", "task_id", "target_id", "repository",
            "branch_ref", "base_oid", "baseline_sha256", "evidence_kind", "payload",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise CandidateValidationError("target evidence fields are invalid")
        payload = value["payload"]
        if not isinstance(payload, Mapping) or set(payload) != {"path", "digest", "size"}:
            raise CandidateValidationError("target evidence artifact fields are invalid")
        return cls(**{**value, "payload": ArtifactRef(**payload)})


@dataclass(frozen=True, slots=True)
class TargetCandidate:
    target_id: str
    repository: str
    branch_ref: str
    base_oid: str
    baseline_sha256: str
    candidate: CandidateBundle

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, CandidateBundle) or self.candidate.baseline_digest != self.baseline_sha256:
            raise CandidateValidationError("target candidate baseline differs")
        _answer_identity(self.target_id, "target candidate id")
        _answer_identity(self.repository, "target candidate repository")
        _answer_identity(self.branch_ref, "target candidate branch")
        if not isinstance(self.base_oid, str) or len(self.base_oid) != 40 or any(
            character not in "0123456789abcdef" for character in self.base_oid
        ):
            raise CandidateValidationError("target candidate base OID is invalid")


def read_target_evidence(
    envelope: TargetEvidenceEnvelope, *, plan: FanoutPlanV2, inputs: RunInputs,
    store: ArtifactStore, evidence_kind: str, max_bytes: int = _TARGET_EVIDENCE_MAX_BYTES,
) -> bytes:
    """Authenticate plan and v3 target identity before reading bounded payload bytes."""
    if not isinstance(envelope, TargetEvidenceEnvelope) or not isinstance(plan, FanoutPlanV2):
        raise CandidateValidationError("target evidence plan or envelope is invalid")
    if not isinstance(inputs, RunInputs) or inputs.targets is None or not isinstance(store, ArtifactStore):
        raise CandidateValidationError("target evidence requires v3 inputs and artifact store")
    if type(max_bytes) is not int or not 0 <= max_bytes <= _TARGET_EVIDENCE_MAX_BYTES:
        raise CandidateValidationError("target evidence byte limit is invalid")
    task = next((item for item in plan.tasks if item.id == envelope.task_id and item.kind == "work"), None)
    binding = inputs.targets.get(envelope.target_id)
    if (
        envelope.run_id != inputs.run_id
        or inputs.compiled_plan_sha256 != hashlib.sha256(canonical_json(plan.to_dict())).hexdigest()
        or task is None or task.target_id != envelope.target_id
        or not isinstance(binding, TargetBinding)
        or binding.spec not in plan.targets
        or envelope.repository != binding.spec.repository
        or envelope.branch_ref != binding.spec.branch_ref
        or envelope.base_oid != binding.base_oid
    ):
        raise CandidateValidationError("target evidence target differs from plan or v3 inputs")
    if envelope.baseline_sha256 != binding.baseline_sha256:
        raise CandidateValidationError("target evidence baseline differs from v3 inputs")
    if envelope.evidence_kind != evidence_kind:
        raise CandidateValidationError("target evidence kind differs")
    prefix = f"target-evidence/{envelope.target_id}/{evidence_kind}/"
    if not envelope.payload.path.startswith(prefix):
        raise CandidateValidationError("target evidence artifact path differs from target")
    if envelope.payload.size > max_bytes:
        raise CandidateValidationError("target evidence byte limit exceeded")
    try:
        return store.read_bytes(envelope.payload)
    except (ArtifactError, OSError, TypeError, ValueError) as error:
        raise CandidateValidationError("target evidence payload failed byte verification") from error


def _target_issue_context(
    task_id: str, plan: FanoutPlanV2, inputs: RunInputs,
) -> tuple[dict[str, str], TargetBinding, object]:
    if not isinstance(plan, FanoutPlanV2) or not isinstance(inputs, RunInputs) or inputs.targets is None:
        raise CandidateValidationError("target issuance requires v2 plan and v3 inputs")
    task = next((item for item in plan.tasks if item.id == task_id and item.kind == "work"), None)
    binding = None if task is None else inputs.targets.get(task.target_id)
    if (
        task is None or not isinstance(binding, TargetBinding)
        or binding.spec not in plan.targets or binding.baseline_sha256 is None
        or inputs.compiled_plan_sha256 != hashlib.sha256(canonical_json(plan.to_dict())).hexdigest()
    ):
        raise CandidateValidationError("target issuance differs from plan or v3 binding")
    return ({
        "run_id": inputs.run_id, "task_id": task_id,
        "target_id": binding.spec.id, "repository": binding.spec.repository,
        "branch_ref": binding.spec.branch_ref, "base_oid": binding.base_oid,
        "baseline_sha256": binding.baseline_sha256,
    }, binding, task)


def _target_ref(ref: ArtifactRef) -> dict[str, object]:
    return {"path": ref.path, "digest": ref.digest, "size": ref.size}


def _target_seal(
    controller: LifecycleController | None, category: str,
    reference: object, expected: dict[str, object],
) -> None:
    if (
        not isinstance(controller, LifecycleController)
        or not isinstance(reference, dict)
        or set(reference) != {"name", "digest"}
    ):
        raise CandidateValidationError("target evidence requires controller issuance")
    try:
        persisted = read_evidence(controller, category, reference["name"], reference["digest"])
    except Exception as error:
        raise CandidateValidationError("target controller evidence is unavailable") from error
    if canonical_json(persisted) != canonical_json(expected):
        raise CandidateValidationError("target controller evidence differs")


def issue_target_candidate(
    candidate: CandidateBundle, *, task_id: str, plan: FanoutPlanV2,
    inputs: RunInputs, store: ArtifactStore, controller: LifecycleController,
) -> TargetEvidenceEnvelope:
    """Controller-seal one target's proposed v1 candidate without changing v1 bytes."""
    target, binding, _task = _target_issue_context(task_id, plan, inputs)
    if (
        not isinstance(candidate, CandidateBundle)
        or candidate.baseline_digest != binding.baseline_sha256
        or not isinstance(store, ArtifactStore)
        or len(candidate.manifest_bytes) > _TARGET_EVIDENCE_MAX_BYTES
    ):
        raise CandidateValidationError("target candidate baseline or byte limit differs")
    try:
        assert_controller(controller)
        manifest_ref = store.write_bytes(
            f"target-evidence/{binding.spec.id}/candidate/{uuid.uuid4().hex}.manifest.json",
            candidate.manifest_bytes,
        )
        candidate_document = _target_ref(manifest_ref)
        seal = {
            "schema_version": "fanout-target-candidate-seal-v1",
            "target": target, "candidate": candidate_document,
            "candidate_sha256": candidate.digest,
        }
        name, digest = write_evidence(controller, "target-candidate", seal)
        wrapper = canonical_json({
            "schema_version": "fanout-target-candidate-v1",
            "target": target, "candidate": candidate_document,
            "controller_evidence": {"name": name, "digest": digest},
        })
        wrapper_ref = store.write_bytes(
            f"target-evidence/{binding.spec.id}/candidate/{uuid.uuid4().hex}.wrapper.json",
            wrapper,
        )
    except Exception as error:
        raise CandidateValidationError("target candidate controller issuance failed") from error
    return TargetEvidenceEnvelope(
        inputs.run_id, task_id, binding.spec.id, binding.spec.repository,
        binding.spec.branch_ref, binding.base_oid, binding.baseline_sha256,
        "candidate", wrapper_ref,
    )


def load_target_candidate(
    envelope: TargetEvidenceEnvelope, *, plan: FanoutPlanV2, inputs: RunInputs,
    store: ArtifactStore, controller: LifecycleController | None = None,
) -> TargetCandidate:
    data = read_target_evidence(
        envelope, plan=plan, inputs=inputs, store=store, evidence_kind="candidate",
    )
    try:
        document = json.loads(data.decode("utf-8"))
        canonical = canonical_json(document)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError) as error:
        raise CandidateValidationError("target candidate wrapper is invalid") from error
    target = {
        "run_id": envelope.run_id, "task_id": envelope.task_id,
        "target_id": envelope.target_id, "repository": envelope.repository,
        "branch_ref": envelope.branch_ref, "base_oid": envelope.base_oid,
        "baseline_sha256": envelope.baseline_sha256,
    }
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "target", "candidate", "controller_evidence"}
        or document["schema_version"] != "fanout-target-candidate-v1"
        or document["target"] != target
        or not isinstance(document["candidate"], dict)
        or set(document["candidate"]) != {"path", "digest", "size"}
        or canonical != data
    ):
        raise CandidateValidationError("target candidate wrapper differs from target")
    candidate_ref = _answer_artifact_ref(ArtifactRef(**document["candidate"]))
    if (
        candidate_ref.size > _TARGET_EVIDENCE_MAX_BYTES
        or not candidate_ref.path.startswith(f"target-evidence/{envelope.target_id}/candidate/")
        or candidate_ref == envelope.payload
    ):
        raise CandidateValidationError("target candidate manifest target or byte limit differs")
    _target_seal(
        controller, "target-candidate", document["controller_evidence"], {
            "schema_version": "fanout-target-candidate-seal-v1",
            "target": target, "candidate": _target_ref(candidate_ref),
            "candidate_sha256": candidate_ref.digest,
        },
    )
    try:
        manifest = store.read_bytes(candidate_ref)
    except (ArtifactError, OSError, TypeError, ValueError) as error:
        raise CandidateValidationError("target candidate manifest failed byte verification") from error
    candidate = CandidateBundle.from_manifest(manifest)
    return TargetCandidate(
        envelope.target_id, envelope.repository, envelope.branch_ref,
        envelope.base_oid, envelope.baseline_sha256, candidate,
    )


def verify_target_candidate(
    candidate_envelope: TargetEvidenceEnvelope, *, baseline: RepositoryBaseline,
    plan: FanoutPlanV2, inputs: RunInputs, store: ArtifactStore,
    controller: LifecycleController,
    environment: Mapping[str, str] | None = None,
) -> tuple[TargetEvidenceEnvelope, "CandidateVerification"]:
    """Freshly verify one issued candidate against its exact captured target baseline."""
    if not isinstance(candidate_envelope, TargetEvidenceEnvelope):
        raise CandidateValidationError("target candidate envelope is invalid")
    target, binding, task = _target_issue_context(candidate_envelope.task_id, plan, inputs)
    candidate = load_target_candidate(
        candidate_envelope, plan=plan, inputs=inputs, store=store,
        controller=controller,
    ).candidate
    try:
        exact_root = binding.root.resolve(strict=True)
    except OSError as error:
        raise CandidateValidationError("verification baseline target root is unavailable") from error
    if (
        not isinstance(baseline, RepositoryBaseline)
        or exact_root != binding.root
        or baseline.repository != exact_root
        or baseline.head != binding.base_oid
        or baseline.digest != binding.baseline_sha256
        or candidate.baseline_digest != baseline.digest
    ):
        raise CandidateValidationError("verification baseline differs from exact target root, base, or digest")
    receipt = verify_candidate(
        baseline, candidate, task.checks, controller=controller,
        environment=environment,
    )
    validate_candidate_verification(controller, receipt)
    if not receipt.valid:
        raise CandidateVerificationError("target candidate did not pass fresh declared checks")
    try:
        result_ref = store.write_bytes(
            candidate_result_path(candidate_envelope.task_id, candidate.digest, receipt.evidence_digest),
            candidate.manifest_bytes,
        )
        result_document = _target_ref(result_ref)
        candidate_evidence = _target_ref(candidate_envelope.payload)
        seal = {
            "schema_version": "fanout-target-verification-seal-v1",
            "target": target, "candidate_sha256": candidate.digest,
            "candidate_evidence": candidate_evidence,
            "result": result_document,
            "verifier_evidence_sha256": receipt.evidence_digest,
        }
        name, digest = write_evidence(controller, "target-verification", seal)
        wrapper_ref = store.write_bytes(
            f"target-evidence/{binding.spec.id}/verification/{uuid.uuid4().hex}.wrapper.json",
            canonical_json({
                "schema_version": "fanout-target-verification-v1",
                "target": target, "candidate": result_document,
                "candidate_evidence": candidate_evidence,
                "controller_evidence": {"name": name, "digest": digest},
            }),
        )
    except Exception as error:
        raise CandidateVerificationError("target verification controller issuance failed") from error
    return TargetEvidenceEnvelope(
        inputs.run_id, candidate_envelope.task_id, binding.spec.id,
        binding.spec.repository, binding.spec.branch_ref, binding.base_oid,
        binding.baseline_sha256, "verification", wrapper_ref,
    ), receipt


def load_target_verification(
    envelope: TargetEvidenceEnvelope, *, plan: FanoutPlanV2, inputs: RunInputs,
    store: ArtifactStore, controller: LifecycleController,
) -> "CandidateVerification":
    data = read_target_evidence(
        envelope, plan=plan, inputs=inputs, store=store, evidence_kind="verification",
    )
    try:
        document = json.loads(data.decode("utf-8"))
        canonical = canonical_json(document)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError) as error:
        raise CandidateValidationError("target verification wrapper is invalid") from error
    target = {
        "run_id": envelope.run_id, "task_id": envelope.task_id,
        "target_id": envelope.target_id, "repository": envelope.repository,
        "branch_ref": envelope.branch_ref, "base_oid": envelope.base_oid,
        "baseline_sha256": envelope.baseline_sha256,
    }
    if (
        not isinstance(document, dict)
        or set(document) != {
            "schema_version", "target", "candidate", "candidate_evidence",
            "controller_evidence",
        }
        or document["schema_version"] != "fanout-target-verification-v1"
        or document["target"] != target
        or not isinstance(document["candidate"], dict)
        or set(document["candidate"]) != {"path", "digest", "size"}
        or not isinstance(document["candidate_evidence"], dict)
        or set(document["candidate_evidence"]) != {"path", "digest", "size"}
        or canonical != data
    ):
        raise CandidateValidationError("target verification wrapper differs from target")
    candidate_ref = _answer_artifact_ref(ArtifactRef(**document["candidate"]))
    candidate_evidence_ref = _answer_artifact_ref(ArtifactRef(**document["candidate_evidence"]))
    if candidate_ref.size > _TARGET_EVIDENCE_MAX_BYTES or candidate_evidence_ref.size > _TARGET_EVIDENCE_MAX_BYTES:
        raise CandidateValidationError("target verification candidate byte limit exceeded")
    if not candidate_evidence_ref.path.startswith(f"target-evidence/{envelope.target_id}/candidate/"):
        raise CandidateValidationError("target verification candidate evidence differs from target")
    parts = PurePosixPath(candidate_ref.path).parts
    if len(parts) < 4 or candidate_ref.path != candidate_result_path(
        envelope.task_id, candidate_ref.digest, parts[-2],
    ):
        raise CandidateValidationError("target verification result lacks exact verifier receipt")
    _target_seal(
        controller, "target-verification", document["controller_evidence"], {
            "schema_version": "fanout-target-verification-seal-v1",
            "target": target, "candidate_sha256": candidate_ref.digest,
            "candidate_evidence": _target_ref(candidate_evidence_ref),
            "result": _target_ref(candidate_ref),
            "verifier_evidence_sha256": parts[-2],
        },
    )
    candidate_envelope = TargetEvidenceEnvelope(
        envelope.run_id, envelope.task_id, envelope.target_id,
        envelope.repository, envelope.branch_ref, envelope.base_oid,
        envelope.baseline_sha256, "candidate", candidate_evidence_ref,
    )
    issued_candidate = load_target_candidate(
        candidate_envelope, plan=plan, inputs=inputs, store=store,
        controller=controller,
    )
    receipt = load_candidate_verification(
        controller, store, task_id=envelope.task_id, candidate_ref=candidate_ref,
    )
    task = next(item for item in plan.tasks if item.id == envelope.task_id)
    if (receipt.baseline_digest != envelope.baseline_sha256 or receipt.checks != task.checks
            or receipt.candidate_digest != issued_candidate.candidate.digest
            or receipt.evidence_digest != parts[-2] or not receipt.valid):
        raise CandidateValidationError("target verification baseline or declared checks differ")
    return receipt


def load_target_checks(
    envelope: TargetEvidenceEnvelope, *, plan: FanoutPlanV2, inputs: RunInputs,
    store: ArtifactStore, controller: LifecycleController,
    verification_envelope: TargetEvidenceEnvelope,
) -> tuple["CheckOutcome", ...]:
    if (
        not isinstance(verification_envelope, TargetEvidenceEnvelope)
        or (envelope.run_id, envelope.task_id, envelope.target_id, envelope.repository,
            envelope.branch_ref, envelope.base_oid, envelope.baseline_sha256)
        != (verification_envelope.run_id, verification_envelope.task_id,
            verification_envelope.target_id, verification_envelope.repository,
            verification_envelope.branch_ref, verification_envelope.base_oid,
            verification_envelope.baseline_sha256)
    ):
        raise CandidateValidationError("target check and verification envelopes differ")
    verification = load_target_verification(
        verification_envelope, plan=plan, inputs=inputs, store=store,
        controller=controller,
    )
    data = read_target_evidence(
        envelope, plan=plan, inputs=inputs, store=store, evidence_kind="check",
    )
    task = next(item for item in plan.tasks if item.id == envelope.task_id)
    if (not verification.valid or verification.baseline_digest != envelope.baseline_sha256
            or verification.checks != task.checks):
        raise CandidateValidationError("target checks lack exact fresh verification")
    expected = canonical_json({
        "schema_version": "fanout-target-checks-v1",
        "candidate_sha256": verification.candidate_digest,
        "checks": [check.to_dict() for check in verification.checks],
        "outcomes": _receipt_payload(
            candidate=verification.candidate,
            baseline_digest=verification.baseline_digest,
            checks=verification.checks, environment=verification.environment,
            valid=verification.valid, outcomes=verification.outcomes,
            failure=verification.failure, workspace=verification.workspace,
            verifier_root=verification.verifier_root,
            reconciliation=verification.reconciliation,
        )["outcomes"],
    })
    if data != expected:
        raise CandidateValidationError("target declared check evidence differs from verification")
    return verification.outcomes


@dataclass(frozen=True, slots=True)
class CheckArtifact:
    """Digest-only evidence that one declared verifier artifact exists."""

    path: str
    digest: str
    size: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _path(self.path, "check artifact path"))
        _digest(self.digest, "check artifact digest")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise CandidateValidationError("check artifact size is invalid")


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """Safe terminal evidence for one immutable argv verifier check."""

    index: int
    argv_digest: str
    status: str
    returncode: int | None
    artifacts: tuple[CheckArtifact, ...] = ()
    failure: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise CandidateValidationError("check outcome index is invalid")
        _digest(self.argv_digest, "check argv digest")
        if self.status not in {item.value for item in ProcessStatus}:
            raise CandidateValidationError("check outcome status is invalid")
        if self.returncode is not None and (isinstance(self.returncode, bool) or not isinstance(self.returncode, int)):
            raise CandidateValidationError("check outcome return code is invalid")
        artifacts = tuple(self.artifacts)
        if any(not isinstance(item, CheckArtifact) for item in artifacts):
            raise CandidateValidationError("check outcome artifacts are invalid")
        if self.failure is not None and (not isinstance(self.failure, str) or not self.failure):
            raise CandidateValidationError("check outcome failure is invalid")
        object.__setattr__(self, "artifacts", artifacts)


@dataclass(frozen=True, slots=True)
class CandidateReconciliationBinding:
    """The exact final barrier and plan revision that precede a synthesis check."""

    run_id: str
    task_id: str
    plan_sha256: str
    plan_revision: int
    barrier_ref: ArtifactRef

    def __post_init__(self) -> None:
        _answer_identity(self.run_id, "run")
        _answer_identity(self.task_id, "task")
        _digest(self.plan_sha256, "candidate reconciliation plan digest")
        if (isinstance(self.plan_revision, bool) or not isinstance(self.plan_revision, int)
                or self.plan_revision < 1):
            raise CandidateValidationError("candidate reconciliation plan revision is invalid")
        _answer_artifact_ref(self.barrier_ref)

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id, "task_id": self.task_id,
            "plan_sha256": self.plan_sha256, "plan_revision": self.plan_revision,
            "barrier_ref": {
                "path": self.barrier_ref.path,
                "digest": self.barrier_ref.digest,
                "size": self.barrier_ref.size,
            },
        }

    @classmethod
    def from_dict(cls, value: object) -> "CandidateReconciliationBinding":
        if not isinstance(value, dict) or set(value) != {
            "run_id", "task_id", "plan_sha256", "plan_revision", "barrier_ref",
        }:
            raise CandidateValidationError("candidate reconciliation binding is invalid")
        ref = value["barrier_ref"]
        if not isinstance(ref, dict) or set(ref) != {"path", "digest", "size"}:
            raise CandidateValidationError("candidate reconciliation barrier reference is invalid")
        return cls(
            value["run_id"], value["task_id"], value["plan_sha256"],
            value["plan_revision"],
            ArtifactRef(ref["path"], ref["digest"], ref["size"]),
        )


@dataclass(frozen=True, slots=True)
class CandidateVerification:
    """An immutable receipt for one candidate checked in one new workspace."""

    candidate: CandidateBundle = field(repr=False)
    candidate_digest: str = ""
    baseline_digest: str = ""
    checks_digest: str = ""
    environment_digest: str = ""
    checks: tuple[PlanCheckV1, ...] = field(default=(), repr=False)
    environment: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    valid: bool = False
    outcomes: tuple[CheckOutcome, ...] = ()
    failure: str | None = None
    workspace: Path = Path(".")
    verifier_root: Path = Path(".")
    controller_id: str = ""
    evidence_name: str = ""
    evidence_digest: str = ""
    reconciliation: CandidateReconciliationBinding | None = None
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, CandidateBundle):
            raise CandidateValidationError("verification candidate is invalid")
        for value, label in (
            (self.candidate_digest, "verification candidate digest"),
            (self.baseline_digest, "verification baseline digest"),
            (self.checks_digest, "verification checks digest"),
            (self.environment_digest, "verification environment digest"),
        ):
            _digest(value, label)
        if self.candidate_digest != self.candidate.digest:
            raise CandidateValidationError("verification candidate digest does not match its candidate")
        if self.candidate.digest != hashlib.sha256(self.candidate.manifest_bytes).hexdigest():
            raise CandidateValidationError("verification candidate manifest does not match its digest")
        if self.baseline_digest != self.candidate.baseline_digest:
            raise CandidateValidationError("verification baseline digest does not match its candidate")
        checks = tuple(self.checks)
        if not checks:
            raise CandidateValidationError("verification receipt requires at least one immutable argv check")
        if any(not isinstance(check, PlanCheckV1) for check in checks):
            raise CandidateValidationError("verification checks are invalid")
        if self.checks_digest != _checks_digest(checks):
            raise CandidateValidationError("verification check digest does not match immutable checks")
        environment = tuple(self.environment)
        if tuple(sorted(environment)) != environment or any(
            not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], str)
            or not isinstance(item[1], str) or "\x00" in item[0] or "\x00" in item[1]
            for item in environment
        ):
            raise CandidateValidationError("verification environment is invalid")
        if self.environment_digest != _environment_digest(environment):
            raise CandidateValidationError("verification environment digest does not match its values")
        if isinstance(self.valid, bool) is False:
            raise CandidateValidationError("verification validity is invalid")
        outcomes = tuple(self.outcomes)
        if any(not isinstance(item, CheckOutcome) for item in outcomes):
            raise CandidateValidationError("verification outcomes are invalid")
        if self.valid and (self.failure is not None or len(outcomes) != len(checks) or any(item.failure for item in outcomes)):
            raise CandidateValidationError("valid verification receipt has failing evidence")
        if self.valid:
            for index, (check, outcome) in enumerate(zip(checks, outcomes, strict=True)):
                expected_argv = hashlib.sha256(canonical_json(list(check.argv))).hexdigest()
                if (
                    outcome.index != index
                    or outcome.argv_digest != expected_argv
                    or outcome.status != ProcessStatus.EXIT.value
                    or outcome.returncode not in check.accepted_exit_codes
                    or tuple(item.path for item in outcome.artifacts) != check.expected_artifacts
                ):
                    raise CandidateValidationError("valid verification receipt has invalid check outcome evidence")
        if not self.valid and (not isinstance(self.failure, str) or not self.failure):
            raise CandidateValidationError("failed verification receipt needs a failure")
        if self._issuer is not _RECEIPT_ISSUER:
            raise CandidateValidationError("verification receipts must be issued by the controller-backed verifier")
        _digest(self.controller_id, "verification controller id")
        if (
            not isinstance(self.evidence_name, str)
            or not self.evidence_name.endswith(".json")
            or len(self.evidence_name) != 69
            or any(character not in "0123456789abcdef" for character in self.evidence_name[:-5])
        ):
            raise CandidateValidationError("verification evidence reference is invalid")
        _digest(self.evidence_digest, "verification evidence digest")
        if self.evidence_name != f"{self.evidence_digest}.json":
            raise CandidateValidationError("verification evidence reference does not match its digest")
        if self.reconciliation is not None and not isinstance(
            self.reconciliation, CandidateReconciliationBinding,
        ):
            raise CandidateValidationError("verification reconciliation binding is invalid")
        workspace, verifier_root = Path(self.workspace), Path(self.verifier_root)
        if not workspace.is_absolute() or not verifier_root.is_absolute():
            raise CandidateValidationError("verification paths must be absolute")
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "verifier_root", verifier_root)


@dataclass(frozen=True, slots=True)
class AnswerSynthesisVerification:
    """Controller-backed evidence for one new answer and its exact source seats."""

    run_id: str
    task_id: str
    plan_sha256: str
    plan_revision: int
    synthesizer_id: str
    answer_ref: ArtifactRef
    source_answers: tuple[tuple[str, ArtifactRef], ...]
    checks: tuple[PlanCheckV1, ...]
    environment: tuple[tuple[str, str], ...]
    valid: bool
    outcomes: tuple[CheckOutcome, ...]
    failure: str | None
    workspace: Path
    controller_id: str
    evidence_name: str
    evidence_digest: str
    baseline_digest: str | None = None
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for value, label in ((self.run_id, "run"), (self.task_id, "task"),
                             (self.synthesizer_id, "synthesizer")):
            _answer_identity(value, label)
        _digest(self.plan_sha256, "answer plan digest")
        if self.baseline_digest is not None:
            _digest(self.baseline_digest, "answer baseline digest")
        if (isinstance(self.plan_revision, bool) or not isinstance(self.plan_revision, int)
                or self.plan_revision < 1):
            raise CandidateValidationError("answer plan revision is invalid")
        _answer_artifact_ref(self.answer_ref)
        sources = tuple(self.source_answers)
        if len(sources) < 2 or tuple(sorted(sources, key=lambda item: item[0])) != sources:
            raise CandidateValidationError("answer synthesis needs ordered distinct sources")
        for seat_id, ref in sources:
            _answer_identity(seat_id, "source seat")
            _answer_artifact_ref(ref)
        if (len({seat for seat, _ in sources}) != len(sources)
                or len({ref.digest for _, ref in sources}) != len(sources)
                or self.answer_ref.digest in {ref.digest for _, ref in sources}):
            raise CandidateValidationError("answer synthesis needs distinct source and result digests")
        checks = tuple(self.checks)
        if any(not isinstance(check, PlanCheckV1) for check in checks):
            raise CandidateValidationError("answer synthesis checks are invalid")
        environment = tuple(self.environment)
        if environment != tuple(sorted(environment)) or any(
            not isinstance(item, tuple) or len(item) != 2
            or not isinstance(item[0], str) or not isinstance(item[1], str)
            or "\x00" in item[0] or "\x00" in item[1]
            for item in environment
        ):
            raise CandidateValidationError("answer synthesis environment is invalid")
        if any(name not in {allowed for check in checks for allowed in check.env_allowlist}
               for name, _ in environment):
            raise CandidateValidationError("answer synthesis environment is outside declared checks")
        outcomes = tuple(self.outcomes)
        if not isinstance(self.valid, bool) or any(not isinstance(item, CheckOutcome) for item in outcomes):
            raise CandidateValidationError("answer synthesis outcomes are invalid")
        if self.valid:
            if self.failure is not None or len(outcomes) != len(checks):
                raise CandidateValidationError("answer synthesis lacks successful declared checks")
            for index, (check, outcome) in enumerate(zip(checks, outcomes, strict=True)):
                if (outcome.index != index
                        or outcome.argv_digest != hashlib.sha256(canonical_json(list(check.argv))).hexdigest()
                        or outcome.status != ProcessStatus.EXIT.value
                        or outcome.returncode not in check.accepted_exit_codes
                        or outcome.failure is not None
                        or tuple(item.path for item in outcome.artifacts) != check.expected_artifacts):
                    raise CandidateValidationError("answer synthesis check outcome is invalid")
        elif not isinstance(self.failure, str) or not self.failure:
            raise CandidateValidationError("failed answer synthesis needs a reason")
        if self._issuer is not _ANSWER_RECEIPT_ISSUER:
            raise CandidateValidationError("answer synthesis receipts must be issued by the verifier")
        _digest(self.controller_id, "answer controller id")
        _digest(self.evidence_digest, "answer evidence digest")
        if self.evidence_name != f"{self.evidence_digest}.json":
            raise CandidateValidationError("answer evidence reference is invalid")
        if self.answer_ref.path != _answer_result_path(self.run_id, self.task_id, self.evidence_digest):
            raise CandidateValidationError("answer artifact is not bound to its evidence")
        if not Path(self.workspace).is_absolute():
            raise CandidateValidationError("answer verifier workspace must be absolute")
        object.__setattr__(self, "source_answers", sources)
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(self, "workspace", Path(self.workspace))


@dataclass(frozen=True, slots=True)
class AnswerSelectionVerification:
    """One checked, explicit final-seat selection with durable controller proof."""

    run_id: str
    task_id: str
    plan_sha256: str
    plan_revision: int
    seat_id: str
    selected_ref: ArtifactRef
    answer_ref: ArtifactRef
    checks: tuple[PlanCheckV1, ...]
    environment: tuple[tuple[str, str], ...]
    valid: bool
    outcomes: tuple[CheckOutcome, ...]
    failure: str | None
    workspace: Path
    controller_id: str
    evidence_name: str
    evidence_digest: str
    baseline_digest: str | None = None
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for value, label in ((self.run_id, "run"), (self.task_id, "task"),
                             (self.seat_id, "selected seat")):
            _answer_identity(value, label)
        _digest(self.plan_sha256, "selection plan digest")
        if self.baseline_digest is not None:
            _digest(self.baseline_digest, "selection baseline digest")
        if (isinstance(self.plan_revision, bool) or not isinstance(self.plan_revision, int)
                or self.plan_revision < 1):
            raise CandidateValidationError("selection plan revision is invalid")
        _answer_artifact_ref(self.selected_ref)
        _answer_artifact_ref(self.answer_ref)
        if (self.selected_ref.digest, self.selected_ref.size) != (
            self.answer_ref.digest, self.answer_ref.size,
        ):
            raise CandidateValidationError("selection result differs from selected seat bytes")
        checks = tuple(self.checks)
        if not checks or any(not isinstance(check, PlanCheckV1) for check in checks):
            raise CandidateValidationError("checked selection needs declared checks")
        environment = tuple(self.environment)
        if environment != tuple(sorted(environment)) or any(
            not isinstance(item, tuple) or len(item) != 2
            or not isinstance(item[0], str) or not isinstance(item[1], str)
            or "\x00" in item[0] or "\x00" in item[1]
            for item in environment
        ) or any(name not in {allowed for check in checks for allowed in check.env_allowlist}
                 for name, _ in environment):
            raise CandidateValidationError("selection environment is invalid")
        outcomes = tuple(self.outcomes)
        if not isinstance(self.valid, bool) or any(not isinstance(item, CheckOutcome) for item in outcomes):
            raise CandidateValidationError("selection outcomes are invalid")
        if self.valid:
            if self.failure is not None or len(outcomes) != len(checks):
                raise CandidateValidationError("selection lacks successful declared checks")
            for index, (check, outcome) in enumerate(zip(checks, outcomes, strict=True)):
                if (outcome.index != index
                        or outcome.argv_digest != hashlib.sha256(canonical_json(list(check.argv))).hexdigest()
                        or outcome.status != ProcessStatus.EXIT.value
                        or outcome.returncode not in check.accepted_exit_codes
                        or outcome.failure is not None
                        or tuple(item.path for item in outcome.artifacts) != check.expected_artifacts):
                    raise CandidateValidationError("selection check outcome is invalid")
        elif not isinstance(self.failure, str) or not self.failure:
            raise CandidateValidationError("failed selection needs a reason")
        if self._issuer is not _ANSWER_RECEIPT_ISSUER:
            raise CandidateValidationError("selection receipt must be issued by verifier")
        _digest(self.controller_id, "selection controller id")
        _digest(self.evidence_digest, "selection evidence digest")
        if self.evidence_name != f"{self.evidence_digest}.json":
            raise CandidateValidationError("selection evidence reference is invalid")
        if self.answer_ref.path != _answer_selection_path(self.run_id, self.task_id, self.evidence_digest):
            raise CandidateValidationError("selected answer is not bound to its evidence")
        if not Path(self.workspace).is_absolute():
            raise CandidateValidationError("selection verifier workspace must be absolute")
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(self, "workspace", Path(self.workspace))


def _verify_answer_in_workspace(
    answer: bytes,
    checks: tuple[PlanCheckV1, ...],
    environment: tuple[tuple[str, str], ...],
    controller: LifecycleController,
    baseline: RepositoryBaseline | None,
    *,
    kind: str,
) -> tuple[Path, tuple[CheckOutcome, ...], str | None]:
    try:
        assert_controller(controller)
        if baseline is not None:
            _baseline(baseline)
            verifier_root = _controller_verifier_root(baseline, controller)
        else:
            verifier_root = controller_directory(controller, "verifier").resolve()
        transaction_root = Path(tempfile.mkdtemp(prefix=f".fanout-{kind}-", dir=verifier_root)).resolve()
        workspace = (
            create_seat_workspace(baseline, transaction_root, f"{kind}-{uuid.uuid4().hex[:16]}").root
            if baseline is not None else transaction_root
        )
        _write_entry(workspace, CandidateEntry("answer.md", "file", 0o600, answer))
        outcomes, failure = _run_checks(workspace, checks, environment)
        if (workspace / "answer.md").read_bytes() != answer:
            failure = f"declared check modified the {kind} answer"
        return workspace, outcomes, failure
    except CandidateValidationError:
        raise
    except Exception as error:
        raise CandidateVerificationError(f"answer {kind} verifier could not run") from error


def verify_answer_synthesis(
    artifacts: ArtifactStore,
    *,
    run_id: str,
    task_id: str,
    plan_sha256: str,
    plan_revision: int,
    sources: Mapping[str, ArtifactRef],
    synthesizer_id: str,
    answer: bytes,
    checks: Sequence[PlanCheckV1],
    controller: LifecycleController,
    environment: Mapping[str, str] | None = None,
    baseline: RepositoryBaseline | None = None,
) -> AnswerSynthesisVerification:
    """Check a new read-only answer in isolation and persist its provenance."""
    if not isinstance(artifacts, ArtifactStore) or not isinstance(sources, Mapping):
        raise CandidateValidationError("answer synthesis requires stored source answers")
    _answer_identity(run_id, "run")
    _answer_identity(task_id, "task")
    _answer_identity(synthesizer_id, "synthesizer")
    _digest(plan_sha256, "answer plan digest")
    if isinstance(plan_revision, bool) or not isinstance(plan_revision, int) or plan_revision < 1:
        raise CandidateValidationError("answer plan revision is invalid")
    if not isinstance(answer, bytes):
        raise CandidateValidationError("synthesized answer must be UTF-8 bytes")
    try:
        decoded = answer.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CandidateValidationError("synthesized answer must be UTF-8 bytes") from error
    if not decoded.strip() or "\x00" in decoded:
        raise CandidateValidationError("synthesized answer must contain nonempty text")
    source_answers = tuple(sorted(sources.items()))
    if len(source_answers) < 2:
        raise CandidateValidationError("answer synthesis needs at least two distinct sources")
    for seat_id, ref in source_answers:
        _answer_identity(seat_id, "source seat")
        _answer_artifact_ref(ref)
        artifacts.read_bytes(ref)
    source_digests = {ref.digest for _, ref in source_answers}
    answer_digest = hashlib.sha256(answer).hexdigest()
    if len(source_digests) != len(source_answers) or answer_digest in source_digests:
        raise CandidateValidationError("answer synthesis needs distinct source and result digests")
    immutable_checks = tuple(checks)
    if any(not isinstance(check, PlanCheckV1) for check in immutable_checks):
        raise CandidateValidationError("answer synthesis checks must be immutable argv checks")
    immutable_checks = tuple(PlanCheckV1.from_dict(check.to_dict(), where=f"answer checks[{index}]")
                             for index, check in enumerate(immutable_checks))
    immutable_environment = _environment(immutable_checks, environment)
    workspace, outcomes, failure = _verify_answer_in_workspace(
        answer, immutable_checks, immutable_environment, controller, baseline,
        kind="synthesis",
    )
    payload = _answer_receipt_payload(
        run_id=run_id, task_id=task_id, plan_sha256=plan_sha256,
        plan_revision=plan_revision, synthesizer_id=synthesizer_id,
        answer_sha256=answer_digest, answer_size=len(answer), source_answers=source_answers,
        baseline_digest=None if baseline is None else baseline.digest,
        checks=immutable_checks, environment=immutable_environment,
        valid=failure is None, outcomes=outcomes, failure=failure,
        workspace=workspace,
    )
    try:
        evidence_name, evidence_digest = write_evidence(controller, "answer-synthesis", payload)
    except Exception as error:
        raise CandidateVerificationError("answer synthesis evidence could not be persisted") from error
    path = _answer_result_path(run_id, task_id, evidence_digest)
    try:
        answer_ref = artifacts.write_bytes(path, answer)
    except ArtifactExistsError:
        answer_ref = ArtifactRef(path, answer_digest, len(answer))
        if artifacts.read_bytes(answer_ref) != answer:
            raise CandidateVerificationError("synthesized answer artifact equivocated")
    return AnswerSynthesisVerification(
        run_id, task_id, plan_sha256, plan_revision, synthesizer_id,
        answer_ref, source_answers, immutable_checks, immutable_environment,
        failure is None, outcomes, failure, workspace,
        controller.controller_id, evidence_name, evidence_digest,
        baseline_digest=None if baseline is None else baseline.digest,
        _issuer=_ANSWER_RECEIPT_ISSUER,
    )


def verify_answer_selection(
    artifacts: ArtifactStore,
    *,
    run_id: str,
    task_id: str,
    plan_sha256: str,
    plan_revision: int,
    seat_id: str,
    selected_ref: ArtifactRef,
    checks: Sequence[PlanCheckV1],
    controller: LifecycleController,
    environment: Mapping[str, str] | None = None,
    baseline: RepositoryBaseline | None = None,
) -> AnswerSelectionVerification:
    """Freshly check one exact seat answer without claiming to synthesize it."""
    if not isinstance(artifacts, ArtifactStore):
        raise CandidateValidationError("selection requires an answer artifact store")
    for value, label in ((run_id, "run"), (task_id, "task"), (seat_id, "seat")):
        _answer_identity(value, label)
    _digest(plan_sha256, "selection plan digest")
    if isinstance(plan_revision, bool) or not isinstance(plan_revision, int) or plan_revision < 1:
        raise CandidateValidationError("selection plan revision is invalid")
    _answer_artifact_ref(selected_ref)
    answer = artifacts.read_bytes(selected_ref)
    try:
        decoded = answer.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CandidateValidationError("selected answer must be UTF-8") from error
    if not decoded.strip() or "\x00" in decoded:
        raise CandidateValidationError("selected answer must contain nonempty text")
    immutable_checks = _checks(checks)
    immutable_environment = _environment(immutable_checks, environment)
    workspace, outcomes, failure = _verify_answer_in_workspace(
        answer, immutable_checks, immutable_environment, controller, baseline,
        kind="selection",
    )
    payload = _answer_selection_payload(
        run_id=run_id, task_id=task_id, plan_sha256=plan_sha256,
        plan_revision=plan_revision, seat_id=seat_id, selected_ref=selected_ref,
        checks=immutable_checks, environment=immutable_environment,
        valid=failure is None, outcomes=outcomes, failure=failure,
        workspace=workspace, baseline_digest=None if baseline is None else baseline.digest,
    )
    try:
        evidence_name, evidence_digest = write_evidence(controller, "answer-selection", payload)
    except Exception as error:
        raise CandidateVerificationError("answer selection evidence could not be persisted") from error
    path = _answer_selection_path(run_id, task_id, evidence_digest)
    try:
        answer_ref = artifacts.write_bytes(path, answer)
    except ArtifactExistsError:
        answer_ref = ArtifactRef(path, selected_ref.digest, selected_ref.size)
        if artifacts.read_bytes(answer_ref) != answer:
            raise CandidateVerificationError("selected answer artifact equivocated")
    return AnswerSelectionVerification(
        run_id, task_id, plan_sha256, plan_revision, seat_id,
        selected_ref, answer_ref, immutable_checks, immutable_environment,
        failure is None, outcomes, failure, workspace, controller.controller_id,
        evidence_name, evidence_digest,
        baseline_digest=None if baseline is None else baseline.digest,
        _issuer=_ANSWER_RECEIPT_ISSUER,
    )


def validate_answer_synthesis(
    controller: LifecycleController,
    artifacts: ArtifactStore,
    receipt: AnswerSynthesisVerification,
) -> None:
    """Recheck signed synthesis evidence and every source/result artifact byte."""
    if not isinstance(receipt, AnswerSynthesisVerification) or not isinstance(artifacts, ArtifactStore):
        raise CandidateValidationError("answer synthesis receipt is invalid")
    try:
        assert_controller(controller)
        if receipt.controller_id != controller.controller_id:
            raise CandidateValidationError("answer synthesis belongs to another controller")
        persisted = read_evidence(
            controller, "answer-synthesis", receipt.evidence_name, receipt.evidence_digest,
        )
        expected = _answer_receipt_payload(
            run_id=receipt.run_id, task_id=receipt.task_id,
            plan_sha256=receipt.plan_sha256, plan_revision=receipt.plan_revision,
            synthesizer_id=receipt.synthesizer_id,
            answer_sha256=receipt.answer_ref.digest, answer_size=receipt.answer_ref.size,
            source_answers=receipt.source_answers, checks=receipt.checks,
            baseline_digest=receipt.baseline_digest,
            environment=receipt.environment, valid=receipt.valid,
            outcomes=receipt.outcomes, failure=receipt.failure, workspace=receipt.workspace,
        )
        if canonical_json(persisted) != canonical_json(expected):
            raise CandidateValidationError("answer synthesis evidence differs from receipt")
        artifacts.read_bytes(receipt.answer_ref)
        for _, ref in receipt.source_answers:
            artifacts.read_bytes(ref)
    except CandidateValidationError:
        raise
    except Exception as error:
        raise CandidateValidationError("answer synthesis evidence or artifacts are unavailable") from error


def validate_answer_selection(
    controller: LifecycleController,
    artifacts: ArtifactStore,
    receipt: AnswerSelectionVerification,
) -> None:
    """Authenticate the selected seat, delivered bytes, and fresh check outcomes."""
    if not isinstance(receipt, AnswerSelectionVerification) or not isinstance(artifacts, ArtifactStore):
        raise CandidateValidationError("answer selection receipt is invalid")
    try:
        assert_controller(controller)
        if receipt.controller_id != controller.controller_id:
            raise CandidateValidationError("answer selection belongs to another controller")
        persisted = read_evidence(
            controller, "answer-selection", receipt.evidence_name, receipt.evidence_digest,
        )
        expected = _answer_selection_payload(
            run_id=receipt.run_id, task_id=receipt.task_id,
            plan_sha256=receipt.plan_sha256, plan_revision=receipt.plan_revision,
            seat_id=receipt.seat_id, selected_ref=receipt.selected_ref,
            checks=receipt.checks, environment=receipt.environment,
            valid=receipt.valid, outcomes=receipt.outcomes, failure=receipt.failure,
            workspace=receipt.workspace, baseline_digest=receipt.baseline_digest,
        )
        if canonical_json(persisted) != canonical_json(expected):
            raise CandidateValidationError("answer selection evidence differs from receipt")
        if artifacts.read_bytes(receipt.answer_ref) != artifacts.read_bytes(receipt.selected_ref):
            raise CandidateValidationError("selected answer bytes differ from delivered result")
    except CandidateValidationError:
        raise
    except Exception as error:
        raise CandidateValidationError("answer selection evidence or artifacts are unavailable") from error


def load_answer_selection(
    controller: LifecycleController,
    artifacts: ArtifactStore,
    *,
    run_id: str,
    task_id: str,
    answer_ref: ArtifactRef,
) -> AnswerSelectionVerification:
    """Restore a checked selection using only the delivered result reference."""
    _answer_identity(run_id, "run")
    _answer_identity(task_id, "task")
    _answer_artifact_ref(answer_ref)
    evidence_digest = PurePosixPath(answer_ref.path).stem
    _digest(evidence_digest, "selection evidence digest")
    if answer_ref.path != _answer_selection_path(run_id, task_id, evidence_digest):
        raise CandidateValidationError("selected answer is not bound to selection evidence")
    try:
        payload = read_evidence(
            controller, "answer-selection", f"{evidence_digest}.json", evidence_digest,
        )
        if set(payload) != {
            "schema_version", "run_id", "task_id", "plan_sha256", "plan_revision",
            "answer_sha256", "answer_size", "baseline_sha256", "source_answers",
            "checks", "environment", "valid", "outcomes", "failure", "workspace",
        } or payload["schema_version"] != "fanout-answer-selection-v1":
            raise CandidateValidationError("answer selection receipt schema is invalid")
        if any(not isinstance(payload[key], list) for key in (
            "source_answers", "checks", "environment", "outcomes",
        )) or len(payload["source_answers"]) != 1:
            raise CandidateValidationError("answer selection receipt collections are invalid")
        selected = payload["source_answers"][0]
        if not isinstance(selected, dict) or set(selected) != {"seat_id", "artifact"}:
            raise CandidateValidationError("answer selection source association is invalid")
        source = selected["artifact"]
        if not isinstance(source, dict) or set(source) != {"path", "digest", "size"}:
            raise CandidateValidationError("answer selection artifact association is invalid")
        if (payload["answer_sha256"], payload["answer_size"]) != (
            answer_ref.digest, answer_ref.size,
        ):
            raise CandidateValidationError("selection receipt differs from delivered answer")
        receipt = AnswerSelectionVerification(
            payload["run_id"], payload["task_id"], payload["plan_sha256"],
            payload["plan_revision"], selected["seat_id"],
            ArtifactRef(source["path"], source["digest"], source["size"]),
            answer_ref, _receipt_checks(payload["checks"]),
            _receipt_environment(payload["environment"]), payload["valid"],
            _receipt_outcomes(payload["outcomes"]), payload["failure"],
            Path(payload["workspace"]), controller.controller_id,
            f"{evidence_digest}.json", evidence_digest,
            baseline_digest=payload["baseline_sha256"],
            _issuer=_ANSWER_RECEIPT_ISSUER,
        )
        validate_answer_selection(controller, artifacts, receipt)
        return receipt
    except CandidateValidationError:
        raise
    except Exception as error:
        raise CandidateValidationError("answer selection receipt cannot be restored") from error


def load_answer_synthesis(
    controller: LifecycleController,
    artifacts: ArtifactStore,
    *,
    run_id: str,
    task_id: str,
    answer_ref: ArtifactRef,
) -> AnswerSynthesisVerification:
    """Reconstruct one signed receipt from the delivered answer's durable path."""
    _answer_identity(run_id, "run")
    _answer_identity(task_id, "task")
    _answer_artifact_ref(answer_ref)
    evidence_digest = PurePosixPath(answer_ref.path).stem
    _digest(evidence_digest, "answer evidence digest")
    if answer_ref.path != _answer_result_path(run_id, task_id, evidence_digest):
        raise CandidateValidationError("answer artifact is not bound to a synthesis receipt")

    def artifact(value: object) -> ArtifactRef:
        if not isinstance(value, dict) or set(value) != {"path", "digest", "size"}:
            raise CandidateValidationError("answer synthesis artifact association is invalid")
        return _answer_artifact_ref(ArtifactRef(value["path"], value["digest"], value["size"]))

    try:
        payload = read_evidence(controller, "answer-synthesis", f"{evidence_digest}.json", evidence_digest)
        if set(payload) != {
            "schema_version", "run_id", "task_id", "plan_sha256", "plan_revision",
            "synthesizer_id", "answer_sha256", "answer_size", "baseline_sha256",
            "source_answers", "checks", "environment", "valid", "outcomes",
            "failure", "workspace",
        } or payload["schema_version"] != "fanout-answer-synthesis-v1":
            raise CandidateValidationError("answer synthesis receipt schema is invalid")
        if any(not isinstance(payload[key], list) for key in (
            "source_answers", "checks", "environment", "outcomes",
        )):
            raise CandidateValidationError("answer synthesis receipt collections are invalid")
        if (payload["answer_sha256"], payload["answer_size"]) != (
            answer_ref.digest, answer_ref.size,
        ):
            raise CandidateValidationError("answer synthesis receipt differs from delivered answer")
        sources = []
        for entry in payload["source_answers"]:
            if not isinstance(entry, dict) or set(entry) != {"seat_id", "artifact"}:
                raise CandidateValidationError("answer synthesis source association is invalid")
            sources.append((entry["seat_id"], artifact(entry["artifact"])))
        receipt = AnswerSynthesisVerification(
            payload["run_id"], payload["task_id"], payload["plan_sha256"],
            payload["plan_revision"], payload["synthesizer_id"], answer_ref,
            tuple(sources), _receipt_checks(payload["checks"]),
            _receipt_environment(payload["environment"]), payload["valid"],
            _receipt_outcomes(payload["outcomes"]), payload["failure"], Path(payload["workspace"]),
            controller.controller_id, f"{evidence_digest}.json", evidence_digest,
            baseline_digest=payload["baseline_sha256"], _issuer=_ANSWER_RECEIPT_ISSUER,
        )
        validate_answer_synthesis(controller, artifacts, receipt)
        return receipt
    except CandidateValidationError:
        raise
    except Exception as error:
        raise CandidateValidationError("answer synthesis receipt cannot be restored") from error


def _receipt_checks(value: list[object]) -> tuple[PlanCheckV1, ...]:
    return tuple(
        PlanCheckV1.from_dict(entry, where=f"verification checks[{index}]")
        for index, entry in enumerate(value)
    )


def _receipt_environment(value: list[object]) -> tuple[tuple[str, str], ...]:
    environment = []
    for entry in value:
        if not isinstance(entry, list) or len(entry) != 2:
            raise CandidateValidationError("verification environment association is invalid")
        environment.append(tuple(entry))
    return tuple(environment)


def _receipt_outcomes(value: list[object]) -> tuple[CheckOutcome, ...]:
    outcomes = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {
            "index", "argv_digest", "status", "returncode", "artifacts", "failure",
        } or not isinstance(entry["artifacts"], list):
            raise CandidateValidationError("verification check outcome association is invalid")
        outcomes.append(CheckOutcome(
            entry["index"], entry["argv_digest"], entry["status"],
            entry["returncode"],
            tuple(CheckArtifact(**_answer_check_artifact(item)) for item in entry["artifacts"]),
            entry["failure"],
        ))
    return tuple(outcomes)


def _answer_check_artifact(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"path", "digest", "size"}:
        raise CandidateValidationError("answer synthesis check artifact association is invalid")
    return value


def _answer_receipt_payload(
    *, run_id: str, task_id: str, plan_sha256: str, plan_revision: int,
    synthesizer_id: str, answer_sha256: str, answer_size: int,
    source_answers: tuple[tuple[str, ArtifactRef], ...],
    baseline_digest: str | None,
    checks: tuple[PlanCheckV1, ...], environment: tuple[tuple[str, str], ...],
    valid: bool, outcomes: tuple[CheckOutcome, ...], failure: str | None,
    workspace: Path,
) -> dict[str, object]:
    def ref_value(ref: ArtifactRef) -> dict[str, object]:
        return {"path": ref.path, "digest": ref.digest, "size": ref.size}

    return {
        "schema_version": "fanout-answer-synthesis-v1",
        "run_id": run_id, "task_id": task_id,
        "plan_sha256": plan_sha256, "plan_revision": plan_revision,
        "synthesizer_id": synthesizer_id,
        "answer_sha256": answer_sha256, "answer_size": answer_size,
        "baseline_sha256": baseline_digest,
        "source_answers": [
            {"seat_id": seat_id, "artifact": ref_value(ref)}
            for seat_id, ref in source_answers
        ],
        "checks": [check.to_dict() for check in checks],
        "environment": [[name, value] for name, value in environment],
        "valid": valid,
        "outcomes": [{
            "index": outcome.index, "argv_digest": outcome.argv_digest,
            "status": outcome.status, "returncode": outcome.returncode,
            "artifacts": [{"path": artifact.path, "digest": artifact.digest, "size": artifact.size}
                          for artifact in outcome.artifacts],
            "failure": outcome.failure,
        } for outcome in outcomes],
        "failure": failure,
        "workspace": os.fspath(workspace),
    }


def _answer_selection_payload(
    *, run_id: str, task_id: str, plan_sha256: str, plan_revision: int,
    seat_id: str, selected_ref: ArtifactRef,
    checks: tuple[PlanCheckV1, ...], environment: tuple[tuple[str, str], ...],
    valid: bool, outcomes: tuple[CheckOutcome, ...], failure: str | None,
    workspace: Path, baseline_digest: str | None,
) -> dict[str, object]:
    payload = _answer_receipt_payload(
        run_id=run_id, task_id=task_id, plan_sha256=plan_sha256,
        plan_revision=plan_revision, synthesizer_id="selection",
        answer_sha256=selected_ref.digest, answer_size=selected_ref.size,
        source_answers=((seat_id, selected_ref),), baseline_digest=baseline_digest,
        checks=checks, environment=environment, valid=valid, outcomes=outcomes,
        failure=failure, workspace=workspace,
    )
    payload.pop("synthesizer_id")
    payload["schema_version"] = "fanout-answer-selection-v1"
    return payload


def _answer_result_path(run_id: str, task_id: str, evidence_digest: str) -> str:
    task_key = hashlib.sha256(canonical_json([run_id, task_id])).hexdigest()[:24]
    return f"results/answers/{task_key}/{evidence_digest}.md"


def _answer_selection_path(run_id: str, task_id: str, evidence_digest: str) -> str:
    task_key = hashlib.sha256(canonical_json([run_id, task_id])).hexdigest()[:24]
    return f"results/answer-selections/{task_key}/{evidence_digest}.md"


def _answer_identity(value: object, label: str) -> str:
    if (not isinstance(value, str) or not value or value.strip() != value
            or len(value.encode("utf-8")) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise CandidateValidationError(f"answer {label} identity is invalid")
    return value


def _answer_artifact_ref(ref: object) -> ArtifactRef:
    if not isinstance(ref, ArtifactRef) or not isinstance(ref.path, str):
        raise CandidateValidationError("answer artifact reference is invalid")
    _path(ref.path, "answer artifact path")
    _digest(ref.digest, "answer artifact digest")
    if isinstance(ref.size, bool) or not isinstance(ref.size, int) or ref.size < 0:
        raise CandidateValidationError("answer artifact size is invalid")
    return ref


def create_candidate(baseline: RepositoryBaseline, workspace: SeatWorkspace | Path | str) -> CandidateBundle:
    """Capture one non-mutating, complete delta from an isolated seat workspace."""
    _baseline(baseline)
    root = _workspace_root(baseline, workspace)
    directories = _scan_workspace_safety(root)
    baseline_entries = {entry.path: entry for entry in baseline.entries}
    baseline_directories = _baseline_directories(baseline)
    baseline_directory_modes = _baseline_directory_modes(baseline)
    paths = set(baseline_entries)
    try:
        listed = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    except Exception as error:
        raise CandidateValidationError("candidate workspace is not a safe Git worktree") from error
    for raw_path in _nul_records(listed):
        paths.add(_path(os.fsdecode(raw_path), "Git candidate path"))

    entries: list[CandidateEntry] = []
    deleted: list[str] = []
    total = 0
    for path in sorted(paths, key=_path_bytes):
        entry = _read_entry(root, path, directory_is_absent=path in baseline_entries)
        if entry is None:
            if path in baseline_entries:
                deleted.append(path)
                continue
            raise CandidateValidationError("candidate workspace changed while it was collected")
        _candidate_budget(baseline, entry, total)
        total += len(entry.data)
        previous = baseline_entries.get(path)
        if previous is None or not _same_entry(previous, entry):
            entries.append(entry)
            if previous is not None and previous.kind != entry.kind:
                deleted.append(path)
    for path, mode in directories.items():
        if baseline_directory_modes.get(path) != mode:
            entries.append(CandidateEntry(path, "directory", mode, b""))
    deleted.extend(sorted(baseline_directories - set(directories), key=_path_bytes))
    return CandidateBundle(baseline.digest, tuple(entries), tuple(deleted))


def synthesize_candidate(
    baseline: RepositoryBaseline,
    *,
    sources: Sequence[CandidateBundle],
    entries: Sequence[CandidateEntry],
    deleted_paths: Sequence[str],
) -> CandidateBundle:
    """Create a new reconciled candidate bound to the exact source-candidate identities."""
    _baseline(baseline)
    source_bundles = tuple(sources)
    if len(source_bundles) < 2 or any(not isinstance(item, CandidateBundle) for item in source_bundles):
        raise CandidateValidationError("synthesis requires at least two candidate sources")
    if any(item.baseline_digest != baseline.digest for item in source_bundles):
        raise CandidateValidationError("synthesis sources use another immutable baseline")
    candidate = CandidateBundle(
        baseline.digest,
        tuple(entries),
        tuple(deleted_paths),
        tuple(item.digest for item in source_bundles),
    )
    _validate_candidate(baseline, candidate)
    return candidate


def source_candidate_artifact_path(
    run_id: str, task_id: str, attempt: int, round_number: int,
    seat_id: str,
) -> str:
    """Name one immutable final-seat snapshot before terminal evidence is published."""
    for value, label in ((run_id, "run"), (task_id, "task"), (seat_id, "seat")):
        _answer_identity(value, label)
    if (isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1
            or isinstance(round_number, bool) or not isinstance(round_number, int)
            or not 1 <= round_number <= 3):
        raise CandidateValidationError("candidate source round identity is invalid")
    scope = hashlib.sha256(canonical_json([run_id, task_id, attempt, round_number])).hexdigest()[:24]
    seat = hashlib.sha256(seat_id.encode("utf-8")).hexdigest()[:16]
    return f"results/source-candidates/{scope}/{seat}/candidate.json"


def candidate_result_path(task_id: str, candidate_digest: str, evidence_digest: str) -> str:
    """Bind a delivered candidate manifest to its fresh verifier receipt."""
    _answer_identity(task_id, "task")
    _digest(candidate_digest, "candidate result digest")
    _digest(evidence_digest, "candidate verification evidence digest")
    task_key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
    return f"results/{task_key}/{evidence_digest}/{candidate_digest}.json"


def verify_candidate(
    baseline: RepositoryBaseline,
    candidate: CandidateBundle,
    checks: Sequence[PlanCheckV1],
    *,
    controller: LifecycleController,
    environment: Mapping[str, str] | None = None,
    reconciliation: CandidateReconciliationBinding | None = None,
) -> CandidateVerification:
    """Materialize and check *candidate* in a newly created verifier workspace every time."""
    _baseline(baseline)
    _validate_candidate(baseline, candidate)
    immutable_checks = _checks(checks)
    immutable_environment = _environment(immutable_checks, environment)
    if reconciliation is not None and not isinstance(reconciliation, CandidateReconciliationBinding):
        raise CandidateValidationError("candidate reconciliation binding is invalid")
    try:
        assert_controller(controller)
    except Exception as error:
        raise CandidateVerificationError("candidate verification requires an authenticated controller") from error
    root = _controller_verifier_root(baseline, controller)
    transaction_root = Path(tempfile.mkdtemp(prefix=".fanout-verify-", dir=root)).resolve()
    try:
        seat_id = f"verify-{uuid.uuid4().hex[:16]}"
        workspace = create_seat_workspace(baseline, transaction_root, seat_id).root.resolve()
        _apply_candidate(baseline, candidate, workspace)
        outcomes, failure = _run_checks(workspace, immutable_checks, immutable_environment)
    except CandidateVerificationError:
        raise
    except Exception as error:
        raise CandidateVerificationError("candidate verifier workspace could not be created") from error
    payload = _receipt_payload(
        candidate=candidate,
        baseline_digest=baseline.digest,
        checks=immutable_checks,
        environment=immutable_environment,
        valid=failure is None,
        outcomes=outcomes,
        failure=failure,
        workspace=workspace,
        verifier_root=transaction_root,
        reconciliation=reconciliation,
    )
    try:
        evidence_name, evidence_digest = write_evidence(controller, "candidate-verification", payload)
    except Exception as error:
        raise CandidateVerificationError("candidate verification evidence could not be persisted") from error
    return CandidateVerification(
        candidate=candidate,
        candidate_digest=candidate.digest,
        baseline_digest=baseline.digest,
        checks_digest=_checks_digest(immutable_checks),
        environment_digest=_environment_digest(immutable_environment),
        checks=immutable_checks,
        environment=immutable_environment,
        valid=failure is None,
        outcomes=outcomes,
        failure=failure,
        workspace=workspace,
        verifier_root=transaction_root,
        controller_id=controller.controller_id,
        evidence_name=evidence_name,
        evidence_digest=evidence_digest,
        reconciliation=reconciliation,
        _issuer=_RECEIPT_ISSUER,
    )


def validate_candidate_verification(
    controller: LifecycleController,
    receipt: CandidateVerification,
) -> None:
    """Authenticate an issued receipt against its controller-owned durable evidence."""
    if not isinstance(receipt, CandidateVerification):
        raise CandidateValidationError("verification receipt is invalid")
    try:
        assert_controller(controller)
        if receipt.controller_id != controller.controller_id:
            raise CandidateValidationError("verification receipt belongs to another controller")
        persisted = read_evidence(
            controller,
            "candidate-verification",
            receipt.evidence_name,
            receipt.evidence_digest,
        )
    except CandidateValidationError:
        raise
    except Exception as error:
        raise CandidateValidationError("verification receipt evidence is unavailable") from error
    expected = _receipt_payload(
        candidate=receipt.candidate,
        baseline_digest=receipt.baseline_digest,
        checks=receipt.checks,
        environment=receipt.environment,
        valid=receipt.valid,
        outcomes=receipt.outcomes,
        failure=receipt.failure,
        workspace=receipt.workspace,
        verifier_root=receipt.verifier_root,
        reconciliation=receipt.reconciliation,
    )
    if canonical_json(persisted) != canonical_json(expected):
        raise CandidateValidationError("verification receipt evidence does not match the receipt")


def load_candidate_verification(
    controller: LifecycleController,
    artifacts: ArtifactStore,
    *,
    task_id: str,
    candidate_ref: ArtifactRef,
) -> CandidateVerification:
    """Reconstruct one fresh verifier receipt from a delivered candidate manifest."""
    _answer_identity(task_id, "task")
    _answer_artifact_ref(candidate_ref)
    parts = PurePosixPath(candidate_ref.path).parts
    if len(parts) < 4:
        raise CandidateValidationError("candidate result lacks a verifier evidence path")
    evidence_digest = parts[-2]
    _digest(evidence_digest, "candidate verification evidence digest")
    if candidate_ref.path != candidate_result_path(task_id, candidate_ref.digest, evidence_digest):
        raise CandidateValidationError("candidate result is not bound to verifier evidence")
    try:
        candidate = CandidateBundle.from_manifest(artifacts.read_bytes(candidate_ref))
        payload = read_evidence(
            controller, "candidate-verification", f"{evidence_digest}.json", evidence_digest,
        )
        fields = {
            "candidate_digest", "baseline_digest", "checks_digest", "environment_digest",
            "checks", "environment", "valid", "outcomes", "failure",
            "workspace", "verifier_root",
        }
        if set(payload) not in (fields, fields | {"reconciliation"}) or any(not isinstance(payload[key], list) for key in (
            "checks", "environment", "outcomes",
        )):
            raise CandidateValidationError("candidate verifier receipt schema is invalid")
        receipt = CandidateVerification(
            candidate=candidate,
            candidate_digest=payload["candidate_digest"],
            baseline_digest=payload["baseline_digest"],
            checks_digest=payload["checks_digest"],
            environment_digest=payload["environment_digest"],
            checks=_receipt_checks(payload["checks"]),
            environment=_receipt_environment(payload["environment"]),
            valid=payload["valid"],
            outcomes=_receipt_outcomes(payload["outcomes"]),
            failure=payload["failure"],
            workspace=Path(payload["workspace"]),
            verifier_root=Path(payload["verifier_root"]),
            controller_id=controller.controller_id,
            evidence_name=f"{evidence_digest}.json",
            evidence_digest=evidence_digest,
            reconciliation=(None if "reconciliation" not in payload else
                            CandidateReconciliationBinding.from_dict(payload["reconciliation"])),
            _issuer=_RECEIPT_ISSUER,
        )
        validate_candidate_verification(controller, receipt)
        return receipt
    except CandidateValidationError:
        raise
    except Exception as error:
        raise CandidateValidationError("candidate verifier receipt cannot be restored") from error


def _receipt_payload(
    *,
    candidate: CandidateBundle,
    baseline_digest: str,
    checks: tuple[PlanCheckV1, ...],
    environment: tuple[tuple[str, str], ...],
    valid: bool,
    outcomes: tuple[CheckOutcome, ...],
    failure: str | None,
    workspace: Path,
    verifier_root: Path,
    reconciliation: CandidateReconciliationBinding | None = None,
) -> dict[str, object]:
    payload = {
        "candidate_digest": candidate.digest,
        "baseline_digest": baseline_digest,
        "checks_digest": _checks_digest(checks),
        "environment_digest": _environment_digest(environment),
        "checks": [check.to_dict() for check in checks],
        "environment": [[name, value] for name, value in environment],
        "valid": valid,
        "outcomes": [
            {
                "index": outcome.index,
                "argv_digest": outcome.argv_digest,
                "status": outcome.status,
                "returncode": outcome.returncode,
                "artifacts": [
                    {"path": artifact.path, "digest": artifact.digest, "size": artifact.size}
                    for artifact in outcome.artifacts
                ],
                "failure": outcome.failure,
            }
            for outcome in outcomes
        ],
        "failure": failure,
        "workspace": os.fspath(workspace),
        "verifier_root": os.fspath(verifier_root),
    }
    if reconciliation is not None:
        payload["reconciliation"] = reconciliation.to_dict()
    return payload


def _apply_candidate(
    baseline: RepositoryBaseline,
    candidate: CandidateBundle,
    destination: Path | str | int,
    *,
    before_operation: Callable[[str, str], None] | None = None,
    operation: Callable[[str, str], None] | None = None,
    mutation_guard: Callable[[str, str], None] | None = None,
    quarantine: Callable[[int, str, os.stat_result, str], None] | None = None,
    created: Callable[[str, os.stat_result], None] | None = None,
    directory_mode: Callable[[int, str, os.stat_result, int, str], None] | None = None,
) -> None:
    """Apply a validated delta through no-follow descriptors; used only in isolated/staged roots."""
    _validate_candidate(baseline, candidate)
    with _opened_root(destination) as root_fd:
        info = os.fstat(root_fd)
        if not stat.S_ISDIR(info.st_mode):
            raise CandidateVerificationError("candidate destination is not a real directory")
        for path in sorted(candidate.deleted_paths, key=lambda value: (-len(PurePosixPath(value).parts), _path_bytes(value))):
            if before_operation is not None:
                before_operation("delete", path)
            guard = _mutation_guard(mutation_guard, "delete", path)
            _remove_path(root_fd, path, guard=guard, quarantine=quarantine)
            if operation is not None:
                operation("delete", path)
        directories = tuple(entry for entry in candidate.entries if entry.kind == "directory")
        for entry in sorted(directories, key=lambda item: (len(PurePosixPath(item.path).parts), _path_bytes(item.path))):
            if before_operation is not None:
                before_operation("directory-prepare", entry.path)
            guard = _mutation_guard(mutation_guard, "directory-prepare", entry.path)
            _write_directory(
                root_fd,
                entry,
                mode=0o700,
                guard=guard,
                quarantine=quarantine,
                created=created,
                directory_mode=directory_mode,
            )
            if operation is not None:
                operation("directory-prepare", entry.path)
        for entry in candidate.entries:
            if entry.kind == "directory":
                continue
            if before_operation is not None:
                before_operation("write", entry.path)
            guard = _mutation_guard(mutation_guard, "write", entry.path)
            _write_entry(root_fd, entry, guard=guard, quarantine=quarantine, created=created)
            if operation is not None:
                operation("write", entry.path)
        for entry in sorted(directories, key=lambda item: (-len(PurePosixPath(item.path).parts), _path_bytes(item.path))):
            if before_operation is not None:
                before_operation("directory-mode", entry.path)
            guard = _mutation_guard(mutation_guard, "directory-mode", entry.path)
            _set_directory_mode(root_fd, entry, guard=guard, directory_mode=directory_mode)
            if operation is not None:
                operation("directory-mode", entry.path)
        _assert_candidate_applied(root_fd, candidate)


def _validate_candidate(baseline: RepositoryBaseline, candidate: CandidateBundle) -> None:
    _baseline(baseline)
    if not isinstance(candidate, CandidateBundle):
        raise CandidateValidationError("candidate must be a CandidateBundle")
    if candidate.baseline_digest != baseline.digest:
        raise CandidateValidationError("candidate baseline digest does not match the immutable baseline")
    baseline_entries = {entry.path: entry for entry in baseline.entries}
    baseline_directories = _baseline_directories(baseline)
    total = 0
    for entry in candidate.entries:
        previous = baseline_entries.get(entry.path)
        if previous is not None and _same_entry(previous, entry):
            raise CandidateValidationError("candidate records an unchanged baseline path")
        if previous is not None and previous.kind != entry.kind and entry.path not in candidate.deleted_paths:
            raise CandidateValidationError("candidate structural replacement must declare its baseline deletion")
        if (
            previous is None
            and entry.path in baseline_directories
            and entry.kind != "directory"
            and entry.path not in candidate.deleted_paths
        ):
            raise CandidateValidationError("candidate structural replacement must declare its baseline deletion")
        _candidate_budget(baseline, entry, total)
        total += len(entry.data)
    for path in candidate.deleted_paths:
        if path not in baseline_entries and path not in baseline_directories:
            raise CandidateValidationError("candidate deletes a path absent from the immutable baseline")
    by_path = {entry.path: entry for entry in candidate.entries}
    for path in set(candidate.deleted_paths) & set(by_path):
        previous = baseline_entries.get(path)
        replacement = by_path[path]
        if previous is not None and previous.kind == replacement.kind:
            raise CandidateValidationError("candidate redundantly deletes and writes one baseline entry")
        if previous is None and replacement.kind == "directory":
            raise CandidateValidationError("candidate redundantly deletes and writes one baseline directory")
    for path in set(candidate.deleted_paths) | set(by_path):
        for index in range(1, len(PurePosixPath(path).parts)):
            ancestor = "/".join(PurePosixPath(path).parts[:index])
            ancestor_entry = by_path.get(ancestor)
            if (
                ancestor in candidate.deleted_paths
                and (ancestor_entry is None or ancestor_entry.kind != "directory")
            ):
                continue
            if ancestor_entry is None and ancestor in baseline_directories:
                continue
            if ancestor_entry is None or ancestor_entry.kind != "directory":
                raise CandidateValidationError("candidate must explicitly represent every parent directory")


def _run_checks(
    workspace: Path,
    checks: tuple[PlanCheckV1, ...],
    environment: tuple[tuple[str, str], ...],
) -> tuple[tuple[CheckOutcome, ...], str | None]:
    outcomes: list[CheckOutcome] = []
    supplied = dict(environment)
    for index, check in enumerate(checks):
        argv_digest = hashlib.sha256(canonical_json(list(check.argv))).hexdigest()
        try:
            cwd = _check_cwd(workspace, check.cwd)
            overrides = {name: supplied[name] for name in check.env_allowlist if name in supplied}
            result = run_command(
                ProcessCommand(
                    argv=check.argv,
                    stdin=b"",
                    cwd=cwd,
                    timeout=float(check.timeout),
                    environment=overrides,
                    slot_root=workspace.parent / ".fanout-check-slots",
                ),
                base_environment={},
            )
        except Exception as error:
            failure = f"check {index} could not run: {type(error).__name__}"
            outcomes.append(CheckOutcome(index, argv_digest, ProcessStatus.SPAWN_ERROR.value, None, failure=failure))
            return tuple(outcomes), failure
        if result.status is not ProcessStatus.EXIT or result.returncode not in check.accepted_exit_codes:
            failure = f"check {index} failed with {result.status.value} ({result.returncode!r})"
            outcomes.append(CheckOutcome(index, argv_digest, result.status.value, result.returncode, failure=failure))
            return tuple(outcomes), failure
        try:
            artifacts = _expected_artifacts(workspace, check.expected_artifacts)
        except CandidateValidationError as error:
            failure = f"check {index} expected artifact failed: {error}"
            outcomes.append(CheckOutcome(index, argv_digest, result.status.value, result.returncode, failure=failure))
            return tuple(outcomes), failure
        outcomes.append(CheckOutcome(index, argv_digest, result.status.value, result.returncode, artifacts))
    return tuple(outcomes), None


def _expected_artifacts(root: Path, paths: tuple[str, ...]) -> tuple[CheckArtifact, ...]:
    artifacts: list[CheckArtifact] = []
    for path in paths:
        entry = _read_entry(root, path)
        if entry is None or entry.kind != "file":
            raise CandidateValidationError(f"expected artifact is not a regular contained file: {path}")
        artifacts.append(CheckArtifact(path, entry.digest, len(entry.data)))
    return tuple(artifacts)


def _checks(value: Sequence[PlanCheckV1]) -> tuple[PlanCheckV1, ...]:
    if isinstance(value, (str, bytes)):
        raise CandidateValidationError("verifier checks must be a sequence")
    checks = tuple(value)
    if not checks:
        raise CandidateValidationError("candidate verification requires at least one immutable argv check")
    if any(not isinstance(item, PlanCheckV1) for item in checks):
        raise CandidateValidationError("verifier checks must contain PlanCheckV1 values")
    return tuple(PlanCheckV1.from_dict(item.to_dict(), where=f"verification checks[{index}]")
                 for index, item in enumerate(checks))


def _environment(checks: tuple[PlanCheckV1, ...], value: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise CandidateValidationError("verification environment must be a mapping")
    allowed = {name for check in checks for name in check.env_allowlist}
    entries: list[tuple[str, str]] = []
    for name, item in value.items():
        if not isinstance(name, str) or not isinstance(item, str) or "\x00" in name or "\x00" in item:
            raise CandidateValidationError("verification environment contains an invalid value")
        if name not in allowed:
            raise CandidateValidationError("verification environment contains a value outside the immutable allowlist")
        entries.append((name, item))
    return tuple(sorted(entries))


def _checks_digest(checks: tuple[PlanCheckV1, ...]) -> str:
    return hashlib.sha256(canonical_json([check.to_dict() for check in checks])).hexdigest()


def _environment_digest(environment: tuple[tuple[str, str], ...]) -> str:
    return hashlib.sha256(canonical_json([[name, value] for name, value in environment])).hexdigest()


def _workspace_root(baseline: RepositoryBaseline, workspace: SeatWorkspace | Path | str) -> Path:
    if isinstance(workspace, SeatWorkspace):
        if workspace.baseline_digest != baseline.digest:
            raise CandidateValidationError("seat workspace belongs to another immutable baseline")
        raw_root = workspace.root
    else:
        raw_root = Path(workspace)
    try:
        info = raw_root.lstat()
    except OSError as error:
        raise CandidateValidationError("candidate workspace must be an existing directory") from error
    if raw_root.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise CandidateValidationError("candidate workspace must be a real directory")
    return raw_root.resolve()


def _controller_verifier_root(baseline: RepositoryBaseline, controller: LifecycleController) -> Path:
    root = controller_directory(controller, "verifier").resolve()
    repository = baseline.repository.resolve()
    try:
        root.relative_to(repository)
    except ValueError:
        try:
            repository.relative_to(root)
        except ValueError:
            return root
    raise CandidateVerificationError("verifier root must not contain the caller repository")


def _scan_workspace_safety(root: Path) -> dict[str, int]:
    directories: dict[str, int] = {}
    root_fd = _open_root(root)
    try:
        _scan_directory(root_fd, (), directories, top_level=True)
    finally:
        os.close(root_fd)
    return directories


def _scan_directory(fd: int, prefix: tuple[str, ...], directories: dict[str, int], *, top_level: bool) -> None:
    for name in sorted(os.listdir(fd), key=os.fsencode):
        if top_level and name == ".git":
            continue
        path = "/".join((*prefix, name))
        if ".git" in PurePosixPath(path).parts:
            raise CandidateValidationError("candidate workspace contains a Git metadata path")
        try:
            information = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError as error:
            raise CandidateValidationError("candidate workspace changed while it was collected") from error
        if stat.S_ISDIR(information.st_mode):
            directories[path] = stat.S_IMODE(information.st_mode)
            child = _open_directory(fd, name)
            try:
                _scan_directory(child, (*prefix, name), directories, top_level=False)
            finally:
                os.close(child)
        elif stat.S_ISREG(information.st_mode):
            continue
        elif stat.S_ISLNK(information.st_mode):
            try:
                target = os.fsencode(os.readlink(name, dir_fd=fd))
            except OSError as error:
                raise CandidateValidationError("candidate workspace changed while it was collected") from error
            _link_target(path, target)
        else:
            raise CandidateValidationError(f"candidate workspace contains a special file: {path}")


def _read_entry(root: Path | str | int, path: str, *, directory_is_absent: bool = False) -> CandidateEntry | None:
    parts = PurePosixPath(path).parts
    with _opened_root(root) as root_fd:
        try:
            parent_fd = _open_parent(root_fd, parts, create=False)
        except FileNotFoundError:
            return None
        except CandidateValidationError as error:
            cause = error.__cause__
            if isinstance(cause, OSError) and cause.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                return None
            raise
        try:
            try:
                information = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if stat.S_ISREG(information.st_mode):
                try:
                    file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
                except OSError as error:
                    if error.errno in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                        raise CandidateValidationError("candidate workspace changed while it was collected") from error
                    raise
                try:
                    opened = os.fstat(file_fd)
                    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (information.st_dev, information.st_ino):
                        raise CandidateValidationError("candidate workspace changed while it was collected")
                    data = _read_all(file_fd)
                finally:
                    os.close(file_fd)
                return CandidateEntry(path, "file", stat.S_IMODE(opened.st_mode), data)
            if stat.S_ISLNK(information.st_mode):
                try:
                    target = os.fsencode(os.readlink(parts[-1], dir_fd=parent_fd))
                    current = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                except OSError as error:
                    raise CandidateValidationError("candidate workspace changed while it was collected") from error
                if (current.st_dev, current.st_ino) != (information.st_dev, information.st_ino):
                    raise CandidateValidationError("candidate workspace changed while it was collected")
                return CandidateEntry(path, "symlink", stat.S_IMODE(current.st_mode), target)
            if stat.S_ISDIR(information.st_mode):
                if directory_is_absent:
                    return None
                return CandidateEntry(path, "directory", stat.S_IMODE(information.st_mode), b"")
            raise CandidateValidationError(f"candidate workspace contains a special file: {path}")
        finally:
            os.close(parent_fd)


def _mutation_guard(
    guard: Callable[[str, str], None] | None,
    action: str,
    path: str,
) -> Callable[[], None] | None:
    if guard is None:
        return None
    return lambda: guard(action, path)


def _remove_path(
    root: Path | str | int,
    path: str,
    *,
    guard: Callable[[], None] | None = None,
    quarantine: Callable[[int, str, os.stat_result, str], None] | None = None,
) -> None:
    parts = PurePosixPath(path).parts
    try:
        with _opened_root(root) as root_fd:
            parent_fd = _open_parent(root_fd, parts, create=False)
            try:
                try:
                    information = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError as error:
                    raise CandidateVerificationError(f"candidate delete path is missing: {path}") from error
                if guard is not None:
                    guard()
                if quarantine is not None:
                    quarantine(parent_fd, parts[-1], information, path)
                    return
                if stat.S_ISDIR(information.st_mode):
                    try:
                        os.rmdir(parts[-1], dir_fd=parent_fd)
                    except OSError as error:
                        raise CandidateVerificationError(f"candidate delete path is a non-empty directory: {path}") from error
                else:
                    os.unlink(parts[-1], dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    except CandidateVerificationError:
        raise
    except OSError as error:
        raise CandidateVerificationError(f"candidate delete failed: {path}") from error


def _write_entry(
    root: Path | str | int,
    entry: CandidateEntry,
    *,
    guard: Callable[[], None] | None = None,
    quarantine: Callable[[int, str, os.stat_result, str], None] | None = None,
    created: Callable[[str, os.stat_result], None] | None = None,
) -> None:
    if entry.kind == "directory":
        raise CandidateVerificationError("directory entries require directory materialization")
    parts = PurePosixPath(entry.path).parts
    try:
        with _opened_root(root) as root_fd:
            parent_fd = _open_parent(root_fd, parts, create=True)
            try:
                _remove_existing(parent_fd, parts[-1], entry.path, guard=guard, quarantine=quarantine)
                if entry.kind == "symlink":
                    os.symlink(entry.data, os.fsencode(parts[-1]), dir_fd=parent_fd)
                    information = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                    if not stat.S_ISLNK(information.st_mode):
                        raise CandidateVerificationError(f"candidate write target is not a symlink: {entry.path}")
                    if created is not None:
                        created(entry.path, information)
                    _set_symlink_mode(parent_fd, parts[-1], entry.mode, entry.path)
                else:
                    file_fd = os.open(
                        parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600, dir_fd=parent_fd,
                    )
                    try:
                        opened = os.fstat(file_fd)
                        if not stat.S_ISREG(opened.st_mode):
                            raise CandidateVerificationError(f"candidate write target is not a file: {entry.path}")
                        if created is not None:
                            created(entry.path, opened)
                        _write_all(file_fd, entry.data)
                        os.fchmod(file_fd, entry.mode)
                        os.fsync(file_fd)
                    finally:
                        os.close(file_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    except CandidateVerificationError:
        raise
    except OSError as error:
        raise CandidateVerificationError(f"candidate write failed: {entry.path}") from error


def _write_directory(
    root: Path | str | int,
    entry: CandidateEntry,
    *,
    mode: int,
    guard: Callable[[], None] | None = None,
    quarantine: Callable[[int, str, os.stat_result, str], None] | None = None,
    created: Callable[[str, os.stat_result], None] | None = None,
    directory_mode: Callable[[int, str, os.stat_result, int, str], None] | None = None,
) -> None:
    parts = PurePosixPath(entry.path).parts
    try:
        with _opened_root(root) as root_fd:
            parent_fd = _open_parent(root_fd, parts, create=True)
            try:
                created_directory = False
                try:
                    information = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    information = None
                    if guard is not None:
                        guard()
                    os.mkdir(parts[-1], mode=mode, dir_fd=parent_fd)
                    created_directory = True
                else:
                    if not stat.S_ISDIR(information.st_mode):
                        _remove_existing(
                            parent_fd,
                            parts[-1],
                            entry.path,
                            guard=guard,
                            quarantine=quarantine,
                            information=information,
                        )
                        os.mkdir(parts[-1], mode=mode, dir_fd=parent_fd)
                        created_directory = True
                    else:
                        if guard is not None:
                            guard()
                if created_directory:
                    information = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(information.st_mode):
                        raise CandidateVerificationError(f"candidate directory changed during write: {entry.path}")
                    if created is not None:
                        created(entry.path, information)
                assert information is not None
                if directory_mode is not None:
                    directory_mode(parent_fd, parts[-1], information, mode, entry.path)
                else:
                    directory_fd = os.open(
                        parts[-1],
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent_fd,
                    )
                    try:
                        opened = os.fstat(directory_fd)
                        if (opened.st_dev, opened.st_ino) != (information.st_dev, information.st_ino):
                            raise CandidateVerificationError(f"candidate directory changed during write: {entry.path}")
                        _set_bound_directory_mode(
                            parent_fd,
                            parts[-1],
                            directory_fd,
                            opened,
                            mode,
                            entry.path,
                            "write",
                        )
                    finally:
                        os.close(directory_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    except CandidateVerificationError:
        raise
    except OSError as error:
        raise CandidateVerificationError(f"candidate directory write failed: {entry.path}") from error


def _set_directory_mode(
    root: Path | str | int,
    entry: CandidateEntry,
    *,
    guard: Callable[[], None] | None = None,
    directory_mode: Callable[[int, str, os.stat_result, int, str], None] | None = None,
) -> None:
    parts = PurePosixPath(entry.path).parts
    try:
        with _opened_root(root) as root_fd:
            parent_fd = _open_parent(root_fd, parts, create=False)
            try:
                information = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISDIR(information.st_mode):
                    raise CandidateVerificationError(f"candidate directory mode target is not a directory: {entry.path}")
                if guard is not None:
                    guard()
                if directory_mode is not None:
                    directory_mode(parent_fd, parts[-1], information, entry.mode, entry.path)
                    os.fsync(parent_fd)
                    return
                directory_fd = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                try:
                    opened = os.fstat(directory_fd)
                    if (opened.st_dev, opened.st_ino) != (information.st_dev, information.st_ino):
                        raise CandidateVerificationError(f"candidate directory changed during mode update: {entry.path}")
                    _set_bound_directory_mode(
                        parent_fd,
                        parts[-1],
                        directory_fd,
                        opened,
                        entry.mode,
                        entry.path,
                        "mode update",
                    )
                finally:
                    os.close(directory_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    except CandidateValidationError:
        raise
    except OSError as error:
        raise CandidateVerificationError(f"candidate directory mode failed: {entry.path}") from error


def _set_bound_directory_mode(
    parent_fd: int,
    name: str,
    directory_fd: int,
    opened: os.stat_result,
    mode: int,
    path: str,
    action: str,
) -> None:
    """Change mode only across a name-to-fd identity interval, reverting a stale fd."""
    original_mode = stat.S_IMODE(opened.st_mode)
    os.fchmod(directory_fd, mode)
    os.fsync(directory_fd)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        os.fchmod(directory_fd, original_mode)
        os.fsync(directory_fd)
        raise CandidateVerificationError(f"candidate directory changed during {action}: {path}") from error
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        os.fchmod(directory_fd, original_mode)
        os.fsync(directory_fd)
        raise CandidateVerificationError(f"candidate directory changed during {action}: {path}")


def _remove_existing(
    parent_fd: int,
    name: str,
    path: str,
    *,
    guard: Callable[[], None] | None = None,
    quarantine: Callable[[int, str, os.stat_result, str], None] | None = None,
    information: os.stat_result | None = None,
) -> None:
    if information is None:
        try:
            information = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if guard is not None:
                guard()
            return
    if guard is not None:
        guard()
    if quarantine is not None:
        quarantine(parent_fd, name, information, path)
        return
    if stat.S_ISDIR(information.st_mode):
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError as error:
            raise CandidateVerificationError(f"candidate destination directory is not empty: {path}") from error
    else:
        os.unlink(name, dir_fd=parent_fd)


def _set_symlink_mode(parent_fd: int, name: str, mode: int, path: str) -> None:
    """Set a link's own mode when the host supports it, never following its target."""
    try:
        os.chmod(name, mode, dir_fd=parent_fd, follow_symlinks=False)
    except (NotImplementedError, OSError) as error:
        try:
            information = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as stat_error:
            raise CandidateVerificationError(f"candidate symlink mode could not be verified: {path}") from stat_error
        if not stat.S_ISLNK(information.st_mode) or stat.S_IMODE(information.st_mode) != mode:
            raise CandidateVerificationError(f"candidate symlink mode could not be preserved: {path}") from error
        return
    try:
        information = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise CandidateVerificationError(f"candidate symlink mode could not be verified: {path}") from error
    if not stat.S_ISLNK(information.st_mode) or stat.S_IMODE(information.st_mode) != mode:
        raise CandidateVerificationError(f"candidate symlink mode could not be preserved: {path}")


def _assert_candidate_applied(root: Path | str | int, candidate: CandidateBundle) -> None:
    for path in candidate.deleted_paths:
        replacements = tuple(
            entry for entry in candidate.entries
            if entry.path == path or entry.path.startswith(path + "/")
        )
        if replacements:
            if not any(entry.path == path for entry in replacements) and not _is_directory(root, path):
                raise CandidateVerificationError(f"candidate structural deletion did not apply: {path}")
            continue
        try:
            value = _read_entry(root, path)
        except CandidateValidationError as error:
            if "changed while" in str(error):
                value = None
            else:
                raise CandidateVerificationError("candidate deletion could not be verified") from error
        if value is not None:
            raise CandidateVerificationError(f"candidate deletion did not apply: {path}")
    for expected in candidate.entries:
        try:
            actual = _read_entry(root, expected.path)
        except CandidateValidationError as error:
            raise CandidateVerificationError("candidate write could not be verified") from error
        if actual is None or actual != expected:
            raise CandidateVerificationError(f"candidate write did not apply: {expected.path}")


def _is_directory(root: Path | str | int, path: str) -> bool:
    parts = PurePosixPath(path).parts
    with _opened_root(root) as root_fd:
        try:
            parent_fd = _open_parent(root_fd, parts, create=False)
        except FileNotFoundError:
            return False
        except CandidateValidationError as error:
            cause = error.__cause__
            if isinstance(cause, OSError) and cause.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                return False
            raise
        try:
            try:
                information = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            return stat.S_ISDIR(information.st_mode)
        finally:
            os.close(parent_fd)


def _check_cwd(workspace: Path, relative: str) -> Path:
    candidate = workspace if relative == "" else workspace / Path(relative)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(workspace.resolve())
    except (OSError, ValueError) as error:
        raise CandidateValidationError("immutable check cwd escapes its verifier workspace") from error
    if not resolved.is_dir():
        raise CandidateValidationError("immutable check cwd is not a directory")
    return resolved


def _baseline(value: RepositoryBaseline) -> None:
    if not isinstance(value, RepositoryBaseline):
        raise CandidateValidationError("baseline must be a RepositoryBaseline")


def _candidate_budget(baseline: RepositoryBaseline, entry: CandidateEntry, total: int) -> None:
    size = len(entry.data)
    if size > baseline.limits.max_file_bytes:
        raise CandidateValidationError("candidate file exceeds the immutable baseline file limit")
    if total + size > baseline.limits.max_total_bytes:
        raise CandidateValidationError("candidate delta exceeds the immutable baseline total limit")


def _same_entry(previous: RepositoryEntry, candidate: CandidateEntry) -> bool:
    return previous.kind == candidate.kind and previous.mode == candidate.mode and previous.data == candidate.data


def _baseline_directories(baseline: RepositoryBaseline) -> set[str]:
    return {entry.path for entry in baseline.directories}


def _baseline_directory_modes(baseline: RepositoryBaseline) -> dict[str, int]:
    return {entry.path: entry.mode for entry in baseline.directories}


def _path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise CandidateValidationError(f"{label} must be a non-empty normalized relative POSIX path")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CandidateValidationError(f"{label} must be valid UTF-8") from error
    pure = PurePosixPath(value)
    if any(_git_metadata_alias(part) for part in pure.parts):
        raise CandidateValidationError(f"{label} addresses Git metadata")
    if (
        pure.is_absolute()
        or not pure.parts
        or str(pure) != value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise CandidateValidationError(f"{label} escapes its candidate root")
    return value


def _path_bytes(value: str) -> bytes:
    return value.encode("utf-8")


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_SIZE or any(character not in "0123456789abcdef" for character in value):
        raise CandidateValidationError(f"{label} must be a lower-case SHA-256 digest")
    return value


def _link_target(path: str, target: bytes) -> None:
    if b"\0" in target:
        raise CandidateValidationError("candidate symlink target contains NUL")
    text = os.fsdecode(target)
    if not text or os.path.isabs(text):
        raise CandidateValidationError(f"candidate symlink escapes its root: {path}")
    stack = list(PurePosixPath(path).parts[:-1])
    for part in text.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not stack:
                raise CandidateValidationError(f"candidate symlink escapes its root: {path}")
            stack.pop()
        else:
            stack.append(part)


def _git_metadata_alias(part: str) -> bool:
    return any(
        unicodedata.normalize(form, part).casefold().rstrip(". ") == ".git"
        for form in ("NFC", "NFD", "NFKC", "NFKD")
    )


def _no_entry_overlap(entries: tuple[CandidateEntry, ...]) -> None:
    by_path = {entry.path: entry for entry in entries}
    for path in by_path:
        parts = PurePosixPath(path).parts
        for index in range(1, len(parts)):
            ancestor = by_path.get("/".join(parts[:index]))
            if ancestor is not None and ancestor.kind != "directory":
                raise CandidateValidationError("candidate entries cannot contain a file or symlink ancestor")


def _nul_records(value: bytes) -> tuple[bytes, ...]:
    if not value:
        return ()
    if not value.endswith(b"\0"):
        raise CandidateValidationError("Git returned malformed candidate paths")
    return tuple(item for item in value[:-1].split(b"\0") if item)


def _open_root(root: Path) -> int:
    try:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise CandidateValidationError("candidate root is not a real directory") from error


@contextmanager
def _opened_root(root: Path | str | int) -> Iterator[int]:
    """Yield a private duplicate of either a safe path root or a pinned directory fd."""
    if isinstance(root, bool):
        raise CandidateValidationError("candidate root is not a real directory")
    if isinstance(root, int):
        try:
            descriptor = os.dup(root)
        except OSError as error:
            raise CandidateValidationError("candidate root is not a real directory") from error
    else:
        descriptor = _open_root(Path(root))
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISDIR(information.st_mode):
            raise CandidateValidationError("candidate root is not a real directory")
        yield descriptor
    finally:
        os.close(descriptor)


def _open_directory(parent_fd: int, name: str) -> int:
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as error:
        raise CandidateValidationError("candidate path contains a symlink or non-directory") from error


def _open_parent(root_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = _open_directory(fd, part)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, _READ_CHUNK)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("short candidate write")
        view = view[count:]
