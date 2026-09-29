"""Owner-controlled execution and reconciliation over the durable fanout layers."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, Sequence, runtime_checkable

from .artifacts import ArtifactExistsError, ArtifactRef, ArtifactStore, canonical_json
from .agy_guard import validate_agy_readonly_guard
from .collaboration import CollaborationCoordinator, RoundPolicy, SeatAssignment, TaskPacket
from .controller import LifecycleController, assert_controller
from .errors import (
    CandidateValidationError,
    ArtifactError,
    ExecutionConflictError,
    ExecutionPendingError,
    ExecutionPreflightError,
    ExecutionValidationError,
    FanoutError,
    LifecycleError,
    HandoverError,
    ProviderRequestError,
    RunAuthorizationError,
    RunStateError,
)
from .lifecycle import (
    CollectedCandidate,
    HandoverResult,
    HandoverTransaction,
    RunOwnedPath,
    claim_run_path,
    garbage_collect,
    handover_candidate,
    prepare_handover_transaction,
    recover_handover as recover_handover_transaction,
    validate_handover_disposition,
)
from .plan import FanoutPlanV1, FanoutPlanV2, PlanTaskV1, validate_plan
from .providers import ProviderRegistry, run_provider
from .repo import (
    RepositoryBaseline,
    validate_seat_workspace,
    validate_seat_workspace_baseline,
)
from .runstate import OwnerCapability, RunInputs
from .scheduler import ActionBarrier, PlanAmendment, ReconciledResult, Scheduler, WorkDispatch
from .scheduler_authority import (
    BackendRecord, _authority_read, _record_matches_binding, _validate_backend_record,
)
from .verification import (
    AnswerSynthesisVerification,
    AnswerSelectionVerification,
    CandidateBundle,
    TargetEvidenceEnvelope,
    CandidateReconciliationBinding,
    candidate_result_path,
    source_candidate_artifact_path,
    CandidateVerification,
    load_answer_synthesis,
    load_answer_selection,
    load_candidate_verification,
    load_target_candidate,
    validate_answer_synthesis,
    validate_answer_selection,
    validate_candidate_verification,
    verify_candidate,
    verify_answer_selection,
    verify_answer_synthesis,
)


_SNAPSHOT_SCHEMA = "fanout-execution-v2"
_RECORD_SCHEMA = "fanout-execution-record-v1"
_MAX_PROVIDER_TURNS = 1_000_000
_MAX_IDENTITY_BYTES = 128
_MAX_EXECUTION_TASKS = 4_096
_MAX_ARTIFACT_PATH_BYTES = 4_096
_MAX_EXECUTION_SNAPSHOT_BYTES = 8 * 1024 * 1024
_MAX_EXECUTION_BACKEND_RECORD_BYTES = 8 * 1024 * 1024
_MAX_EXECUTION_STATUS_BYTES = 2 * 1024 * 1024
_PROVIDER_TRANSITION_SCHEMA = "fanout-provider-profile-transition-v1"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ExecutionValidationError(f"{label} must be a bounded identity")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ExecutionValidationError(f"{label} must be valid UTF-8") from error
    if (
        len(encoded) > _MAX_IDENTITY_BYTES
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ExecutionValidationError(f"{label} must be a bounded identity")
    return value


def _callable_identity(value: object) -> tuple[object | None, object] | None:
    if not callable(value):
        return None
    owner = getattr(value, "__self__", None)
    implementation = getattr(value, "__func__", None)
    if implementation is not None:
        return owner, implementation
    if isinstance(value, type):
        return None, value
    if hasattr(value, "__code__"):
        return None, value
    return value, getattr(type(value), "__call__")


def _same_callable(
    expected: tuple[object | None, object] | None,
    observed: object,
) -> bool:
    current = _callable_identity(observed)
    return (
        expected is not None
        and current is not None
        and expected[0] is current[0]
        and expected[1] is current[1]
    )


def _provider_profiles_digest(profiles: Mapping[str, str]) -> str:
    return _digest(canonical_json({
        "profiles": dict(sorted(profiles.items())),
        "schema_version": "fanout-provider-profiles-v1",
    }))


def _registry_binding(
    registry: object,
    profiles: Mapping[str, str],
) -> tuple[tuple[object | None, object], tuple[tuple[str, object], ...]]:
    observed = getattr(registry, "profile_digests", None)
    require = getattr(registry, "require", None)
    require_identity = _callable_identity(require)
    if (
        not isinstance(observed, Mapping)
        or dict(observed) != dict(profiles)
        or require_identity is None
    ):
        raise ExecutionPreflightError(
            "provider profile transition registry identity changed"
        )
    adapters: list[tuple[str, object]] = []
    try:
        from .providers import ProviderRegistry, parse_profile_binding_key
        profiled = isinstance(registry, ProviderRegistry)
        executor_ids = set()
        for key in profiles:
            executor_id = parse_profile_binding_key(key)[0] if profiled else key
            executor_ids.add(executor_id)
        for executor_id in sorted(executor_ids):
            if executor_id == "maka":
                raise ExecutionPreflightError(
                    "provider profile transition cannot admit Maka as an executor"
                )
            adapters.append((executor_id, require(executor_id)))
    except ExecutionPreflightError:
        raise
    except Exception as error:
        raise ExecutionPreflightError(
            "provider profile transition registry admission failed"
        ) from error
    return require_identity, tuple(adapters)


def _artifact(
    value: object,
    *,
    maximum_path_bytes: int = _MAX_ARTIFACT_PATH_BYTES,
) -> ArtifactRef:
    if not isinstance(value, ArtifactRef):
        raise ExecutionValidationError("execution artifact reference is invalid")
    pure = PurePosixPath(value.path)
    if (
        not value.path
        or "\\" in value.path
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or len(value.path.encode("utf-8")) > maximum_path_bytes
        or not _is_digest(value.digest)
        or isinstance(value.size, bool)
        or not isinstance(value.size, int)
        or value.size < 0
    ):
        raise ExecutionValidationError("execution artifact reference is invalid")
    return value


def _artifact_document(ref: ArtifactRef) -> dict[str, object]:
    return {"digest": ref.digest, "path": ref.path, "size": ref.size}


def validate_target_candidate_binding(
    candidate: TargetEvidenceEnvelope, *, task_id: str, plan: FanoutPlanV2,
    inputs: RunInputs, store: ArtifactStore | None = None,
    controller: LifecycleController | None = None,
) -> CandidateBundle:
    """Accept only controller-issued candidate bytes for one admitted target task."""
    if (
        not isinstance(candidate, TargetEvidenceEnvelope)
        or not isinstance(plan, FanoutPlanV2)
        or not isinstance(inputs, RunInputs) or inputs.targets is None
        or inputs.compiled_plan_sha256 != _digest(canonical_json(plan.to_dict()))
        or not isinstance(store, ArtifactStore)
        or not isinstance(controller, LifecycleController)
    ):
        raise ExecutionValidationError("target candidate requires controller-sealed v2 evidence")
    if candidate.task_id != task_id:
        raise ExecutionValidationError("target candidate differs from submitted task")
    try:
        issued = load_target_candidate(
            candidate, plan=plan, inputs=inputs, store=store,
            controller=controller,
        )
    except CandidateValidationError as error:
        raise ExecutionValidationError("target candidate controller evidence differs") from error
    task = next((item for item in plan.tasks if item.id == task_id and item.kind == "work"), None)
    binding = inputs.targets.get(issued.target_id)
    if (
        task is None or task.target_id != issued.target_id
        or binding is None or binding.spec not in plan.targets
        or issued.repository != binding.spec.repository
        or issued.branch_ref != binding.spec.branch_ref
        or issued.base_oid != binding.base_oid
        or issued.baseline_sha256 != binding.baseline_sha256
        or issued.candidate.baseline_digest != binding.baseline_sha256
    ):
        raise ExecutionValidationError("target candidate differs from task or v3 target binding")
    return issued.candidate


def _affected_amendment(
    old_plan: FanoutPlanV1,
    new_plan: FanoutPlanV1,
    old_inputs: RunInputs,
    new_inputs: RunInputs,
    revision: int,
) -> PlanAmendment:
    old_tasks = {task.id: task for task in old_plan.tasks}
    new_tasks = {task.id: task for task in new_plan.tasks}
    changed = {
        task_id for task_id in set(old_tasks) | set(new_tasks)
        if old_tasks.get(task_id) != new_tasks.get(task_id)
    }
    old_order = [task.id for task in old_plan.tasks if task.kind == "work"]
    new_order = [task.id for task in new_plan.tasks if task.kind == "work"]
    positions = {task_id: index for index, task_id in enumerate(new_order)}
    common = [task_id for task_id in old_order if task_id in positions]
    for index, left in enumerate(common):
        for right in common[index + 1:]:
            if positions[left] > positions[right]:
                changed.update((left, right))

    affected: set[str]
    if (
        old_plan.schema_version != new_plan.schema_version
        or old_plan.source != new_plan.source
        or old_plan.defaults != new_plan.defaults
        or old_plan.source_steps != new_plan.source_steps
    ):
        affected = {
            task.id for task in (*old_plan.tasks, *new_plan.tasks)
            if task.kind == "work"
        }
    else:
        affected = set()
        for plan, tasks in ((old_plan, old_tasks), (new_plan, new_tasks)):
            for task in plan.tasks:
                if task.kind != "work":
                    continue
                parent_id = task.parent_id
                changed_ancestor = False
                while parent_id is not None:
                    if parent_id in changed:
                        changed_ancestor = True
                        break
                    parent_id = tasks[parent_id].parent_id
                if task.id in changed or changed_ancestor:
                    affected.add(task.id)

    new_work = {task.id: task for task in new_plan.tasks if task.kind == "work"}
    if (
        old_inputs.compiler_sha256 != new_inputs.compiler_sha256
        or old_inputs.parser_sha256 != new_inputs.parser_sha256
    ):
        affected.update(new_work)
    else:
        affected.update(
            task_id
            for task_id in set(old_inputs.skill_manifests) | set(new_inputs.skill_manifests)
            if old_inputs.skill_manifests.get(task_id)
            != new_inputs.skill_manifests.get(task_id)
        )
        from .providers import profile_binding_changed
        for task in new_work.values():
            policy = task.provider_policy or new_plan.defaults
            if (task.execution_class != "orchestrator-action" and any(
                profile_binding_changed(old_inputs.provider_profiles, new_inputs.provider_profiles,
                                        executor, task.execution_class, policy.quality_tier,
                                        old_shape=old_inputs.profile_shape,
                                        new_shape=new_inputs.profile_shape)
                for executor in policy.executor_ids)):
                affected.add(task.id)

    ordered = tuple(
        task.id for task in new_plan.tasks
        if task.kind == "work" and task.id in affected
    )
    ordered += tuple(
        task.id for task in old_plan.tasks
        if task.kind == "work" and task.id in affected and task.id not in new_tasks
    )
    return PlanAmendment(revision, ordered, _digest(canonical_json(new_plan.to_dict())))


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    """Owner-approved upper bound on billable provider turns for one run."""

    max_provider_turns: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_provider_turns, bool)
            or not isinstance(self.max_provider_turns, int)
            or not 1 <= self.max_provider_turns <= _MAX_PROVIDER_TURNS
        ):
            raise ExecutionValidationError("provider turn budget must be a bounded positive integer")


@dataclass(frozen=True, slots=True)
class ProviderProfileTransition:
    """One exact no-spend transition between authenticated provider registries."""

    old_plan_sha256: str
    old_inputs_digest: str
    old_profiles_sha256: str
    new_plan_sha256: str
    new_inputs_digest: str
    new_profiles_sha256: str
    expected_plan_revision: int
    service_sha256: str
    schema_version: str = _PROVIDER_TRANSITION_SCHEMA
    _registry: object = field(default=None, repr=False, compare=False)
    _registry_require: tuple[object | None, object] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _registry_adapters: tuple[tuple[str, object], ...] = field(
        default=(),
        repr=False,
        compare=False,
    )
    _old_profiles: tuple[tuple[str, str], ...] = field(
        default=(),
        repr=False,
        compare=False,
    )
    _new_profiles: tuple[tuple[str, str], ...] = field(
        default=(),
        repr=False,
        compare=False,
    )
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self.schema_version != _PROVIDER_TRANSITION_SCHEMA
            or any(
                not _is_digest(value)
                for value in (
                    self.old_plan_sha256,
                    self.old_inputs_digest,
                    self.old_profiles_sha256,
                    self.new_plan_sha256,
                    self.new_inputs_digest,
                    self.new_profiles_sha256,
                    self.service_sha256,
                )
            )
            or isinstance(self.expected_plan_revision, bool)
            or not isinstance(self.expected_plan_revision, int)
            or self.expected_plan_revision < 1
            or self._registry_require is None
            or not self._old_profiles
            or not self._new_profiles
        ):
            raise ExecutionValidationError(
                "provider profile transition binding is invalid"
            )


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """Explicit aggregate bounds for execution persistence and public status."""

    max_tasks: int = _MAX_EXECUTION_TASKS
    max_artifact_path_bytes: int = _MAX_ARTIFACT_PATH_BYTES
    max_snapshot_bytes: int = _MAX_EXECUTION_SNAPSHOT_BYTES
    max_backend_record_bytes: int = _MAX_EXECUTION_BACKEND_RECORD_BYTES
    max_status_bytes: int = _MAX_EXECUTION_STATUS_BYTES

    def __post_init__(self) -> None:
        maxima = {
            "max_tasks": _MAX_EXECUTION_TASKS,
            "max_artifact_path_bytes": _MAX_ARTIFACT_PATH_BYTES,
            "max_snapshot_bytes": _MAX_EXECUTION_SNAPSHOT_BYTES,
            "max_backend_record_bytes": _MAX_EXECUTION_BACKEND_RECORD_BYTES,
            "max_status_bytes": _MAX_EXECUTION_STATUS_BYTES,
        }
        for name, maximum in maxima.items():
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= maximum
            ):
                raise ExecutionValidationError(
                    f"execution {name} must be a bounded positive integer"
                )


@dataclass(frozen=True, slots=True)
class TaskPreparation:
    """Immutable, fully admitted context for one non-action scheduler task."""

    packet: TaskPacket
    seats: tuple[SeatAssignment, ...]
    policy: RoundPolicy
    inputs: RunInputs
    plan_revision: int
    registry: object | None = field(default=None, repr=False, compare=False)
    profile_bindings: Mapping[str, tuple[str, int | float]] = field(
        init=False, repr=False, compare=False,
    )
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.packet, TaskPacket)
            or not isinstance(self.policy, RoundPolicy)
            or not isinstance(self.inputs, RunInputs)
            or isinstance(self.plan_revision, bool)
            or not isinstance(self.plan_revision, int)
            or self.plan_revision < 1
        ):
            raise ExecutionValidationError("task preparation inputs are invalid")
        if (
            self.packet.run_id != self.inputs.run_id
            or self.packet.compiled_plan_sha256 != self.inputs.compiled_plan_sha256
            or self.inputs.skill_manifests.get(self.packet.task_id)
            != self.packet.skill_manifest_sha256
        ):
            raise ExecutionValidationError("task preparation changed immutable run inputs")
        seats = tuple(self.seats)
        if (
            not seats
            or any(not isinstance(seat, SeatAssignment) for seat in seats)
            or tuple(seat.executor_id for seat in seats) != self.policy.executor_ids
            or len({seat.seat_id for seat in seats}) != len(seats)
        ):
            raise ExecutionValidationError("task preparation seats differ from provider policy")
        provider_policy = self.packet.provider_policy
        if (
            provider_policy.executor_ids != self.policy.executor_ids
            or provider_policy.rounds != self.policy.rounds
            or provider_policy.minimum_success != self.policy.minimum_success
            or provider_policy.timeout != self.policy.timeout
            or provider_policy.retries != self.policy.retries
            or provider_policy.quality_tier != self.policy.quality_tier
            or provider_policy.timeout_override_reason != self.policy.timeout_override_reason
            or provider_policy.timeout_override_review_sha256
            != self.policy.timeout_override_review_sha256
        ):
            raise ExecutionValidationError("task preparation policy differs from compiled plan")
        from .providers import ProviderRegistry
        profiled = self.inputs.profile_shape == "class-tier"
        if not profiled and isinstance(self.registry, ProviderRegistry):
            raise ExecutionValidationError("real executor registry requires class-tier profile shape")
        bindings: dict[str, tuple[str, int | float]] = {}
        if profiled:
            if not isinstance(self.registry, ProviderRegistry):
                raise ExecutionValidationError("profiled task preparation requires an executor registry")
            for executor_id in self.policy.executor_ids:
                profile = self.registry.select(
                    executor_id, self.packet.execution_class, self.policy.quality_tier,
                )
                if self.inputs.provider_profiles.get(profile.binding_key) != profile.digest:
                    raise ExecutionValidationError("task preparation profile digest differs from run inputs")
                try:
                    timeout = provider_policy.effective_timeout(profile)
                except FanoutError as error:
                    raise ExecutionValidationError("task preparation profile timeout is invalid") from error
                bindings[executor_id] = profile.digest, timeout
                if executor_id == "agy" and self.packet.execution_class == "read-only":
                    seat = next(item for item in seats if item.executor_id == executor_id)
                    if seat.agy_guard is not None:
                        try:
                            assert seat.workspace_verification is not None
                            validate_agy_readonly_guard(
                                seat.agy_guard, seat.workspace_verification.workspace.root, profile,
                            )
                        except (AssertionError, ProviderRequestError) as error:
                            raise ExecutionValidationError(
                                "task preparation agy guard changed association"
                            ) from error
        document = {
            "context_sha256": self.packet.context_sha256,
            "inputs_digest": self.inputs.digest,
            "plan_revision": self.plan_revision,
            "policy": {
                "executor_ids": list(self.policy.executor_ids),
                "rounds": self.policy.rounds,
                "minimum_success": self.policy.minimum_success,
                "timeout": self.policy.timeout,
                "retries": self.policy.retries,
                "max_workers": self.policy.max_workers,
                "quality_tier": self.policy.quality_tier,
                "timeout_override_reason": self.policy.timeout_override_reason,
                "timeout_override_review_sha256": self.policy.timeout_override_review_sha256,
            },
            "profile_bindings": {
                executor: {"profile_sha256": digest, "effective_timeout": timeout}
                for executor, (digest, timeout) in sorted(bindings.items())
            },
            "seats": [
                {
                    "delivery_evidence_sha256": seat.delivery_evidence_sha256,
                    "executor_id": seat.executor_id,
                    "seat_id": seat.seat_id,
                    "skill_bundle_sha256": seat.skill_bundle_sha256,
                    "staged_manifest_sha256": seat.staged_manifest_sha256,
                    "workspace_evidence_sha256": (
                        None
                        if seat.workspace_verification is None
                        else seat.workspace_verification.evidence_digest
                    ),
                    "agy_guard_sha256": (
                        seat.agy_guard_receipt_sha256
                        if seat.agy_guard_receipt_sha256 is not None else
                        None if seat.agy_guard is None else seat.agy_guard.receipt_sha256
                    ),
                }
                for seat in seats
            ],
        }
        object.__setattr__(self, "seats", seats)
        object.__setattr__(self, "profile_bindings", MappingProxyType(bindings))
        object.__setattr__(self, "digest", _digest(canonical_json(document)))


@dataclass(frozen=True, slots=True)
class ExecutionTaskState:
    """Safe durable execution metadata; provider answers never enter this state."""

    task_id: str
    preparation_sha256: str
    decision_plan_revision: int
    decision_plan_sha256: str
    decision_inputs_digest: str
    task_sha256: str
    packet_context_sha256: str
    barriers: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        _identity(self.task_id, "execution task id")
        if (
            isinstance(self.decision_plan_revision, bool)
            or not isinstance(self.decision_plan_revision, int)
            or self.decision_plan_revision < 1
            or not _is_digest(self.decision_plan_sha256)
            or not _is_digest(self.decision_inputs_digest)
            or not _is_digest(self.task_sha256)
        ):
            raise ExecutionValidationError("execution task decision binding is invalid")
        if self.preparation_sha256 and not _is_digest(self.preparation_sha256):
            raise ExecutionValidationError("execution preparation digest is invalid")
        if self.packet_context_sha256 and not _is_digest(self.packet_context_sha256):
            raise ExecutionValidationError("execution packet context digest is invalid")
        if bool(self.preparation_sha256) != bool(self.packet_context_sha256):
            raise ExecutionValidationError("execution task context is incomplete")
        barriers = tuple(self.barriers)
        if len(barriers) > 3:
            raise ExecutionValidationError("execution barrier collection exceeds the round limit")
        for barrier in barriers:
            _artifact(barrier)
        object.__setattr__(self, "barriers", barriers)

    def to_dict(self) -> dict[str, object]:
        return {
            "barriers": [_artifact_document(ref) for ref in self.barriers],
            "decision_inputs_digest": self.decision_inputs_digest,
            "decision_plan_revision": self.decision_plan_revision,
            "decision_plan_sha256": self.decision_plan_sha256,
            "packet_context_sha256": self.packet_context_sha256 or None,
            "preparation_sha256": self.preparation_sha256 or None,
            "task_sha256": self.task_sha256,
            "task_id": self.task_id,
        }


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """CAS-persisted execution state containing only identities, digests, and refs."""

    run_id: str
    plan_sha256: str
    inputs_digest: str
    backend_identity: str
    backend_key: str
    backend_revision: int
    plan_revision: int
    tasks: tuple[ExecutionTaskState, ...]
    schema_version: str = _SNAPSHOT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != _SNAPSHOT_SCHEMA:
            raise ExecutionValidationError("execution snapshot schema is invalid")
        _identity(self.run_id, "execution run id")
        _identity(self.backend_identity, "execution backend identity")
        _identity(self.backend_key, "execution backend key")
        if not _is_digest(self.plan_sha256) or not _is_digest(self.inputs_digest):
            raise ExecutionValidationError("execution snapshot input binding is invalid")
        if (
            isinstance(self.backend_revision, bool)
            or not isinstance(self.backend_revision, int)
            or self.backend_revision < 1
        ):
            raise ExecutionValidationError("execution backend revision is invalid")
        if (
            isinstance(self.plan_revision, bool)
            or not isinstance(self.plan_revision, int)
            or self.plan_revision < 1
        ):
            raise ExecutionValidationError("execution plan revision is invalid")
        tasks = tuple(self.tasks)
        if any(not isinstance(task, ExecutionTaskState) for task in tasks):
            raise ExecutionValidationError("execution snapshot task state is invalid")
        if len({task.task_id for task in tasks}) != len(tasks):
            raise ExecutionValidationError("execution snapshot task identities are duplicate")
        if len(tasks) > _MAX_EXECUTION_TASKS:
            raise ExecutionValidationError("execution snapshot task limit exceeded")
        object.__setattr__(self, "tasks", tasks)
        if len(canonical_json(self.to_dict())) > _MAX_EXECUTION_SNAPSHOT_BYTES:
            raise ExecutionValidationError("execution snapshot byte limit exceeded")

    def to_dict(self) -> dict[str, object]:
        return {
            "backend_identity": self.backend_identity,
            "backend_key": self.backend_key,
            "backend_revision": self.backend_revision,
            "inputs_digest": self.inputs_digest,
            "plan_sha256": self.plan_sha256,
            "plan_revision": self.plan_revision,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "tasks": [task.to_dict() for task in self.tasks],
        }

    @property
    def digest(self) -> str:
        return _digest(canonical_json(self.to_dict()))


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    revision: int
    snapshot: ExecutionSnapshot
    schema_version: str = _RECORD_SCHEMA

    def __post_init__(self) -> None:
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
            or not isinstance(self.snapshot, ExecutionSnapshot)
            or self.snapshot.backend_revision != self.revision
            or self.schema_version != _RECORD_SCHEMA
        ):
            raise ExecutionValidationError("execution backend record is invalid")
        if len(self.canonical_bytes) > _MAX_EXECUTION_BACKEND_RECORD_BYTES:
            raise ExecutionValidationError("execution backend record byte limit exceeded")

    def to_dict(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "schema_version": self.schema_version,
            "snapshot": self.snapshot.to_dict(),
            "snapshot_sha256": self.snapshot.digest,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict())

    @property
    def snapshot_sha256(self) -> str:
        return self.snapshot.digest


def _execution_record_from_dict(value: object) -> ExecutionRecord:
    """Decode one exact v2 record before service-level plan and input checks."""
    fields = {"revision", "schema_version", "snapshot", "snapshot_sha256"}
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != _RECORD_SCHEMA:
        raise ExecutionValidationError("execution backend record fields are invalid")
    record = ExecutionRecord(
        revision=value["revision"],
        snapshot=_execution_snapshot_from_dict(value["snapshot"]),
    )
    if value["snapshot_sha256"] != record.snapshot_sha256 or value != record.to_dict():
        raise ExecutionValidationError("execution backend record digest or content is invalid")
    return record


def _execution_snapshot_from_dict(value: object) -> ExecutionSnapshot:
    fields = {
        "backend_identity", "backend_key", "backend_revision", "inputs_digest",
        "plan_sha256", "plan_revision", "run_id", "schema_version", "tasks",
    }
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != _SNAPSHOT_SCHEMA:
        raise ExecutionValidationError("execution snapshot fields are invalid")
    tasks = value["tasks"]
    if not isinstance(tasks, list) or len(tasks) > _MAX_EXECUTION_TASKS:
        raise ExecutionValidationError("execution snapshot tasks are invalid")
    snapshot = ExecutionSnapshot(
        run_id=value["run_id"], plan_sha256=value["plan_sha256"],
        inputs_digest=value["inputs_digest"],
        backend_identity=value["backend_identity"], backend_key=value["backend_key"],
        backend_revision=value["backend_revision"], plan_revision=value["plan_revision"],
        tasks=tuple(_execution_task_from_dict(item) for item in tasks),
    )
    if snapshot.to_dict() != value:
        raise ExecutionValidationError("execution snapshot content is invalid")
    return snapshot


def _execution_task_from_dict(value: object) -> ExecutionTaskState:
    fields = {
        "barriers", "decision_inputs_digest", "decision_plan_revision",
        "decision_plan_sha256", "packet_context_sha256", "preparation_sha256",
        "task_sha256", "task_id",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ExecutionValidationError("execution task state fields are invalid")
    barriers = value["barriers"]
    if not isinstance(barriers, list) or len(barriers) > 3:
        raise ExecutionValidationError("execution barrier collection is invalid")
    refs = []
    for artifact in barriers:
        if not isinstance(artifact, dict) or set(artifact) != {"digest", "path", "size"}:
            raise ExecutionValidationError("execution barrier reference fields are invalid")
        refs.append(_artifact(ArtifactRef(
            path=artifact["path"], digest=artifact["digest"], size=artifact["size"],
        )))
    preparation = value["preparation_sha256"]
    context = value["packet_context_sha256"]
    if preparation is not None and not isinstance(preparation, str):
        raise ExecutionValidationError("execution preparation digest is invalid")
    if context is not None and not isinstance(context, str):
        raise ExecutionValidationError("execution packet context digest is invalid")
    state = ExecutionTaskState(
        task_id=value["task_id"],
        preparation_sha256="" if preparation is None else preparation,
        decision_plan_revision=value["decision_plan_revision"],
        decision_plan_sha256=value["decision_plan_sha256"],
        decision_inputs_digest=value["decision_inputs_digest"],
        task_sha256=value["task_sha256"],
        packet_context_sha256="" if context is None else context,
        barriers=tuple(refs),
    )
    if state.to_dict() != value:
        raise ExecutionValidationError("execution task state content is invalid")
    return state


@runtime_checkable
class ExecutionBackend(Protocol):
    """Owner-authorized full-state CAS persistence for safe execution metadata."""

    def identity(self) -> str: ...
    def key(self) -> str: ...
    def max_record_bytes(self) -> int: ...
    def read(self) -> ExecutionRecord | None: ...
    def compare_and_set(
        self,
        expected_revision: int,
        snapshot: ExecutionSnapshot,
        *,
        owner: OwnerCapability,
    ) -> ExecutionRecord: ...


@dataclass(frozen=True, slots=True)
class ExecutionTaskStatus:
    task_id: str
    phase: str
    completed_rounds: int

    def __post_init__(self) -> None:
        _identity(self.task_id, "status task id")
        if not isinstance(self.phase, str) or not self.phase:
            raise ExecutionValidationError("status task phase is invalid")
        if (
            isinstance(self.completed_rounds, bool)
            or not isinstance(self.completed_rounds, int)
            or not 0 <= self.completed_rounds <= 3
        ):
            raise ExecutionValidationError("status completed round count is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "phase": self.phase,
            "completed_rounds": self.completed_rounds,
        }


@dataclass(frozen=True, slots=True)
class ExecutionStatus:
    run_id: str
    plan_revision: int
    scheduler_revision: int
    execution_revision: int
    projected_provider_turns: int
    tasks: tuple[ExecutionTaskStatus, ...]
    authority_state: str = "resolved"
    target_states: Mapping[str, str] | None = None
    overall_state: str | None = None

    def __post_init__(self) -> None:
        _identity(self.run_id, "status run id")
        for name in ("plan_revision", "scheduler_revision", "execution_revision"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExecutionValidationError(f"status {name} is invalid")
        if (
            isinstance(self.projected_provider_turns, bool)
            or not isinstance(self.projected_provider_turns, int)
            or not 0 <= self.projected_provider_turns <= _MAX_PROVIDER_TURNS
        ):
            raise ExecutionValidationError("status projected provider turns are invalid")
        tasks = tuple(self.tasks)
        if any(not isinstance(task, ExecutionTaskStatus) for task in tasks):
            raise ExecutionValidationError("status tasks are invalid")
        if len({task.task_id for task in tasks}) != len(tasks):
            raise ExecutionValidationError("status task identities are duplicate")
        if len(tasks) > _MAX_EXECUTION_TASKS:
            raise ExecutionValidationError("status task limit exceeded")
        if not isinstance(self.authority_state, str) or self.authority_state not in {
            "resolved", "unresolved",
        }:
            raise ExecutionValidationError("status authority state is invalid")
        if self.target_states is not None:
            states = dict(self.target_states)
            if (not states or any(
                not isinstance(key, str) or not key
                or value not in {"pending", "candidate", "delivered", "blocked"}
                for key, value in states.items()
            ) or self.overall_state not in {
                "pending", "candidate", "delivered", "blocked", "partial",
            }):
                raise ExecutionValidationError("status target states are invalid")
            object.__setattr__(self, "target_states", MappingProxyType(dict(sorted(states.items()))))
        elif self.overall_state is not None:
            raise ExecutionValidationError("status overall state requires target states")
        object.__setattr__(self, "tasks", tasks)
        if len(canonical_json(self.to_dict())) > _MAX_EXECUTION_STATUS_BYTES:
            raise ExecutionValidationError("execution status byte limit exceeded")

    def to_dict(self) -> dict[str, object]:
        value = {
            "authority_state": self.authority_state,
            "execution_revision": self.execution_revision,
            "plan_revision": self.plan_revision,
            "projected_provider_turns": self.projected_provider_turns,
            "run_id": self.run_id,
            "scheduler_revision": self.scheduler_revision,
            "tasks": [task.to_dict() for task in self.tasks],
        }
        if self.target_states is not None:
            value["target_states"] = dict(self.target_states)
            value["overall_state"] = self.overall_state
        return value


@dataclass(frozen=True, slots=True)
class _PendingRevision:
    plan: FanoutPlanV1
    inputs: RunInputs
    revision: int
    affected_task_ids: frozenset[str]
    journal_phase: str


@dataclass(frozen=True, slots=True)
class AmendmentDocuments:
    """Exact replacement source authenticated from one durable journal intent."""

    revision: int
    binding: tuple[str, str, str, str, str, str]
    plan: FanoutPlanV1
    inputs: RunInputs
    profile_digests: Mapping[str, str]


class ExecutionService:
    """Compose scheduling, collaboration, reconciliation, handover, and exact GC."""

    def __init__(
        self,
        *,
        plan: FanoutPlanV1 | Mapping[str, object],
        inputs: RunInputs,
        preparations: Mapping[str, TaskPreparation],
        provider_profile_digests: Mapping[str, str],
        budget: ExecutionBudget,
        scheduler: object,
        journal: object,
        coordinator: object,
        artifacts: ArtifactStore,
        backend: ExecutionBackend,
        memory_preflight: Callable[[], bool],
        baseline: RepositoryBaseline | None = None,
        lifecycle_controller: LifecycleController | None = None,
        limits: ExecutionLimits | None = None,
    ) -> None:
        self.plan = validate_plan(plan)
        if not isinstance(inputs, RunInputs):
            raise ExecutionValidationError("execution inputs must be RunInputs")
        if not isinstance(budget, ExecutionBudget):
            raise ExecutionValidationError("execution budget is invalid")
        if not isinstance(artifacts, ArtifactStore):
            raise ExecutionValidationError("execution artifacts must be an ArtifactStore")
        if not isinstance(backend, ExecutionBackend):
            raise ExecutionValidationError("execution backend does not implement durable CAS")
        if not callable(memory_preflight):
            raise ExecutionValidationError("memory preflight must be callable")
        limits = limits or ExecutionLimits()
        if not isinstance(limits, ExecutionLimits):
            raise ExecutionValidationError("execution limits are invalid")
        required_scheduler = (
            "schedule_ready", "mark_active", "begin_reconciliation", "complete_reconciliation",
            "complete_action", "fail_task", "task_phase", "result_for", "result_receipt",
            "accept_amendment",
        )
        required_journal = ("authorize_owner", "append", "append_amendment")
        required_coordinator = ("preflight_round", "execute_round", "restore_barrier")
        if any(not callable(getattr(scheduler, name, None)) for name in required_scheduler):
            raise ExecutionValidationError("scheduler does not implement the execution contract")
        if any(not callable(getattr(journal, name, None)) for name in required_journal):
            raise ExecutionValidationError("journal does not implement owner authorization")
        if any(not callable(getattr(coordinator, name, None)) for name in required_coordinator):
            raise ExecutionValidationError("coordinator does not implement durable barriers")
        prepared = dict(preparations)
        if any(not isinstance(key, str) or not isinstance(value, TaskPreparation)
               for key, value in prepared.items()):
            raise ExecutionValidationError("execution preparations are invalid")
        expected = {
            task.id for task in self.plan.tasks
            if task.kind == "work" and task.execution_class != "orchestrator-action"
        }
        if set(prepared) != expected or any(value.packet.task_id != key for key, value in prepared.items()):
            raise ExecutionValidationError("execution preparations do not cover exact non-action work")
        profiles = dict(provider_profile_digests)
        if any(not isinstance(key, str) or not _is_digest(value) for key, value in profiles.items()):
            raise ExecutionValidationError("provider profile bindings are invalid")
        if baseline is not None and not isinstance(baseline, RepositoryBaseline):
            raise ExecutionValidationError("execution repository baseline is invalid")
        if lifecycle_controller is not None:
            try:
                assert_controller(lifecycle_controller)
            except Exception as error:
                raise ExecutionValidationError("execution lifecycle controller is invalid") from error
        self.inputs = inputs
        self.preparations = MappingProxyType(prepared)
        self.provider_profile_digests = MappingProxyType(profiles)
        self.budget = budget
        self.scheduler = scheduler
        self.journal = journal
        self.coordinator = coordinator
        self.artifacts = artifacts
        self.backend = backend
        self.memory_preflight = memory_preflight
        self.baseline = baseline
        self.lifecycle_controller = lifecycle_controller
        self.limits = limits
        self._composition_coordinator = coordinator
        self._composition_scheduler = scheduler
        self._composition_journal = journal
        self._composition_artifacts = artifacts
        self._composition_backend = backend
        self._composition_memory = getattr(coordinator, "memory", None)
        self._composition_registry = getattr(coordinator, "registry", None)
        self._composition_owner = getattr(coordinator, "owner", None)
        self._composition_baseline = baseline
        self._composition_controller = lifecycle_controller
        self._composition_provider_boundary = MappingProxyType({
            name: _callable_identity(getattr(coordinator, name, None))
            for name in (
                "execute_round",
                "_run_provider_barrier",
                "_provider_turn",
                "provider_runner",
            )
        })
        try:
            self._backend_identity = _identity(backend.identity(), "execution backend identity")
            self._backend_key = _identity(backend.key(), "execution backend key")
            backend_record_limit = backend.max_record_bytes()
        except ExecutionValidationError:
            raise
        except Exception as error:
            raise ExecutionValidationError("execution backend identity is unavailable") from error
        if (
            isinstance(backend_record_limit, bool)
            or not isinstance(backend_record_limit, int)
            or backend_record_limit < limits.max_backend_record_bytes
            or backend_record_limit > _MAX_EXECUTION_BACKEND_RECORD_BYTES
        ):
            raise ExecutionValidationError(
                "execution backend record limit is smaller than the configured bound"
            )
        self._backend_record_limit = backend_record_limit
        self._provider_transition_issuer = object()
        self._provider_transition_service_sha256 = _digest(canonical_json({
            "backend_identity": self._backend_identity,
            "backend_key": self._backend_key,
            "inputs_digest": self.inputs.digest,
            "plan_sha256": self._plan_digest,
            "run_id": self.inputs.run_id,
            "schema_version": "fanout-execution-service-identity-v1",
        }))
        self._record: ExecutionRecord | None = None
        self._barrier_cache: dict[tuple[ArtifactRef, str], object] = {}
        self._validate_initial_capacity()

    @staticmethod
    def project_v2_handover_terminal(
        scheduler: Scheduler, journal: object, task_id: str, *, owner: OwnerCapability,
    ) -> None:
        """Project one authenticated journal terminal without launching providers or Git."""
        if (not isinstance(scheduler, Scheduler)
                or not isinstance(scheduler.plan, FanoutPlanV2)
                or scheduler._journal is not journal):
            raise ExecutionValidationError("v2 handover projection requires exact scheduler journal")
        try:
            _intent, terminal = journal.branch_handover_state(task_id)
        except (KeyError, RunStateError, OSError) as error:
            raise ExecutionValidationError("v2 handover journal terminal is unavailable") from error
        if terminal is None:
            raise ExecutionPendingError("v2 handover terminal is not durable")
        scheduler._authenticate_handover(task_id, terminal)
        scheduler.mark_handover_terminal(task_id, terminal, owner=owner)

    @staticmethod
    def v2_status(scheduler: Scheduler) -> ExecutionStatus:
        """Read target progress from one authenticated v2 scheduler snapshot."""
        if not isinstance(scheduler, Scheduler) or not isinstance(scheduler.plan, FanoutPlanV2):
            raise ExecutionValidationError("v2 status requires a v2 scheduler")
        tasks = tuple(task for task in scheduler.plan.tasks if task.kind == "work")
        states: dict[str, str] = {}
        for target in scheduler.plan.targets:
            target_tasks = tuple(task for task in tasks if task.target_id == target.id)
            phases = tuple(scheduler.task_phase(task.id) for task in target_tasks)
            handover_blocked = bool(
                scheduler._journal is not None
                and any(task.id in scheduler._journal.state.branch_blocked for task in target_tasks)
            )
            delivered = not handover_blocked and (any(
                task.execution_class == "repo-write"
                and scheduler.handover_terminal_for(task.id) is not None
                for task in target_tasks
            ) or (
                bool(target_tasks)
                and all(task.execution_class != "repo-write" for task in target_tasks)
                and all(phase == "completed" for phase in phases)
            ))
            candidate = any(
                task.execution_class == "repo-write" and scheduler.result_for(task.id) is not None
                for task in target_tasks
            )
            states[target.id] = (
                "blocked" if handover_blocked or any(phase in {"failed", "blocked-dependency"} for phase in phases) else
                "delivered" if delivered else
                "candidate" if candidate else "pending"
            )
        values = set(states.values())
        overall = ("partial" if "delivered" in values and "blocked" in values else
                   "blocked" if "blocked" in values else
                   "delivered" if values == {"delivered"} else
                   "candidate" if "candidate" in values else "pending")
        return ExecutionStatus(
            scheduler.inputs.run_id, scheduler.plan_revision, scheduler.revision,
            0, 0,
            tuple(ExecutionTaskStatus(task.id, scheduler.task_phase(task.id), 0)
                  for task in tasks),
            target_states=states, overall_state=overall,
        )

    def start(self, *, owner: OwnerCapability) -> ExecutionStatus:
        """Preflight the entire immutable run, initialize safe state, and schedule ready work."""
        self._preflight(owner)
        if self._read_backend() is not None:
            raise ExecutionConflictError("execution run is already initialized")
        tasks = tuple(
            self._new_task_state(task, self.scheduler.plan_revision)
            for task in self.plan.tasks if task.kind == "work"
        )
        snapshot = ExecutionSnapshot(
            self.inputs.run_id,
            self._plan_digest,
            self.inputs.digest,
            self._backend_identity,
            self._backend_key,
            1,
            1,
            tasks,
        )
        self._record = self._commit_backend(0, snapshot, owner)
        decisions = self.scheduler.schedule_ready(owner=owner)
        self._record_decisions(decisions, owner)
        return self.status()

    def status(self) -> ExecutionStatus:
        """Return bounded public metadata without prompts, answers, credentials, or source bytes."""
        if isinstance(self.scheduler, Scheduler):
            if not isinstance(self._composition_owner, OwnerCapability):
                raise ExecutionConflictError("scheduler status owner is unavailable")
            if not self._scheduler_authority_is_current(self._composition_owner):
                return ExecutionStatus(
                    self.inputs.run_id, 0, 0, 0, 0, (), "unresolved",
                )
        pending = self._pending_scheduler_revision()
        record = self._read_backend(pending=pending)
        if record is not None:
            self._validate_completed_results(record)
        plan = self.plan if pending is None else pending.plan
        states = {} if record is None else {item.task_id: item for item in record.snapshot.tasks}
        tasks = tuple(
            ExecutionTaskStatus(
                task.id,
                self.scheduler.task_phase(task.id),
                len(states[task.id].barriers) if task.id in states else 0,
            )
            for task in plan.tasks if task.kind == "work"
        )
        result = ExecutionStatus(
            self.inputs.run_id,
            self.scheduler.plan_revision,
            self.scheduler.revision,
            0 if record is None else record.revision,
            self._projected_turns,
            tasks,
        )
        if len(canonical_json(result.to_dict())) > self.limits.max_status_bytes:
            raise ExecutionConflictError("execution status exceeds its configured byte limit")
        return result

    def resume(self, *, owner: OwnerCapability) -> ExecutionStatus:
        """Recover durable barriers and advance scheduled work without rerunning settled turns."""
        self._preflight(owner)
        self._validate_completed_results()
        record = self._read_backend()
        if record is None:
            raise ExecutionConflictError("execution run has not been started")
        self._record = record
        decisions = self.scheduler.schedule_ready(owner=owner)
        self._record_decisions(decisions, owner)
        for task in self._work_tasks:
            phase = self.scheduler.task_phase(task.id)
            if phase in {"scheduled", "active"}:
                self._drive_task(task, owner)
            elif phase == "reconciliation-pending":
                self._ensure_reconciliation_journal(task.id, owner)
        decisions = self.scheduler.schedule_ready(owner=owner)
        self._record_decisions(decisions, owner)
        return self.status()

    def verify_repo_synthesis(
        self,
        task_id: str,
        candidate: CandidateBundle,
        *,
        owner: OwnerCapability,
        environment: Mapping[str, str] | None = None,
    ) -> CandidateVerification:
        """Run fresh declared checks after the exact final repository barrier."""
        self._authorize_command(owner)
        task = self._task(task_id)
        if (task.execution_class != "repo-write"
                or self.scheduler.task_phase(task_id) != "reconciliation-pending"):
            raise ExecutionPendingError("repository synthesis verification requires a final pending barrier")
        state = self._execution_state(task_id)
        policy = task.provider_policy or self.plan.defaults
        if len(state.barriers) != policy.rounds:
            raise ExecutionPendingError("repository synthesis verification requires a final provider barrier")
        baseline = self._require_baseline(task)
        sources = self._frozen_repo_candidates(task, baseline, state)
        available = Counter(item.candidate.digest for item in sources)
        requested = Counter(candidate.source_candidate_digests)
        if (len(candidate.source_candidate_digests) < 2
                or any(count > available[digest] for digest, count in requested.items())):
            raise ExecutionPendingError("candidate lacks exact final-seat synthesis provenance")
        binding = CandidateReconciliationBinding(
            self.inputs.run_id, task.id, state.decision_plan_sha256,
            state.decision_plan_revision, state.barriers[-1],
        )
        return verify_candidate(
            baseline, candidate, task.checks,
            controller=self._require_controller(), environment=environment,
            reconciliation=binding,
        )

    def verify_read_only_selection(
        self,
        task_id: str,
        seat_id: str,
        *,
        owner: OwnerCapability,
        environment: Mapping[str, str] | None = None,
    ) -> AnswerSelectionVerification:
        """Freshly check one exact final-seat answer for explicit selection."""
        self._authorize_command(owner)
        task = self._task(task_id)
        if (task.execution_class != "read-only" or not task.checks
                or task.reconciliation_policy != "select-or-synthesize"
                or self.scheduler.task_phase(task_id) != "reconciliation-pending"):
            raise ExecutionPendingError("checked selection requires an ordinary pending read-only task")
        state = self._execution_state(task_id)
        policy = task.provider_policy or self.plan.defaults
        if len(state.barriers) != policy.rounds:
            raise ExecutionPendingError("checked selection requires a final provider barrier")
        barrier = self._restore_barrier(state.barriers[-1], self._packet_for(task))
        selected = next(
            (terminal for terminal in barrier.valid_terminals if terminal.seat_id == seat_id),
            None,
        )
        if selected is None or selected.answer_ref is None:
            raise ExecutionPendingError("selected seat lacks an exact final answer")
        return verify_answer_selection(
            self.artifacts, run_id=self.inputs.run_id, task_id=task.id,
            plan_sha256=state.decision_plan_sha256,
            plan_revision=state.decision_plan_revision,
            seat_id=seat_id, selected_ref=selected.answer_ref,
            checks=task.checks, controller=self._require_controller(),
            environment=environment, baseline=self.baseline,
        )

    def verify_read_only_synthesis(
        self,
        task_id: str,
        *,
        source_seat_ids: Sequence[str],
        synthesizer_id: str,
        answer: bytes,
        owner: OwnerCapability,
        environment: Mapping[str, str] | None = None,
    ) -> AnswerSynthesisVerification:
        """Check a new answer against exact final seats under this task's plan."""
        self._authorize_command(owner)
        task = self._task(task_id)
        if (task.execution_class != "read-only"
                or self.scheduler.task_phase(task_id) != "reconciliation-pending"):
            raise ExecutionPendingError("answer synthesis requires a pending read-only task")
        state = self._execution_state(task_id)
        policy = task.provider_policy or self.plan.defaults
        if len(state.barriers) != policy.rounds:
            raise ExecutionPendingError("answer synthesis requires a final provider barrier")
        barrier = self._restore_barrier(state.barriers[-1], self._packet_for(task))
        available = {
            terminal.seat_id: terminal.answer_ref
            for terminal in barrier.valid_terminals if terminal.answer_ref is not None
        }
        try:
            requested = tuple(source_seat_ids)
        except TypeError as error:
            raise ExecutionPendingError("answer synthesis sources are invalid") from error
        if (len(requested) < 2 or any(not isinstance(seat_id, str) for seat_id in requested)
                or len(set(requested)) != len(requested) or any(
            seat_id not in available for seat_id in requested
        )):
            raise ExecutionPendingError("answer synthesis sources differ from final seats")
        return verify_answer_synthesis(
            self.artifacts, run_id=self.inputs.run_id, task_id=task.id,
            plan_sha256=state.decision_plan_sha256,
            plan_revision=state.decision_plan_revision,
            sources={seat_id: available[seat_id] for seat_id in requested},
            synthesizer_id=synthesizer_id, answer=answer,
            checks=task.checks, controller=self._require_controller(),
            environment=environment, baseline=self.baseline,
        )

    def submit(
        self,
        task_id: str,
        result: ArtifactRef | AnswerSynthesisVerification | AnswerSelectionVerification | CandidateVerification | object,
        *,
        owner: OwnerCapability,
    ) -> ReconciledResult:
        """Submit an explicit seat selection or a freshly verified synthesis."""
        self._authorize_command(owner)
        task = self._task(task_id)
        if self.scheduler.task_phase(task_id) not in {"reconciliation-pending", "completed"}:
            raise ExecutionPendingError("task is not pending owner reconciliation")
        artifact = self._submission_artifact(task, result)
        phase = self._journal_phase(task_id)
        if phase == "reconciliation-pending":
            self.journal.append("reconciliation-verifying", task_id=task_id, owner=owner)
        elif phase not in {"reconciliation-verifying", "completed"}:
            raise ExecutionConflictError("run journal is not pending reconciliation")
        scheduler_phase = self.scheduler.task_phase(task_id)
        if scheduler_phase == "reconciliation-pending":
            receipt = self.scheduler.result_receipt(task_id, artifact)
            receipt = self.scheduler.complete_reconciliation(task_id, receipt, owner=owner)
        else:
            receipt = self.scheduler.result_for(task_id)
            if not isinstance(receipt, ReconciledResult) or receipt.artifact != artifact:
                raise ExecutionConflictError("completed reconciliation differs from submitted evidence")
        if self._journal_phase(task_id) == "reconciliation-verifying":
            self.journal.append("completed", task_id=task_id, owner=owner)
        self._record_decisions(self.scheduler.schedule_ready(owner=owner), owner)
        return receipt

    def prepare_provider_profile_transition(
        self,
        replacement_plan: FanoutPlanV1 | Mapping[str, object],
        new_inputs: RunInputs,
        registry: object,
        provider_profile_digests: Mapping[str, str],
        *,
        owner: OwnerCapability,
    ) -> ProviderProfileTransition:
        """Authenticate old and prospective provider identities without mutating either."""
        replacement = validate_plan(replacement_plan)
        if replacement.source != self.plan.source:
            raise ExecutionPreflightError("source-changing amendment requires durable replacement source")
        if not isinstance(new_inputs, RunInputs):
            raise ExecutionValidationError("amendment inputs must be RunInputs")
        try:
            profiles = dict(provider_profile_digests)
        except (TypeError, ValueError) as error:
            raise ExecutionValidationError(
                "provider profile transition bindings are invalid"
            ) from error
        try:
            pending = self._pending_scheduler_revision()
        except ExecutionConflictError as error:
            raise ExecutionPreflightError(
                "provider profile transition pending authority changed"
            ) from error
        if (
            pending is not None
            and pending.journal_phase in {
                "plan-amendment-intent", "plan-amendment-accepted",
            }
            and pending.plan == replacement
            and pending.inputs == new_inputs
        ):
            # The old CLI pin can disappear after the authenticated scheduler CAS.
            # No old-profile turn can run while the amendment is pending; the
            # replacement still receives a full preflight before acceptance.
            self._authenticate_composition(
                owner,
                require_memory_health=True,
                allow_scheduler_revision=True,
                previous_scheduler_inputs=new_inputs,
            )
        else:
            self._preflight(
                owner,
                allow_journal_input_revision=True,
                previous_scheduler_inputs=new_inputs,
            )
        new_plan_sha256 = _digest(canonical_json(replacement.to_dict()))
        if (
            new_inputs.run_id != self.inputs.run_id
            or new_inputs.compiled_plan_sha256 != new_plan_sha256
            or new_inputs.repo_baseline_sha256 != self.inputs.repo_baseline_sha256
            or profiles != dict(new_inputs.provider_profiles)
            or profiles == dict(self.provider_profile_digests)
        ):
            raise ExecutionPreflightError(
                "provider profile transition changed its plan or input binding"
            )
        scheduler_revision = getattr(self.scheduler, "plan_revision", None)
        if (
            isinstance(scheduler_revision, bool)
            or not isinstance(scheduler_revision, int)
            or scheduler_revision < 1
        ):
            raise ExecutionPreflightError("scheduler amendment revision is invalid")
        old_revision = scheduler_revision - int(
            self.scheduler.plan_sha256 != self._plan_digest
            or self.scheduler.inputs_digest != self.inputs.digest
        )
        if old_revision < 1:
            raise ExecutionPreflightError("scheduler amendment revision is invalid")
        anticipated = _affected_amendment(
            self.plan,
            replacement,
            self.inputs,
            new_inputs,
            old_revision + 1,
        )
        self._validate_provider_recovery_state(
            replacement,
            new_inputs,
            old_revision,
        )
        current_task_ids = {
            task.id for task in self.plan.tasks if task.kind == "work"
        }
        if (
            not anticipated.affected_task_ids
            or any(
                task_id in current_task_ids
                and self.scheduler.plan_revision == old_revision
                and self.scheduler.task_phase(task_id) != "unscheduled"
                for task_id in anticipated.affected_task_ids
            )
        ):
            raise ExecutionPreflightError(
                "provider profile transition requires wholly unscheduled affected work"
            )
        from .providers import ProviderRegistry
        if (new_inputs.profile_shape == "class-tier") != isinstance(registry, ProviderRegistry):
            raise ExecutionPreflightError("provider profile shape differs from transition registry")
        require_identity, adapters = _registry_binding(registry, profiles)
        old_profiles = dict(self.provider_profile_digests)
        return ProviderProfileTransition(
            self._plan_digest,
            self.inputs.digest,
            _provider_profiles_digest(old_profiles),
            new_plan_sha256,
            new_inputs.digest,
            _provider_profiles_digest(profiles),
            old_revision,
            self._provider_transition_service_sha256,
            _registry=registry,
            _registry_require=require_identity,
            _registry_adapters=adapters,
            _old_profiles=tuple(sorted(old_profiles.items())),
            _new_profiles=tuple(sorted(profiles.items())),
            _issuer=self._provider_transition_issuer,
        )

    def submit_amendment(
        self,
        replacement_plan: FanoutPlanV1 | Mapping[str, object],
        new_inputs: RunInputs,
        preparations: Mapping[str, TaskPreparation],
        provider_profile_digests: Mapping[str, str],
        *,
        expected_plan_revision: int,
        provider_transition: ProviderProfileTransition | None = None,
        owner: OwnerCapability,
    ) -> PlanAmendment:
        """Accept only a fully preflighted replacement for an unscheduled subtree."""
        if (
            isinstance(expected_plan_revision, bool)
            or not isinstance(expected_plan_revision, int)
            or expected_plan_revision < 1
        ):
            raise ExecutionValidationError("expected plan revision is invalid")
        replacement = validate_plan(replacement_plan)
        if replacement.source != self.plan.source:
            raise ExecutionPreflightError("source-changing amendment requires durable replacement source")
        if not isinstance(new_inputs, RunInputs):
            raise ExecutionValidationError("amendment inputs must be RunInputs")
        self._authenticate_composition(
            owner,
            require_memory_health=True,
            allow_scheduler_revision=True,
            previous_scheduler_inputs=new_inputs,
        )
        pending = self._pending_scheduler_revision()
        if self._read_backend(pending=pending) is None:
            raise ExecutionConflictError("execution run has not been started")
        next_revision = expected_plan_revision + 1
        anticipated = _affected_amendment(
            self.plan,
            replacement,
            self.inputs,
            new_inputs,
            next_revision,
        )
        if not anticipated.affected_task_ids:
            raise ExecutionPreflightError("amendment does not affect schedulable work")
        profiles_changed = (
            dict(provider_profile_digests)
            != dict(self.provider_profile_digests)
        )
        if profiles_changed:
            self._validate_provider_transition(
                provider_transition,
                replacement,
                new_inputs,
                provider_profile_digests,
                expected_plan_revision,
            )
        elif provider_transition is not None:
            raise ExecutionPreflightError(
                "provider profile transition is invalid for unchanged profiles"
            )
        affected = frozenset(anticipated.affected_task_ids)
        current_task_ids = {
            task.id for task in self.plan.tasks if task.kind == "work"
        }
        if any(
            task_id in current_task_ids
            and self.scheduler.plan_revision == expected_plan_revision
            and self.scheduler.task_phase(task_id) != "unscheduled"
            for task_id in affected
        ):
            raise ExecutionPreflightError("amendment affects work that is already live")
        proposed = dict(preparations)
        replacement_preparation_ids = {
            task.id
            for task in replacement.tasks
            if task.kind == "work"
            and task.execution_class != "orchestrator-action"
        }
        if not set(proposed).issubset(replacement_preparation_ids):
            raise ExecutionValidationError("amendment preparations contain foreign tasks")
        merged: dict[str, TaskPreparation] = {}
        for task in replacement.tasks:
            if task.kind != "work" or task.execution_class == "orchestrator-action":
                continue
            if task.id not in affected and task.id in self.preparations:
                merged[task.id] = self.preparations[task.id]
                continue
            preparation = proposed.get(task.id)
            if preparation is None or preparation.plan_revision != next_revision:
                raise ExecutionValidationError(
                    "affected amendment work lacks its exact revision preparation"
                )
            merged[task.id] = preparation
        candidate = self._replacement(
            replacement,
            new_inputs,
            merged,
            provider_profile_digests,
        )
        candidate._preflight(
            owner,
            allow_journal_input_revision=True,
            previous_scheduler_inputs=self.inputs,
            provider_transition=provider_transition,
        )
        scheduler_is_current = (
            self.scheduler.plan_revision == expected_plan_revision
            and self.scheduler.plan == self.plan
            and self.scheduler.plan_sha256 == self._plan_digest
            and self.scheduler.inputs_digest == self.inputs.digest
        )
        scheduler_is_replacement = (
            self.scheduler.plan_revision == next_revision
            and self.scheduler.plan == replacement
            and self.scheduler.plan_sha256 == anticipated.plan_sha256
            and self.scheduler.inputs_digest == new_inputs.digest
        )
        if not (scheduler_is_current or scheduler_is_replacement):
            raise ExecutionConflictError("scheduler amendment binding changed")
        binding = (
            self._plan_digest,
            self.inputs.digest,
            anticipated.plan_sha256,
            new_inputs.digest,
            _provider_profiles_digest(self.provider_profile_digests),
            _provider_profiles_digest(provider_profile_digests),
        )
        existing = self._journal_amendment(next_revision)
        if existing is not None and existing.binding != binding:
            raise ExecutionConflictError("amendment revision changed binding")
        if existing is None:
            self._persist_amendment_documents(replacement, new_inputs, provider_transition)
            self.journal.append_amendment(
                "plan-amendment-intent",
                revision=next_revision,
                old_plan_sha256=binding[0],
                old_inputs_digest=binding[1],
                new_plan_sha256=binding[2],
                new_inputs_digest=binding[3],
                old_profiles_sha256=binding[4],
                new_profiles_sha256=binding[5],
                owner=owner,
            )
        else:
            documents = self.recovery_documents(self.journal, self.artifacts)
            if (
                documents.revision != next_revision
                or documents.plan != replacement
                or documents.inputs != new_inputs
                or dict(documents.profile_digests) != dict(provider_profile_digests)
            ):
                raise ExecutionConflictError("amendment document changed binding")
        if self.scheduler.plan_revision == expected_plan_revision:
            amendment = self.scheduler.accept_amendment(
                replacement,
                new_inputs,
                expected_plan_revision=expected_plan_revision,
                owner=owner,
            )
            if amendment != anticipated:
                raise ExecutionConflictError(
                    "scheduler amendment differs from the preflighted replacement"
                )
        elif (
            self.scheduler.plan_revision == expected_plan_revision + 1
            and self.scheduler.plan == replacement
            and self.scheduler.plan_sha256 == anticipated.plan_sha256
            and self.scheduler.inputs_digest == new_inputs.digest
        ):
            amendment = anticipated
        else:
            raise ExecutionConflictError("scheduler amendment revision changed")
        existing = self._journal_amendment(next_revision)
        if existing is None or existing.binding != binding:
            raise ExecutionConflictError("amendment intent changed before acceptance")
        if existing.phase == "plan-amendment-intent":
            self.journal.append_amendment(
                "plan-amendment-accepted",
                revision=next_revision,
                old_plan_sha256=binding[0],
                old_inputs_digest=binding[1],
                new_plan_sha256=binding[2],
                new_inputs_digest=binding[3],
                old_profiles_sha256=binding[4],
                new_profiles_sha256=binding[5],
                owner=owner,
            )
        elif existing.phase != "plan-amendment-accepted":
            raise ExecutionConflictError("amendment journal phase is invalid")
        pending = self._pending_scheduler_revision()
        current = self._read_backend(pending=pending)
        if current is None:
            raise ExecutionConflictError("execution run has not been started")
        if (
            current.snapshot.plan_sha256 == anticipated.plan_sha256
            and current.snapshot.inputs_digest == new_inputs.digest
        ):
            candidate._validate_snapshot(current.snapshot)
            updated = current
        else:
            self._validate_snapshot(current.snapshot, pending=pending)
            previous = {state.task_id: state for state in current.snapshot.tasks}
            task_states: list[ExecutionTaskState] = []
            for task in replacement.tasks:
                if task.kind != "work":
                    continue
                if task.id not in affected and task.id in previous:
                    task_states.append(previous[task.id])
                else:
                    task_states.append(candidate._new_task_state(task, next_revision))
            snapshot = replace(
                current.snapshot,
                plan_sha256=anticipated.plan_sha256,
                inputs_digest=new_inputs.digest,
                plan_revision=next_revision,
                tasks=tuple(task_states),
                backend_revision=current.revision + 1,
            )
            try:
                updated = candidate._commit_backend(current.revision, snapshot, owner)
            except ExecutionConflictError as commit_error:
                try:
                    observed = candidate._read_backend()
                except ExecutionConflictError:
                    raise commit_error
                if (
                    observed is None
                    or observed.snapshot.plan_sha256 != anticipated.plan_sha256
                    or observed.snapshot.inputs_digest != new_inputs.digest
                ):
                    raise commit_error
                updated = observed
        if provider_transition is not None:
            self.coordinator.registry = provider_transition._registry
            self._composition_registry = provider_transition._registry
        self.plan = replacement
        self.inputs = new_inputs
        self.preparations = MappingProxyType(merged)
        self.provider_profile_digests = MappingProxyType(dict(provider_profile_digests))
        self._provider_transition_service_sha256 = _digest(canonical_json({
            "backend_identity": self._backend_identity,
            "backend_key": self._backend_key,
            "inputs_digest": self.inputs.digest,
            "plan_sha256": self._plan_digest,
            "run_id": self.inputs.run_id,
            "schema_version": "fanout-execution-service-identity-v1",
        }))
        self._record = updated
        self._record_decisions(self.scheduler.schedule_ready(owner=owner), owner)
        return amendment

    def recover_pending_amendment(
        self,
        preparations: Mapping[str, TaskPreparation],
        *,
        registry: object | None = None,
        adapter_catalog: object | None = None,
        owner: OwnerCapability,
    ) -> PlanAmendment:
        """Replay one durable amendment from its authenticated replacement documents."""
        self.journal.authorize_owner(owner)
        pending = self._pending_scheduler_revision()
        if self._read_backend(pending=pending) is None:
            raise ExecutionConflictError("execution run has not been started")
        revisions = tuple(sorted(self._journal_amendments()))
        if not revisions:
            raise ExecutionPendingError("no durable amendment is pending")
        documents = self.recovery_documents(self.journal, self.artifacts)
        revision, binding = documents.revision, documents.binding
        if revision != self.scheduler.plan_revision + int(pending is None):
            raise ExecutionConflictError("pending amendment revision changed")
        if binding[:2] != (self._plan_digest, self.inputs.digest):
            raise ExecutionConflictError("pending amendment old binding changed")
        replacement, new_inputs = documents.plan, documents.inputs
        if (
            new_inputs.run_id != self.inputs.run_id
            or new_inputs.repo_baseline_sha256 != self.inputs.repo_baseline_sha256
        ):
            raise ExecutionConflictError("amendment replacement document binding changed")
        profiles = dict(new_inputs.provider_profiles)
        profiles_changed = profiles != dict(self.provider_profile_digests)
        selected_registry = (
            registry if registry is not None else
            self.load_amendment_registry(
                owner=owner, adapter_catalog=adapter_catalog,
            )
            if new_inputs.profile_shape == "class-tier" and profiles_changed else
            self.coordinator.registry
        )
        if new_inputs.profile_shape == "class-tier":
            from .providers import ProviderRegistry, parse_profile_binding_key
            if not isinstance(selected_registry, ProviderRegistry):
                raise ExecutionConflictError("amendment executor registry is unavailable")
            for key, digest in sorted(profiles.items()):
                descriptor = self._read_amendment_document(
                    self.artifacts, "executor-profiles", digest,
                )
                try:
                    profile = selected_registry.select(*parse_profile_binding_key(key))
                except FanoutError as error:
                    raise ExecutionConflictError("amendment executor profile is unavailable") from error
                if profile.digest != digest or descriptor != profile.to_dict():
                    raise ExecutionConflictError("amendment executor profile descriptor changed")
        transition = None
        if profiles_changed:
            transition = self.prepare_provider_profile_transition(
                replacement, new_inputs, selected_registry, profiles, owner=owner,
            )
        return self.submit_amendment(
            replacement, new_inputs, preparations, profiles,
            expected_plan_revision=revision - 1,
            provider_transition=transition, owner=owner,
        )

    def abandon_pending_amendment(self, *, owner: OwnerCapability) -> None:
        """Abandon an intent only before either scheduler or execution CAS advances."""
        self.journal.authorize_owner(owner)
        if self._pending_scheduler_revision() is not None:
            raise ExecutionConflictError("scheduler authority already advanced the amendment")
        if not self._scheduler_authority_is_current(owner):
            raise ExecutionConflictError("scheduler durable authority already changed")
        self._authenticate_composition(owner, require_memory_health=False)
        record = self._read_backend()
        if record is None or (
            record.snapshot.plan_sha256, record.snapshot.inputs_digest,
            record.snapshot.plan_revision,
        ) != (self._plan_digest, self.inputs.digest, self.scheduler.plan_revision):
            raise ExecutionConflictError("execution authority already advanced the amendment")
        revision = self.scheduler.plan_revision + 1
        amendment = self._journal_amendment(revision)
        if (
            amendment is None
            or amendment.phase != "plan-amendment-intent"
            or amendment.binding[:2] != (self._plan_digest, self.inputs.digest)
            or amendment.binding[4]
            != _provider_profiles_digest(self.provider_profile_digests)
        ):
            raise ExecutionConflictError("no exact old-authority amendment intent exists")
        self.journal.append_amendment(
            "plan-amendment-abandoned",
            revision=revision,
            old_plan_sha256=amendment.binding[0],
            old_inputs_digest=amendment.binding[1],
            new_plan_sha256=amendment.binding[2],
            new_inputs_digest=amendment.binding[3],
            old_profiles_sha256=amendment.binding[4],
            new_profiles_sha256=amendment.binding[5],
            owner=owner,
        )

    @staticmethod
    def recovery_documents(
        journal: object, artifacts: ArtifactStore, *, revision: int | None = None,
    ) -> AmendmentDocuments:
        """Load replacement docs before selecting old/new scheduler state at startup."""
        state = getattr(journal, "state", None)
        amendments = getattr(state, "amendments", None)
        if not isinstance(amendments, Mapping) or not amendments:
            raise ExecutionPendingError("no durable amendment is pending")
        initial_inputs = getattr(journal, "inputs", None)
        if not isinstance(initial_inputs, RunInputs) or any(
            type(item) is not int for item in amendments
        ):
            raise ExecutionConflictError("journal amendment chain is invalid")
        accepted_revision = 1
        accepted_binding = (
            initial_inputs.compiled_plan_sha256,
            initial_inputs.digest,
            _provider_profiles_digest(initial_inputs.provider_profiles),
        )
        revisions = tuple(sorted(amendments))
        for index, candidate_revision in enumerate(revisions):
            candidate = amendments[candidate_revision]
            candidate_binding = getattr(candidate, "binding", None)
            phase = getattr(candidate, "phase", None)
            if (
                candidate_revision != accepted_revision + 1
                or getattr(candidate, "revision", None) != candidate_revision
                or not isinstance(candidate_binding, tuple)
                or len(candidate_binding) != 6
                or any(not _is_digest(item) for item in candidate_binding)
                or candidate_binding[:2] + (candidate_binding[4],)
                != accepted_binding
                or phase not in {"plan-amendment-intent", "plan-amendment-accepted"}
            ):
                raise ExecutionConflictError("journal amendment chain changed")
            if phase == "plan-amendment-accepted":
                accepted_revision = candidate_revision
                accepted_binding = (
                    candidate_binding[2], candidate_binding[3], candidate_binding[5],
                )
            elif index != len(revisions) - 1:
                raise ExecutionConflictError("journal amendment chain has a nonfinal intent")
        if revision is None:
            revision = revisions[-1]
        elif type(revision) is not int or revision not in amendments:
            raise ExecutionConflictError("amendment revision has no durable documents")
        record = amendments[revision]
        binding = getattr(record, "binding", None)
        if (
            getattr(record, "revision", None) != revision
            or getattr(record, "phase", None)
            not in {"plan-amendment-intent", "plan-amendment-accepted"}
            or not isinstance(binding, tuple)
            or len(binding) != 6
            or any(not _is_digest(item) for item in binding)
        ):
            raise ExecutionConflictError("amendment journal binding is invalid")
        plan_value = ExecutionService._read_amendment_document(
            artifacts, "plans", binding[2],
        )
        inputs_value = ExecutionService._read_amendment_document(
            artifacts, "inputs", binding[3],
        )
        profiles_value = ExecutionService._read_amendment_document(
            artifacts, "profiles", binding[5],
        )
        try:
            plan = validate_plan(plan_value)
            inputs = RunInputs.from_dict(inputs_value)
        except FanoutError as error:
            raise ExecutionConflictError("amendment replacement document is invalid") from error
        if (
            _digest(canonical_json(plan.to_dict())) != binding[2]
            or inputs.digest != binding[3]
            or inputs.compiled_plan_sha256 != binding[2]
            or profiles_value != {
                "profiles": dict(sorted(inputs.provider_profiles.items())),
                "schema_version": "fanout-provider-profiles-v1",
            }
        ):
            raise ExecutionConflictError("amendment replacement document binding changed")
        if inputs.profile_shape == "class-tier":
            from .providers import parse_profile_binding_key
            for key, digest in sorted(inputs.provider_profiles.items()):
                try:
                    executor_id, execution_class, quality_tier = (
                        parse_profile_binding_key(key)
                    )
                except FanoutError as error:
                    raise ExecutionConflictError(
                        "amendment executor profile key is invalid"
                    ) from error
                descriptor = ExecutionService._read_amendment_document(
                    artifacts, "executor-profiles", digest,
                )
                if (
                    not isinstance(descriptor, dict)
                    or descriptor.get("executor_id") != executor_id
                    or descriptor.get("execution_class") != execution_class
                    or descriptor.get("quality_tier") != quality_tier
                ):
                    raise ExecutionConflictError(
                        "amendment executor profile descriptor changed"
                    )
        return AmendmentDocuments(
            revision, binding, plan, inputs,
            MappingProxyType(dict(inputs.provider_profiles)),
        )

    def load_amendment_registry(
        self, *, owner: OwnerCapability, adapter_catalog: object | None = None,
    ) -> object:
        """Rebuild profiled amendment authority from stored descriptors and known adapters."""
        self.journal.authorize_owner(owner)
        revisions = tuple(sorted(self._journal_amendments()))
        if not revisions:
            raise ExecutionPendingError("no durable amendment is pending")
        record = self._journal_amendment(revisions[-1])
        assert record is not None
        try:
            inputs = self.recovery_documents(self.journal, self.artifacts).inputs
            from .providers import (
                ExecutorProfile, ProviderRegistry, parse_profile_binding_key,
            )
            current = (
                self.coordinator.registry
                if isinstance(self.coordinator.registry, ProviderRegistry) else None
            )
            catalog = adapter_catalog if isinstance(adapter_catalog, ProviderRegistry) else None
            if inputs.profile_shape != "class-tier" or (current is None and catalog is None):
                raise ExecutionConflictError("amendment registry cannot be rebuilt from legacy bindings")
            profiles = []
            for key, digest in sorted(inputs.provider_profiles.items()):
                executor_id, _execution_class, _quality_tier = parse_profile_binding_key(key)
                old_adapter = (
                    current.require(executor_id)
                    if current is not None and executor_id in current.executor_ids else None
                )
                catalog_adapter = (
                    catalog.require(executor_id)
                    if catalog is not None and executor_id in catalog.executor_ids else None
                )
                if old_adapter is not None and catalog_adapter is not None and (
                    type(old_adapter) is not type(catalog_adapter)
                    or old_adapter.capabilities != catalog_adapter.capabilities
                ):
                    raise ExecutionConflictError("amendment adapter catalog changed old identity")
                adapter = old_adapter or catalog_adapter
                if adapter is None:
                    raise ExecutionConflictError("amendment adapter is unavailable")
                descriptor = self._read_amendment_document(
                    self.artifacts, "executor-profiles", digest,
                )
                profile = ExecutorProfile.from_dict(descriptor, adapter=adapter)
                if profile.binding_key != key or profile.digest != digest:
                    raise ExecutionConflictError("amendment executor profile descriptor changed")
                profiles.append(profile)
            return ProviderRegistry(
                profiles, version_probe=(catalog or current)._version_probe,
            )
        except ExecutionConflictError:
            raise
        except FanoutError as error:
            raise ExecutionConflictError("amendment registry descriptor is invalid") from error

    def action_complete(
        self,
        task_id: str,
        artifact: ArtifactRef,
        *,
        owner: OwnerCapability,
    ) -> ReconciledResult:
        """Complete an orchestrator-action barrier with exact durable owner evidence."""
        self._authorize_command(owner)
        task = self._task(task_id)
        if task.execution_class != "orchestrator-action":
            raise ExecutionValidationError("action-complete requires an orchestrator-action task")
        self.artifacts.read_bytes(_artifact(artifact))
        phase = self._journal_phase(task_id)
        if phase == "blocked-action":
            self.journal.append("action-complete", task_id=task_id, owner=owner)
        elif phase not in {"action-complete", "completed"}:
            raise ExecutionConflictError("run journal is not blocked on the action")
        scheduler_phase = self.scheduler.task_phase(task_id)
        if scheduler_phase == "blocked-action":
            receipt = self.scheduler.result_receipt(task_id, artifact)
            receipt = self.scheduler.complete_action(task_id, receipt, owner=owner)
        elif scheduler_phase == "completed":
            receipt = self.scheduler.result_for(task_id)
            if not isinstance(receipt, ReconciledResult) or receipt.artifact != artifact:
                raise ExecutionConflictError("completed action differs from submitted evidence")
        else:
            raise ExecutionConflictError("scheduler is not blocked on the action")
        self._record_decisions(self.scheduler.schedule_ready(owner=owner), owner)
        return receipt

    def collect(self, task_id: str, *, owner: OwnerCapability) -> tuple[CollectedCandidate, ...]:
        """Collect exact repo-write seat deltas without mutating caller or peer workspaces."""
        pending = self._authorize_command(owner, allow_pending_intent=True)
        task = self._task(task_id)
        if pending is not None and (
            task_id in pending.affected_task_ids
            or all(item.id != task_id for item in pending.plan.tasks)
        ):
            raise ExecutionPendingError("task is affected by the pending amendment")
        baseline = self._require_baseline(task)
        policy = task.provider_policy or self.plan.defaults
        if (
            self.scheduler.task_phase(task_id)
            not in {"reconciliation-pending", "completed"}
            or len(self._execution_state(task_id).barriers) != policy.rounds
        ):
            raise ExecutionPendingError("provider rounds are not durably complete")
        return self._frozen_repo_candidates(task, baseline)

    def handover(
        self,
        task_id: str,
        verification: CandidateVerification,
        *,
        owner: OwnerCapability,
    ) -> HandoverResult:
        """Hand over only the exact verified candidate completed by reconciliation."""
        self._authorize_command(owner)
        task = self._task(task_id)
        baseline = self._require_baseline(task)
        controller = self._require_controller()
        validate_candidate_verification(controller, verification)
        if (
            not verification.valid
            or verification.baseline_digest != baseline.digest
            or verification.checks != task.checks
        ):
            raise ExecutionPendingError(
                "handover requires valid verification against the exact task checks"
            )
        result = self.scheduler.result_for(task_id)
        if not isinstance(result, ReconciledResult):
            raise ExecutionPendingError("task reconciliation has not completed")
        if self.artifacts.read_bytes(result.artifact) != verification.candidate.manifest_bytes:
            raise ExecutionPendingError("handover candidate differs from completed reconciliation")
        phase = self._journal_phase(task_id)
        if phase in {"completed", "handover-rolled-back", "handover-not-mutated"}:
            transaction = prepare_handover_transaction(
                baseline,
                verification,
                controller=controller,
                task_id=task_id,
            )
            self.journal.append(
                "handover-intent",
                task_id=task_id,
                transaction_root=str(transaction.transaction_root),
                transaction_sha256=transaction.transaction_sha256,
                owner=owner,
            )
        elif phase == "handover-intent":
            raise ExecutionConflictError(
                "run journal requires recovery of its exact handover transaction"
            )
        else:
            raise ExecutionConflictError("run journal is not ready for handover")
        outcome = handover_candidate(
            baseline,
            verification,
            controller=controller,
            task_id=task_id,
            transaction=transaction,
        )
        validate_handover_disposition(
            controller,
            outcome,
            task_id=task_id,
            baseline_digest=baseline.digest,
            candidate_digest=verification.candidate.digest,
            transaction_digest=transaction.transaction_sha256,
        )
        if self._journal_phase(task_id) == "handover-intent":
            self.journal.append(
                "handover-complete",
                task_id=task_id,
                transaction_root=str(transaction.transaction_root),
                transaction_sha256=transaction.transaction_sha256,
                disposition_sha256=outcome.evidence_digest,
                owner=owner,
            )
        return outcome

    def recover_handover(
        self,
        task_id: str,
        transaction_root: Path | str,
        verification: CandidateVerification,
        *,
        owner: OwnerCapability,
    ) -> HandoverResult:
        """Recover one exact controller-owned handover transaction."""
        self._authorize_command(owner)
        task = self._task(task_id)
        baseline = self._require_baseline(task)
        controller = self._require_controller()
        validate_candidate_verification(controller, verification)
        if (
            not verification.valid
            or verification.baseline_digest != baseline.digest
            or verification.checks != task.checks
        ):
            raise ExecutionPendingError(
                "handover recovery requires the exact verified task candidate"
            )
        result = self.scheduler.result_for(task_id)
        if (
            not isinstance(result, ReconciledResult)
            or self.artifacts.read_bytes(result.artifact)
            != verification.candidate.manifest_bytes
        ):
            raise ExecutionPendingError(
                "handover recovery candidate differs from completed reconciliation"
            )
        phase = self._journal_phase(task_id)
        terminal_events = {
            "committed": "handover-complete",
            "rolled-back": "handover-rolled-back",
            "conflict": "handover-conflict",
            "not-mutated": "handover-not-mutated",
        }
        if phase not in {"handover-intent", *terminal_events.values()}:
            raise ExecutionConflictError(
                "execution journal has no recoverable handover intent"
            )
        binding = self._journal_handover(task_id)
        if (
            binding is None
            or Path(transaction_root) != Path(binding.transaction_root)
            or binding.phase != phase
        ):
            raise ExecutionConflictError(
                "execution handover transaction changed association"
            )
        try:
            disposition = recover_handover_transaction(
                controller,
                transaction_root,
                conflict_disposition=True,
                expected_task_id=task_id,
                expected_baseline_digest=baseline.digest,
                expected_candidate_digest=verification.candidate.digest,
                expected_transaction_digest=binding.transaction_sha256,
            )
        except HandoverError as error:
            try:
                disposition = recover_handover_transaction(
                    controller,
                    transaction_root,
                    conflict_disposition=True,
                    expected_task_id=task_id,
                    expected_baseline_digest=baseline.digest,
                    expected_candidate_digest=verification.candidate.digest,
                    expected_transaction_digest=binding.transaction_sha256,
                )
            except HandoverError:
                raise error
        validate_handover_disposition(
            controller,
            disposition,
            task_id=task_id,
            baseline_digest=baseline.digest,
            candidate_digest=verification.candidate.digest,
            transaction_digest=binding.transaction_sha256,
        )
        event = terminal_events[disposition.status]
        phase = self._journal_phase(task_id)
        if phase == "handover-intent":
            self.journal.append(
                event,
                task_id=task_id,
                transaction_root=binding.transaction_root,
                transaction_sha256=binding.transaction_sha256,
                disposition_sha256=disposition.evidence_digest,
                owner=owner,
            )
        elif (
            phase != event
            or binding.disposition_sha256 != disposition.evidence_digest
        ):
            raise ExecutionConflictError(
                "execution handover disposition changed association"
            )
        return disposition

    def gc(
        self,
        owned_paths: Sequence[RunOwnedPath],
        *,
        owner: OwnerCapability,
    ) -> tuple[Path, ...]:
        """Delete only controller-authenticated, unchanged run-owned inode claims."""
        self._authorize_command(owner)
        return garbage_collect(self._require_controller(), owned_paths)

    def claim_gc_path(self, path: str, *, owner: OwnerCapability) -> RunOwnedPath:
        """Issue an exact cleanup claim below the authenticated run root."""
        self._authorize_command(owner)
        return claim_run_path(self._require_controller(), path)

    @property
    def _plan_digest(self) -> str:
        return _digest(canonical_json(self.plan.to_dict()))

    @property
    def _work_tasks(self) -> tuple[PlanTaskV1, ...]:
        return tuple(
            task for task in self.plan.tasks
            if task.kind == "work" and task.execution_class != "orchestrator-action"
        )

    @property
    def _projected_turns(self) -> int:
        return sum(
            len(preparation.seats)
            * preparation.policy.rounds
            * (preparation.policy.retries + 1)
            for preparation in self.preparations.values()
        )

    def _preflight(
        self,
        owner: OwnerCapability,
        *,
        allow_journal_input_revision: bool = False,
        previous_scheduler_inputs: RunInputs | None = None,
        provider_transition: ProviderProfileTransition | None = None,
    ) -> None:
        # No durable v2 execution route or certified native seat exists yet.
        if self.plan.schema_version == "v2" or self.inputs.targets is not None:
            raise ExecutionPreflightError(
                "v2 execution requires a validated native seat boundary"
            )
        # Real repo-write seats have no certified native route yet. Refuse the
        # entire plan before any read-only CLI can be probed.
        # Injected fake transports are a private test seam, not CLI input.
        if any(task.execution_class == "repo-write" for task in self._work_tasks):
            coordinator_registry = getattr(self.coordinator, "registry", None)
            transport = getattr(self.coordinator, "provider_runner", None)
            if (isinstance(coordinator_registry, ProviderRegistry)
                    and (transport is None or transport is run_provider)):
                raise ExecutionPreflightError(
                    "repo-write execution requires a validated native seat boundary"
                )
        self._authenticate_composition(
            owner,
            require_memory_health=True,
            allow_scheduler_revision=allow_journal_input_revision,
            previous_scheduler_inputs=previous_scheduler_inputs,
            provider_transition=provider_transition,
        )
        if not allow_journal_input_revision:
            self._require_settled_amendment("provider spend")
        errors: list[Exception] = []
        try:
            if self.inputs.run_id != self.journal.inputs.run_id:
                raise ExecutionPreflightError("journal belongs to another run")
            if self.inputs.digest != self.journal.inputs.digest:
                amended = any(
                    getattr(record, "phase", None) == "plan-amendment-accepted"
                    and getattr(record, "binding", (None, None, None, None))[2]
                    == self._plan_digest
                    and getattr(record, "binding", (None, None, None, None))[3]
                    == self.inputs.digest
                    for record in self._journal_amendments().values()
                )
                if not allow_journal_input_revision and not amended:
                    raise ExecutionPreflightError("journal immutable inputs changed")
                if self.inputs.repo_baseline_sha256 != self.journal.inputs.repo_baseline_sha256:
                    raise ExecutionPreflightError("amendment changed the immutable repository baseline")
            if not allow_journal_input_revision and (
                self.scheduler.plan != self.plan
                or self.scheduler.plan_sha256 != self._plan_digest
                or self.scheduler.inputs_digest != self.inputs.digest
            ):
                raise ExecutionPreflightError("scheduler plan or immutable inputs changed")
            if dict(self.provider_profile_digests) != dict(self.inputs.provider_profiles):
                raise ExecutionPreflightError("provider profile preflight changed")
            if self._projected_turns > self.budget.max_provider_turns:
                raise ExecutionPreflightError(
                    "provider turn budget is below the compiled worst-case spend"
                )
            if self.baseline is not None and self.inputs.repo_baseline_sha256 != self.baseline.digest:
                raise ExecutionPreflightError("repository baseline preflight changed")
            if self._work_tasks:
                if self.baseline is None or self.inputs.repo_baseline_sha256 != self.baseline.digest:
                    raise ExecutionPreflightError("provider seats require the immutable repository baseline")
                self._require_controller()
        except RunAuthorizationError:
            raise
        except Exception as error:
            errors.append(error)
        run_workspace_roots: set[Path] = set()
        for task in self._work_tasks:
            preparation = self.preparations.get(task.id)
            try:
                if preparation is None:
                    raise ExecutionPreflightError("task preparation is missing")
                packet = preparation.packet
                if (
                    packet.run_id != preparation.inputs.run_id
                    or packet.run_id != self.inputs.run_id
                    or packet.compiled_plan_sha256
                    != preparation.inputs.compiled_plan_sha256
                    or packet.task_sha256 != _digest(canonical_json(task.to_dict()))
                    or packet.execution_class != task.execution_class
                    or preparation.inputs.skill_manifests.get(task.id)
                    != packet.skill_manifest_sha256
                    or preparation.plan_revision
                    > self.scheduler.plan_revision + int(allow_journal_input_revision)
                    or any(seat.executor_id == "maka" for seat in preparation.seats)
                ):
                    raise ExecutionPreflightError("task immutable preparation changed")
                if task.execution_class in {"repo-write", "read-only"}:
                    baseline = self.baseline
                    if not isinstance(baseline, RepositoryBaseline):
                        raise ExecutionPreflightError(
                            "provider task lacks repository baseline evidence"
                        )
                    controller = self._require_controller()
                    if packet.cwd.resolve() != baseline.repository:
                        raise ExecutionPreflightError(
                            "task cwd differs from the immutable repository baseline"
                        )
                    for seat in preparation.seats:
                        verification = seat.workspace_verification
                        if verification is None:
                            raise ExecutionPreflightError(
                                f"{task.execution_class} preparation lacks workspace evidence"
                            )
                        if (
                            verification.workspace.seat_id != seat.seat_id
                            or verification.workspace.baseline_digest != baseline.digest
                            or verification.workspace.root in run_workspace_roots
                        ):
                            raise ExecutionPreflightError(
                                "seat workspace changed task association or is shared"
                            )
                        run_workspace_roots.add(verification.workspace.root)
                        if self._workspace_was_dispatched(task.id, seat.seat_id):
                            validate_seat_workspace(controller, verification)
                        else:
                            validate_seat_workspace_baseline(
                                baseline,
                                controller,
                                verification,
                            )
                preflight_kwargs = {
                    "round": 1,
                    "expected_inputs": preparation.inputs,
                }
                if (
                    provider_transition is not None
                    and isinstance(self.coordinator, CollaborationCoordinator)
                ):
                    preflight_kwargs["registry_override"] = provider_transition._registry
                self.coordinator.preflight_round(
                    packet,
                    preparation.seats,
                    preparation.policy,
                    **preflight_kwargs,
                )
            except Exception as error:
                errors.append(error)
        if errors:
            first = errors[0]
            if isinstance(first, ExecutionPreflightError):
                raise first
            raise ExecutionPreflightError(
                f"execution preflight failed for {len(errors)} immutable input(s)"
            ) from first
        if not allow_journal_input_revision:
            self._validate_completed_results()

    def _scheduler_authority_is_current(self, owner: OwnerCapability) -> bool:
        """Compare the cached scheduler with fresh authenticated durable authority."""
        if not isinstance(self.scheduler, Scheduler):
            raise ExecutionConflictError("scheduler durable authority is unavailable")
        try:
            _, authority = _authority_read(
                self.scheduler._authority, self.scheduler._authority_id,
                self.scheduler._authority_key, self.inputs.run_id,
                self.scheduler._backend.identity(), self.scheduler._backend.key(),
                owner,
            )
            record = self.scheduler._backend.read()
            if isinstance(record, BackendRecord):
                _validate_backend_record(record)
        except Exception as error:
            raise ExecutionConflictError("scheduler durable authority is unavailable") from error
        return (
            isinstance(record, BackendRecord)
            and record == self.scheduler._record
            and authority["pending"] is None
            and _record_matches_binding(record, authority["committed"])
        )

    def _pending_scheduler_revision(self) -> _PendingRevision | None:
        """Authenticate the amendment chain and recognize one scheduler revision ahead."""
        try:
            scheduler_revision = self.scheduler.plan_revision
            scheduler_inputs = self.scheduler.inputs
            journal_inputs = self.journal.inputs
            if (
                isinstance(scheduler_revision, bool)
                or not isinstance(scheduler_revision, int)
                or scheduler_revision < 1
                or not isinstance(scheduler_inputs, RunInputs)
                or not isinstance(journal_inputs, RunInputs)
                or scheduler_inputs.run_id != self.inputs.run_id
                or journal_inputs.run_id != self.inputs.run_id
            ):
                raise ExecutionConflictError("scheduler amendment revision is invalid")
            revisions = tuple(sorted(self._journal_amendments()))
            accepted_revision = 1
            accepted_binding = (
                journal_inputs.compiled_plan_sha256,
                journal_inputs.digest,
                _provider_profiles_digest(journal_inputs.provider_profiles),
            )
            intent = None
            for index, revision in enumerate(revisions):
                amendment = self._journal_amendment(revision)
                if (
                    isinstance(revision, bool)
                    or not isinstance(revision, int)
                    or revision != accepted_revision + 1
                    or amendment is None
                    or (
                        amendment.binding[0], amendment.binding[1],
                        amendment.binding[4],
                    ) != accepted_binding
                ):
                    raise ExecutionConflictError("journal amendment revision chain changed")
                if amendment.phase == "plan-amendment-accepted":
                    accepted_revision = revision
                    accepted_binding = (
                        amendment.binding[2],
                        amendment.binding[3],
                        amendment.binding[5],
                    )
                elif index == len(revisions) - 1:
                    intent = amendment
                else:
                    raise ExecutionConflictError("journal amendment intent is not final")
            if scheduler_revision < accepted_revision:
                raise ExecutionConflictError(
                    "accepted amendment lacks its scheduler revision"
                )
            if scheduler_revision == accepted_revision:
                expected_binding = accepted_binding
            elif intent is not None and scheduler_revision == intent.revision:
                expected_binding = (
                    intent.binding[2], intent.binding[3], intent.binding[5],
                )
            else:
                raise ExecutionConflictError(
                    "scheduler amendment revision lacks a journal binding"
                )
            scheduler_plan_sha256 = _digest(canonical_json(
                validate_plan(self.scheduler.plan).to_dict()
            ))
            if (
                (
                    scheduler_plan_sha256,
                    scheduler_inputs.digest,
                    _provider_profiles_digest(scheduler_inputs.provider_profiles),
                ) != expected_binding
                or self.scheduler.plan_sha256 != scheduler_plan_sha256
                or self.scheduler.inputs_digest != scheduler_inputs.digest
                or scheduler_inputs.compiled_plan_sha256 != scheduler_plan_sha256
            ):
                raise ExecutionConflictError(
                    "scheduler amendment revision binding changed"
                )
        except ExecutionConflictError:
            raise
        except Exception as error:
            raise ExecutionConflictError(
                "amendment authority is unavailable or ambiguous"
            ) from error
        if (
            self.scheduler.plan == self.plan
            and self.scheduler.inputs == self.inputs
            and self.scheduler.plan_sha256 == self._plan_digest
            and self.scheduler.inputs_digest == self.inputs.digest
        ):
            if intent is not None and (
                intent.revision != self.scheduler.plan_revision + 1
                or intent.binding[:2]
                != (self._plan_digest, self.inputs.digest)
                or intent.binding[4]
                != _provider_profiles_digest(self.provider_profile_digests)
                or dict(self.provider_profile_digests)
                != dict(self.inputs.provider_profiles)
                or self._decision_bindings().get(self.scheduler.plan_revision)
                != (self._plan_digest, self.inputs.digest)
            ):
                raise ExecutionConflictError("pending amendment intent binding changed")
            return None
        try:
            replacement = validate_plan(self.scheduler.plan)
            new_inputs = self.scheduler.inputs
            revision = self.scheduler.plan_revision
            if (
                not isinstance(new_inputs, RunInputs)
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 2
            ):
                raise ExecutionConflictError("pending amendment revision is invalid")
            if intent is not None and intent.revision != revision:
                raise ExecutionConflictError(
                    "pending amendment revision is superseded by a later intent"
                )
            new_plan_sha256 = _digest(canonical_json(replacement.to_dict()))
            binding = (
                self._plan_digest,
                self.inputs.digest,
                new_plan_sha256,
                new_inputs.digest,
                _provider_profiles_digest(self.provider_profile_digests),
                _provider_profiles_digest(new_inputs.provider_profiles),
            )
            if (
                self.inputs.run_id != self.journal.inputs.run_id
                or self.inputs.compiled_plan_sha256 != binding[0]
                or dict(self.provider_profile_digests)
                != dict(self.inputs.provider_profiles)
                or new_inputs.run_id != self.inputs.run_id
                or new_inputs.repo_baseline_sha256
                != self.inputs.repo_baseline_sha256
                or new_inputs.compiled_plan_sha256 != binding[2]
                or self.scheduler.plan_sha256 != binding[2]
                or self.scheduler.inputs_digest != binding[3]
                or self._decision_bindings().get(revision - 1)
                != binding[:2]
            ):
                raise ExecutionConflictError("pending amendment input binding changed")
            amendment = self._journal_amendment(revision)
            if amendment is None or amendment.binding != binding:
                raise ExecutionConflictError("pending amendment journal binding changed")
            affected = frozenset(_affected_amendment(
                self.plan, replacement, self.inputs, new_inputs, revision,
            ).affected_task_ids)
            if not affected:
                raise ExecutionConflictError("pending amendment has no affected work")
            if sum(task.kind == "work" for task in replacement.tasks) > self.limits.max_tasks:
                raise ExecutionConflictError("pending amendment task limit exceeded")
            # A failed ancestor projects unscheduled work as blocked-dependency.
            if any(
                self.scheduler.task_phase(task.id)
                not in {"unscheduled", "blocked-dependency"}
                for task in replacement.tasks
                if task.kind == "work" and task.id in affected
            ):
                raise ExecutionConflictError("pending amendment affected work is live")
            return _PendingRevision(
                replacement, new_inputs, revision, affected, amendment.phase,
            )
        except ExecutionConflictError:
            raise
        except Exception as error:
            raise ExecutionConflictError(
                "pending amendment state is unavailable or ambiguous"
            ) from error

    def _require_settled_amendment(self, operation: str) -> None:
        if self._pending_scheduler_revision() is not None or any(
            self._journal_amendment(revision).phase == "plan-amendment-intent"
            for revision in self._journal_amendments()
        ):
            raise ExecutionPreflightError(
                f"pending amendment transition must resolve before {operation}"
            )

    def _authorize_command(
        self,
        owner: OwnerCapability,
        *,
        allow_pending_intent: bool = False,
    ) -> _PendingRevision | None:
        """Authorize non-spending lifecycle work against already-durable run state."""
        pending = None
        if allow_pending_intent:
            self.journal.authorize_owner(owner)
            pending = self._pending_scheduler_revision()
        self._authenticate_composition(
            owner,
            require_memory_health=False,
            allow_scheduler_revision=pending is not None,
            previous_scheduler_inputs=None if pending is None else pending.inputs,
        )
        if not allow_pending_intent:
            self._require_settled_amendment("owner mutation")
        if (
            self.inputs.run_id != self.journal.inputs.run_id
            or self.inputs.compiled_plan_sha256 != self._plan_digest
            or (pending is None and (
                self.scheduler.plan != self.plan
                or self.scheduler.plan_sha256 != self._plan_digest
                or self.scheduler.inputs_digest != self.inputs.digest
            ))
            or dict(self.provider_profile_digests) != dict(self.inputs.provider_profiles)
            or (
                self.inputs.repo_baseline_sha256 is not None
                and (
                    self.baseline is None
                    or self.inputs.repo_baseline_sha256 != self.baseline.digest
                )
            )
        ):
            raise ExecutionConflictError("execution command input binding changed")
        record = self._read_backend(pending=pending)
        if record is None:
            raise ExecutionConflictError("execution run has not been started")
        self._record = record
        self._validate_completed_results()
        return pending

    def _validate_provider_recovery_state(
        self,
        replacement: FanoutPlanV1,
        new_inputs: RunInputs,
        expected_plan_revision: int,
    ) -> None:
        """Reissue a transition only from one exact durable amendment prefix."""
        new_plan_sha256 = _digest(canonical_json(replacement.to_dict()))
        binding = (
            self._plan_digest,
            self.inputs.digest,
            new_plan_sha256,
            new_inputs.digest,
            _provider_profiles_digest(self.provider_profile_digests),
            _provider_profiles_digest(new_inputs.provider_profiles),
        )
        amendment = self._journal_amendment(expected_plan_revision + 1)
        if amendment is not None and amendment.binding != binding:
            raise ExecutionPreflightError(
                "provider profile transition journal binding changed"
            )
        scheduler_current = (
            self.scheduler.plan_revision == expected_plan_revision
            and self.scheduler.plan == self.plan
            and self.scheduler.plan_sha256 == binding[0]
            and self.scheduler.inputs_digest == binding[1]
        )
        scheduler_replacement = (
            self.scheduler.plan_revision == expected_plan_revision + 1
            and self.scheduler.plan == replacement
            and self.scheduler.plan_sha256 == binding[2]
            and self.scheduler.inputs_digest == binding[3]
        )
        if (
            not (scheduler_current or scheduler_replacement)
            or (amendment is None and not scheduler_current)
            or (
                amendment is not None
                and amendment.phase == "plan-amendment-accepted"
                and not scheduler_replacement
            )
        ):
            raise ExecutionPreflightError(
                "provider profile transition scheduler or journal state changed"
            )
        record = self._read_backend(
            pending=self._pending_scheduler_revision(),
        )
        if record is None:
            raise ExecutionConflictError("execution run has not been started")

    def _validate_provider_transition(
        self,
        transition: ProviderProfileTransition | None,
        replacement: FanoutPlanV1,
        new_inputs: RunInputs,
        profiles: Mapping[str, str],
        expected_plan_revision: int,
    ) -> None:
        new_profiles = dict(profiles)
        old_profiles = dict(self.provider_profile_digests)
        new_plan_sha256 = _digest(canonical_json(replacement.to_dict()))
        if (
            not isinstance(transition, ProviderProfileTransition)
            or transition._issuer is not self._provider_transition_issuer
            or transition.service_sha256
            != self._provider_transition_service_sha256
            or transition.old_plan_sha256 != self._plan_digest
            or transition.old_inputs_digest != self.inputs.digest
            or transition.old_profiles_sha256
            != _provider_profiles_digest(old_profiles)
            or transition.new_plan_sha256 != new_plan_sha256
            or transition.new_inputs_digest != new_inputs.digest
            or transition.new_profiles_sha256
            != _provider_profiles_digest(new_profiles)
            or transition.expected_plan_revision != expected_plan_revision
            or dict(transition._old_profiles) != old_profiles
            or dict(transition._new_profiles) != new_profiles
            or new_profiles != dict(new_inputs.provider_profiles)
            or transition._registry is self._composition_registry
        ):
            raise ExecutionPreflightError(
                "provider profile transition changed its exact binding"
            )
        require_identity, adapters = _registry_binding(
            transition._registry,
            new_profiles,
        )
        if (
            not _same_callable(
                transition._registry_require,
                getattr(transition._registry, "require", None),
            )
            or require_identity != transition._registry_require
            or adapters != transition._registry_adapters
        ):
            raise ExecutionPreflightError(
                "provider profile transition registry identity changed"
            )

    def _authenticate_composition(
        self,
        owner: OwnerCapability,
        *,
        require_memory_health: bool,
        allow_scheduler_revision: bool = False,
        previous_scheduler_inputs: RunInputs | None = None,
        provider_transition: ProviderProfileTransition | None = None,
    ) -> None:
        """Prove every mutable execution dependency belongs to one exact run."""
        if (
            self.coordinator is not self._composition_coordinator
            or self.scheduler is not self._composition_scheduler
            or self.journal is not self._composition_journal
            or self.artifacts is not self._composition_artifacts
            or self.backend is not self._composition_backend
            or self.baseline is not self._composition_baseline
            or self.lifecycle_controller is not self._composition_controller
        ):
            raise ExecutionPreflightError(
                "execution composition changed dependency identity"
            )
        if any(
            not _same_callable(expected, getattr(self.coordinator, name, None))
            for name, expected in self._composition_provider_boundary.items()
            if expected is not None
        ):
            raise ExecutionPreflightError(
                "execution composition changed provider boundary identity"
            )
        self.journal.authorize_owner(owner)
        try:
            coordinator = self.coordinator
            if (
                getattr(coordinator, "journal", None) is not self.journal
                or getattr(coordinator, "artifacts", None) is not self.artifacts
                or getattr(coordinator, "memory", None)
                is not self._composition_memory
                or getattr(coordinator, "registry", None)
                is not self._composition_registry
                or getattr(coordinator, "owner", None)
                is not self._composition_owner
            ):
                raise ExecutionPreflightError(
                    "execution composition changed coordinator dependency identity"
                )
            coordinator_owner = getattr(coordinator, "owner", None)
            self.journal.authorize_owner(coordinator_owner)
            memory = getattr(coordinator, "memory", None)
            if memory is None or getattr(memory, "artifacts", None) is not self.artifacts:
                raise ExecutionPreflightError(
                    "execution composition changed memory or artifact identity"
                )
            configured_probe = getattr(memory, "preflight", None)
            if (
                not callable(configured_probe)
                or getattr(self.memory_preflight, "__self__", None) is not memory
                or getattr(self.memory_preflight, "__func__", None)
                is not getattr(configured_probe, "__func__", None)
            ):
                raise ExecutionPreflightError(
                    "execution composition changed memory health identity"
                )
            registry = getattr(coordinator, "registry", None)
            registry_profiles = getattr(registry, "profile_digests", None)
            expected_registry_profiles = dict(self.provider_profile_digests)
            if provider_transition is not None:
                if (
                    not isinstance(provider_transition, ProviderProfileTransition)
                    or provider_transition._issuer
                    is not self._provider_transition_issuer
                ):
                    raise ExecutionPreflightError(
                        "provider profile transition service identity changed"
                    )
                expected_registry_profiles = dict(
                    provider_transition._old_profiles
                )
                prospective_profiles = dict(
                    provider_transition._new_profiles
                )
                require_identity, adapters = _registry_binding(
                    provider_transition._registry,
                    prospective_profiles,
                )
                if (
                    not _same_callable(
                        provider_transition._registry_require,
                        getattr(provider_transition._registry, "require", None),
                    )
                    or require_identity != provider_transition._registry_require
                    or adapters != provider_transition._registry_adapters
                ):
                    raise ExecutionPreflightError(
                        "provider profile transition registry identity changed"
                    )
            if (
                not isinstance(registry_profiles, Mapping)
                or dict(registry_profiles) != expected_registry_profiles
                or (
                    provider_transition is None
                    and dict(registry_profiles)
                    != dict(self.inputs.provider_profiles)
                )
            ):
                raise ExecutionPreflightError(
                    "execution composition changed provider profile identity"
                )
            scheduler_inputs = getattr(self.scheduler, "inputs", None)
            scheduler_inputs_match = scheduler_inputs == self.inputs
            if allow_scheduler_revision:
                scheduler_inputs_match = scheduler_inputs_match or (
                    previous_scheduler_inputs is not None
                    and scheduler_inputs == previous_scheduler_inputs
                )
            if (
                not scheduler_inputs_match
                or getattr(self.scheduler, "artifacts", None) is not self.artifacts
            ):
                raise ExecutionPreflightError(
                    "execution composition changed scheduler or run input identity"
                )
            coordinator_controller = getattr(
                coordinator,
                "lifecycle_controller",
                None,
            )
            coordinator_baseline = getattr(coordinator, "repository_baseline", None)
            if (
                coordinator_controller is not self.lifecycle_controller
                or coordinator_baseline is not self.baseline
            ):
                raise ExecutionPreflightError(
                    "execution composition changed repository controller identity"
                )
            if require_memory_health and self.memory_preflight() is not True:
                raise ExecutionPreflightError(
                    "memory preflight did not report healthy"
                )
        except RunAuthorizationError as error:
            raise ExecutionPreflightError(
                "execution composition changed coordinator owner identity"
            ) from error
        except ExecutionPreflightError:
            raise
        except Exception as error:
            raise ExecutionPreflightError(
                "execution composition authentication failed"
            ) from error

    def _read_backend(
        self,
        *,
        pending: _PendingRevision | None = None,
    ) -> ExecutionRecord | None:
        try:
            record = self.backend.read()
        except Exception as error:
            raise ExecutionConflictError("execution backend read failed") from error
        if record is None:
            state = getattr(self.journal, "state", None)
            task_phases = getattr(state, "task_phases", None)
            if (
                self.scheduler.plan_revision != 1
                or self.scheduler.revision != 1
                or self._journal_amendments()
                or not isinstance(task_phases, Mapping)
                or task_phases
                or getattr(state, "seq", 0) != 0
                or getattr(state, "seat_phases", {})
                or getattr(state, "handovers", {})
                or any(
                    self.scheduler.task_phase(task.id) != "unscheduled"
                    for task in self.plan.tasks if task.kind == "work"
                )
            ):
                raise ExecutionConflictError(
                    "execution record is missing after prior run activity"
                )
            return None
        if not isinstance(record, ExecutionRecord):
            raise ExecutionConflictError("execution backend returned invalid state")
        if (
            len(record.canonical_bytes) > self.limits.max_backend_record_bytes
            or len(record.canonical_bytes) > self._backend_record_limit
        ):
            raise ExecutionConflictError("execution backend record exceeds its byte limit")
        self._validate_snapshot(record.snapshot, pending=pending)
        return record

    def _commit_backend(
        self,
        expected_revision: int,
        snapshot: ExecutionSnapshot,
        owner: OwnerCapability,
    ) -> ExecutionRecord:
        self._validate_snapshot(snapshot)
        candidate = ExecutionRecord(expected_revision + 1, snapshot)
        if (
            len(candidate.canonical_bytes) > self.limits.max_backend_record_bytes
            or len(candidate.canonical_bytes) > self._backend_record_limit
        ):
            raise ExecutionConflictError("execution backend record exceeds its byte limit")
        try:
            record = self.backend.compare_and_set(expected_revision, snapshot, owner=owner)
        except (ExecutionConflictError, RunAuthorizationError):
            raise
        except FanoutError:
            raise
        except Exception as error:
            raise ExecutionConflictError("execution backend compare-and-set failed") from error
        if (
            not isinstance(record, ExecutionRecord)
            or record.revision != expected_revision + 1
            or record.snapshot != snapshot
        ):
            raise ExecutionConflictError("execution backend returned an inconsistent commit")
        return record

    def _validate_snapshot(
        self,
        snapshot: ExecutionSnapshot,
        *,
        pending: _PendingRevision | None = None,
    ) -> None:
        replacement_snapshot = (
            pending is not None
            and isinstance(snapshot, ExecutionSnapshot)
            and snapshot.plan_sha256 == pending.inputs.compiled_plan_sha256
            and snapshot.inputs_digest == pending.inputs.digest
        )
        if (
            replacement_snapshot
            and pending.journal_phase != "plan-amendment-accepted"
        ):
            raise ExecutionConflictError(
                "pending amendment execution snapshot precedes journal acceptance"
            )
        plan = pending.plan if replacement_snapshot else self.plan
        plan_sha256 = (
            pending.inputs.compiled_plan_sha256
            if replacement_snapshot else self._plan_digest
        )
        inputs_digest = pending.inputs.digest if replacement_snapshot else self.inputs.digest
        plan_revision = (
            pending.revision if replacement_snapshot
            else pending.revision - 1 if pending is not None
            else self.scheduler.plan_revision
        )
        if (
            not isinstance(snapshot, ExecutionSnapshot)
            or snapshot.schema_version != _SNAPSHOT_SCHEMA
            or snapshot.run_id != self.inputs.run_id
            or snapshot.plan_sha256 != plan_sha256
            or snapshot.inputs_digest != inputs_digest
            or snapshot.plan_revision != plan_revision
            or snapshot.backend_identity != self._backend_identity
            or snapshot.backend_key != self._backend_key
        ):
            raise ExecutionConflictError("execution snapshot plan revision or input binding changed")
        expected = tuple(task.id for task in plan.tasks if task.kind == "work")
        if len(snapshot.tasks) > self.limits.max_tasks:
            raise ExecutionConflictError("execution snapshot task limit exceeded")
        if len(canonical_json(snapshot.to_dict())) > self.limits.max_snapshot_bytes:
            raise ExecutionConflictError("execution snapshot byte limit exceeded")
        if tuple(state.task_id for state in snapshot.tasks) != expected:
            raise ExecutionConflictError("execution snapshot tasks changed association")
        previous_tasks = {
            task.id: task for task in self.plan.tasks if task.kind == "work"
        }
        decision_bindings = self._decision_bindings()
        for task, state in zip(
            (item for item in plan.tasks if item.kind == "work"),
            snapshot.tasks,
            strict=True,
        ):
            decision = (
                state.decision_plan_revision,
                state.decision_plan_sha256,
                state.decision_inputs_digest,
            )
            if decision_bindings.get(state.decision_plan_revision) != decision[1:]:
                raise ExecutionConflictError("execution task decision binding changed")
            task_sha256 = _digest(canonical_json(task.to_dict()))
            if state.task_sha256 != task_sha256:
                raise ExecutionConflictError("execution task definition changed")
            if replacement_snapshot and task.id in pending.affected_task_ids:
                if (
                    state.decision_plan_revision != pending.revision
                    or state.decision_plan_sha256 != plan_sha256
                    or state.decision_inputs_digest != inputs_digest
                    or state.barriers
                    or (
                        task.execution_class == "orchestrator-action"
                        and (state.preparation_sha256 or state.packet_context_sha256)
                    )
                    or (
                        task.execution_class != "orchestrator-action"
                        and (not state.preparation_sha256 or not state.packet_context_sha256)
                    )
                ):
                    raise ExecutionConflictError(
                        "pending amendment task state changed association"
                    )
            else:
                if replacement_snapshot and previous_tasks.get(task.id) != task:
                    raise ExecutionConflictError(
                        "pending amendment task definition changed"
                    )
                if task.execution_class == "orchestrator-action":
                    if state.preparation_sha256 or state.packet_context_sha256:
                        raise ExecutionConflictError("action task acquired provider context")
                else:
                    preparation = self.preparations[task.id]
                    if (
                        state.preparation_sha256 != preparation.digest
                        or state.packet_context_sha256
                        != preparation.packet.context_sha256
                        or state.decision_plan_revision != preparation.plan_revision
                        or state.decision_plan_sha256
                        != preparation.packet.compiled_plan_sha256
                        or state.decision_inputs_digest != preparation.inputs.digest
                    ):
                        raise ExecutionConflictError("execution task preparation changed")
            policy = task.provider_policy or plan.defaults
            if len(state.barriers) > (0 if task.execution_class == "orchestrator-action" else policy.rounds):
                raise ExecutionConflictError("execution snapshot has too many barriers")
            for ref in state.barriers:
                try:
                    _artifact(
                        ref,
                        maximum_path_bytes=self.limits.max_artifact_path_bytes,
                    )
                    self.artifacts.read_bytes(ref)
                except (ArtifactError, ExecutionValidationError) as error:
                    raise ExecutionConflictError("execution barrier artifact is unavailable") from error

    def _record_decisions(
        self,
        decisions: Sequence[WorkDispatch | ActionBarrier],
        owner: OwnerCapability,
    ) -> None:
        for decision in decisions:
            if isinstance(decision, ActionBarrier):
                phase = self._journal_phase(decision.task_id)
                if phase is None:
                    self.journal.append("blocked-action", task_id=decision.task_id, owner=owner)
                elif phase != "blocked-action":
                    raise ExecutionConflictError("action barrier journal state changed")
            elif not isinstance(decision, WorkDispatch):
                raise ExecutionValidationError("scheduler returned an invalid decision")

    def _drive_task(self, task: PlanTaskV1, owner: OwnerCapability) -> None:
        if self.scheduler.task_phase(task.id) == "scheduled":
            self.scheduler.mark_active(task.id, owner=owner)
        if self.scheduler.task_phase(task.id) != "active":
            return
        preparation = self.preparations[task.id]
        dependencies = tuple(
            self.scheduler.result_for(dependency).artifact
            for dependency in task.depends_on
        )
        packet = dataclasses.replace(preparation.packet, dependency_artifacts=dependencies)
        state = self._execution_state(task.id)
        source: object | None = None
        for round_number in range(1, preparation.policy.rounds + 1):
            if len(state.barriers) < round_number:
                discover = getattr(self.coordinator, "discover_barrier", None)
                discovered = None if not callable(discover) else discover(
                    packet,
                    round=round_number,
                )
                if discovered is not None:
                    ref = getattr(discovered, "barrier_ref", None)
                    if not isinstance(ref, ArtifactRef):
                        raise ExecutionConflictError(
                            "discovered barrier has no durable artifact reference"
                        )
                    self.artifacts.read_bytes(ref)
                    state = replace(state, barriers=state.barriers + (ref,))
                    self._commit_task_state(state, owner)
                    self._barrier_cache[(ref, packet.context_sha256)] = discovered
            if len(state.barriers) >= round_number:
                source = self._restore_barrier(state.barriers[round_number - 1], packet)
                if getattr(source, "status", None) == "blocked-memory":
                    survivors = tuple(
                        seat for seat in preparation.seats
                        if seat.seat_id in {item.seat_id for item in source.valid_terminals}
                    )
                    if isinstance(self.scheduler.plan, FanoutPlanV2):
                        self.scheduler.assert_dispatchable(task.id)
                    recovered = self.coordinator.execute_round(
                        packet,
                        survivors,
                        preparation.policy,
                        round=round_number,
                        peer_source=None if round_number == 1 else previous,
                        recovery=source,
                        expected_inputs=preparation.inputs,
                    )
                    ref = getattr(recovered, "barrier_ref", None)
                    if not isinstance(ref, ArtifactRef):
                        raise ExecutionConflictError(
                            "coordinator recovery returned no durable barrier reference"
                        )
                    self.artifacts.read_bytes(ref)
                    if ref != state.barriers[round_number - 1]:
                        updated = list(state.barriers)
                        updated[round_number - 1] = ref
                        state = replace(state, barriers=tuple(updated))
                        self._commit_task_state(state, owner)
                    self._barrier_cache[(ref, packet.context_sha256)] = recovered
                    source = recovered
            else:
                survivors = preparation.seats
                if source is not None:
                    valid_ids = {item.seat_id for item in source.valid_terminals}
                    survivors = tuple(seat for seat in preparation.seats if seat.seat_id in valid_ids)
                if isinstance(self.scheduler.plan, FanoutPlanV2):
                    self.scheduler.assert_dispatchable(task.id)
                source = self.coordinator.execute_round(
                    packet,
                    survivors,
                    preparation.policy,
                    round=round_number,
                    peer_source=None if round_number == 1 else previous,
                    expected_inputs=preparation.inputs,
                )
                ref = getattr(source, "barrier_ref", None)
                if not isinstance(ref, ArtifactRef):
                    raise ExecutionConflictError("coordinator returned no durable barrier reference")
                self.artifacts.read_bytes(ref)
                state = replace(state, barriers=state.barriers + (ref,))
                self._commit_task_state(state, owner)
                self._barrier_cache[(ref, packet.context_sha256)] = source
            status = getattr(source, "status", None)
            if status == "blocked-memory":
                return
            if status == "failed-minimum":
                self.scheduler.fail_task(task.id, "provider minimum success was not met", owner=owner)
                return
            if status != "round-complete":
                raise ExecutionConflictError("coordinator returned an invalid barrier status")
            previous = source
        if task.execution_class == "repo-write" and not getattr(source, "candidate_sources", ()):
            raise ExecutionPendingError("final repository barrier has no durable source snapshot")
        self.scheduler.begin_reconciliation(task.id, owner=owner)
        self._ensure_reconciliation_journal(task.id, owner)

    def _restore_barrier(self, ref: ArtifactRef, packet: TaskPacket) -> object:
        cache_key = (ref, packet.context_sha256)
        cached = self._barrier_cache.get(cache_key)
        if cached is not None:
            return cached
        barrier = self.coordinator.restore_barrier(ref, packet=packet)
        if getattr(barrier, "barrier_ref", None) != ref:
            raise ExecutionConflictError("restored barrier changed durable association")
        if getattr(barrier, "context_sha256", packet.context_sha256) != packet.context_sha256:
            raise ExecutionConflictError("restored barrier changed packet context")
        self._barrier_cache[cache_key] = barrier
        return barrier

    def _commit_task_state(self, state: ExecutionTaskState, owner: OwnerCapability) -> None:
        record = self._record or self._read_backend()
        if record is None:
            raise ExecutionConflictError("execution state is unavailable")
        states = {item.task_id: item for item in record.snapshot.tasks}
        current = states.get(state.task_id)
        appending = (
            current is not None
            and len(state.barriers) == len(current.barriers) + 1
            and state.barriers[:-1] == current.barriers
        )
        replacing_blocked = (
            current is not None
            and len(state.barriers) == len(current.barriers)
            and bool(state.barriers)
            and state.barriers[:-1] == current.barriers[:-1]
        )
        if not (appending or replacing_blocked):
            raise ExecutionConflictError("execution task state is stale or regressive")
        states[state.task_id] = state
        snapshot = replace(
            record.snapshot,
            backend_revision=record.revision + 1,
            tasks=tuple(states[item.task_id] for item in record.snapshot.tasks),
        )
        self._record = self._commit_backend(record.revision, snapshot, owner)

    def _execution_state(self, task_id: str) -> ExecutionTaskState:
        record = self._record or self._read_backend()
        if record is None:
            raise ExecutionConflictError("execution state is unavailable")
        return next(state for state in record.snapshot.tasks if state.task_id == task_id)

    def _ensure_reconciliation_journal(self, task_id: str, owner: OwnerCapability) -> None:
        phase = self._journal_phase(task_id)
        if phase in {None, "memory-recovered", "action-complete", "amendment-accepted"}:
            self.journal.append("reconciliation-pending", task_id=task_id, owner=owner)
        elif phase != "reconciliation-pending":
            raise ExecutionConflictError("reconciliation journal state changed")

    def _submission_artifact(
        self,
        task: PlanTaskV1,
        result: ArtifactRef | AnswerSynthesisVerification | AnswerSelectionVerification | CandidateVerification | object,
    ) -> ArtifactRef:
        if isinstance(result, ArtifactRef):
            if task.reconciliation_policy == "synthesis-required":
                raise ExecutionPendingError("task reconciliation requires verified synthesis")
            if task.checks:
                raise ExecutionPendingError("single-seat selection cannot prove declared checks")
            self.artifacts.read_bytes(result)
            state = self._execution_state(task.id)
            policy = task.provider_policy or self.plan.defaults
            if len(state.barriers) != policy.rounds:
                raise ExecutionPendingError("final provider barrier is not durable")
            barrier = self._restore_barrier(state.barriers[-1], self._packet_for(task))
            answers = {
                terminal.answer_ref
                for terminal in barrier.valid_terminals
                if getattr(terminal, "answer_ref", None) is not None
            }
            if result not in answers:
                raise ExecutionPendingError("selected result is not a verified final-barrier answer")
            return result
        if isinstance(result, AnswerSelectionVerification):
            if task.execution_class != "read-only":
                raise ExecutionPendingError("answer selection is valid only for read-only reconciliation")
            try:
                validate_answer_selection(self._require_controller(), self.artifacts, result)
            except CandidateValidationError as error:
                raise ExecutionPendingError("answer selection evidence is unauthenticated") from error
            self._validate_selection_binding(task, result)
            return result.answer_ref
        if isinstance(result, AnswerSynthesisVerification):
            if task.execution_class != "read-only":
                raise ExecutionPendingError("answer synthesis is valid only for read-only reconciliation")
            try:
                validate_answer_synthesis(self._require_controller(), self.artifacts, result)
            except CandidateValidationError as error:
                raise ExecutionPendingError("answer synthesis evidence is unauthenticated") from error
            state = self._execution_state(task.id)
            self._validate_answer_binding(
                task, result, state.decision_plan_revision,
                state.decision_plan_sha256, state,
            )
            return result.answer_ref
        if not isinstance(result, CandidateVerification):
            raise ExecutionPendingError("reconciliation requires verified result evidence")
        if task.execution_class != "repo-write":
            raise ExecutionPendingError("candidate synthesis is valid only for repo-write reconciliation")
        self._validate_candidate_binding(task, result)
        path = candidate_result_path(task.id, result.candidate.digest, result.evidence_digest)
        return self._write_exact(path, result.candidate.manifest_bytes)

    def _validate_selection_binding(
        self,
        task: PlanTaskV1,
        receipt: AnswerSelectionVerification,
        state: ExecutionTaskState | None = None,
    ) -> None:
        state = self._execution_state(task.id) if state is None else state
        if (
            task.reconciliation_policy != "select-or-synthesize"
            or not receipt.valid
            or receipt.run_id != self.inputs.run_id
            or receipt.task_id != task.id
            or receipt.plan_sha256 != state.decision_plan_sha256
            or receipt.plan_revision != state.decision_plan_revision
            or receipt.checks != task.checks
            or (self.baseline is not None and receipt.baseline_digest != self.baseline.digest)
        ):
            raise ExecutionPendingError("checked selection lacks exact fresh task verification")
        policy = task.provider_policy or self.plan.defaults
        if len(state.barriers) != policy.rounds:
            raise ExecutionPendingError("checked selection lacks a final provider barrier")
        barrier = self._restore_barrier(state.barriers[-1], self._packet_for(task))
        if not any(
            terminal.seat_id == receipt.seat_id
            and terminal.answer_ref == receipt.selected_ref
            for terminal in barrier.valid_terminals
        ):
            raise ExecutionPendingError("checked selection differs from exact final-barrier seat")

    def _validate_candidate_binding(
        self,
        task: PlanTaskV1,
        result: CandidateVerification,
        state: ExecutionTaskState | None = None,
    ) -> None:
        controller = self._require_controller()
        baseline = self._require_baseline(task)
        try:
            validate_candidate_verification(controller, result)
        except CandidateValidationError as error:
            raise ExecutionPendingError("candidate synthesis verification is unauthenticated") from error
        if (
            not result.valid
            or result.baseline_digest != baseline.digest
            or result.checks != task.checks
        ):
            raise ExecutionPendingError("candidate synthesis has not passed exact fresh verification")
        state = self._execution_state(task.id) if state is None else state
        policy = task.provider_policy or self.plan.defaults
        if len(state.barriers) != policy.rounds:
            raise ExecutionPendingError("final provider barrier is not durable")
        if result.reconciliation != CandidateReconciliationBinding(
            self.inputs.run_id, task.id, state.decision_plan_sha256,
            state.decision_plan_revision, state.barriers[-1],
        ):
            raise ExecutionPendingError("candidate lacks a fresh task-bound verifier receipt")
        barrier = self._restore_barrier(state.barriers[-1], self._packet_for(task))
        valid_seats = {terminal.seat_id for terminal in barrier.valid_terminals}
        collected = self._frozen_repo_candidates(task, baseline, state)
        direct_digests = Counter(
            item.candidate.digest for item in collected if item.seat_id in valid_seats
        )
        sources = result.candidate.source_candidate_digests
        requested = Counter(sources)
        if (
            task.reconciliation_policy != "synthesis-required"
            or len(sources) < 2
            or any(count > direct_digests[digest] for digest, count in requested.items())
        ):
            raise ExecutionPendingError(
                "candidate lacks exact final-seat synthesis provenance"
            )

    def _validate_answer_binding(
        self,
        task: PlanTaskV1,
        receipt: AnswerSynthesisVerification,
        plan_revision: int,
        plan_sha256: str,
        state: ExecutionTaskState | None = None,
    ) -> None:
        if (
            not receipt.valid
            or receipt.run_id != self.inputs.run_id
            or receipt.task_id != task.id
            or receipt.plan_sha256 != plan_sha256
            or receipt.plan_revision != plan_revision
            or receipt.checks != task.checks
            or (bool(task.checks) and self.baseline is not None
                and receipt.baseline_digest != self.baseline.digest)
        ):
            raise ExecutionPendingError("answer synthesis lacks exact run, plan, or declared check verification")
        state = self._execution_state(task.id) if state is None else state
        policy = task.provider_policy or self.plan.defaults
        if len(state.barriers) != policy.rounds:
            raise ExecutionPendingError("final provider barrier is not durable")
        barrier = self._restore_barrier(state.barriers[-1], self._packet_for(task))
        answers = {
            terminal.seat_id: terminal.answer_ref
            for terminal in barrier.valid_terminals
            if terminal.answer_ref is not None
        }
        if any(answers.get(seat_id) != ref for seat_id, ref in receipt.source_answers):
            raise ExecutionPendingError("answer synthesis source is not an exact final-barrier answer")
        if receipt.answer_ref.digest in {ref.digest for ref in answers.values()}:
            raise ExecutionPendingError("answer synthesis is a final seat answer, not a new synthesis")

    def _validate_completed_results(self, record: ExecutionRecord | None = None) -> None:
        if record is None:
            record = self._record or self._read_backend(pending=self._pending_scheduler_revision())
        if record is None:
            return
        states = {state.task_id: state for state in record.snapshot.tasks}
        scheduler_tasks = {
            task.id for task in self.scheduler.plan.tasks if task.kind == "work"
        }
        for task in self._work_tasks:
            if (task.id not in scheduler_tasks
                    or task.execution_class not in {"read-only", "repo-write"}
                    or self.scheduler.task_phase(task.id) != "completed"):
                continue
            result = self.scheduler.result_for(task.id)
            state = states.get(task.id)
            if not isinstance(result, ReconciledResult) or state is None:
                raise ExecutionConflictError("completed task lacks a durable reconciled result")
            try:
                if task.execution_class == "repo-write":
                    verification = load_candidate_verification(
                        self._require_controller(), self.artifacts,
                        task_id=task.id, candidate_ref=result.artifact,
                    )
                    self._validate_candidate_binding(task, verification, state)
                elif result.artifact.path.startswith("results/answer-selections/"):
                    selection = load_answer_selection(
                        self._require_controller(), self.artifacts,
                        run_id=self.inputs.run_id, task_id=task.id,
                        answer_ref=result.artifact,
                    )
                    self._validate_selection_binding(task, selection, state)
                elif result.artifact.path.startswith("results/answers/"):
                    receipt = load_answer_synthesis(
                        self._require_controller(), self.artifacts,
                        run_id=self.inputs.run_id, task_id=task.id,
                        answer_ref=result.artifact,
                    )
                    preparation = self.preparations[task.id]
                    self._validate_answer_binding(
                        task, receipt, result.plan_revision,
                        preparation.packet.compiled_plan_sha256,
                        state,
                    )
                else:
                    if task.checks or task.reconciliation_policy == "synthesis-required":
                        raise ExecutionPendingError("completed answer lacks required synthesis receipt")
                    self.artifacts.read_bytes(result.artifact)
                    policy = task.provider_policy or self.plan.defaults
                    if len(state.barriers) != policy.rounds:
                        raise ExecutionPendingError("completed selection lacks a final provider barrier")
                    barrier = self._restore_barrier(state.barriers[-1], self._packet_for(task))
                    if result.artifact not in {
                        terminal.answer_ref for terminal in barrier.valid_terminals
                    }:
                        raise ExecutionPendingError("completed selection differs from final provider answers")
            except (CandidateValidationError, ExecutionPendingError, ExecutionValidationError,
                    ArtifactError, KeyError) as error:
                raise ExecutionConflictError("completed synthesis receipt is invalid") from error

    def _frozen_repo_candidates(
        self,
        task: PlanTaskV1,
        baseline: RepositoryBaseline,
        state: ExecutionTaskState | None = None,
    ) -> tuple[CollectedCandidate, ...]:
        state = self._execution_state(task.id) if state is None else state
        if not state.barriers:
            raise ExecutionPendingError("final repository source snapshot is unavailable")
        packet = self._packet_for(task)
        barrier = self._restore_barrier(state.barriers[-1], packet)
        sources = getattr(barrier, "candidate_sources", ())
        if not sources:
            raise ExecutionPendingError("final repository source snapshot is unavailable")
        valid_seats = {terminal.seat_id for terminal in barrier.valid_terminals}
        if {seat_id for seat_id, _ in sources} != valid_seats:
            raise ExecutionPendingError("final repository source seats differ from barrier")
        collected: list[CollectedCandidate] = []
        for seat_id, ref in sources:
            data = self.artifacts.read_bytes(ref)
            candidate = CandidateBundle.from_manifest(data)
            if (
                candidate.baseline_digest != baseline.digest
                or candidate.digest != ref.digest
                or ref.path != source_candidate_artifact_path(
                    packet.run_id, task.id, packet.attempt, barrier.round,
                    seat_id,
                )
            ):
                raise ExecutionPendingError("final repository candidate snapshot differs from barrier")
            collected.append(CollectedCandidate(seat_id, candidate))
        return tuple(collected)

    def _packet_for(self, task: PlanTaskV1) -> TaskPacket:
        preparation = self.preparations[task.id]
        dependencies = tuple(
            self.scheduler.result_for(dependency).artifact
            for dependency in task.depends_on
        )
        return dataclasses.replace(preparation.packet, dependency_artifacts=dependencies)

    def _write_exact(self, path: str, data: bytes) -> ArtifactRef:
        try:
            return self.artifacts.write_bytes(path, data)
        except ArtifactExistsError:
            ref = ArtifactRef(path, _digest(data), len(data))
            try:
                existing = self.artifacts.read_bytes(ref)
            except ArtifactError as error:
                raise ExecutionConflictError("result artifact equivocated") from error
            if existing != data:
                raise ExecutionConflictError("result artifact equivocated")
            return ref

    def _persist_amendment_documents(
        self,
        plan: FanoutPlanV1,
        inputs: RunInputs,
        transition: ProviderProfileTransition | None,
    ) -> None:
        """Make every replacement input durable before publishing an intent."""
        documents = (
            ("plans", canonical_json(plan.to_dict()), inputs.compiled_plan_sha256),
            ("inputs", canonical_json(inputs.to_dict()), inputs.digest),
            ("profiles", canonical_json({
                "profiles": dict(sorted(inputs.provider_profiles.items())),
                "schema_version": "fanout-provider-profiles-v1",
            }), _provider_profiles_digest(inputs.provider_profiles)),
        )
        for kind, data, expected in documents:
            if _digest(data) != expected:
                raise ExecutionConflictError("amendment document digest changed")
            self._write_exact(f"amendments/{kind}/{expected}.json", data)
        if inputs.profile_shape == "class-tier":
            from .providers import ProviderRegistry, parse_profile_binding_key
            registry = (
                transition._registry if transition is not None
                else self.coordinator.registry
            )
            if not isinstance(registry, ProviderRegistry):
                raise ExecutionConflictError("amendment profile registry is unavailable")
            for key, expected in sorted(inputs.provider_profiles.items()):
                profile = registry.select(*parse_profile_binding_key(key))
                data = canonical_json(profile.to_dict())
                if profile.digest != expected or _digest(data) != expected:
                    raise ExecutionConflictError("amendment profile descriptor changed")
                self._write_exact(
                    f"amendments/executor-profiles/{expected}.json", data,
                )

    @staticmethod
    def _read_amendment_document(
        artifacts: ArtifactStore, kind: str, digest: str,
    ) -> object:
        try:
            raw = artifacts.read_by_digest(
                f"amendments/{kind}/{digest}.json", digest,
            )
            value = json.loads(raw.decode("utf-8"))
            if canonical_json(value) != raw:
                raise ExecutionConflictError("amendment document is not canonical")
            return value
        except ExecutionConflictError:
            raise
        except (ArtifactError, OSError, UnicodeError, ValueError, TypeError) as error:
            raise ExecutionConflictError("amendment document is missing or invalid") from error

    def _task(self, task_id: str) -> PlanTaskV1:
        _identity(task_id, "execution task id")
        matches = [task for task in self.plan.tasks if task.kind == "work" and task.id == task_id]
        if len(matches) != 1:
            raise ExecutionValidationError("execution task is unknown")
        return matches[0]

    def _require_baseline(self, task: PlanTaskV1) -> RepositoryBaseline:
        if task.execution_class != "repo-write" or self.baseline is None:
            raise ExecutionValidationError("command requires repo-write baseline evidence")
        return self.baseline

    def _require_controller(self) -> LifecycleController:
        if not isinstance(self.lifecycle_controller, LifecycleController):
            raise ExecutionValidationError("command requires an authenticated lifecycle controller")
        try:
            assert_controller(self.lifecycle_controller)
        except LifecycleError as error:
            raise ExecutionValidationError("lifecycle controller authentication failed") from error
        return self.lifecycle_controller

    def _new_task_state(
        self,
        task: PlanTaskV1,
        plan_revision: int,
    ) -> ExecutionTaskState:
        task_sha256 = _digest(canonical_json(task.to_dict()))
        if task.execution_class == "orchestrator-action":
            return ExecutionTaskState(
                task.id,
                "",
                plan_revision,
                self._plan_digest,
                self.inputs.digest,
                task_sha256,
                "",
            )
        preparation = self.preparations[task.id]
        if preparation.plan_revision != plan_revision:
            raise ExecutionValidationError(
                "task preparation belongs to another plan revision"
            )
        return ExecutionTaskState(
            task.id,
            preparation.digest,
            preparation.plan_revision,
            preparation.packet.compiled_plan_sha256,
            preparation.inputs.digest,
            task_sha256,
            preparation.packet.context_sha256,
        )

    def _journal_amendments(self) -> Mapping[int, object]:
        amendments = getattr(getattr(self.journal, "state", None), "amendments", None)
        if not isinstance(amendments, Mapping):
            raise ExecutionValidationError("journal amendment state is unavailable")
        return amendments

    def _journal_amendment(self, revision: int) -> object | None:
        record = self._journal_amendments().get(revision)
        if record is None:
            return None
        binding = getattr(record, "binding", None)
        if (
            getattr(record, "revision", None) != revision
            or getattr(record, "phase", None)
            not in {"plan-amendment-intent", "plan-amendment-accepted"}
            or not isinstance(binding, tuple)
            or len(binding) != 6
            or any(not _is_digest(item) for item in binding)
        ):
            raise ExecutionConflictError("journal amendment record is invalid")
        return record

    def _decision_bindings(self) -> dict[int, tuple[str, str]]:
        bindings = {
            1: (
                self.journal.inputs.compiled_plan_sha256,
                self.journal.inputs.digest,
            )
        }
        for revision, record in self._journal_amendments().items():
            checked = self._journal_amendment(revision)
            assert checked is record
            if getattr(record, "phase") == "plan-amendment-accepted":
                binding = getattr(record, "binding")
                bindings[revision] = (
                    binding[2],
                    binding[3],
                )
        return bindings

    def _workspace_was_dispatched(self, task_id: str, seat_id: str) -> bool:
        phases = getattr(getattr(self.journal, "state", None), "seat_phases", None)
        if isinstance(phases, Mapping):
            return any(key[:2] == (task_id, seat_id) for key in phases)
        record = self._record
        if record is None:
            raw = self.backend.read()
            record = raw if isinstance(raw, ExecutionRecord) else None
        if record is None:
            return False
        state = next(
            (item for item in record.snapshot.tasks if item.task_id == task_id),
            None,
        )
        return state is not None and bool(state.barriers)

    def _journal_phase(self, task_id: str) -> str | None:
        phases = getattr(getattr(self.journal, "state", None), "task_phases", None)
        if not isinstance(phases, Mapping):
            raise ExecutionValidationError("journal task state is unavailable")
        value = phases.get(task_id)
        if value is not None and not isinstance(value, str):
            raise ExecutionValidationError("journal task phase is invalid")
        return value

    def _journal_handover(self, task_id: str) -> object | None:
        handovers = getattr(getattr(self.journal, "state", None), "handovers", None)
        if not isinstance(handovers, Mapping):
            raise ExecutionValidationError("journal handover state is unavailable")
        record = handovers.get(task_id)
        if record is None:
            return None
        phase = getattr(record, "phase", None)
        root = getattr(record, "transaction_root", None)
        transaction_sha256 = getattr(record, "transaction_sha256", None)
        disposition_sha256 = getattr(record, "disposition_sha256", None)
        if (
            getattr(record, "task_id", None) != task_id
            or phase
            not in {
                "handover-intent",
                "handover-complete",
                "handover-rolled-back",
                "handover-conflict",
                "handover-not-mutated",
            }
            or not isinstance(root, str)
            or not Path(root).is_absolute()
            or str(Path(root)) != root
            or not _is_digest(transaction_sha256)
            or (phase == "handover-intent" and disposition_sha256 is not None)
            or (phase != "handover-intent" and not _is_digest(disposition_sha256))
        ):
            raise ExecutionConflictError("journal handover record is invalid")
        return record

    def _replacement(
        self,
        plan: FanoutPlanV1,
        inputs: RunInputs,
        preparations: Mapping[str, TaskPreparation],
        profiles: Mapping[str, str],
    ) -> "ExecutionService":
        candidate = ExecutionService(
            plan=plan,
            inputs=inputs,
            preparations=preparations,
            provider_profile_digests=profiles,
            budget=self.budget,
            scheduler=self.scheduler,
            journal=self.journal,
            coordinator=self.coordinator,
            artifacts=self.artifacts,
            backend=self.backend,
            memory_preflight=self.memory_preflight,
            baseline=self.baseline,
            lifecycle_controller=self.lifecycle_controller,
            limits=self.limits,
        )
        candidate._provider_transition_issuer = self._provider_transition_issuer
        candidate._provider_transition_service_sha256 = (
            self._provider_transition_service_sha256
        )
        return candidate

    def _validate_initial_capacity(self) -> None:
        tasks = tuple(task for task in self.plan.tasks if task.kind == "work")
        if len(tasks) > self.limits.max_tasks:
            raise ExecutionValidationError("execution task limit exceeded")
        capacity_revision = max(
            (preparation.plan_revision for preparation in self.preparations.values()),
            default=max(1, getattr(self.scheduler, "plan_revision", 1)),
        )
        states = tuple(
            self._new_task_state(
                task,
                capacity_revision
                if task.execution_class == "orchestrator-action"
                else self.preparations[task.id].plan_revision,
            )
            for task in tasks
        )
        snapshot = ExecutionSnapshot(
            self.inputs.run_id,
            self._plan_digest,
            self.inputs.digest,
            self._backend_identity,
            self._backend_key,
            1,
            capacity_revision,
            states,
        )
        snapshot_bytes = canonical_json(snapshot.to_dict())
        if len(snapshot_bytes) > self.limits.max_snapshot_bytes:
            raise ExecutionValidationError("execution snapshot byte limit exceeded")
        record = ExecutionRecord(1, snapshot)
        if len(record.canonical_bytes) > self.limits.max_backend_record_bytes:
            raise ExecutionValidationError("execution backend record byte limit exceeded")
        status = ExecutionStatus(
            self.inputs.run_id,
            0,
            0,
            0,
            self._projected_turns,
            tuple(ExecutionTaskStatus(task.id, "unscheduled", 0) for task in tasks),
        )
        if len(canonical_json(status.to_dict())) > self.limits.max_status_bytes:
            raise ExecutionValidationError("execution status byte limit exceeded")


__all__ = [
    "AmendmentDocuments",
    "ExecutionBackend",
    "ExecutionBudget",
    "ExecutionLimits",
    "ExecutionRecord",
    "ExecutionService",
    "ExecutionSnapshot",
    "ExecutionStatus",
    "ExecutionTaskState",
    "ExecutionTaskStatus",
    "ProviderProfileTransition",
    "TaskPreparation",
]
