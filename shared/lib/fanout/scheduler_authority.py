"""Authority-anchored deterministic scheduling over a validated fanout plan."""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Mapping, Protocol, runtime_checkable

from .artifacts import ArtifactRef, ArtifactStore, canonical_json
from .errors import (
    ArtifactError, FanoutError, RunAuthorizationError, RunStateError,
    SchedulerConflictError, SchedulerStateError,
)
from .plan import FanoutPlanV1, FanoutPlanV2, PlanTaskV1, PlanTaskV2, validate_plan
from .runstate import (
    AnchorRevision, BranchHandoverIntentV2, HandoverTerminalV2, LocalAnchorAuthority,
    OwnerCapability, RemoteAnchorAuthority, RunInputs, RunInspection, RunJournal,
    _AnchorAuthority, _require_anchor_store,
)
from .controller import LifecycleController, assert_controller
from .errors import CandidateValidationError
from .verification import TargetEvidenceEnvelope, load_target_candidate, load_target_verification

_SNAPSHOT_SCHEMA = "fanout-scheduler-v2"
_TARGET_SNAPSHOT_SCHEMA = "fanout-scheduler-v3"
_AUTHORITY_SCHEMA = "fanout-scheduler-authority-v2"
_DECISION_SCHEMA = "fanout-scheduler-decision-v1"
_RECEIPT_SCHEMA = "fanout-scheduler-result-v1"
_ZERO = "0" * 64
_PHASES = frozenset({
    "unscheduled", "scheduled", "active", "reconciliation-pending",
    "blocked-action", "completed", "failed",
})
_ACTIVE_PHASES = frozenset({
    "scheduled", "active", "reconciliation-pending", "blocked-action",
})
_MAX_ACTIVE_TASKS = 2
_MAX_ACTIVE_SEATS = 6
_MAX_AUTHORITY_BYTES = 256 * 1024
_BLOCKED_HANDOVER_REASON = "blocked handover dependency"


@dataclass(frozen=True, slots=True)
class ReconciledResult:
    """A content-addressed result receipt bound to one exact scheduled task."""

    run_id: str
    task_id: str
    plan_revision: int
    plan_sha256: str
    inputs_digest: str
    artifact: ArtifactRef

    def __post_init__(self) -> None:
        _identity(self.run_id, "receipt run id")
        _identity(self.task_id, "receipt task id")
        _positive_int(self.plan_revision, "receipt plan revision")
        _digest(self.plan_sha256, "receipt plan digest")
        _digest(self.inputs_digest, "receipt inputs digest")
        _artifact(self.artifact)


@dataclass(frozen=True, slots=True)
class ScheduledDependencyV2:
    kind: str
    source_task_id: str
    source_target_id: str
    receipt_sha256: str
    artifact: ArtifactRef

    def __post_init__(self) -> None:
        if self.kind not in {"artifact", "handover"}:
            raise SchedulerStateError("scheduled dependency mode is invalid")
        _identity(self.source_task_id, "dependency source task")
        _identity(self.source_target_id, "dependency source target")
        _digest(self.receipt_sha256, "dependency receipt digest")
        _artifact(self.artifact)


@dataclass(frozen=True, slots=True)
class WorkDispatchV2:
    task_id: str
    plan_revision: int
    seat_count: int
    dependencies: tuple[ScheduledDependencyV2, ...]
    dispatch_id: str


@dataclass(frozen=True, slots=True)
class ActionBarrierV2:
    task_id: str
    plan_revision: int
    dependencies: tuple[ScheduledDependencyV2, ...]
    barrier_id: str


@dataclass(frozen=True, slots=True)
class WorkDispatch:
    task_id: str
    plan_revision: int
    seat_count: int
    dependencies: tuple[ReconciledResult, ...]
    dispatch_id: str


@dataclass(frozen=True, slots=True)
class ActionBarrier:
    task_id: str
    plan_revision: int
    dependencies: tuple[ReconciledResult, ...]
    barrier_id: str


@dataclass(frozen=True, slots=True)
class PlanAmendment:
    plan_revision: int
    affected_task_ids: tuple[str, ...]
    plan_sha256: str


@dataclass(frozen=True, slots=True)
class BlockedHandoverInspection:
    blocked_target_ids: tuple[str, ...]
    backend_revision: int


@dataclass(frozen=True, slots=True)
class SchedulerTaskState:
    task_id: str
    phase: str = "unscheduled"
    plan_revision: int = 0
    seat_count: int = 0
    dependencies: tuple[ReconciledResult, ...] = ()
    result: ReconciledResult | None = None
    failure_reason: str | None = None
    decision_plan_sha256: str = ""
    decision_inputs_digest: str = ""
    decision_id: str = ""


@dataclass(frozen=True, slots=True)
class SchedulerTaskStateV2(SchedulerTaskState):
    dependencies: tuple[ScheduledDependencyV2, ...] = ()
    handover_terminal: HandoverTerminalV2 | None = None


@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    plan: FanoutPlanV1
    plan_sha256: str
    plan_revision: int
    run_id: str
    inputs_digest: str
    backend_identity: str
    backend_key: str
    backend_revision: int
    previous_commit: str
    tasks: tuple[SchedulerTaskState | SchedulerTaskStateV2, ...]
    schema_version: str = _SNAPSHOT_SCHEMA


@dataclass(frozen=True, slots=True)
class BackendRecord:
    revision: int
    snapshot: SchedulerSnapshot

    def __post_init__(self) -> None:
        _positive_int(self.revision, "backend revision")
        if not isinstance(self.snapshot, SchedulerSnapshot):
            raise SchedulerStateError("backend record snapshot is invalid")


@runtime_checkable
class SchedulerBackend(Protocol):
    """Durable full-state CAS storage; authority is a separate monotonic store."""

    def identity(self) -> str: ...
    def key(self) -> str: ...
    def read(self) -> BackendRecord | None: ...
    def compare_and_set(
        self, expected_revision: int, snapshot: SchedulerSnapshot, *, owner: OwnerCapability,
    ) -> BackendRecord: ...


