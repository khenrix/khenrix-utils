"""Bounded deterministic scheduling over a validated fanout plan.

The scheduler records decisions through an owner-authorized compare-and-set
backend.  It deliberately does not run providers, publish memory, reconcile
answers, or mutate repositories.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Mapping, Protocol, runtime_checkable

from .artifacts import ArtifactRef, canonical_json
from .errors import (
    FanoutError,
    RunAuthorizationError,
    SchedulerConflictError,
    SchedulerStateError,
)
from .plan import FanoutPlanV1, PlanTaskV1, validate_plan
from .runstate import OwnerCapability


_SNAPSHOT_SCHEMA = "fanout-scheduler-v1"
_PHASES = frozenset({
    "unscheduled", "scheduled", "active", "reconciliation-pending",
    "blocked-action", "completed", "failed",
})
_ACTIVE_PHASES = frozenset({
    "scheduled", "active", "reconciliation-pending", "blocked-action",
})
_MAX_ACTIVE_TASKS = 2
_MAX_ACTIVE_SEATS = 6


@dataclass(frozen=True, slots=True)
class ReconciledResult:
    """One content-addressed task result admitted as an exact dependency input."""

    task_id: str
    plan_revision: int
    artifact: ArtifactRef

    def __post_init__(self) -> None:
        _task_id(self.task_id)
        _positive_int(self.plan_revision, "result plan_revision")
        _artifact(self.artifact)


@dataclass(frozen=True, slots=True)
class WorkDispatch:
    """A durable provider-work decision; later layers perform the execution."""

    task_id: str
    plan_revision: int
    seat_count: int
    dependencies: tuple[ReconciledResult, ...]
    dispatch_id: str


@dataclass(frozen=True, slots=True)
class ActionBarrier:
    """A ready orchestrator-only action which must never reach an executor."""

    task_id: str
    plan_revision: int
    dependencies: tuple[ReconciledResult, ...]
    barrier_id: str


@dataclass(frozen=True, slots=True)
class PlanAmendment:
    """The accepted immutable plan revision and its affected work subtree."""

    plan_revision: int
    affected_task_ids: tuple[str, ...]
    plan_sha256: str


@dataclass(frozen=True, slots=True)
class SchedulerTaskState:
    """Durable scheduler-only state for one schedulable work leaf."""

    task_id: str
    phase: str = "unscheduled"
    plan_revision: int = 0
    seat_count: int = 0
    dependencies: tuple[ReconciledResult, ...] = ()
    result: ReconciledResult | None = None
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    """One complete CAS value; the validated plan travels with its state."""

    plan: FanoutPlanV1
    plan_sha256: str
    plan_revision: int
    tasks: tuple[SchedulerTaskState, ...]
    schema_version: str = _SNAPSHOT_SCHEMA


@dataclass(frozen=True, slots=True)
class BackendRecord:
    """A backend revision paired with the exact value stored at that revision."""

    revision: int
    snapshot: SchedulerSnapshot

    def __post_init__(self) -> None:
        _positive_int(self.revision, "backend revision")
        if not isinstance(self.snapshot, SchedulerSnapshot):
            raise SchedulerStateError("backend record snapshot is invalid")


@runtime_checkable
class SchedulerBackend(Protocol):
    """Owner-authorized durable compare-and-set storage for scheduler state."""

    def read(self) -> BackendRecord | None:
        """Return the current exact record, or ``None`` for an empty backend."""

    def compare_and_set(
        self,
        expected_revision: int,
        snapshot: SchedulerSnapshot,
        *,
        owner: OwnerCapability,
    ) -> BackendRecord:
        """Store *snapshot* only at *expected_revision* and return its record."""


class Scheduler:
    """Choose dependency-ready leaves and record every decision before return."""

    def __init__(self, backend: SchedulerBackend, record: BackendRecord) -> None:
        self._backend = backend
        self._record = record

    @classmethod
    def create(
        cls,
        plan: FanoutPlanV1 | Mapping[str, object],
        backend: SchedulerBackend,
        *,
        owner: OwnerCapability,
    ) -> "Scheduler":
        """Create revision one in an empty backend."""
        plan = validate_plan(plan)
        _backend(backend)
        _owner(owner)
        try:
            existing = backend.read()
        except Exception as error:
            raise SchedulerConflictError("scheduler backend read failed") from error
        if existing is not None:
            raise SchedulerConflictError("scheduler backend is not empty")
        _preflight_plan(plan)
        snapshot = SchedulerSnapshot(
            plan=plan,
            plan_sha256=_plan_digest(plan),
            plan_revision=1,
            tasks=tuple(
                SchedulerTaskState(task.id) for task in plan.tasks if task.kind == "work"
            ),
        )
        _validate_snapshot(snapshot)
        record = _commit_backend(backend, 0, snapshot, owner)
        return cls(backend, record)

    @classmethod
    def resume(
        cls,
        expected_plan: FanoutPlanV1 | Mapping[str, object],
        backend: SchedulerBackend,
    ) -> "Scheduler":
        """Resume exact durable state while rejecting a different plan revision."""
        expected_plan = validate_plan(expected_plan)
        _backend(backend)
        try:
            record = backend.read()
        except Exception as error:
            raise SchedulerConflictError("scheduler backend read failed") from error
        if not isinstance(record, BackendRecord):
            raise SchedulerStateError("scheduler backend has no valid state")
        _validate_snapshot(record.snapshot)
        if record.snapshot.plan_sha256 != _plan_digest(expected_plan) or record.snapshot.plan != expected_plan:
            raise SchedulerStateError("scheduler plan drift detected during resume")
        return cls(backend, record)

    @property
    def plan(self) -> FanoutPlanV1:
        return self._record.snapshot.plan

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
        return sum(state.seat_count for state in self._record.snapshot.tasks)

    def task_phase(self, task_id: str) -> str:
        """Return a persisted phase or the derived dependency-failure block."""
        states = self._states()
        state = states.get(task_id)
        if state is None:
            raise SchedulerStateError(f"unknown scheduler task: {task_id}")
        if state.phase == "unscheduled" and self._has_failed_ancestor(task_id, states, set()):
            return "blocked-dependency"
        return state.phase

    def result_for(self, task_id: str) -> ReconciledResult | None:
        state = self._states().get(task_id)
        if state is None:
            raise SchedulerStateError(f"unknown scheduler task: {task_id}")
        return state.result

    def pending_work(self) -> tuple[WorkDispatch, ...]:
        """Describe already-recorded, not-yet-started work without rescheduling it."""
        tasks = self._plan_tasks()
        return tuple(
            _work_dispatch(state)
            for state in self._record.snapshot.tasks
            if state.phase == "scheduled" and tasks[state.task_id].execution_class != "orchestrator-action"
        )

    def pending_actions(self) -> tuple[ActionBarrier, ...]:
        """Describe durable owner-action barriers without creating new decisions."""
        return tuple(
            _action_barrier(state)
            for state in self._record.snapshot.tasks
            if state.phase == "blocked-action"
        )

    def schedule_ready(self, *, owner: OwnerCapability) -> tuple[WorkDispatch | ActionBarrier, ...]:
        """Persist as many deterministic ready decisions as both global caps admit."""
        _owner(owner)
        states = self._states()
        active_tasks = self.active_task_count
        active_seats = self.active_seat_count
        decisions: list[WorkDispatch | ActionBarrier] = []
        for task in (item for item in self.plan.tasks if item.kind == "work"):
            state = states[task.id]
            if state.phase != "unscheduled":
                continue
            dependencies = self._ready_dependencies(task, states)
            if dependencies is None or active_tasks >= _MAX_ACTIVE_TASKS:
                continue
            if task.execution_class == "orchestrator-action":
                next_state = replace(
                    state, phase="blocked-action", plan_revision=self.plan_revision,
                    dependencies=dependencies,
                )
                decision: WorkDispatch | ActionBarrier = _action_barrier(next_state)
            else:
                seats = _seat_count(self.plan, task)
                if active_seats + seats > _MAX_ACTIVE_SEATS:
                    continue
                next_state = replace(
                    state, phase="scheduled", plan_revision=self.plan_revision,
                    seat_count=seats, dependencies=dependencies,
                )
                active_seats += seats
                decision = _work_dispatch(next_state)
            states[task.id] = next_state
            decisions.append(decision)
            active_tasks += 1
        if not decisions:
            return ()
        self._commit(self._snapshot_with(states=states), owner)
        return tuple(decisions)

    def mark_active(self, task_id: str, *, owner: OwnerCapability) -> None:
        """Record that a previously durable dispatch was admitted to execution."""
        state = self._require_phase(task_id, "scheduled")
        states = self._states()
        states[task_id] = replace(state, phase="active")
        self._commit(self._snapshot_with(states=states), owner)

    def begin_reconciliation(self, task_id: str, *, owner: OwnerCapability) -> None:
        """Enter the owner-only result-selection barrier and release executor seats."""
        state = self._require_phase(task_id, "active")
        states = self._states()
        states[task_id] = replace(state, phase="reconciliation-pending", seat_count=0)
        self._commit(self._snapshot_with(states=states), owner)

    def complete_reconciliation(
        self,
        task_id: str,
        artifact: ArtifactRef,
        *,
        owner: OwnerCapability,
    ) -> ReconciledResult:
        """Admit one exact reconciled artifact and unlock its dependants."""
        _artifact(artifact)
        state = self._require_phase(task_id, "reconciliation-pending")
        result = ReconciledResult(task_id, state.plan_revision, artifact)
        states = self._states()
        states[task_id] = replace(state, phase="completed", result=result)
        self._commit(self._snapshot_with(states=states), owner)
        return result

    def complete_action(
        self,
        task_id: str,
        artifact: ArtifactRef,
        *,
        owner: OwnerCapability,
    ) -> ReconciledResult:
        """Owner-complete an external action barrier using a durable receipt."""
        _artifact(artifact)
        state = self._require_phase(task_id, "blocked-action")
        result = ReconciledResult(task_id, state.plan_revision, artifact)
        states = self._states()
        states[task_id] = replace(state, phase="completed", result=result)
        self._commit(self._snapshot_with(states=states), owner)
        return result

    def fail_task(self, task_id: str, reason: str, *, owner: OwnerCapability) -> None:
        """Terminally fail one branch while leaving independent branches schedulable."""
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
        self._commit(self._snapshot_with(states=states), owner)

    def accept_amendment(
        self,
        replacement_plan: FanoutPlanV1 | Mapping[str, object],
        *,
        expected_plan_revision: int,
        owner: OwnerCapability,
    ) -> PlanAmendment:
        """Replace only wholly unscheduled affected hierarchy subtrees."""
        if (isinstance(expected_plan_revision, bool)
                or not isinstance(expected_plan_revision, int)):
            raise SchedulerConflictError("stale plan revision")
        if expected_plan_revision != self.plan_revision:
            raise SchedulerConflictError("stale plan revision")
        replacement_plan = validate_plan(replacement_plan)
        _preflight_plan(replacement_plan)
        previous_plan = self.plan
        affected = _affected_work(previous_plan, replacement_plan)
        if not affected:
            raise SchedulerStateError("amendment does not affect schedulable work")
        old_states = self._states()
        unsafe = sorted(
            task_id for task_id in affected
            if task_id in old_states and old_states[task_id].phase != "unscheduled"
        )
        if unsafe:
            raise SchedulerStateError(
                "amendment unscheduled subtree contains live work: " + ", ".join(unsafe)
            )
        old_tasks = self._plan_tasks()
        new_states: dict[str, SchedulerTaskState] = {}
        for task in replacement_plan.tasks:
            if task.kind != "work":
                continue
            if (task.id not in affected and task.id in old_tasks
                    and old_tasks[task.id] == task):
                new_states[task.id] = old_states[task.id]
            else:
                new_states[task.id] = SchedulerTaskState(task.id)
        revision = self.plan_revision + 1
        snapshot = SchedulerSnapshot(
            plan=replacement_plan,
            plan_sha256=_plan_digest(replacement_plan),
            plan_revision=revision,
            tasks=tuple(new_states[task.id] for task in replacement_plan.tasks if task.kind == "work"),
        )
        self._commit(snapshot, owner)
        ordered_affected = tuple(
            task.id for task in replacement_plan.tasks
            if task.kind == "work" and task.id in affected
        ) + tuple(
            task.id for task in previous_plan.tasks
            if task.kind == "work" and task.id in affected
            and all(current.id != task.id for current in replacement_plan.tasks)
        )
        return PlanAmendment(revision, ordered_affected, snapshot.plan_sha256)

    def _ready_dependencies(
        self,
        task: PlanTaskV1,
        states: Mapping[str, SchedulerTaskState],
    ) -> tuple[ReconciledResult, ...] | None:
        results: list[ReconciledResult] = []
        for dependency in task.depends_on:
            state = states[dependency]
            if state.phase != "completed" or state.result is None:
                return None
            results.append(state.result)
        return tuple(results)

    def _has_failed_ancestor(
        self,
        task_id: str,
        states: Mapping[str, SchedulerTaskState],
        visiting: set[str],
    ) -> bool:
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
            raise SchedulerStateError(
                f"task {task_id} requires {phase} phase, found {actual!r}"
            )
        return state

    def _states(self) -> dict[str, SchedulerTaskState]:
        return {state.task_id: state for state in self._record.snapshot.tasks}

    def _plan_tasks(self) -> dict[str, PlanTaskV1]:
        return {task.id: task for task in self.plan.tasks if task.kind == "work"}

    def _snapshot_with(
        self,
        *,
        states: Mapping[str, SchedulerTaskState],
    ) -> SchedulerSnapshot:
        return replace(
            self._record.snapshot,
            tasks=tuple(states[task.id] for task in self.plan.tasks if task.kind == "work"),
        )

    def _commit(self, snapshot: SchedulerSnapshot, owner: OwnerCapability) -> None:
        _owner(owner)
        _validate_snapshot(snapshot)
        record = _commit_backend(self._backend, self.revision, snapshot, owner)
        self._record = record


def _commit_backend(
    backend: SchedulerBackend,
    expected_revision: int,
    snapshot: SchedulerSnapshot,
    owner: OwnerCapability,
) -> BackendRecord:
    try:
        record = backend.compare_and_set(expected_revision, snapshot, owner=owner)
    except (SchedulerConflictError, RunAuthorizationError):
        raise
    except FanoutError:
        raise
    except Exception as error:
        raise SchedulerConflictError("scheduler backend compare-and-set failed") from error
    if (not isinstance(record, BackendRecord)
            or record.revision != expected_revision + 1
            or record.snapshot != snapshot):
        raise SchedulerConflictError("scheduler backend returned an inconsistent commit")
    return record


def _validate_snapshot(snapshot: SchedulerSnapshot) -> None:
    if not isinstance(snapshot, SchedulerSnapshot) or snapshot.schema_version != _SNAPSHOT_SCHEMA:
        raise SchedulerStateError("scheduler snapshot schema is invalid")
    plan = validate_plan(snapshot.plan)
    _preflight_plan(plan)
    _positive_int(snapshot.plan_revision, "plan revision")
    if snapshot.plan_sha256 != _plan_digest(plan):
        raise SchedulerStateError("scheduler snapshot plan digest mismatch")
    if not isinstance(snapshot.tasks, tuple):
        raise SchedulerStateError("scheduler task collection must be an immutable tuple")
    work = tuple(task for task in plan.tasks if task.kind == "work")
    if any(not isinstance(state, SchedulerTaskState) for state in snapshot.tasks):
        raise SchedulerStateError("scheduler task collection contains an invalid value")
    if tuple(state.task_id for state in snapshot.tasks) != tuple(task.id for task in work):
        raise SchedulerStateError("scheduler snapshot tasks do not match the plan")
    states = {state.task_id: state for state in snapshot.tasks}
    tasks = {task.id: task for task in work}
    for state in snapshot.tasks:
        _validate_task_state(state, tasks[state.task_id], plan, states, snapshot.plan_revision)
    active_tasks = sum(state.phase in _ACTIVE_PHASES for state in snapshot.tasks)
    if active_tasks > _MAX_ACTIVE_TASKS:
        raise SchedulerStateError("scheduler active task cap is inconsistent")
    active_seats = sum(state.seat_count for state in snapshot.tasks)
    if active_seats > _MAX_ACTIVE_SEATS:
        raise SchedulerStateError("scheduler active seat cap is inconsistent")


def _validate_task_state(
    state: SchedulerTaskState,
    task: PlanTaskV1,
    plan: FanoutPlanV1,
    states: Mapping[str, SchedulerTaskState],
    current_plan_revision: int,
) -> None:
    if not isinstance(state, SchedulerTaskState) or state.phase not in _PHASES:
        raise SchedulerStateError("scheduler task phase is invalid")
    if (isinstance(state.plan_revision, bool) or not isinstance(state.plan_revision, int)
            or state.plan_revision < 0 or state.plan_revision > current_plan_revision):
        raise SchedulerStateError("scheduler task plan revision is invalid")
    if isinstance(state.seat_count, bool) or not isinstance(state.seat_count, int) or state.seat_count < 0:
        raise SchedulerStateError("scheduler task seat count is invalid")
    if state.phase == "unscheduled":
        if any((state.plan_revision, state.seat_count, state.dependencies,
                state.result, state.failure_reason)):
            raise SchedulerStateError("unscheduled task carries execution state")
        return
    if state.plan_revision < 1:
        raise SchedulerStateError("scheduled task lacks a plan revision")
    expected_dependencies: list[ReconciledResult] = []
    for dependency in task.depends_on:
        dependency_state = states[dependency]
        if dependency_state.phase != "completed" or dependency_state.result is None:
            raise SchedulerStateError("scheduled task has an unsettled dependency")
        expected_dependencies.append(dependency_state.result)
    if state.dependencies != tuple(expected_dependencies):
        raise SchedulerStateError("scheduled task has missing or foreign dependency results")
    if state.phase in {"scheduled", "active"}:
        if task.execution_class == "orchestrator-action":
            raise SchedulerStateError("orchestrator action was admitted as provider work")
        if state.seat_count != _seat_count(plan, task):
            raise SchedulerStateError("scheduled task seat count violates the seat cap contract")
    elif state.seat_count != 0:
        raise SchedulerStateError("non-executing task retains active executor seats")
    if state.phase == "reconciliation-pending" and task.execution_class == "orchestrator-action":
        raise SchedulerStateError("orchestrator action entered reconciliation")
    if state.phase == "blocked-action" and task.execution_class != "orchestrator-action":
        raise SchedulerStateError("provider work entered an action barrier")
    if state.phase == "completed":
        if not isinstance(state.result, ReconciledResult):
            raise SchedulerStateError("completed task lacks a typed reconciled result")
        if state.result.task_id != state.task_id:
            raise SchedulerStateError("completed task lacks its exact reconciled result")
        if state.result.plan_revision != state.plan_revision:
            raise SchedulerStateError("completed result belongs to another plan revision")
        _artifact(state.result.artifact)
    elif state.result is not None:
        raise SchedulerStateError("non-completed task carries a reconciled result")
    if state.phase == "failed":
        if not isinstance(state.failure_reason, str) or not state.failure_reason.strip():
            raise SchedulerStateError("failed task lacks a reason")
    elif state.failure_reason is not None:
        raise SchedulerStateError("non-failed task carries a failure reason")


def _preflight_plan(plan: FanoutPlanV1) -> None:
    for task in plan.tasks:
        if task.kind == "work" and task.execution_class != "orchestrator-action":
            seats = _seat_count(plan, task)
            if seats > _MAX_ACTIVE_SEATS:
                raise SchedulerStateError(
                    f"task {task.id} requires {seats} seats, above the six-seat cap"
                )


def _affected_work(old: FanoutPlanV1, new: FanoutPlanV1) -> set[str]:
    old_tasks = {task.id: task for task in old.tasks}
    new_tasks = {task.id: task for task in new.tasks}
    changed = {
        task_id for task_id in set(old_tasks) | set(new_tasks)
        if old_tasks.get(task_id) != new_tasks.get(task_id)
    }
    changed.update(_reordered_work(old, new))
    global_changed = (
        old.schema_version != new.schema_version
        or old.source != new.source
        or old.defaults != new.defaults
        or old.source_steps != new.source_steps
    )
    if global_changed:
        return {
            task.id for task in (*old.tasks, *new.tasks) if task.kind == "work"
        }
    affected: set[str] = set()
    for plan, tasks in ((old, old_tasks), (new, new_tasks)):
        for task in plan.tasks:
            if task.kind != "work":
                continue
            if task.id in changed or _has_changed_group_ancestor(task, tasks, changed):
                affected.add(task.id)
    return affected


def _reordered_work(old: FanoutPlanV1, new: FanoutPlanV1) -> set[str]:
    old_order = [task.id for task in old.tasks if task.kind == "work"]
    new_order = [task.id for task in new.tasks if task.kind == "work"]
    new_positions = {task_id: index for index, task_id in enumerate(new_order)}
    common = [task_id for task_id in old_order if task_id in new_positions]
    affected: set[str] = set()
    for index, left in enumerate(common):
        for right in common[index + 1:]:
            if new_positions[left] > new_positions[right]:
                affected.update((left, right))
    return affected


def _has_changed_group_ancestor(
    task: PlanTaskV1,
    tasks: Mapping[str, PlanTaskV1],
    changed: set[str],
) -> bool:
    parent_id = task.parent_id
    while parent_id is not None:
        if parent_id in changed:
            return True
        parent = tasks[parent_id]
        parent_id = parent.parent_id
    return False


def _work_dispatch(state: SchedulerTaskState) -> WorkDispatch:
    return WorkDispatch(
        state.task_id, state.plan_revision, state.seat_count, state.dependencies,
        _decision_id("work", state),
    )


def _action_barrier(state: SchedulerTaskState) -> ActionBarrier:
    return ActionBarrier(
        state.task_id, state.plan_revision, state.dependencies,
        _decision_id("action", state),
    )


def _decision_id(kind: str, state: SchedulerTaskState) -> str:
    payload = {
        "kind": kind,
        "task_id": state.task_id,
        "plan_revision": state.plan_revision,
        "dependencies": [
            {
                "task_id": item.task_id,
                "plan_revision": item.plan_revision,
                "artifact": {
                    "path": item.artifact.path,
                    "digest": item.artifact.digest,
                    "size": item.artifact.size,
                },
            }
            for item in state.dependencies
        ],
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _plan_digest(plan: FanoutPlanV1) -> str:
    return hashlib.sha256(canonical_json(plan.to_dict())).hexdigest()


def _seat_count(plan: FanoutPlanV1, task: PlanTaskV1) -> int:
    policy = task.provider_policy or plan.defaults
    return len(policy.executor_ids)


def _artifact(value: object) -> ArtifactRef:
    if not isinstance(value, ArtifactRef):
        raise SchedulerStateError("result artifact must be an ArtifactRef")
    if not isinstance(value.path, str) or not value.path or "\\" in value.path:
        raise SchedulerStateError("result artifact path is invalid")
    pure = PurePosixPath(value.path)
    if (not pure.parts or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)):
        raise SchedulerStateError("result artifact path is invalid")
    if (not isinstance(value.digest, str) or len(value.digest) != 64
            or any(character not in "0123456789abcdef" for character in value.digest)):
        raise SchedulerStateError("result artifact digest is invalid")
    if isinstance(value.size, bool) or not isinstance(value.size, int) or value.size < 0:
        raise SchedulerStateError("result artifact size is invalid")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SchedulerStateError(f"{label} must be a positive integer")
    return value


def _task_id(value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise SchedulerStateError("scheduler task id is invalid")
    return value


def _owner(value: object) -> OwnerCapability:
    if not isinstance(value, OwnerCapability):
        raise RunAuthorizationError("owner capability is required")
    return value


def _backend(value: object) -> SchedulerBackend:
    if not isinstance(value, SchedulerBackend):
        raise SchedulerStateError("scheduler backend does not implement its protocol")
    return value


# Keep the original import path stable while the anchored implementation lives in
# its own module.  The aliases deliberately replace every public scheduler
# contract so callers cannot accidentally construct a legacy, unanchored state.
from .scheduler_authority import (  # noqa: E402
    ActionBarrier,
    BackendRecord,
    PlanAmendment,
    ReconciledResult,
    Scheduler,
    SchedulerBackend,
    SchedulerSnapshot,
    SchedulerTaskState,
    WorkDispatch,
)