class Scheduler:
    """Choose dependency-ready work only after authority-anchored state commits."""

    def __init__(self, backend: SchedulerBackend, artifacts: ArtifactStore, inputs: RunInputs,
                 record: BackendRecord, authority: _AnchorAuthority, authority_id: str,
                 authority_key: str, authority_revision: int,
                 authority_record: dict[str, object], *, journal: RunJournal | None = None,
                 lifecycle_controller: LifecycleController | None = None) -> None:
        self._backend = backend
        self._artifacts = artifacts
        self._inputs = inputs
        self._record = record
        self._authority = authority
        self._authority_id = authority_id
        self._authority_key = authority_key
        self._authority_revision = authority_revision
        self._authority_record = authority_record
        self._journal = journal
        self._lifecycle_controller = lifecycle_controller
        self._failed = False

    @classmethod
    def create(cls, plan: FanoutPlanV1 | Mapping[str, object], inputs: RunInputs,
               backend: SchedulerBackend, artifacts: ArtifactStore, *, owner: OwnerCapability,
               anchor_store: LocalAnchorAuthority | RemoteAnchorAuthority,
               journal: RunJournal | None = None,
               lifecycle_controller: LifecycleController | None = None) -> "Scheduler":
        return cls._create_impl(
            plan, inputs, backend, artifacts, owner=owner,
            anchor_store=anchor_store, allow_test=False, journal=journal,
            lifecycle_controller=lifecycle_controller,
        )

    @classmethod
    def _create_for_test(cls, plan: FanoutPlanV1 | Mapping[str, object], inputs: RunInputs,
                         backend: SchedulerBackend, artifacts: ArtifactStore, *, owner: OwnerCapability,
                         anchor_store: _AnchorAuthority, journal: RunJournal | None = None,
                         lifecycle_controller: LifecycleController | None = None) -> "Scheduler":
        return cls._create_impl(
            plan, inputs, backend, artifacts, owner=owner,
            anchor_store=anchor_store, allow_test=True, journal=journal,
            lifecycle_controller=lifecycle_controller,
        )

    @classmethod
    def _create_impl(cls, plan: FanoutPlanV1 | Mapping[str, object], inputs: RunInputs,
                     backend: SchedulerBackend, artifacts: ArtifactStore, *, owner: OwnerCapability,
                     anchor_store: _AnchorAuthority, allow_test: bool,
                     journal: RunJournal | None, lifecycle_controller: LifecycleController | None) -> "Scheduler":
        plan = validate_plan(plan)
        _owner(owner)
        _target_authority(plan, inputs, artifacts, journal, lifecycle_controller,
                          owner=owner, allow_test=allow_test)
        _artifact_store(artifacts)
        backend_id, backend_key = _backend_identity(backend)
        authority_id = _authority_identity(anchor_store, allow_test=allow_test)
        _validate_inputs(plan, inputs)
        _preflight_plan(plan)
        snapshot = _initial_snapshot(plan, inputs, backend_id, backend_key, artifacts)
        authority_key = _authority_key(owner, inputs.run_id, backend_id, backend_key)
        binding = _snapshot_binding(snapshot)
        pending = _authority_record(
            1, authority_id, authority_key, inputs.run_id, backend_id, backend_key,
            committed=None, pending=binding, owner=owner,
        )
        try:
            _authority_create(anchor_store, authority_key, pending)
        except SchedulerConflictError:
            pass
        return cls._resume_impl(
            plan, inputs, backend, artifacts, owner=owner,
            anchor_store=anchor_store, allow_test=allow_test, journal=journal,
            lifecycle_controller=lifecycle_controller,
        )

    @classmethod
    def resume(cls, expected_plan: FanoutPlanV1 | Mapping[str, object],
               expected_inputs: RunInputs, backend: SchedulerBackend,
               artifacts: ArtifactStore, *, owner: OwnerCapability,
               anchor_store: LocalAnchorAuthority | RemoteAnchorAuthority,
               journal: RunJournal | None = None,
               lifecycle_controller: LifecycleController | None = None) -> "Scheduler":
        return cls._resume_impl(
            expected_plan, expected_inputs, backend, artifacts, owner=owner,
            anchor_store=anchor_store, allow_test=False, journal=journal,
            lifecycle_controller=lifecycle_controller,
        )

    @classmethod
    def resume_exact_v2(cls, expected_plan: FanoutPlanV2 | Mapping[str, object],
                        expected_inputs: RunInputs, backend: SchedulerBackend,
                        artifacts: ArtifactStore, *, owner: OwnerCapability,
                        anchor_store: LocalAnchorAuthority | RemoteAnchorAuthority,
                        journal: RunJournal,
                        lifecycle_controller: LifecycleController) -> "Scheduler":
        """Reopen owner handover authority without repairing pending scheduler state."""
        if not isinstance(validate_plan(expected_plan), FanoutPlanV2):
            raise SchedulerStateError("exact owner reopen requires a v2 plan")
        return cls._resume_impl(
            expected_plan, expected_inputs, backend, artifacts, owner=owner,
            anchor_store=anchor_store, allow_test=False, journal=journal,
            lifecycle_controller=lifecycle_controller, allow_repair=False,
        )

    @classmethod
    def _resume_for_test(cls, expected_plan: FanoutPlanV1 | Mapping[str, object],
                         expected_inputs: RunInputs, backend: SchedulerBackend,
                         artifacts: ArtifactStore, *, owner: OwnerCapability,
                         anchor_store: _AnchorAuthority, journal: RunJournal | None = None,
                         lifecycle_controller: LifecycleController | None = None) -> "Scheduler":
        return cls._resume_impl(
            expected_plan, expected_inputs, backend, artifacts, owner=owner,
            anchor_store=anchor_store, allow_test=True, journal=journal,
            lifecycle_controller=lifecycle_controller,
        )

    @classmethod
    def _resume_impl(cls, expected_plan: FanoutPlanV1 | Mapping[str, object],
                     expected_inputs: RunInputs, backend: SchedulerBackend,
                     artifacts: ArtifactStore, *, owner: OwnerCapability,
                     anchor_store: _AnchorAuthority, allow_test: bool,
                     journal: RunJournal | None, lifecycle_controller: LifecycleController | None,
                     allow_repair: bool = True) -> "Scheduler":
        plan = validate_plan(expected_plan)
        _owner(owner)
        _target_authority(plan, expected_inputs, artifacts, journal, lifecycle_controller,
                          owner=owner, allow_test=allow_test)
        _artifact_store(artifacts)
        backend_id, backend_key = _backend_identity(backend)
        authority_id = _authority_identity(anchor_store, allow_test=allow_test)
        _validate_inputs(plan, expected_inputs)
        authority_key = _authority_key(owner, expected_inputs.run_id, backend_id, backend_key)
        revision, authority = _authority_read(
            anchor_store, authority_id, authority_key, expected_inputs.run_id,
            backend_id, backend_key, owner,
        )
        try:
            record = backend.read()
        except Exception as error:
            raise SchedulerConflictError("scheduler backend read failed") from error
        if record is not None:
            if not isinstance(record, BackendRecord):
                raise SchedulerStateError("scheduler backend has no valid state")
            _validate_backend_record(record)
            _validate_snapshot(record.snapshot, expected_inputs, backend_id, backend_key, artifacts)
        pending = authority["pending"]
        committed = authority["committed"]
        if pending is not None:
            if not allow_repair:
                raise SchedulerStateError("scheduler authority is pending; explicit recovery required")
            if record is None:
                if committed is not None:
                    raise SchedulerStateError("scheduler backend diverges from committed authority state")
                initial = _initial_snapshot(
                    plan, expected_inputs, backend_id, backend_key, artifacts,
                )
                if pending != _snapshot_binding(initial):
                    raise SchedulerStateError("scheduler initial authority binding is divergent")
                record = _commit_backend(backend, 0, initial, owner)
            if _record_matches_binding(record, pending):
                resolved = _authority_record(
                    revision.revision + 1, authority_id, authority_key,
                    expected_inputs.run_id, backend_id, backend_key,
                    committed=pending, pending=None, owner=owner,
                )
                revision = _authority_cas(anchor_store, authority_key, revision.revision, resolved)
                authority = resolved
            elif committed is not None and _record_matches_binding(record, committed):
                resolved = _authority_record(
                    revision.revision + 1, authority_id, authority_key,
                    expected_inputs.run_id, backend_id, backend_key,
                    committed=committed, pending=None, owner=owner,
                )
                revision = _authority_cas(anchor_store, authority_key, revision.revision, resolved)
                authority = resolved
            else:
                raise SchedulerStateError("scheduler backend diverges from pending authority state")
        if record is None:
            raise SchedulerStateError("scheduler backend has no valid state")
        committed = authority["committed"]
        if committed is None or not _record_matches_binding(record, committed):
            raise SchedulerStateError("scheduler backend rollback or revision mismatch")
        _validate_snapshot(record.snapshot, expected_inputs, backend_id, backend_key, artifacts)
        if record.snapshot.plan != plan or record.snapshot.plan_sha256 != _plan_digest(plan):
            raise SchedulerStateError("scheduler plan drift detected during resume")
        if record.snapshot.inputs_digest != expected_inputs.digest:
            raise SchedulerStateError("scheduler run input drift detected during resume")
        scheduler = cls(
            backend, artifacts, expected_inputs, record, anchor_store, authority_id,
            authority_key, revision.revision, authority, journal=journal,
            lifecycle_controller=lifecycle_controller,
        )
        if isinstance(plan, FanoutPlanV2):
            for state in record.snapshot.tasks:
                if isinstance(state, SchedulerTaskStateV2) and state.handover_terminal is not None:
                    scheduler._authenticate_handover(
                        state.task_id, state.handover_terminal, allow_blocked=True,
                    )
            if allow_repair:
                scheduler._quarantine_blocked(owner)
        return scheduler

    @classmethod
    def inspect_blocked(cls, expected_plan: FanoutPlanV2 | Mapping[str, object],
                        expected_inputs: RunInputs, run_root: Path | str,
                        artifacts: ArtifactStore, *, owner: OwnerCapability,
                        original_journal_inputs: RunInputs,
                        anchor_store: LocalAnchorAuthority | RemoteAnchorAuthority,
                        lifecycle_controller: LifecycleController) -> BlockedHandoverInspection:
        return cls._inspect_blocked_impl(
            expected_plan, expected_inputs, run_root, artifacts, owner=owner,
            original_journal_inputs=original_journal_inputs,
            journal_anchor=anchor_store, scheduler_anchor=anchor_store,
            lifecycle_controller=lifecycle_controller, allow_test=False,
        )

    @classmethod
    def inspect_status(cls, expected_plan: FanoutPlanV2 | Mapping[str, object],
                       expected_inputs: RunInputs, run_root: Path | str,
                       artifacts: ArtifactStore, *, owner: OwnerCapability,
                       original_journal_inputs: RunInputs,
                       anchor_store: LocalAnchorAuthority | RemoteAnchorAuthority,
                       lifecycle_controller: LifecycleController):
        """Authenticate and project v2 scheduler status without any authority CAS."""
        return cls._inspect_blocked_impl(
            expected_plan, expected_inputs, run_root, artifacts, owner=owner,
            original_journal_inputs=original_journal_inputs,
            journal_anchor=anchor_store, scheduler_anchor=anchor_store,
            lifecycle_controller=lifecycle_controller, allow_test=False,
            project_status=True,
        )

    @classmethod
    def _inspect_blocked_for_test(cls, expected_plan: FanoutPlanV2 | Mapping[str, object],
                                  expected_inputs: RunInputs, run_root: Path | str,
                                  artifacts: ArtifactStore, *, owner: OwnerCapability,
                                  original_journal_inputs: RunInputs,
                                  journal_anchor: _AnchorAuthority,
                                  scheduler_anchor: _AnchorAuthority,
                                  lifecycle_controller: LifecycleController
                                  ) -> BlockedHandoverInspection:
        return cls._inspect_blocked_impl(
            expected_plan, expected_inputs, run_root, artifacts, owner=owner,
            original_journal_inputs=original_journal_inputs,
            journal_anchor=journal_anchor, scheduler_anchor=scheduler_anchor,
            lifecycle_controller=lifecycle_controller, allow_test=True,
        )

    @classmethod
    def _inspect_blocked_impl(cls, expected_plan: FanoutPlanV2 | Mapping[str, object],
                              expected_inputs: RunInputs, run_root: Path | str,
                              artifacts: ArtifactStore, *, owner: OwnerCapability,
                              original_journal_inputs: RunInputs,
                              journal_anchor: _AnchorAuthority,
                              scheduler_anchor: _AnchorAuthority,
                              lifecycle_controller: LifecycleController,
                              allow_test: bool,
                              project_status: bool = False):
        from .storage import FileSchedulerBackend
        plan = validate_plan(expected_plan)
        if not isinstance(plan, FanoutPlanV2):
            raise SchedulerStateError("blocked handover inspection requires a v2 plan")
        _owner(owner)
        _artifact_store(artifacts)
        assert_controller(lifecycle_controller)
        root = Path(run_root)
        if artifacts.root != root / "artifacts":
            raise SchedulerStateError("blocked handover inspection requires exact run artifacts")
        _validate_inputs(plan, expected_inputs)
        inspection: RunInspection = (
            RunJournal._inspect_for_test(
                root, original_journal_inputs, owner, anchor_store=journal_anchor,
            ) if allow_test else RunJournal.inspect(
                root, original_journal_inputs, owner, anchor_store=journal_anchor,
            )
        )
        if inspection.pending_authority or inspection.inputs != original_journal_inputs:
            raise SchedulerStateError("journal inspection is pending or belongs to another run")
        with FileSchedulerBackend.inspect(root, run_id=expected_inputs.run_id, owner=owner) as backend:
            backend_id, backend_key = _backend_identity(backend)
            authority_id = _authority_identity(scheduler_anchor, allow_test=allow_test)
            authority_key = _authority_key(owner, expected_inputs.run_id, backend_id, backend_key)
            revision, authority = _authority_read(
                scheduler_anchor, authority_id, authority_key, expected_inputs.run_id,
                backend_id, backend_key, owner,
            )
            del revision
            record = backend.read()
            if (record is None or not isinstance(record, BackendRecord)
                    or authority["pending"] is not None
                    or authority["committed"] is None
                    or not _record_matches_binding(record, authority["committed"])):
                raise SchedulerStateError("scheduler inspection lacks a committed authority binding")
            _validate_backend_record(record)
            _validate_snapshot(record.snapshot, expected_inputs, backend_id, backend_key, artifacts)
            if (record.snapshot.plan != plan
                    or record.snapshot.plan_sha256 != _plan_digest(plan)
                    or record.snapshot.inputs_digest != expected_inputs.digest):
                raise SchedulerStateError("scheduler inspection plan or input drift")
            accepted = max(
                (revision for revision, amendment in inspection.state.amendments.items()
                 if amendment.phase == "plan-amendment-accepted"),
                default=1,
            )
            if record.snapshot.plan_revision != accepted:
                raise SchedulerStateError("scheduler inspection revision lacks accepted journal amendment")
            if accepted == 1:
                if (plan != record.snapshot.plan
                        or expected_inputs != original_journal_inputs
                        or _plan_digest(plan) != original_journal_inputs.compiled_plan_sha256):
                    raise SchedulerStateError("scheduler inspection differs from original journal inputs")
            else:
                from .execute import ExecutionService
                try:
                    documents = ExecutionService.recovery_documents(
                        inspection, artifacts, revision=accepted,
                    )
                except FanoutError as error:
                    raise SchedulerStateError("scheduler inspection amendment documents are invalid") from error
                if documents.plan != plan or documents.inputs != expected_inputs:
                    raise SchedulerStateError("scheduler inspection differs from accepted amendment")
            inspected = cls(
                backend, artifacts, expected_inputs, record, scheduler_anchor,
                authority_id, authority_key, 0, authority,
                journal=inspection if project_status else None,
                lifecycle_controller=lifecycle_controller,
            )
            blocked_targets = set()
            for task_id, (intent_sha256, _reason) in inspection.state.branch_blocked.items():
                state = inspected._states().get(task_id)
                handover = inspection.state.branch_handovers.get(task_id)
                if (not isinstance(state, SchedulerTaskStateV2)
                        or handover is None or handover[0].sha256 != intent_sha256
                        or (state.handover_terminal is not None
                            and state.handover_terminal != handover[1])):
                    raise SchedulerStateError("blocked handover differs from scheduler or journal")
                if handover[1] is not None and handover[1].intent_sha256 != intent_sha256:
                    raise SchedulerStateError("blocked terminal differs from its intent")
                inspected._validate_handover_claim(task_id, handover[1], handover[0])
                blocked_targets.add(inspected._plan_tasks()[task_id].target_id)
            if project_status:
                for task_id, (_intent, terminal) in inspection.state.branch_handovers.items():
                    if terminal is not None:
                        inspected._authenticate_handover(
                            task_id, terminal, allow_blocked=task_id in inspection.state.branch_blocked,
                        )
                for state in record.snapshot.tasks:
                    if isinstance(state, SchedulerTaskStateV2) and state.handover_terminal is not None:
                        inspected._authenticate_handover(
                            state.task_id, state.handover_terminal,
                            allow_blocked=state.task_id in inspection.state.branch_blocked,
                        )
                from .execute import ExecutionService
                return ExecutionService.v2_status(inspected)
            return BlockedHandoverInspection(tuple(sorted(blocked_targets)), record.revision)

    @property
    def plan(self) -> FanoutPlanV1:
        return self._record.snapshot.plan

    @property
    def plan_sha256(self) -> str:
        return self._record.snapshot.plan_sha256

    @property
    def inputs_digest(self) -> str:
        return self._record.snapshot.inputs_digest

    @property
    def inputs(self) -> RunInputs:
        return self._inputs

    @property
    def artifacts(self) -> ArtifactStore:
        return self._artifacts

    @property
    def plan_revision(self) -> int:
        return self._record.snapshot.plan_revision

    @property
    def revision(self) -> int:
        return self._record.revision

    @property
    def active_task_count(self) -> int:
        return sum(state.phase in _ACTIVE_PHASES for state in self._record.snapshot.tasks)

    @property
    def active_seat_count(self) -> int:
        return sum(
            state.seat_count for state in self._record.snapshot.tasks
            if state.phase in _ACTIVE_PHASES
        )

    def task_phase(self, task_id: str) -> str:
        state = self._states().get(task_id)
        if state is None:
            raise SchedulerStateError(f"unknown scheduler task: {task_id}")
        if state.phase == "unscheduled" and (
            self._has_failed_ancestor(task_id, self._states(), set())
            or self._blocked_handover_dependency(task_id, set())
        ):
            return "blocked-dependency"
        return state.phase

    def assert_dispatchable(self, task_id: str) -> None:
        state = self._states().get(task_id)
        if state is None or state.phase not in _ACTIVE_PHASES:
            raise SchedulerStateError("task is not dispatchable")
        if self._blocked_handover_dependency(task_id, set()):
            raise SchedulerStateError(_BLOCKED_HANDOVER_REASON)

    def result_for(self, task_id: str) -> ReconciledResult | None:
        state = self._states().get(task_id)
        if state is None:
            raise SchedulerStateError(f"unknown scheduler task: {task_id}")
        if state.result is not None:
            self._verify_artifact(state.result.artifact)
        return state.result

    def handover_source_for(self, task_id: str) -> ReconciledResult:
        """Read one settled writer decision and its exact reconciled artifact."""
        state = self._states().get(task_id)
        task = self._plan_tasks().get(task_id)
        if (not isinstance(self.plan, FanoutPlanV2)
                or not isinstance(state, SchedulerTaskStateV2)
                or task is None or task.execution_class != "repo-write"
                or state.phase != "completed" or state.result is None
                or state.handover_terminal is not None):
            raise SchedulerStateError("handover source is absent, amended, or unsettled")
        self._verify_receipt(state.result, state)
        return state.result

    def handover_terminal_for(self, task_id: str) -> HandoverTerminalV2 | None:
        state = self._states().get(task_id)
        if state is None:
            raise SchedulerStateError(f"unknown scheduler task: {task_id}")
        if not isinstance(state, SchedulerTaskStateV2):
            raise SchedulerStateError("handover terminal requires a v2 scheduler")
        if state.handover_terminal is not None:
            self._authenticate_handover(task_id, state.handover_terminal)
        return state.handover_terminal

    def dependency_record_for(self, recipient_task_id: str, source_task_id: str):
        """Issue one Task 6 record from the authenticated declared predecessor."""
        if not isinstance(self.plan, FanoutPlanV2):
            raise SchedulerStateError("dependency records require a v2 plan")
        tasks = self._plan_tasks()
        recipient = tasks.get(recipient_task_id)
        source = tasks.get(source_task_id)
        state = self._states().get(source_task_id)
        if (recipient is None or source is None or state is None
                or source_task_id not in recipient.depends_on
                or state.phase != "completed" or state.result is None):
            raise SchedulerStateError("dependency source is absent or unsettled")
        mode = recipient.dependency_modes[source_task_id]
        if mode == "handover":
            if not isinstance(state, SchedulerTaskStateV2) or state.handover_terminal is None:
                raise SchedulerStateError("handover dependency lacks terminal")
            self._authenticate_handover(source_task_id, state.handover_terminal)
            receipt_sha256 = state.handover_terminal.terminal_sha256
            artifact = state.handover_terminal.evidence
        else:
            self._verify_receipt(state.result, state)
            receipt_sha256 = _receipt_sha256(state.result)
            artifact = state.result.artifact
        from .collaboration import DependencyRecord
        return DependencyRecord(
            self._inputs.run_id, recipient_task_id, recipient.target_id,
            source_task_id, source.target_id, mode, state.plan_revision,
            receipt_sha256, artifact,
        )

    def result_receipt(self, task_id: str, artifact: ArtifactRef) -> ReconciledResult:
        state = self._states().get(task_id)
        if state is None or state.phase not in _ACTIVE_PHASES:
            raise SchedulerStateError("result receipt requires a scheduled task or action barrier")
        self._verify_artifact(artifact)
        return ReconciledResult(
            self._inputs.run_id, task_id, state.plan_revision,
            state.decision_plan_sha256, state.decision_inputs_digest, artifact,
        )

    def pending_work(self) -> tuple[WorkDispatch | WorkDispatchV2, ...]:
        states = tuple(state for state in self._record.snapshot.tasks
                       if state.phase == "scheduled"
                       and not self._blocked_handover_dependency(state.task_id, set()))
        self._verify_state_receipts(states)
        return tuple(_work_dispatch(state) for state in states)

    def pending_actions(self) -> tuple[ActionBarrier | ActionBarrierV2, ...]:
        states = tuple(state for state in self._record.snapshot.tasks
                       if state.phase == "blocked-action"
                       and not self._blocked_handover_dependency(state.task_id, set()))
        self._verify_state_receipts(states)
        return tuple(_action_barrier(state) for state in states)

    def schedule_ready(self, *, owner: OwnerCapability) -> tuple[WorkDispatch | ActionBarrier | WorkDispatchV2 | ActionBarrierV2, ...]:
        self._assert_usable()
        states = self._states()
        quarantined = self._quarantine_states(states)
        active_tasks = sum(state.phase in _ACTIVE_PHASES for state in states.values())
        active_seats = sum(state.seat_count for state in states.values()
                           if state.phase in _ACTIVE_PHASES)
        decisions: list[WorkDispatch | ActionBarrier] = []
        for task in (item for item in self.plan.tasks if item.kind == "work"):
            state = states[task.id]
            if state.phase != "unscheduled":
                continue
            dependencies = self._ready_dependencies(task, states)
            if dependencies is None or active_tasks >= _MAX_ACTIVE_TASKS:
                continue
            seats = 0 if task.execution_class == "orchestrator-action" else _seat_count(self.plan, task)
            if active_seats + seats > _MAX_ACTIVE_SEATS:
                continue
            phase = "blocked-action" if task.execution_class == "orchestrator-action" else "scheduled"
            state = replace(
                state, phase=phase, plan_revision=self.plan_revision, seat_count=seats,
                dependencies=dependencies, decision_plan_sha256=self.plan_sha256,
                decision_inputs_digest=self.inputs_digest,
            )
            kind = "action" if task.execution_class == "orchestrator-action" else "work"
            state = replace(
                state,
                decision_id=_decision_id(kind, self._inputs.run_id, state, seats),
            )
            states[task.id] = state
            decisions.append(_action_barrier(state) if kind == "action" else _work_dispatch(state))
            active_tasks += 1
            active_seats += seats
        if not decisions and not quarantined:
            return ()
        self._commit(self._snapshot_with(states=states), owner, self._inputs)
        return tuple(decisions)

    def mark_active(self, task_id: str, *, owner: OwnerCapability) -> None:
        state = self._require_phase(task_id, "scheduled")
        states = self._states()
        states[task_id] = replace(state, phase="active")
        self._commit(self._snapshot_with(states=states), owner, self._inputs)

    def begin_reconciliation(self, task_id: str, *, owner: OwnerCapability) -> None:
        state = self._require_phase(task_id, "active")
        states = self._states()
        states[task_id] = replace(state, phase="reconciliation-pending", seat_count=0)
        self._commit(self._snapshot_with(states=states), owner, self._inputs)

    def complete_reconciliation(self, task_id: str, receipt: ReconciledResult,
                                *, owner: OwnerCapability) -> ReconciledResult:
        state = self._require_phase(task_id, "reconciliation-pending")
        self._verify_receipt(receipt, state)
        states = self._states()
        states[task_id] = replace(state, phase="completed", result=receipt)
        self._commit(self._snapshot_with(states=states), owner, self._inputs)
        return receipt

    def complete_action(self, task_id: str, receipt: ReconciledResult,
                        *, owner: OwnerCapability) -> ReconciledResult:
        state = self._require_phase(task_id, "blocked-action")
        self._verify_receipt(receipt, state)
        states = self._states()
        states[task_id] = replace(state, phase="completed", result=receipt)
        self._commit(self._snapshot_with(states=states), owner, self._inputs)
        return receipt

    def mark_handover_terminal(self, task_id: str, terminal: HandoverTerminalV2,
                               *, owner: OwnerCapability) -> None:
        self._assert_usable()
        _owner(owner)
        if self._journal is not None:
            self._journal.authorize_owner(owner)
        state = self._states().get(task_id)
        if not isinstance(state, SchedulerTaskStateV2) or state.phase != "completed":
            raise SchedulerStateError("handover requires a completed v2 task")
        if state.handover_terminal is not None:
            if state.handover_terminal != terminal:
                raise SchedulerStateError("handover terminal differs from settled task")
            self._authenticate_handover(task_id, terminal)
            return
        self._authenticate_handover(task_id, terminal)
        states = self._states()
        states[task_id] = replace(state, handover_terminal=terminal)
        self._commit(self._snapshot_with(states=states), owner, self._inputs)

    def _authenticate_handover(self, task_id: str,
                               terminal: HandoverTerminalV2, *,
                               allow_blocked: bool = False) -> BranchHandoverIntentV2:
        if not isinstance(self.plan, FanoutPlanV2) or self._journal is None or self._lifecycle_controller is None:
            raise SchedulerStateError("handover requires v2 journal and controller authority")
        state = self._states().get(task_id)
        task = self._plan_tasks().get(task_id)
        if (not isinstance(state, SchedulerTaskStateV2) or state.phase != "completed"
                or task is None or task.execution_class != "repo-write"
                or state.result is None or not isinstance(terminal, HandoverTerminalV2)):
            raise SchedulerStateError("handover requires a completed repository writer")
        try:
            intent, recorded = self._journal.branch_handover_state(task_id)
        except (KeyError, RunStateError, OSError) as error:
            raise SchedulerStateError("handover journal terminal is unavailable") from error
        if recorded != terminal or intent.sha256 != terminal.intent_sha256:
            raise SchedulerStateError("handover terminal differs from authenticated journal")
        blocked = self._journal.state.branch_blocked.get(task_id)
        if blocked is not None and (blocked[0] != intent.sha256 or not allow_blocked):
            raise SchedulerStateError("handover intent is durably blocked")
        self._validate_handover_claim(task_id, terminal, intent)
        if blocked is not None:
            return intent
        try:
            from .branch_handover import verify_git_handover_proof
            verify_git_handover_proof(
                intent, terminal, inputs=self._inputs, artifacts=self._artifacts,
                controller=self._lifecycle_controller, journal=self._journal,
            )
        except Exception as error:
            raise SchedulerStateError("handover Git commit proof is invalid") from error
        return intent

    def _validate_handover_claim(self, task_id: str, terminal: HandoverTerminalV2 | None,
                                 intent: BranchHandoverIntentV2) -> None:
        state = self._states().get(task_id)
        task = self._plan_tasks().get(task_id)
        if (not isinstance(state, SchedulerTaskStateV2) or state.phase != "completed"
                or task is None or task.execution_class != "repo-write" or state.result is None
                or self._lifecycle_controller is None):
            raise SchedulerStateError("handover requires a completed repository writer")
        binding = self._inputs.targets.get(task.target_id) if self._inputs.targets is not None else None
        if (binding is None or intent.run_id != self._inputs.run_id
                or intent.task_id != task_id or intent.target_id != task.target_id
                or intent.repository != binding.spec.repository
                or intent.branch_ref != binding.spec.branch_ref
                or intent.base_oid != binding.base_oid
                or intent.old_ref_oid != (binding.branch_oid or "0" * 40)
                or intent.plan_revision != state.plan_revision
                or intent.plan_sha256 != state.decision_plan_sha256
                or intent.inputs_digest != state.decision_inputs_digest):
            raise SchedulerStateError("handover intent differs from completed task or target")
        try:
            _verify_target_handover_evidence(
                intent, terminal, plan=self.plan, inputs=self._inputs,
                artifacts=self._artifacts, controller=self._lifecycle_controller,
                result=state.result,
            )
        except (CandidateValidationError, ArtifactError, OSError, ValueError) as error:
            raise SchedulerStateError("handover candidate or fresh verifier evidence is invalid") from error

    def _blocked_handover_dependency(self, task_id: str, visiting: set[str]) -> bool:
        if not isinstance(self.plan, FanoutPlanV2) or self._journal is None:
            return False
        if task_id in visiting:
            raise SchedulerStateError("scheduler dependency cycle detected")
        visiting.add(task_id)
        try:
            task = self._plan_tasks()[task_id]
            return any(
                (task.dependency_modes[dependency] == "handover"
                 and dependency in self._journal.state.branch_blocked)
                or self._blocked_handover_dependency(dependency, visiting)
                for dependency in task.depends_on
            )
        finally:
            visiting.remove(task_id)

    def _quarantine_states(self, states: dict[str, SchedulerTaskState]) -> bool:
        changed = False
        for task_id, state in states.items():
            if (state.phase in _ACTIVE_PHASES
                    and self._blocked_handover_dependency(task_id, set())):
                states[task_id] = replace(
                    state, phase="failed", seat_count=0,
                    failure_reason=_BLOCKED_HANDOVER_REASON,
                )
                changed = True
        return changed

    def _quarantine_blocked(self, owner: OwnerCapability) -> None:
        states = self._states()
        if self._quarantine_states(states):
            self._commit(self._snapshot_with(states=states), owner, self._inputs)

    def fail_task(self, task_id: str, reason: str, *, owner: OwnerCapability) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise SchedulerStateError("task failure reason must be non-empty")
        state = self._states().get(task_id)
        if state is None or state.phase not in _ACTIVE_PHASES:
            phase = None if state is None else state.phase
            raise SchedulerStateError(f"task {task_id} cannot fail from phase {phase!r}")
        states = self._states()
        states[task_id] = replace(
            state, phase="failed", seat_count=0, failure_reason=reason.strip(),
        )
        self._commit(self._snapshot_with(states=states), owner, self._inputs)

    def accept_amendment(self, replacement_plan: FanoutPlanV1 | Mapping[str, object],
                         new_inputs: RunInputs, *, expected_plan_revision: int,
                         owner: OwnerCapability) -> PlanAmendment:
        if (isinstance(expected_plan_revision, bool) or not isinstance(expected_plan_revision, int)
                or expected_plan_revision != self.plan_revision):
            raise SchedulerConflictError("stale plan revision")
        replacement_plan = validate_plan(replacement_plan)
        if type(replacement_plan) is not type(self.plan):
            raise SchedulerStateError("amendment cannot change scheduler plan version")
        _validate_inputs(replacement_plan, new_inputs)
        _preflight_plan(replacement_plan)
        if new_inputs.run_id != self._inputs.run_id:
            raise SchedulerStateError("amendment run id does not match the active run")
        if new_inputs.repo_baseline_sha256 != self._inputs.repo_baseline_sha256:
            raise SchedulerStateError("amendment cannot change the immutable repository baseline")
        if isinstance(self.plan, FanoutPlanV2):
            if new_inputs.targets != self._inputs.targets:
                raise SchedulerStateError("amendment cannot change an admitted target binding")
            old_tasks = self._plan_tasks()
            new_tasks = {task.id: task for task in replacement_plan.tasks if task.kind == "work"}
            for state in self._record.snapshot.tasks:
                if state.phase == "unscheduled":
                    continue
                old_task = old_tasks[state.task_id]
                new_task = new_tasks.get(state.task_id)
                if new_task is None or new_task.target_id != old_task.target_id:
                    raise SchedulerStateError("amendment cannot retarget settled work")
        previous_plan = self.plan
        affected = _affected_work(previous_plan, replacement_plan)
        affected.update(_input_affected_work(previous_plan, replacement_plan, self._inputs, new_inputs))
        if not affected:
            raise SchedulerStateError("amendment does not affect schedulable work")
        old_states = self._states()
        unsafe = sorted(task_id for task_id in affected
                        if task_id in old_states and old_states[task_id].phase != "unscheduled")
        if unsafe:
            raise SchedulerStateError(
                "amendment unscheduled subtree contains live work: " + ", ".join(unsafe)
            )
        old_tasks = self._plan_tasks()
        new_states: dict[str, SchedulerTaskState] = {}
        for task in replacement_plan.tasks:
            if task.kind != "work":
                continue
            if task.id not in affected and task.id in old_tasks and old_tasks[task.id] == task:
                new_states[task.id] = old_states[task.id]
            else:
                new_states[task.id] = (SchedulerTaskStateV2(task.id)
                                       if isinstance(replacement_plan, FanoutPlanV2)
                                       else SchedulerTaskState(task.id))
        revision = self.plan_revision + 1
        plan_sha256 = _plan_digest(replacement_plan)
        snapshot = replace(
            self._record.snapshot, plan=replacement_plan, plan_sha256=plan_sha256,
            plan_revision=revision, inputs_digest=new_inputs.digest,
            tasks=tuple(new_states[task.id] for task in replacement_plan.tasks if task.kind == "work"),
        )
        self._commit(snapshot, owner, new_inputs)
        ordered = tuple(task.id for task in replacement_plan.tasks
                        if task.kind == "work" and task.id in affected)
        ordered += tuple(task.id for task in previous_plan.tasks
                         if task.kind == "work" and task.id in affected
                         and all(current.id != task.id for current in replacement_plan.tasks))
        return PlanAmendment(revision, ordered, plan_sha256)

    def _commit(self, snapshot: SchedulerSnapshot, owner: OwnerCapability,
                inputs: RunInputs) -> None:
        self._assert_usable()
        _owner(owner)
        revision, authority = _authority_read(
            self._authority, self._authority_id, self._authority_key,
            self._inputs.run_id, self._record.snapshot.backend_identity,
            self._record.snapshot.backend_key, owner,
        )
        if revision.revision != self._authority_revision or authority != self._authority_record:
            raise SchedulerConflictError("scheduler authority advanced concurrently")
        if authority["pending"] is not None:
            raise SchedulerConflictError("scheduler authority has an unresolved pending transition")
        committed = authority["committed"]
        if committed is None or not _record_matches_binding(self._record, committed):
            raise SchedulerStateError("local scheduler state no longer matches authority")
        candidate = replace(
            snapshot, backend_revision=self.revision + 1,
            previous_commit=committed["commit_id"],
        )
        _validate_snapshot(candidate, inputs, candidate.backend_identity, candidate.backend_key, self._artifacts)
        binding = _snapshot_binding(candidate)
        pending = _authority_record(
            revision.revision + 1, self._authority_id, self._authority_key,
            self._inputs.run_id,
            candidate.backend_identity, candidate.backend_key,
            committed=committed, pending=binding, owner=owner,
        )
        pending_revision = _authority_cas(
            self._authority, self._authority_key, revision.revision, pending,
        )
        try:
            record = _commit_backend(self._backend, self.revision, candidate, owner)
        except BaseException:
            self._failed = True
            raise
        resolved = _authority_record(
            pending_revision.revision + 1, self._authority_id, self._authority_key,
            self._inputs.run_id,
            candidate.backend_identity, candidate.backend_key,
            committed=binding, pending=None, owner=owner,
        )
        try:
            committed_revision = _authority_cas(
                self._authority, self._authority_key, pending_revision.revision, resolved,
            )
        except BaseException:
            self._failed = True
            raise
        self._record = record
        self._inputs = inputs
        self._authority_revision = committed_revision.revision
        self._authority_record = resolved

    def _ready_dependencies(self, task: PlanTaskV1,
                            states: Mapping[str, SchedulerTaskState]) -> tuple[ReconciledResult | ScheduledDependencyV2, ...] | None:
        results: list[ReconciledResult | ScheduledDependencyV2] = []
        for dependency in task.depends_on:
            state = states[dependency]
            if state.phase != "completed" or state.result is None:
                return None
            if isinstance(task, PlanTaskV2):
                source = self._plan_tasks()[dependency]
                mode = task.dependency_modes[dependency]
                if mode == "handover" and (
                    not isinstance(state, SchedulerTaskStateV2)
                    or state.handover_terminal is None
                    or (self._journal is not None and dependency in self._journal.state.branch_blocked)
                ):
                    return None
                if mode == "handover":
                    self._authenticate_handover(dependency, state.handover_terminal)
                    receipt_sha256 = state.handover_terminal.terminal_sha256
                    artifact = state.handover_terminal.evidence
                else:
                    self._verify_receipt(state.result, state)
                    receipt_sha256 = _receipt_sha256(state.result)
                    artifact = state.result.artifact
                results.append(ScheduledDependencyV2(
                    mode, dependency, source.target_id, receipt_sha256, artifact,
                ))
            else:
                self._verify_artifact(state.result.artifact)
                results.append(state.result)
        return tuple(results)

    def _verify_receipt(self, receipt: ReconciledResult, state: SchedulerTaskState) -> None:
        if (not isinstance(receipt, ReconciledResult)
                or receipt.run_id != self._inputs.run_id
                or receipt.task_id != state.task_id
                or receipt.plan_revision != state.plan_revision
                or receipt.plan_sha256 != state.decision_plan_sha256
                or receipt.inputs_digest != state.decision_inputs_digest):
            raise SchedulerStateError("result receipt is foreign or stale")
        self._verify_artifact(receipt.artifact)

    def _verify_artifact(self, ref: ArtifactRef) -> None:
        _artifact(ref)
        try:
            self._artifacts.read_bytes(ref)
        except (ArtifactError, OSError, TypeError, ValueError) as error:
            raise SchedulerStateError("result artifact verification failed") from error

    def _verify_state_receipts(self, states: tuple[SchedulerTaskState, ...]) -> None:
        for state in states:
            for receipt in state.dependencies:
                self._verify_artifact(receipt.artifact)
            if state.result is not None:
                self._verify_artifact(state.result.artifact)

    def _has_failed_ancestor(self, task_id: str, states: Mapping[str, SchedulerTaskState],
                             visiting: set[str]) -> bool:
        if task_id in visiting:
            raise SchedulerStateError("scheduler dependency cycle detected")
        visiting.add(task_id)
        try:
            for dependency in self._plan_tasks()[task_id].depends_on:
                state = states[dependency]
                if state.phase == "failed" or (
                    state.phase == "unscheduled"
                    and self._has_failed_ancestor(dependency, states, visiting)
                ):
                    return True
            return False
        finally:
            visiting.remove(task_id)

    def _require_phase(self, task_id: str, phase: str) -> SchedulerTaskState:
        state = self._states().get(task_id)
        if state is None or state.phase != phase:
            actual = None if state is None else state.phase
            raise SchedulerStateError(f"task {task_id} requires {phase} phase, found {actual!r}")
        if self._blocked_handover_dependency(task_id, set()):
            raise SchedulerStateError(_BLOCKED_HANDOVER_REASON)
        return state

    def _states(self) -> dict[str, SchedulerTaskState]:
        return {state.task_id: state for state in self._record.snapshot.tasks}

    def _plan_tasks(self) -> dict[str, PlanTaskV1]:
        return {task.id: task for task in self.plan.tasks if task.kind == "work"}

    def _snapshot_with(self, *, states: Mapping[str, SchedulerTaskState]) -> SchedulerSnapshot:
        return replace(
            self._record.snapshot,
            tasks=tuple(states[task.id] for task in self.plan.tasks if task.kind == "work"),
        )

    def _assert_usable(self) -> None:
        if self._failed:
            raise SchedulerStateError("scheduler requires authority recovery")


def _target_authority(plan: FanoutPlanV1, inputs: RunInputs, artifacts: ArtifactStore,
                      journal: RunJournal | None,
                      controller: LifecycleController | None, *,
                      owner: OwnerCapability, allow_test: bool) -> None:
    if not isinstance(plan, FanoutPlanV2):
        return
    if (allow_test and journal is None and controller is None
            and all(task.kind != "work" or "handover" not in task.dependency_modes.values()
                    for task in plan.tasks)):
        return
    if (inputs.targets is None or not isinstance(journal, RunJournal)
            or not isinstance(controller, LifecycleController)
            or journal.inputs.run_id != inputs.run_id
            or journal.inputs.targets != inputs.targets
            or artifacts.root != journal.root / "artifacts"):
        raise SchedulerStateError("v2 scheduler requires its exact journal, artifacts, and controller")
    journal.authorize_owner(owner)
    try:
        assert_controller(controller)
    except Exception as error:
        raise SchedulerStateError("v2 scheduler controller is invalid") from error


def _verify_target_handover_evidence(
    intent: BranchHandoverIntentV2, terminal: HandoverTerminalV2 | None, *,
    plan: FanoutPlanV2, inputs: RunInputs, artifacts: ArtifactStore,
    controller: LifecycleController, result: ReconciledResult,
) -> None:
    """Authenticate the exact Task 6 envelopes named by a Task 7 intent."""
    binding = inputs.targets[intent.target_id]
    envelopes = tuple(TargetEvidenceEnvelope(
        intent.run_id, intent.task_id, intent.target_id, intent.repository,
        intent.branch_ref, intent.base_oid, binding.baseline_sha256, kind, ref,
    ) for kind, ref in (("candidate", intent.candidate_ref),
                       ("verification", intent.verification_ref)))
    candidate = load_target_candidate(
        envelopes[0], plan=plan, inputs=inputs, store=artifacts,
        controller=controller,
    )
    verification = load_target_verification(
        envelopes[1], plan=plan, inputs=inputs, store=artifacts,
        controller=controller,
    )
    wrapper = artifacts.read_json(intent.verification_ref)
    if (not isinstance(wrapper, dict) or wrapper.get("candidate_evidence") != {
            "path": intent.candidate_ref.path, "digest": intent.candidate_ref.digest,
            "size": intent.candidate_ref.size,
    }):
        raise SchedulerStateError("handover verification wrapper names another candidate issuance")
    candidate_digest = candidate.candidate.digest
    if (not verification.valid or verification.candidate_digest != candidate_digest
            or result.artifact.digest != candidate_digest
            or (terminal is not None and terminal.candidate_sha256 != candidate_digest)):
        raise SchedulerStateError("handover candidate differs from reconciled result")
    if terminal is not None:
        artifacts.read_bytes(terminal.evidence)


def _commit_backend(backend: SchedulerBackend, expected_revision: int,
                    snapshot: SchedulerSnapshot, owner: OwnerCapability) -> BackendRecord:
    try:
        record = backend.compare_and_set(expected_revision, snapshot, owner=owner)
    except (SchedulerConflictError, RunAuthorizationError):
        raise
    except FanoutError:
        raise
    except Exception as error:
        raise SchedulerConflictError("scheduler backend compare-and-set failed") from error
    if (not isinstance(record, BackendRecord) or record.revision != expected_revision + 1
            or record.snapshot != snapshot):
        raise SchedulerConflictError("scheduler backend returned an inconsistent commit")
    _validate_backend_record(record)
    return record


def _validate_backend_record(record: BackendRecord) -> None:
    if record.revision != record.snapshot.backend_revision:
        raise SchedulerStateError("backend and snapshot revisions disagree")


def _initial_snapshot(plan: FanoutPlanV1, inputs: RunInputs, backend_id: str,
                      backend_key: str, artifacts: ArtifactStore) -> SchedulerSnapshot:
    snapshot = SchedulerSnapshot(
        plan=plan, plan_sha256=_plan_digest(plan), plan_revision=1,
        run_id=inputs.run_id, inputs_digest=inputs.digest,
        backend_identity=backend_id, backend_key=backend_key,
        backend_revision=1, previous_commit=_ZERO,
        tasks=tuple(
            (SchedulerTaskStateV2(task.id) if isinstance(plan, FanoutPlanV2)
             else SchedulerTaskState(task.id))
            for task in plan.tasks if task.kind == "work"
        ),
        schema_version=(_TARGET_SNAPSHOT_SCHEMA if isinstance(plan, FanoutPlanV2)
                        else _SNAPSHOT_SCHEMA),
    )
    _validate_snapshot(snapshot, inputs, backend_id, backend_key, artifacts)
    return snapshot


def _validate_snapshot(snapshot: SchedulerSnapshot, inputs: RunInputs,
                       backend_id: str, backend_key: str, artifacts: ArtifactStore) -> None:
    if not isinstance(snapshot, SchedulerSnapshot):
        raise SchedulerStateError("scheduler snapshot schema is invalid")
    plan = validate_plan(snapshot.plan)
    schema = _TARGET_SNAPSHOT_SCHEMA if isinstance(plan, FanoutPlanV2) else _SNAPSHOT_SCHEMA
    if snapshot.schema_version != schema:
        raise SchedulerStateError("scheduler snapshot schema is invalid")
    _validate_inputs(plan, inputs)
    _preflight_plan(plan)
    if (snapshot.run_id != inputs.run_id or snapshot.inputs_digest != inputs.digest
            or snapshot.backend_identity != backend_id or snapshot.backend_key != backend_key):
        raise SchedulerStateError("scheduler snapshot input or backend binding is invalid")
    _positive_int(snapshot.backend_revision, "snapshot backend revision")
    _positive_int(snapshot.plan_revision, "plan revision")
    _digest(snapshot.previous_commit, "previous scheduler commit")
    if snapshot.plan_sha256 != _plan_digest(plan):
        raise SchedulerStateError("scheduler snapshot plan digest mismatch")
    if not isinstance(snapshot.tasks, tuple):
        raise SchedulerStateError("scheduler task collection must be an immutable tuple")
    work = tuple(task for task in plan.tasks if task.kind == "work")
    state_type = SchedulerTaskStateV2 if isinstance(plan, FanoutPlanV2) else SchedulerTaskState
    if any(type(state) is not state_type for state in snapshot.tasks):
        raise SchedulerStateError("scheduler task collection contains an invalid value")
    if tuple(state.task_id for state in snapshot.tasks) != tuple(task.id for task in work):
        raise SchedulerStateError("scheduler snapshot tasks do not match the plan")
    states = {state.task_id: state for state in snapshot.tasks}
    tasks = {task.id: task for task in work}
    for state in snapshot.tasks:
        _validate_task_state(
            state, tasks[state.task_id], plan, states, snapshot.plan_revision,
            snapshot.run_id, artifacts,
        )
    if sum(state.phase in _ACTIVE_PHASES for state in snapshot.tasks) > _MAX_ACTIVE_TASKS:
        raise SchedulerStateError("scheduler active task cap is inconsistent")
    if sum(
        state.seat_count for state in snapshot.tasks if state.phase in _ACTIVE_PHASES
    ) > _MAX_ACTIVE_SEATS:
        raise SchedulerStateError("scheduler active seat cap is inconsistent")


def _validate_task_state(state: SchedulerTaskState, task: PlanTaskV1, plan: FanoutPlanV1,
                         states: Mapping[str, SchedulerTaskState], current_revision: int,
                         run_id: str, artifacts: ArtifactStore) -> None:
    if state.phase not in _PHASES:
        raise SchedulerStateError("scheduler task phase is invalid")
    if (isinstance(state.plan_revision, bool) or not isinstance(state.plan_revision, int)
            or state.plan_revision < 0 or state.plan_revision > current_revision):
        raise SchedulerStateError("scheduler task plan revision is invalid")
    if isinstance(state.seat_count, bool) or not isinstance(state.seat_count, int) or state.seat_count < 0:
        raise SchedulerStateError("scheduler task seat count is invalid")
    if state.phase == "unscheduled":
        if any((state.plan_revision, state.seat_count, state.dependencies, state.result,
                state.failure_reason, state.decision_plan_sha256,
                state.decision_inputs_digest, state.decision_id,
                getattr(state, "handover_terminal", None))):
            raise SchedulerStateError("unscheduled task carries execution state")
        return
    if state.plan_revision < 1:
        raise SchedulerStateError("scheduled task lacks a plan revision")
    _digest(state.decision_plan_sha256, "decision plan digest")
    _digest(state.decision_inputs_digest, "decision inputs digest")
    _digest(state.decision_id, "decision id")
    expected_dependencies: list[ReconciledResult | ScheduledDependencyV2] = []
    for dependency in task.depends_on:
        dependency_state = states[dependency]
        if dependency_state.phase != "completed" or dependency_state.result is None:
            raise SchedulerStateError("scheduled task has an unsettled dependency")
        if isinstance(task, PlanTaskV2):
            source = next(item for item in plan.tasks if item.id == dependency)
            mode = task.dependency_modes[dependency]
            if mode == "handover":
                if not isinstance(dependency_state, SchedulerTaskStateV2) or dependency_state.handover_terminal is None:
                    raise SchedulerStateError("scheduled task has an unsettled handover dependency")
                terminal = dependency_state.handover_terminal
                digest, artifact = terminal.terminal_sha256, terminal.evidence
            else:
                digest, artifact = _receipt_sha256(dependency_state.result), dependency_state.result.artifact
            expected_dependencies.append(ScheduledDependencyV2(
                mode, dependency, source.target_id, digest, artifact,
            ))
        else:
            expected_dependencies.append(dependency_state.result)
    if state.dependencies != tuple(expected_dependencies):
        raise SchedulerStateError("scheduled task has missing or foreign dependency results")
    kind = "action" if task.execution_class == "orchestrator-action" else "work"
    decision_seats = 0 if kind == "action" else _seat_count(plan, task)
    expected_seats = decision_seats if state.phase in {"scheduled", "active"} else 0
    if state.seat_count != expected_seats:
        raise SchedulerStateError("scheduled task seat count violates the seat cap contract")
    if state.decision_id != _decision_id(kind, run_id, state, decision_seats):
        raise SchedulerStateError("scheduler decision id does not bind its exact inputs")
    if state.phase == "reconciliation-pending" and kind == "action":
        raise SchedulerStateError("orchestrator action entered reconciliation")
    if state.phase == "blocked-action" and kind != "action":
        raise SchedulerStateError("provider work entered an action barrier")
    for receipt in state.dependencies:
        if isinstance(receipt, ScheduledDependencyV2):
            try:
                artifacts.read_bytes(receipt.artifact)
            except (ArtifactError, OSError, TypeError, ValueError) as error:
                raise SchedulerStateError("dependency artifact verification failed") from error
        else:
            _verify_stored_receipt(receipt, artifacts)
    if isinstance(state, SchedulerTaskStateV2):
        terminal = state.handover_terminal
        if terminal is not None:
            if state.phase != "completed" or task.execution_class != "repo-write":
                raise SchedulerStateError("handover terminal belongs to non-completed writer")
            try:
                artifacts.read_bytes(terminal.evidence)
            except (ArtifactError, OSError, TypeError, ValueError) as error:
                raise SchedulerStateError("handover evidence artifact failed verification") from error
    if state.phase == "completed":
        if not isinstance(state.result, ReconciledResult):
            raise SchedulerStateError("completed task lacks a typed reconciled result")
        if (state.result.run_id != run_id or state.result.task_id != state.task_id
                or state.result.plan_revision != state.plan_revision
                or state.result.plan_sha256 != state.decision_plan_sha256
                or state.result.inputs_digest != state.decision_inputs_digest):
            raise SchedulerStateError("completed task result receipt is foreign or stale")
        _verify_stored_receipt(state.result, artifacts)
    elif state.result is not None:
        raise SchedulerStateError("non-completed task carries a reconciled result")
    if state.phase == "failed":
        if not isinstance(state.failure_reason, str) or not state.failure_reason.strip():
            raise SchedulerStateError("failed task lacks a reason")
    elif state.failure_reason is not None:
        raise SchedulerStateError("non-failed task carries a failure reason")


def _verify_stored_receipt(receipt: ReconciledResult, artifacts: ArtifactStore) -> None:
    if not isinstance(receipt, ReconciledResult):
        raise SchedulerStateError("stored dependency is not a result receipt")
    try:
        artifacts.read_bytes(receipt.artifact)
    except (ArtifactError, OSError, TypeError, ValueError) as error:
        raise SchedulerStateError("result artifact verification failed") from error


def _validate_inputs(plan: FanoutPlanV1, inputs: RunInputs) -> None:
    if not isinstance(inputs, RunInputs):
        raise SchedulerStateError("scheduler requires immutable RunInputs")
    if isinstance(plan, FanoutPlanV2):
        targets = {target.id: target for target in plan.targets}
        if (inputs.targets is None or set(inputs.targets) != set(targets)
                or any(inputs.targets[target_id].spec != spec
                       for target_id, spec in targets.items())):
            raise SchedulerStateError("RunInputs target registry does not match the v2 plan")
    if inputs.compiled_plan_sha256 != _plan_digest(plan):
        raise SchedulerStateError("RunInputs compiled plan digest does not match the plan")
    if inputs.source_sha256 != plan.source.sha256:
        raise SchedulerStateError("RunInputs source digest does not match the plan")
    from .providers import profile_binding_key
    profiled = inputs.profile_shape == "class-tier"
    for task in plan.tasks:
        if task.kind != "work" or task.execution_class == "orchestrator-action":
            continue
        policy = task.provider_policy or plan.defaults
        required = {
            profile_binding_key(executor, task.execution_class, policy.quality_tier)
            if profiled else executor
            for executor in policy.executor_ids
        }
        missing = required - set(inputs.provider_profiles)
        if missing:
            raise SchedulerStateError("RunInputs provider profiles do not cover the plan")
        if task.id not in inputs.skill_manifests:
            raise SchedulerStateError("RunInputs skill manifests do not cover the plan")


def _preflight_plan(plan: FanoutPlanV1) -> None:
    for task in plan.tasks:
        if (task.kind == "work" and task.execution_class != "orchestrator-action"
                and _seat_count(plan, task) > _MAX_ACTIVE_SEATS):
            raise SchedulerStateError(f"task {task.id} requires seats above the six-seat cap")


def _snapshot_dict(snapshot: SchedulerSnapshot) -> dict[str, object]:
    return {
        "schema_version": snapshot.schema_version, "plan": snapshot.plan.to_dict(),
        "plan_sha256": snapshot.plan_sha256, "plan_revision": snapshot.plan_revision,
        "run_id": snapshot.run_id, "inputs_digest": snapshot.inputs_digest,
        "backend_identity": snapshot.backend_identity, "backend_key": snapshot.backend_key,
        "backend_revision": snapshot.backend_revision, "previous_commit": snapshot.previous_commit,
        "tasks": [_task_state_dict(state) for state in snapshot.tasks],
    }


def _task_state_dict(state: SchedulerTaskState) -> dict[str, object]:
    value = {
        "task_id": state.task_id, "phase": state.phase,
        "plan_revision": state.plan_revision, "seat_count": state.seat_count,
        "dependencies": [(_dependency_dict(item) if isinstance(item, ScheduledDependencyV2)
                          else _receipt_dict(item)) for item in state.dependencies],
        "result": None if state.result is None else _receipt_dict(state.result),
        "failure_reason": state.failure_reason,
        "decision_plan_sha256": state.decision_plan_sha256,
        "decision_inputs_digest": state.decision_inputs_digest,
        "decision_id": state.decision_id,
    }
    if isinstance(state, SchedulerTaskStateV2):
        value["handover_terminal"] = (None if state.handover_terminal is None
                                      else state.handover_terminal.to_dict())
    return value


def _receipt_dict(receipt: ReconciledResult) -> dict[str, object]:
    return {
        "schema_version": _RECEIPT_SCHEMA, "run_id": receipt.run_id,
        "task_id": receipt.task_id, "plan_revision": receipt.plan_revision,
        "plan_sha256": receipt.plan_sha256, "inputs_digest": receipt.inputs_digest,
        "artifact": _artifact_dict(receipt.artifact),
    }


def _receipt_sha256(receipt: ReconciledResult) -> str:
    return hashlib.sha256(canonical_json(_receipt_dict(receipt))).hexdigest()


def _dependency_dict(dependency: ScheduledDependencyV2) -> dict[str, object]:
    return {
        "kind": dependency.kind, "source_task_id": dependency.source_task_id,
        "source_target_id": dependency.source_target_id,
        "receipt_sha256": dependency.receipt_sha256,
        "artifact": _artifact_dict(dependency.artifact),
    }


def _artifact_dict(ref: ArtifactRef) -> dict[str, object]:
    return {"path": ref.path, "digest": ref.digest, "size": ref.size}


def _snapshot_from_dict(value: object) -> SchedulerSnapshot:
    """Decode storage bytes without admitting a different v2 snapshot shape."""
    fields = {
        "schema_version", "plan", "plan_sha256", "plan_revision", "run_id",
        "inputs_digest", "backend_identity", "backend_key", "backend_revision",
        "previous_commit", "tasks",
    }
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] not in {
        _SNAPSHOT_SCHEMA, _TARGET_SNAPSHOT_SCHEMA,
    }:
        raise SchedulerStateError("scheduler snapshot fields are invalid")
    plan = validate_plan(value["plan"])
    v2 = isinstance(plan, FanoutPlanV2)
    if value["schema_version"] != (_TARGET_SNAPSHOT_SCHEMA if v2 else _SNAPSHOT_SCHEMA):
        raise SchedulerStateError("scheduler snapshot schema differs from plan")
    states = value["tasks"]
    if not isinstance(states, list) or len(states) > 4_096:
        raise SchedulerStateError("scheduler snapshot tasks are invalid")
    snapshot = SchedulerSnapshot(
        plan=plan,
        plan_sha256=_digest(value["plan_sha256"], "scheduler plan digest"),
        plan_revision=_positive_int(value["plan_revision"], "plan revision"),
        run_id=_identity(value["run_id"], "scheduler run id"),
        inputs_digest=_digest(value["inputs_digest"], "scheduler inputs digest"),
        backend_identity=_identity(value["backend_identity"], "scheduler backend identity"),
        backend_key=_identity(value["backend_key"], "scheduler backend key"),
        backend_revision=_positive_int(value["backend_revision"], "backend revision"),
        previous_commit=_digest(value["previous_commit"], "previous scheduler commit"),
        tasks=tuple(_task_state_from_dict(item, v2=v2) for item in states),
        schema_version=value["schema_version"],
    )
    if snapshot.plan_sha256 != _plan_digest(plan) or _snapshot_dict(snapshot) != value:
        raise SchedulerStateError("scheduler snapshot content or plan digest is invalid")
    return snapshot


def _task_state_from_dict(value: object, *, v2: bool = False) -> SchedulerTaskState:
    fields = {
        "task_id", "phase", "plan_revision", "seat_count", "dependencies",
        "result", "failure_reason", "decision_plan_sha256",
        "decision_inputs_digest", "decision_id",
    }
    if not isinstance(value, dict) or set(value) != (fields | ({"handover_terminal"} if v2 else set())):
        raise SchedulerStateError("scheduler task state fields are invalid")
    dependencies = value["dependencies"]
    if not isinstance(dependencies, list) or len(dependencies) > 4_096:
        raise SchedulerStateError("scheduler dependencies are invalid")
    phase = value["phase"]
    failure_reason = value["failure_reason"]
    if (phase not in _PHASES or failure_reason is not None
            and (not isinstance(failure_reason, str) or not failure_reason.strip())):
        raise SchedulerStateError("scheduler task phase or failure reason is invalid")
    for name in ("decision_plan_sha256", "decision_inputs_digest", "decision_id"):
        if not isinstance(value[name], str) or value[name] and not _digest(value[name], name):
            raise SchedulerStateError("scheduler decision digest is invalid")
    result = value["result"]
    values = dict(
        task_id=_identity(value["task_id"], "scheduler task id"),
        phase=phase,
        plan_revision=_nonnegative_int(value["plan_revision"], "task plan revision"),
        seat_count=_nonnegative_int(value["seat_count"], "task seat count"),
        dependencies=tuple((_dependency_from_dict(item) if v2 else _receipt_from_dict(item))
                           for item in dependencies),
        result=None if result is None else _receipt_from_dict(result),
        failure_reason=failure_reason,
        decision_plan_sha256=value["decision_plan_sha256"],
        decision_inputs_digest=value["decision_inputs_digest"],
        decision_id=value["decision_id"],
    )
    if v2:
        terminal = value["handover_terminal"]
        try:
            values["handover_terminal"] = (None if terminal is None
                                          else HandoverTerminalV2.from_dict(terminal))
        except RunStateError as error:
            raise SchedulerStateError("scheduler handover terminal is invalid") from error
        return SchedulerTaskStateV2(**values)
    return SchedulerTaskState(**values)


def _dependency_from_dict(value: object) -> ScheduledDependencyV2:
    fields = {"kind", "source_task_id", "source_target_id", "receipt_sha256", "artifact"}
    if not isinstance(value, dict) or set(value) != fields:
        raise SchedulerStateError("scheduled dependency fields are invalid")
    ref = value["artifact"]
    if not isinstance(ref, dict) or set(ref) != {"path", "digest", "size"}:
        raise SchedulerStateError("scheduled dependency artifact fields are invalid")
    return ScheduledDependencyV2(
        value["kind"], value["source_task_id"], value["source_target_id"],
        value["receipt_sha256"], _artifact(ArtifactRef(**ref)),
    )


def _receipt_from_dict(value: object) -> ReconciledResult:
    fields = {
        "schema_version", "run_id", "task_id", "plan_revision", "plan_sha256",
        "inputs_digest", "artifact",
    }
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != _RECEIPT_SCHEMA:
        raise SchedulerStateError("scheduler result receipt fields are invalid")
    artifact = value["artifact"]
    if not isinstance(artifact, dict) or set(artifact) != {"path", "digest", "size"}:
        raise SchedulerStateError("scheduler result artifact fields are invalid")
    return ReconciledResult(
        run_id=value["run_id"], task_id=value["task_id"],
        plan_revision=value["plan_revision"], plan_sha256=value["plan_sha256"],
        inputs_digest=value["inputs_digest"],
        artifact=_artifact(ArtifactRef(
            path=artifact["path"], digest=artifact["digest"], size=artifact["size"],
        )),
    )


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchedulerStateError(f"{label} must be a non-negative integer")
    return value


def _snapshot_binding(snapshot: SchedulerSnapshot) -> dict[str, object]:
    base: dict[str, object] = {
        "backend_revision": snapshot.backend_revision,
        "snapshot_sha256": hashlib.sha256(canonical_json(_snapshot_dict(snapshot))).hexdigest(),
        "plan_sha256": snapshot.plan_sha256, "plan_revision": snapshot.plan_revision,
        "inputs_digest": snapshot.inputs_digest, "previous_commit": snapshot.previous_commit,
    }
    base["commit_id"] = hashlib.sha256(canonical_json(base)).hexdigest()
    return base


def _record_matches_binding(record: BackendRecord, binding: object) -> bool:
    if not isinstance(binding, dict) or record.revision != record.snapshot.backend_revision:
        return False
    try:
        return binding == _snapshot_binding(record.snapshot)
    except (AttributeError, TypeError, ValueError):
        return False


def _authority_record(authority_revision: int, authority_id: str,
                      authority_key: str, run_id: str,
                      backend_id: str, backend_key: str, *, committed: object,
                      pending: object, owner: OwnerCapability) -> dict[str, object]:
    _positive_int(authority_revision, "scheduler authority revision")
    record: dict[str, object] = {
        "schema_version": _AUTHORITY_SCHEMA, "authority_id": authority_id,
        "authority_revision": authority_revision,
        "authority_key": authority_key, "run_id": run_id,
        "backend_identity": backend_id, "backend_key": backend_key,
        "committed": committed, "pending": pending,
    }
    record["mac"] = _authority_mac(owner, record)
    return record


def _authority_mac(owner: OwnerCapability, record: Mapping[str, object]) -> str:
    unsigned = {key: value for key, value in record.items() if key != "mac"}
    return hmac.digest(
        owner._mac_key(), b"fanout-scheduler-authority-v2" + canonical_json(unsigned), "sha256",
    ).hex()


def _authority_create(authority: _AnchorAuthority, key: str,
                      record: Mapping[str, object]) -> AnchorRevision:
    signed_revision = _signed_authority_revision(record)
    if signed_revision != 1:
        raise SchedulerStateError("scheduler authority create revision must be one")
    encoded = _authority_bytes(record)
    try:
        result = authority.create(key, encoded)  # type: ignore[attr-defined]
    except Exception as error:
        raise SchedulerConflictError("scheduler authority create failed") from error
    return _validate_authority_result(result, 1, encoded, signed_revision)


def _authority_cas(authority: _AnchorAuthority, key: str, expected_revision: int,
                   record: Mapping[str, object]) -> AnchorRevision:
    signed_revision = _signed_authority_revision(record)
    if signed_revision != expected_revision + 1:
        raise SchedulerStateError("scheduler authority CAS revision is not contiguous")
    encoded = _authority_bytes(record)
    try:
        result = authority.compare_and_set(key, expected_revision, encoded)  # type: ignore[attr-defined]
    except Exception as error:
        raise SchedulerConflictError("scheduler authority compare-and-set failed") from error
    return _validate_authority_result(
        result, expected_revision + 1, encoded, signed_revision,
    )


def _authority_read(authority: _AnchorAuthority, authority_id: str, key: str,
                    run_id: str, backend_id: str, backend_key: str,
                    owner: OwnerCapability) -> tuple[AnchorRevision, dict[str, object]]:
    try:
        result = authority.read(key)  # type: ignore[attr-defined]
    except Exception as error:
        raise SchedulerConflictError("scheduler authority read failed") from error
    result = _validate_authority_result(result, None, None, None)
    try:
        value = json.loads(result.value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchedulerStateError("scheduler authority value is malformed") from error
    allowed = {
        "schema_version", "authority_id", "authority_key", "run_id",
        "authority_revision", "backend_identity", "backend_key",
        "committed", "pending", "mac",
    }
    if (not isinstance(value, dict) or set(value) != allowed
            or value.get("schema_version") != _AUTHORITY_SCHEMA
            or canonical_json(value) != result.value):
        raise SchedulerStateError("scheduler authority value has an invalid schema")
    if (_signed_authority_revision(value) != result.revision):
        raise SchedulerStateError(
            "scheduler authority signed revision does not match the attested revision"
        )
    if (value["authority_id"] != authority_id or value["authority_key"] != key
            or value["run_id"] != run_id or value["backend_identity"] != backend_id
            or value["backend_key"] != backend_key):
        raise SchedulerStateError("scheduler authority belongs to another backend or run")
    expected_mac = _authority_mac(owner, value)
    if not isinstance(value["mac"], str) or not hmac.compare_digest(value["mac"], expected_mac):
        raise RunAuthorizationError("owner capability cannot authenticate scheduler authority")
    committed = value["committed"]
    pending = value["pending"]
    if committed is not None:
        _validate_binding(committed)
    if pending is not None:
        _validate_binding(pending)
        previous = _ZERO if committed is None else committed["commit_id"]
        prior_revision = 0 if committed is None else committed["backend_revision"]
        if (pending["previous_commit"] != previous
                or pending["backend_revision"] != prior_revision + 1
                or (committed is not None and pending["plan_revision"] not in {
                    committed["plan_revision"], committed["plan_revision"] + 1,
                })):
            raise SchedulerStateError("scheduler authority pending transition is not contiguous")
    return result, value


def _validate_binding(value: object) -> None:
    required = {
        "backend_revision", "snapshot_sha256", "plan_sha256", "plan_revision",
        "inputs_digest", "previous_commit", "commit_id",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise SchedulerStateError("scheduler authority binding is malformed")
    _positive_int(value["backend_revision"], "authority backend revision")
    _positive_int(value["plan_revision"], "authority plan revision")
    for name in required - {"backend_revision", "plan_revision"}:
        _digest(value[name], f"authority {name}")
    base = {key: item for key, item in value.items() if key != "commit_id"}
    if value["commit_id"] != hashlib.sha256(canonical_json(base)).hexdigest():
        raise SchedulerStateError("scheduler authority commit identity is invalid")


def _validate_authority_result(result: object, expected_revision: int | None,
                               expected_value: bytes | None,
                               signed_revision: int | None) -> AnchorRevision:
    if (not isinstance(result, AnchorRevision)
            or isinstance(result.revision, bool) or not isinstance(result.revision, int)
            or result.revision < 1 or not isinstance(result.value, bytes)
            or len(result.value) > _MAX_AUTHORITY_BYTES):
        raise SchedulerStateError("scheduler authority returned a malformed revision")
    if signed_revision is not None and result.revision != signed_revision:
        raise SchedulerStateError(
            "scheduler authority signed revision does not match the attested revision"
        )
    if expected_revision is not None and result.revision != expected_revision:
        raise SchedulerConflictError("scheduler authority returned a stale or skipped revision")
    if expected_value is not None and result.value != expected_value:
        raise SchedulerStateError("scheduler authority did not retain the exact value")
    return result


def _signed_authority_revision(record: Mapping[str, object]) -> int:
    revision = record.get("authority_revision")
    return _positive_int(revision, "scheduler authority signed revision")


def _authority_bytes(record: Mapping[str, object]) -> bytes:
    encoded = canonical_json(record)
    if len(encoded) > _MAX_AUTHORITY_BYTES:
        raise SchedulerStateError("scheduler authority value exceeds its byte limit")
    return encoded


def _authority_identity(authority: _AnchorAuthority, *, allow_test: bool) -> str:
    try:
        return _require_anchor_store(authority, allow_test=allow_test)
    except RunStateError as error:
        raise SchedulerStateError("a trusted remote or local scheduler authority is required") from error


def _authority_key(owner: OwnerCapability, run_id: str, backend_id: str,
                   backend_key: str) -> str:
    payload = canonical_json({
        "domain": "fanout-scheduler-key-v1", "run_id": run_id,
        "backend_identity": backend_id, "backend_key": backend_key,
    })
    return hmac.digest(owner._mac_key(), b"fanout-scheduler-key-v1" + payload, "sha256").hex()


def _backend_identity(backend: object) -> tuple[str, str]:
    if not isinstance(backend, SchedulerBackend):
        raise SchedulerStateError("scheduler backend does not implement its protocol")
    try:
        return _identity(backend.identity(), "scheduler backend identity"), _identity(
            backend.key(), "scheduler backend key",
        )
    except SchedulerStateError:
        raise
    except Exception as error:
        raise SchedulerStateError("scheduler backend identity is unavailable") from error


def _input_affected_work(old_plan: FanoutPlanV1, new_plan: FanoutPlanV1,
                         old: RunInputs, new: RunInputs) -> set[str]:
    work = {task.id: task for task in new_plan.tasks if task.kind == "work"}
    if old.compiler_sha256 != new.compiler_sha256 or old.parser_sha256 != new.parser_sha256:
        return set(work)
    affected = {
        task_id for task_id in set(old.skill_manifests) | set(new.skill_manifests)
        if old.skill_manifests.get(task_id) != new.skill_manifests.get(task_id)
    }
    from .providers import profile_binding_changed
    for task in work.values():
        policy = task.provider_policy or new_plan.defaults
        if (task.execution_class != "orchestrator-action" and any(
                profile_binding_changed(old.provider_profiles, new.provider_profiles,
                                        executor, task.execution_class, policy.quality_tier,
                                        old_shape=old.profile_shape, new_shape=new.profile_shape)
                for executor in policy.executor_ids)):
            affected.add(task.id)
    return affected


def _affected_work(old: FanoutPlanV1, new: FanoutPlanV1) -> set[str]:
    old_tasks = {task.id: task for task in old.tasks}
    new_tasks = {task.id: task for task in new.tasks}
    changed = {task_id for task_id in set(old_tasks) | set(new_tasks)
               if old_tasks.get(task_id) != new_tasks.get(task_id)}
    changed.update(_reordered_work(old, new))
    if (old.schema_version != new.schema_version or old.source != new.source
            or old.defaults != new.defaults or old.source_steps != new.source_steps):
        return {task.id for task in (*old.tasks, *new.tasks) if task.kind == "work"}
    affected: set[str] = set()
    for plan, tasks in ((old, old_tasks), (new, new_tasks)):
        for task in plan.tasks:
            if task.kind == "work" and (
                task.id in changed or _has_changed_group_ancestor(task, tasks, changed)
            ):
                affected.add(task.id)
    return affected


def _reordered_work(old: FanoutPlanV1, new: FanoutPlanV1) -> set[str]:
    old_order = [task.id for task in old.tasks if task.kind == "work"]
    new_order = [task.id for task in new.tasks if task.kind == "work"]
    positions = {task_id: index for index, task_id in enumerate(new_order)}
    common = [task_id for task_id in old_order if task_id in positions]
    affected: set[str] = set()
    for index, left in enumerate(common):
        for right in common[index + 1:]:
            if positions[left] > positions[right]:
                affected.update((left, right))
    return affected


def _has_changed_group_ancestor(task: PlanTaskV1, tasks: Mapping[str, PlanTaskV1],
                                changed: set[str]) -> bool:
    parent_id = task.parent_id
    while parent_id is not None:
        if parent_id in changed:
            return True
        parent_id = tasks[parent_id].parent_id
    return False


def _decision_id(kind: str, run_id: str, state: SchedulerTaskState,
                 decision_seats: int) -> str:
    payload = {
        "schema_version": ("fanout-scheduler-decision-v2"
                           if isinstance(state, SchedulerTaskStateV2) else _DECISION_SCHEMA),
        "run_id": run_id,
        "inputs_digest": state.decision_inputs_digest,
        "plan_sha256": state.decision_plan_sha256,
        "plan_revision": state.plan_revision,
        "kind": kind, "task_id": state.task_id, "seat_count": decision_seats,
        "dependencies": [(_dependency_dict(item) if isinstance(item, ScheduledDependencyV2)
                          else _receipt_dict(item)) for item in state.dependencies],
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _work_dispatch(state: SchedulerTaskState) -> WorkDispatch | WorkDispatchV2:
    if isinstance(state, SchedulerTaskStateV2):
        return WorkDispatchV2(
            state.task_id, state.plan_revision, state.seat_count,
            state.dependencies, state.decision_id,
        )
    return WorkDispatch(
        state.task_id, state.plan_revision, state.seat_count,
        state.dependencies, state.decision_id,
    )


def _action_barrier(state: SchedulerTaskState) -> ActionBarrier | ActionBarrierV2:
    if isinstance(state, SchedulerTaskStateV2):
        return ActionBarrierV2(
            state.task_id, state.plan_revision, state.dependencies, state.decision_id,
        )
    return ActionBarrier(
        state.task_id, state.plan_revision, state.dependencies, state.decision_id,
    )


def _plan_digest(plan: FanoutPlanV1) -> str:
    return hashlib.sha256(canonical_json(plan.to_dict())).hexdigest()


def _seat_count(plan: FanoutPlanV1, task: PlanTaskV1) -> int:
    return len((task.provider_policy or plan.defaults).executor_ids)


def _artifact(value: object) -> ArtifactRef:
    if not isinstance(value, ArtifactRef):
        raise SchedulerStateError("result artifact must be an ArtifactRef")
    if not isinstance(value.path, str) or not value.path or "\\" in value.path:
        raise SchedulerStateError("result artifact path is invalid")
    pure = PurePosixPath(value.path)
    if not pure.parts or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SchedulerStateError("result artifact path is invalid")
    _digest(value.digest, "result artifact digest")
    if isinstance(value.size, bool) or not isinstance(value.size, int) or value.size < 0:
        raise SchedulerStateError("result artifact size is invalid")
    return value


def _artifact_store(value: object) -> ArtifactStore:
    if not isinstance(value, ArtifactStore):
        raise SchedulerStateError("scheduler requires an ArtifactStore")
    return value


def _owner(value: object) -> OwnerCapability:
    if not isinstance(value, OwnerCapability):
        raise RunAuthorizationError("owner capability is required")
    return value


def _identity(value: object, label: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 256
            or value.strip() != value
            or any(not (char.isalnum() or char in "._-/") for char in value)):
        raise SchedulerStateError(f"{label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise SchedulerStateError(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SchedulerStateError(f"{label} must be a positive integer")
    return value
