"""Owner-controlled orchestration contracts for one compiled fanout run."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_execute_contracts",
    FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)
lifecycle = sys.modules[f"{SPEC.name}.lifecycle"]


def _digest(value: bytes | str) -> str:
    data = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _task(
    task_id: str,
    *,
    execution_class: str = "read-only",
    depends_on=(),
    checks=(),
    owned_paths=(),
):
    policy = None
    if execution_class != "orchestrator-action":
        policy = {
            "executor_ids": ["claude", "codex"],
            "rounds": 1,
            "timeout": 10,
            "retries": 0,
            "minimum_success": 2,
        }
    return {
        "id": task_id,
        "kind": "work",
        "parent_id": None,
        "title": task_id,
        "objective": f"Complete {task_id}.",
        "source_step_ids": [],
        "depends_on": list(depends_on),
        "execution_class": execution_class,
        "required_skills": [],
        "none_reason": "No specialist skill is needed.",
        "owned_paths": list(owned_paths),
        "acceptance": [f"{task_id} is complete."],
        "checks": [check.to_dict() for check in checks],
        "provider_policy": policy,
    }


def _plan(*tasks):
    tasks = copy.deepcopy(list(tasks))
    for index, task in enumerate(tasks, start=1):
        task["source_step_ids"] = [f"Task {index}/Step 1"]
    source_steps = [
        {"id": task["source_step_ids"][0], "sha256": _digest(task["id"])}
        for task in tasks
    ]
    return fanout.FanoutPlanV1.from_dict({
        "schema_version": "v1",
        "source": {
            "path": "docs/superpowers/plans/execute.md",
            "sha256": _digest("source"),
            "parser_version": "parser-v1",
        },
        "defaults": {
            "executor_ids": ["claude", "codex"],
            "rounds": 1,
            "timeout": 10,
            "retries": 0,
            "minimum_success": 2,
        },
        "source_steps": source_steps,
        "tasks": tasks,
    })


def _inputs(plan, *, repo_digest=None, profile_digests=None):
    work = [task for task in plan.tasks if task.execution_class != "orchestrator-action"]
    executors = {
        executor for task in work
        for executor in (task.provider_policy or plan.defaults).executor_ids
    } or set(plan.defaults.executor_ids)
    return fanout.RunInputs(
        run_id="run-execute",
        compiled_plan_sha256=_digest(fanout.canonical_json(plan.to_dict())),
        source_sha256=plan.source.sha256,
        draft_sha256=_digest("draft"),
        compiler_sha256=_digest("compiler"),
        parser_sha256=_digest("parser"),
        provider_profiles=(profile_digests if profile_digests is not None else
                           {executor: _digest(executor) for executor in executors}),
        profile_shape="class-tier" if profile_digests is not None else "flat",
        skill_manifests={task.id: _digest(f"skills:{task.id}") for task in work},
        repo_baseline_sha256=repo_digest,
    )


def test_target_candidate_submission_rejects_same_bytes_under_wrong_target(tmp_path):
    source = _plan(_task("work"))
    document = source.to_dict()
    target = fanout.TargetSpec(
        "booking", "github.com/example/booking", "TASK-123",
        "refs/heads/feat/TASK-123-booking",
    )
    document["schema_version"] = "v2"
    document["targets"] = [target.to_dict()]
    document["source_steps"][0]["target_id"] = "booking"
    document["tasks"][0]["target_id"] = "booking"
    document["tasks"][0]["dependency_modes"] = {}
    plan = fanout.plan.FanoutPlanV2.from_dict(document)
    binding = fanout.TargetBinding(
        target, tmp_path, tmp_path, 1, 1, "a" * 40, None, "b" * 64,
        "refs/heads/main",
    )
    inputs = dataclasses.replace(
        _inputs(plan, profile_digests={
            "claude/read-only/standard": "c" * 64,
            "codex/read-only/standard": "d" * 64,
        }),
        targets={"booking": binding},
    )
    bundle = fanout.CandidateBundle(binding.baseline_sha256, (), ())
    candidate = fanout.TargetCandidate(
        "booking", target.repository, target.branch_ref, binding.base_oid,
        binding.baseline_sha256, bundle,
    )
    with pytest.raises(fanout.ExecutionValidationError, match="controller"):
        fanout.validate_target_candidate_binding(
            candidate, task_id="work", plan=plan, inputs=inputs,
        )
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    with fanout.ArtifactStore(tmp_path / "artifacts") as store:
        issued = fanout.issue_target_candidate(
            bundle, task_id="work", plan=plan, inputs=inputs,
            store=store, controller=controller,
        )
        assert fanout.validate_target_candidate_binding(
            issued, task_id="work", plan=plan, inputs=inputs,
            store=store, controller=controller,
        ) == bundle
        wrong = dataclasses.replace(issued, target_id="address")
        with pytest.raises(fanout.ExecutionValidationError, match="controller"):
            fanout.validate_target_candidate_binding(
                wrong, task_id="work", plan=plan, inputs=inputs,
                store=store, controller=controller,
            )


@pytest.mark.parametrize("forgery", ("candidate_seal", "verification_seal", "invalid_receipt"))
def test_v2_projection_rejects_journaled_terminal_without_fresh_controller_proof(tmp_path, forgery):
    import test_fanout_scheduler as scheduler_cases

    scheduler, backend, journal, controller, baseline, owner = (
        scheduler_cases._v2_handover_fixture(tmp_path)
    )
    scheduler.schedule_ready(owner=owner)
    scheduler.mark_active("address", owner=owner)
    scheduler.begin_reconciliation("address", owner=owner)
    intent, terminal, bundle = scheduler_cases._v2_terminal(
        scheduler, backend, journal, controller, baseline, owner,
    )
    scheduler_cases._reconcile_v2_candidate(scheduler, backend, bundle, owner)
    verification = backend.artifacts.read_json(intent.verification_ref)
    if forgery == "candidate_seal":
        wrapper = backend.artifacts.read_json(intent.candidate_ref)
        wrapper["controller_evidence"] = {"name": "invented", "digest": "f" * 64}
        forged_candidate = backend.artifacts.write_json(
            "target-evidence/address/candidate/forged.wrapper.json", wrapper,
        )
        verification["candidate_evidence"] = dataclasses.asdict(forged_candidate)
        intent = dataclasses.replace(intent, candidate_ref=forged_candidate)
    elif forgery == "verification_seal":
        verification["controller_evidence"] = {"name": "invented", "digest": "f" * 64}
    else:
        result = backend.artifacts.write_bytes(
            scheduler_cases.fanout.candidate_result_path("address", bundle.digest, "d" * 64),
            bundle.manifest_bytes,
        )
        verification["candidate"] = dataclasses.asdict(result)
        seal = {
            "schema_version": "fanout-target-verification-seal-v1",
            "target": verification["target"],
            "candidate_sha256": result.digest,
            "candidate_evidence": verification["candidate_evidence"],
            "result": verification["candidate"],
            "verifier_evidence_sha256": "d" * 64,
        }
        controller_module = sys.modules[f"{scheduler_cases.SPEC.name}.controller"]
        name, digest = controller_module.write_evidence(controller, "target-verification", seal)
        verification["controller_evidence"] = {"name": name, "digest": digest}
    forged_verification = backend.artifacts.write_json(
        f"target-evidence/address/verification/{forgery}.wrapper.json", verification,
    )
    intent = dataclasses.replace(intent, verification_ref=forged_verification)
    unsigned = terminal._unsigned_dict()
    unsigned["intent_sha256"] = intent.sha256
    terminal = scheduler_cases.fanout.HandoverTerminalV2(
        intent.sha256, terminal.commit_oid, terminal.evidence,
        terminal.candidate_sha256, _digest(scheduler_cases.fanout.canonical_json(unsigned)),
    )
    journal.append_branch_intent(intent, owner=owner)
    journal.append_branch_terminal("address", terminal, owner=owner)
    revision = scheduler.revision
    with pytest.raises(scheduler_cases.fanout.SchedulerStateError, match="candidate|seal|evidence"):
        scheduler_cases.fanout.ExecutionService.project_v2_handover_terminal(
            scheduler, journal, "address", owner=owner,
        )
    assert scheduler.revision == revision
    assert scheduler.schedule_ready(owner=owner) == ()


def test_v2_status_reports_candidate_delivery_and_independent_partial_failure(tmp_path):
    import test_fanout_scheduler as scheduler_cases

    scheduler, backend, journal, controller, baseline, owner = (
        scheduler_cases._v2_handover_fixture(tmp_path)
    )
    service = scheduler_cases.fanout.ExecutionService
    scheduler.schedule_ready(owner=owner)
    assert service.v2_status(scheduler).target_states == {
        "address": "pending", "booking": "pending", "events": "pending",
    }
    scheduler.mark_active("address", owner=owner)
    scheduler.begin_reconciliation("address", owner=owner)
    bundle = scheduler_cases.fanout.CandidateBundle(baseline.digest, (), ())
    scheduler_cases._reconcile_v2_candidate(scheduler, backend, bundle, owner)
    assert service.v2_status(scheduler).target_states["address"] == "candidate"
    terminal = scheduler_cases._deliver_v2_candidate(
        scheduler, backend, journal, controller, baseline, owner,
    )
    service.project_v2_handover_terminal(scheduler, journal, "address", owner=owner)
    scheduler.fail_task("events", "verified failure", owner=owner)
    status = service.v2_status(scheduler)
    assert status.target_states == {
        "address": "delivered", "booking": "pending", "events": "blocked",
    }
    assert status.overall_state == "partial"
    assert status.to_dict()["overall_state"] == "partial"
    revision = scheduler.revision
    service.project_v2_handover_terminal(scheduler, journal, "address", owner=owner)
    assert scheduler.revision == revision


def test_v2_status_delivers_completed_read_only_only_run(tmp_path):
    import test_fanout_scheduler as scheduler_cases

    scheduler, backend, _journal, _controller, _baseline, owner = (
        scheduler_cases._v2_handover_fixture(tmp_path, read_only_only=True)
    )
    decision, = scheduler.schedule_ready(owner=owner)
    assert decision.task_id == "events"
    scheduler.mark_active("events", owner=owner)
    scheduler.begin_reconciliation("events", owner=owner)
    receipt = scheduler_cases._receipt(scheduler, backend, "events", "verified event bytes")
    scheduler.complete_reconciliation("events", receipt, owner=owner)
    status = scheduler_cases.fanout.ExecutionService.v2_status(scheduler)
    assert status.target_states == {"events": "delivered"}
    assert status.overall_state == "delivered"


def test_v2_target_mismatch_cannot_enter_journal_or_scheduler_cas(tmp_path):
    import test_fanout_scheduler as scheduler_cases

    scheduler, backend, journal, controller, baseline, owner = (
        scheduler_cases._v2_handover_fixture(tmp_path)
    )
    scheduler.schedule_ready(owner=owner)
    scheduler.mark_active("address", owner=owner)
    scheduler.begin_reconciliation("address", owner=owner)
    intent, terminal, bundle = scheduler_cases._v2_terminal(
        scheduler, backend, journal, controller, baseline, owner,
    )
    scheduler_cases._reconcile_v2_candidate(scheduler, backend, bundle, owner)
    revision = scheduler.revision
    with pytest.raises(scheduler_cases.fanout.RunStateError, match="target|binding"):
        journal.append_branch_intent(dataclasses.replace(intent, target_id="booking"), owner=owner)
    with pytest.raises(scheduler_cases.fanout.ExecutionValidationError, match="journal terminal"):
        scheduler_cases.fanout.ExecutionService.project_v2_handover_terminal(
            scheduler, journal, "address", owner=owner,
        )
    assert scheduler.revision == revision
    assert scheduler.schedule_ready(owner=owner) == ()


def test_v1_status_wire_omits_v2_target_fields():
    status = fanout.ExecutionStatus("run", 1, 1, 1, 0, ())
    assert fanout.canonical_json(status.to_dict()) == (
        b'{"authority_state":"resolved","execution_revision":1,'
        b'"plan_revision":1,"projected_provider_turns":0,"run_id":"run",'
        b'"scheduler_revision":1,"tasks":[]}\n'
    )


class _Journal:
    def __init__(self, inputs, owner):
        self.inputs = inputs
        self.owner = owner
        self.state = SimpleNamespace(task_phases={}, amendments={}, handovers={})
        self.fail_event_once = None

    def authorize_owner(self, owner):
        if owner is not self.owner:
            raise fanout.RunAuthorizationError("wrong owner")

    def append(
        self,
        event_type,
        *,
        task_id,
        owner=None,
        transaction_root=None,
        transaction_sha256=None,
        disposition_sha256=None,
        **_,
    ):
        self.authorize_owner(owner)
        if self.fail_event_once == event_type:
            self.fail_event_once = None
            raise RuntimeError(f"injected {event_type} interruption")
        if event_type.startswith("handover-"):
            current = self.state.handovers.get(task_id)
            if event_type == "handover-intent":
                if disposition_sha256 is not None:
                    raise fanout.RunStateError("handover intent has a disposition")
            elif (
                current is None
                or current.phase != "handover-intent"
                or current.transaction_root != transaction_root
                or current.transaction_sha256 != transaction_sha256
                or disposition_sha256 is None
            ):
                raise fanout.RunStateError("handover transaction changed")
            self.state.handovers[task_id] = SimpleNamespace(
                task_id=task_id,
                phase=event_type,
                transaction_root=transaction_root,
                transaction_sha256=transaction_sha256,
                disposition_sha256=disposition_sha256,
            )
        self.state.task_phases[task_id] = event_type

    def append_amendment(
        self,
        event_type,
        *,
        revision,
        old_plan_sha256,
        old_inputs_digest,
        new_plan_sha256,
        new_inputs_digest,
        old_profiles_sha256,
        new_profiles_sha256,
        owner,
    ):
        self.authorize_owner(owner)
        current = self.state.amendments.get(revision)
        binding = (
            old_plan_sha256,
            old_inputs_digest,
            new_plan_sha256,
            new_inputs_digest,
            old_profiles_sha256,
            new_profiles_sha256,
        )
        if current is not None and current.binding != binding:
            raise fanout.RunStateError("amendment binding changed")
        self.state.amendments[revision] = SimpleNamespace(
            phase=event_type,
            binding=binding,
            revision=revision,
        )


class _Scheduler:
    def __init__(self, plan, inputs, artifacts):
        self.plan = plan
        self.inputs = inputs
        self.plan_sha256 = _digest(fanout.canonical_json(plan.to_dict()))
        self.inputs_digest = inputs.digest
        self.plan_revision = 1
        self.revision = 1
        self.artifacts = artifacts
        self.phases = {task.id: "unscheduled" for task in plan.tasks}
        self.results = {}
        self.amendments = []

    @property
    def active_task_count(self):
        return sum(value in {"scheduled", "active", "reconciliation-pending", "blocked-action"}
                   for value in self.phases.values())

    @property
    def active_seat_count(self):
        return 0

    def task_phase(self, task_id):
        return self.phases[task_id]

    def result_for(self, task_id):
        return self.results.get(task_id)

    def pending_work(self):
        return ()

    def pending_actions(self):
        return ()

    def schedule_ready(self, *, owner):
        decisions = []
        for task in self.plan.tasks:
            if self.phases[task.id] != "unscheduled":
                continue
            if any(self.phases[item] != "completed" for item in task.depends_on):
                continue
            self.phases[task.id] = (
                "blocked-action" if task.execution_class == "orchestrator-action" else "scheduled"
            )
            if task.execution_class == "orchestrator-action":
                decisions.append(fanout.ActionBarrier(task.id, self.plan_revision, (), f"action-{task.id}"))
            else:
                policy = task.provider_policy or self.plan.defaults
                decisions.append(fanout.WorkDispatch(
                    task.id,
                    self.plan_revision,
                    len(policy.executor_ids),
                    (),
                    f"work-{task.id}",
                ))
        if decisions:
            self.revision += 1
        return tuple(decisions)

    def mark_active(self, task_id, *, owner):
        assert self.phases[task_id] == "scheduled"
        self.phases[task_id] = "active"
        self.revision += 1

    def begin_reconciliation(self, task_id, *, owner):
        assert self.phases[task_id] == "active"
        self.phases[task_id] = "reconciliation-pending"
        self.revision += 1

    def result_receipt(self, task_id, artifact):
        assert self.artifacts.read_bytes(artifact)
        return fanout.ReconciledResult(
            "run-execute", task_id, self.plan_revision, self.plan_sha256,
            self.inputs_digest, artifact,
        )

    def complete_reconciliation(self, task_id, receipt, *, owner):
        assert self.phases[task_id] == "reconciliation-pending"
        self.phases[task_id] = "completed"
        self.results[task_id] = receipt
        self.revision += 1
        return receipt

    def complete_action(self, task_id, receipt, *, owner):
        assert self.phases[task_id] == "blocked-action"
        self.phases[task_id] = "completed"
        self.results[task_id] = receipt
        self.revision += 1
        return receipt

    def fail_task(self, task_id, reason, *, owner):
        self.phases[task_id] = "failed"
        self.revision += 1

    def accept_amendment(self, replacement_plan, new_inputs, *, expected_plan_revision, owner):
        assert expected_plan_revision == self.plan_revision
        previous_tasks = tuple(self.plan.tasks)
        self.plan = replacement_plan
        self.plan_sha256 = _digest(fanout.canonical_json(replacement_plan.to_dict()))
        self.inputs_digest = new_inputs.digest
        self.inputs = new_inputs
        self.plan_revision += 1
        self.revision += 1
        self.phases = {task.id: self.phases.get(task.id, "unscheduled") for task in replacement_plan.tasks}
        result = fanout.PlanAmendment(
            self.plan_revision,
            tuple(task.id for task in replacement_plan.tasks if self.phases[task.id] == "unscheduled")
            + tuple(
                task.id for task in previous_tasks
                if task.kind == "work" and task.id not in self.phases
            ),
            self.plan_sha256,
        )
        self.amendments.append(result)
        return result


@dataclass
class _Barrier:
    task_id: str
    round: int
    status: str
    barrier_ref: object
    valid_terminals: tuple[object, ...]
    context_sha256: str
    candidate_sources: tuple[tuple[str, object], ...] = ()


class _Memory:
    def __init__(self, artifacts):
        self.artifacts = artifacts
        self.healthy = True
        self.preflight_calls = 0

    def preflight(self):
        self.preflight_calls += 1
        return self.healthy


class _Registry:
    def __init__(self, profile_digests):
        self.profile_digests = dict(profile_digests)

    def require(self, executor_id):
        if executor_id not in self.profile_digests:
            raise fanout.ProviderRequestError(f"unknown executor: {executor_id}")
        return executor_id


class _Coordinator:
    def __init__(
        self,
        artifacts,
        *,
        journal,
        owner,
        memory,
        registry,
        lifecycle_controller=None,
        repository_baseline=None,
    ):
        self.artifacts = artifacts
        self.journal = journal
        self.owner = owner
        self.memory = memory
        self.registry = registry
        self.lifecycle_controller = lifecycle_controller
        self.repository_baseline = repository_baseline
        self.preflights = []
        self.execute_calls = []
        self.provider_dispatches = []
        self.barriers = {}
        self.fail_task = None
        self.block_once = set()
        self.repo_candidate_bytes = None
        self.identical_answers = False
        self.provider_runner = self._execute_round

    def preflight_round(self, packet, seats, policy, *, round=1, expected_inputs=None):
        self.preflights.append(packet.task_id)
        if packet.task_id == self.fail_task:
            raise fanout.CollaborationValidationError("injected late preflight failure")
        return tuple(seats)

    def execute_round(
        self, packet, seats, policy, *, round, peer_source=None, recovery=None,
        expected_inputs=None,
    ):
        return self.provider_runner(
            packet,
            seats,
            policy,
            round=round,
            peer_source=peer_source,
            recovery=recovery,
            expected_inputs=expected_inputs,
        )

    def _execute_round(
        self, packet, seats, policy, *, round, peer_source=None, recovery=None,
        expected_inputs=None,
    ):
        self.execute_calls.append((packet.task_id, round, tuple(seat.seat_id for seat in seats)))
        if recovery is not None:
            sources = self._candidate_sources(packet, seats, policy, round, recovery.valid_terminals)
            data = fanout.canonical_json({
                "task_id": packet.task_id,
                "round": round,
                "recovered": True,
                "candidate_sources": [
                    {"seat_id": seat_id, "digest": ref.digest} for seat_id, ref in sources
                ],
            })
            ref = self.artifacts.write_bytes(
                f"fake/{packet.task_id}/{round}/barrier-recovered.json",
                data,
            )
            barrier = _Barrier(
                packet.task_id,
                round,
                "round-complete",
                ref,
                recovery.valid_terminals,
                packet.context_sha256,
                sources,
            )
            self.barriers[ref] = barrier
            return barrier
        self.provider_dispatches.append((packet.task_id, round))
        terminals = []
        for seat in seats:
            if packet.execution_class == "repo-write" and self.repo_candidate_bytes is not None:
                (seat.workspace_verification.workspace.root / "changed.txt").write_bytes(
                    self.repo_candidate_bytes[seat.seat_id]
                )
            answer = (b"same safe answer\n" if self.identical_answers
                      else f"safe answer from {seat.seat_id}".encode())
            answer_ref = self.artifacts.write_bytes(
                f"fake/{packet.task_id}/{round}/{seat.seat_id}/answer.txt",
                answer,
            )
            terminals.append(SimpleNamespace(seat_id=seat.seat_id, valid=True, answer_ref=answer_ref))
        data = fanout.canonical_json({"task_id": packet.task_id, "round": round})
        ref = self.artifacts.write_bytes(f"fake/{packet.task_id}/{round}/barrier.json", data)
        status = "blocked-memory" if packet.task_id in self.block_once else "round-complete"
        self.block_once.discard(packet.task_id)
        sources = (() if status != "round-complete" else
                   self._candidate_sources(packet, seats, policy, round, tuple(terminals)))
        if sources:
            data = fanout.canonical_json({
                "task_id": packet.task_id, "round": round,
                "candidate_sources": [
                    {"seat_id": seat_id, "digest": source.digest}
                    for seat_id, source in sources
                ],
            })
            ref = self.artifacts.write_bytes(
                f"fake/{packet.task_id}/{round}/barrier-sources.json", data,
            )
        barrier = _Barrier(
            packet.task_id,
            round,
            status,
            ref,
            tuple(terminals),
            packet.context_sha256,
            sources,
        )
        self.barriers[ref] = barrier
        return barrier

    def _candidate_sources(self, packet, seats, policy, round, terminals):
        if packet.execution_class != "repo-write" or round != policy.rounds:
            return ()
        seat_by_id = {seat.seat_id: seat for seat in seats}
        sources = []
        for terminal in sorted(terminals, key=lambda item: item.seat_id):
            seat = seat_by_id[terminal.seat_id]
            candidate = fanout.create_candidate(
                self.repository_baseline, seat.workspace_verification.workspace,
            )
            path = fanout.source_candidate_artifact_path(
                packet.run_id, packet.task_id, packet.attempt, round,
                terminal.seat_id,
            )
            try:
                ref = self.artifacts.write_bytes(path, candidate.manifest_bytes)
            except fanout.ArtifactExistsError:
                ref = fanout.ArtifactRef(path, candidate.digest, len(candidate.manifest_bytes))
                assert self.artifacts.read_bytes(ref) == candidate.manifest_bytes
            sources.append((terminal.seat_id, ref))
        return tuple(sources)

    def restore_barrier(self, ref, *, packet):
        barrier = self.barriers[ref]
        if barrier.context_sha256 != packet.context_sha256:
            raise fanout.CollaborationValidationError(
                "barrier terminal context does not match the task packet"
            )
        return barrier

    def discover_barrier(self, packet, *, round):
        matches = [
            barrier for barrier in self.barriers.values()
            if barrier.task_id == packet.task_id and barrier.round == round
        ]
        for status in ("round-complete", "failed-minimum", "blocked-memory"):
            selected = [barrier for barrier in matches if barrier.status == status]
            if selected:
                return selected[-1]
        return None


class _Backend:
    def __init__(self, owner):
        self.owner = owner
        self.record = None

    def identity(self):
        return "execute-test-backend/v1"

    def key(self):
        return "execution/run-execute"

    def max_record_bytes(self):
        return 8 * 1024 * 1024

    def read(self):
        return self.record

    def compare_and_set(self, expected_revision, snapshot, *, owner):
        if owner is not self.owner:
            raise fanout.RunAuthorizationError("wrong owner")
        actual = 0 if self.record is None else self.record.revision
        if expected_revision != actual:
            raise fanout.ExecutionConflictError("stale execution state")
        self.record = fanout.ExecutionRecord(actual + 1, snapshot)
        return self.record


def _seats(
    tmp_path,
    task_id,
    artifacts,
    executors=("claude", "codex"),
    workspace_verifications=None,
):
    source = tmp_path / "skill-source"
    source.mkdir(parents=True, exist_ok=True)
    resolver = fanout.SkillResolver((fanout.SkillRoot("tests", source, 0),))
    bundle = fanout.canonical_json({"schema_version": "fanout-seat-skill-bundle-v1", "skills": []})
    seats = []
    for executor in executors:
        admission = resolver.admit(
            (), task_id=task_id, seat_id=executor, provider=executor,
            session_id=f"session-{executor}",
        )
        admission = resolver.stage(admission, tmp_path / f"skills-{task_id}-{executor}")
        admission = resolver.verify_engine_delivery(admission, ())
        seats.append(fanout.SeatAssignment.from_admission(
            admission,
            skill_bundle=bundle,
            artifacts=artifacts,
            workspace_verification=(workspace_verifications or {}).get(executor),
        ))
    return tuple(seats), bundle


def _runtime(tmp_path, plan, *, registry=None):
    owner = fanout.OwnerCapability.from_token("o" * 43)
    repository = _repository(tmp_path)
    baseline = fanout.capture_repository_baseline(repository)
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    inputs = _inputs(
        plan,
        repo_digest=baseline.digest,
        profile_digests=(None if registry is None else registry.profile_digests),
    )
    artifacts = fanout.ArtifactStore(tmp_path / "artifacts")
    scheduler = _Scheduler(plan, inputs, artifacts)
    journal = _Journal(inputs, owner)
    memory = _Memory(artifacts)
    registry = _Registry(inputs.provider_profiles) if registry is None else registry
    coordinator = _Coordinator(
        artifacts,
        journal=journal,
        owner=owner,
        memory=memory,
        registry=registry,
        lifecycle_controller=controller,
        repository_baseline=baseline,
    )
    backend = _Backend(owner)
    preparations = {}
    workspaces = {}
    compiled = fanout.canonical_json(plan.to_dict())
    for task in plan.tasks:
        if task.execution_class == "orchestrator-action":
            continue
        verifications = {}
        for executor in (task.provider_policy or plan.defaults).executor_ids:
            workspace = fanout.create_seat_workspace(
                baseline, controller.root / "workspaces" / task.id, executor,
            )
            workspaces[(task.id, executor)] = workspace
            verifications[executor] = fanout.verify_seat_workspace(
                baseline, workspace, controller=controller,
            )
        seats, bundle = _seats(
            tmp_path, task.id, artifacts,
            executors=(task.provider_policy or plan.defaults).executor_ids,
            workspace_verifications=verifications,
        )
        packet_values = dict(
            run_id=inputs.run_id,
            task_id=task.id,
            attempt=1,
            compiled_plan=compiled,
            compiled_plan_sha256=inputs.compiled_plan_sha256,
            source_markdown=b"source",
            task=fanout.canonical_json(task.to_dict()),
            task_sha256=_digest(fanout.canonical_json(task.to_dict())),
            skill_bundle=bundle,
            skill_manifest_sha256=inputs.skill_manifests[task.id],
            dependency_artifacts=(),
            execution_class=task.execution_class,
            cwd=repository,
        )
        packet = (
            fanout.TaskPacket.for_repo_write(
                workspace_verifications=tuple(verifications.values()),
                lifecycle_controller=controller, **packet_values,
            ) if task.execution_class == "repo-write" else fanout.TaskPacket(**packet_values)
        )
        preparations[task.id] = fanout.TaskPreparation(
            packet,
            seats,
            fanout.RoundPolicy.from_provider_policy(task.provider_policy or plan.defaults),
            inputs,
            1,
            registry=registry if isinstance(registry, fanout.ProviderRegistry) else None,
        )
    service = fanout.ExecutionService(
        plan=plan,
        inputs=inputs,
        preparations=preparations,
        provider_profile_digests=dict(inputs.provider_profiles),
        budget=fanout.ExecutionBudget(max_provider_turns=64),
        scheduler=scheduler,
        journal=journal,
        coordinator=coordinator,
        artifacts=artifacts,
        backend=backend,
        memory_preflight=memory.preflight,
        baseline=baseline,
        lifecycle_controller=controller,
    )
    return SimpleNamespace(
        repository=repository,
        baseline=baseline,
        controller=controller,
        workspaces=workspaces,
        owner=owner,
        inputs=inputs,
        artifacts=artifacts,
        scheduler=scheduler,
        journal=journal,
        coordinator=coordinator,
        memory=memory,
        registry=registry,
        initial_registry_profiles=(
            tuple(registry._profiles.values()) if isinstance(registry, fanout.ProviderRegistry)
            else None
        ),
        backend=backend,
        service=service,
        preparations=preparations,
    )


def _read_only_preparation(tmp_path, plan, inputs, task, artifacts, *, runtime, registry=None):
    workspace_root = (
        runtime.controller.root / "amendment-workspaces"
        / _digest(os.fspath(tmp_path))[:16] / task.id
    )
    verifications = {}
    for executor in (task.provider_policy or plan.defaults).executor_ids:
        workspace = fanout.create_seat_workspace(
            runtime.baseline, workspace_root, executor,
        )
        verifications[executor] = fanout.verify_seat_workspace(
            runtime.baseline, workspace, controller=runtime.controller,
        )
    seats, bundle = _seats(
        tmp_path, task.id, artifacts,
        workspace_verifications=verifications,
    )
    packet = fanout.TaskPacket(
        run_id=inputs.run_id,
        task_id=task.id,
        attempt=1,
        compiled_plan=fanout.canonical_json(plan.to_dict()),
        compiled_plan_sha256=inputs.compiled_plan_sha256,
        source_markdown=b"source",
        task=fanout.canonical_json(task.to_dict()),
        task_sha256=_digest(fanout.canonical_json(task.to_dict())),
        skill_bundle=bundle,
        skill_manifest_sha256=inputs.skill_manifests[task.id],
        dependency_artifacts=(),
        execution_class="read-only",
        cwd=runtime.repository,
    )
    return fanout.TaskPreparation(
        packet,
        seats,
        fanout.RoundPolicy.from_provider_policy(task.provider_policy or plan.defaults),
        inputs,
        2,
        registry=registry,
    )


def test_start_finishes_every_preflight_before_any_provider_spend(tmp_path):
    plan = _plan(_task("first"), _task("second"))
    runtime = _runtime(tmp_path, plan)
    runtime.coordinator.fail_task = "second"

    with pytest.raises(fanout.ExecutionPreflightError, match="preflight"):
        runtime.service.start(owner=runtime.owner)

    assert runtime.coordinator.preflights == ["first", "second"]
    assert runtime.coordinator.execute_calls == []
    assert runtime.backend.record is None
    assert set(runtime.scheduler.phases.values()) == {"unscheduled"}


def test_mixed_plan_rejects_repo_write_before_first_read_only_profile_probe(tmp_path):
    plan = _plan(_task("read"), _task("write", execution_class="repo-write",
                                    owned_paths=("src/change.py",)))
    version_probes = []

    def forbidden_version(executor):
        version_probes.append(executor)
        raise AssertionError("FORBIDDEN_VERSION_PROBE")

    registry = fanout.ProviderRegistry.default(version_probe=forbidden_version)
    runtime = _runtime(tmp_path, plan, registry=registry)

    def probe_profile_before_launch(packet, seats, policy, **_kwargs):
        profile = registry.select(seats[0].executor_id, packet.execution_class,
                                  policy.quality_tier)
        registry.assert_installed_version(profile)
        return tuple(seats)

    runtime.coordinator.preflight_round = probe_profile_before_launch
    runtime.coordinator.provider_runner = fanout.run_provider

    with pytest.raises(fanout.ExecutionPreflightError) as error:
        runtime.service.start(owner=runtime.owner)

    assert version_probes == []
    assert "native seat boundary" in str(error.value)
    assert runtime.backend.record is None


@pytest.mark.parametrize("provider_runner", (fanout.run_provider, lambda _request: None))
def test_v2_read_only_service_preflight_refuses_before_provider_probe(provider_runner):
    service = SimpleNamespace(
        _work_tasks=(SimpleNamespace(execution_class="read-only"),),
        plan=SimpleNamespace(schema_version="v2"),
        inputs=SimpleNamespace(targets={"target": object()}),
        coordinator=SimpleNamespace(registry=fanout.ProviderRegistry.default(),
                                    provider_runner=provider_runner),
        _authenticate_composition=lambda *_args, **_kwargs: pytest.fail(
            "service authenticated or probed before v2 native gate"
        ),
    )
    with pytest.raises(fanout.ExecutionPreflightError, match="native seat boundary"):
        fanout.ExecutionService._preflight(service, object())


def test_start_refuses_read_only_without_exact_verified_snapshot_before_provider_spend(tmp_path):
    runtime = _runtime(tmp_path, _plan(_task("work")))
    admitted = runtime.preparations["work"]
    seats = (
        dataclasses.replace(admitted.seats[0], workspace_verification=None),
        admitted.seats[1],
    )
    unverified = dataclasses.replace(admitted, seats=seats)
    service = fanout.ExecutionService(
        plan=runtime.service.plan,
        inputs=runtime.inputs,
        preparations={"work": unverified},
        provider_profile_digests=dict(runtime.inputs.provider_profiles),
        budget=fanout.ExecutionBudget(max_provider_turns=64),
        scheduler=runtime.scheduler,
        journal=runtime.journal,
        coordinator=runtime.coordinator,
        artifacts=runtime.artifacts,
        backend=runtime.backend,
        memory_preflight=runtime.memory.preflight,
        baseline=runtime.baseline,
        lifecycle_controller=runtime.controller,
    )

    with pytest.raises(fanout.ExecutionPreflightError, match="read-only|baseline|workspace"):
        service.start(owner=runtime.owner)

    assert runtime.coordinator.execute_calls == []
    assert runtime.backend.record is None


def test_start_refuses_one_snapshot_reused_by_seats_of_different_tasks(tmp_path):
    runtime = _runtime(tmp_path, _plan(_task("first"), _task("second")))
    first = runtime.preparations["first"]
    second = runtime.preparations["second"]
    reused = dataclasses.replace(
        second.seats[0], workspace_verification=first.seats[0].workspace_verification,
    )
    preparations = {
        "first": first,
        "second": dataclasses.replace(second, seats=(reused, second.seats[1])),
    }
    service = fanout.ExecutionService(
        plan=runtime.service.plan,
        inputs=runtime.inputs,
        preparations=preparations,
        provider_profile_digests=dict(runtime.inputs.provider_profiles),
        budget=fanout.ExecutionBudget(max_provider_turns=64),
        scheduler=runtime.scheduler,
        journal=runtime.journal,
        coordinator=runtime.coordinator,
        artifacts=runtime.artifacts,
        backend=runtime.backend,
        memory_preflight=runtime.memory.preflight,
        baseline=runtime.baseline,
        lifecycle_controller=runtime.controller,
    )

    with pytest.raises(fanout.ExecutionPreflightError, match="shared|workspace"):
        service.start(owner=runtime.owner)

    assert runtime.coordinator.execute_calls == []
    assert runtime.backend.record is None


def test_resume_is_content_addressed_and_never_reruns_completed_provider_work(tmp_path):
    runtime = _runtime(tmp_path, _plan(_task("work")))
    started = runtime.service.start(owner=runtime.owner)
    assert started.tasks[0].phase == "scheduled"
    assert runtime.coordinator.execute_calls == []

    pending = runtime.service.resume(owner=runtime.owner)
    assert pending.tasks[0].phase == "reconciliation-pending"
    assert runtime.coordinator.execute_calls == [("work", 1, ("claude", "codex"))]
    first_record = runtime.backend.record
    assert first_record.snapshot.tasks[0].barriers[0].digest

    recovered = fanout.ExecutionService(
        plan=runtime.scheduler.plan,
        inputs=runtime.inputs,
        preparations=runtime.preparations,
        provider_profile_digests=dict(runtime.inputs.provider_profiles),
        budget=fanout.ExecutionBudget(max_provider_turns=64),
        scheduler=runtime.scheduler,
        journal=runtime.journal,
        coordinator=runtime.coordinator,
        artifacts=runtime.artifacts,
        backend=runtime.backend,
        memory_preflight=runtime.memory.preflight,
        baseline=runtime.baseline,
        lifecycle_controller=runtime.controller,
    )
    status = recovered.resume(owner=runtime.owner)

    assert status.tasks[0].phase == "reconciliation-pending"
    assert runtime.coordinator.execute_calls == [("work", 1, ("claude", "codex"))]
    assert "safe answer" not in json.dumps(status.to_dict())


def test_resume_recovers_memory_barrier_without_repeating_provider_spend(tmp_path):
    runtime = _runtime(tmp_path, _plan(_task("work")))
    runtime.coordinator.block_once.add("work")
    runtime.service.start(owner=runtime.owner)

    blocked = runtime.service.resume(owner=runtime.owner)
    assert blocked.tasks[0].phase == "active"
    assert runtime.coordinator.provider_dispatches == [("work", 1)]
    blocked_ref = runtime.backend.record.snapshot.tasks[0].barriers[0]

    recovered = runtime.service.resume(owner=runtime.owner)
    assert recovered.tasks[0].phase == "reconciliation-pending"
    assert runtime.coordinator.provider_dispatches == [("work", 1)]
    assert runtime.backend.record.snapshot.tasks[0].barriers[0] != blocked_ref


def test_only_owner_can_submit_and_unverified_synthesis_stays_pending(tmp_path):
    runtime = _runtime(tmp_path, _plan(_task("work")))
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    wrong = fanout.OwnerCapability.from_token("x" * 43)
    unverified = fanout.CandidateBundle(_digest("baseline"), (), ())

    with pytest.raises(fanout.RunAuthorizationError):
        runtime.service.submit("work", unverified, owner=wrong)
    with pytest.raises(fanout.ExecutionPendingError, match="verified"):
        runtime.service.submit("work", unverified, owner=runtime.owner)

    assert runtime.scheduler.task_phase("work") == "reconciliation-pending"
    barrier = next(iter(runtime.coordinator.barriers.values()))
    receipt = runtime.service.submit(
        "work", barrier.valid_terminals[0].answer_ref, owner=runtime.owner,
    )
    assert receipt.task_id == "work"
    assert runtime.scheduler.task_phase("work") == "completed"


def test_repo_write_plan_cannot_downgrade_required_synthesis():
    ordinary = _plan(_task("ordinary"))
    repository = _plan(_task("write", execution_class="repo-write"))
    assert ordinary.tasks[0].reconciliation_policy == "select-or-synthesize"
    assert repository.tasks[0].reconciliation_policy == "synthesis-required"
    historical_wire_plan = repository.to_dict()
    assert "reconciliation_policy" not in historical_wire_plan["tasks"][0]
    assert fanout.FanoutPlanV1.from_dict(historical_wire_plan).tasks[0].reconciliation_policy == "synthesis-required"

    amended_wire_plan = copy.deepcopy(historical_wire_plan)
    amended_wire_plan["tasks"][0]["objective"] = "Amended repository work."
    amended_wire_plan["tasks"][0]["reconciliation_policy"] = "select-or-synthesize"
    with pytest.raises(fanout.PlanValidationError, match="synthesis"):
        fanout.FanoutPlanV1.from_dict(amended_wire_plan)

    amended_wire_plan["tasks"][0]["reconciliation_policy"] = 0
    with pytest.raises(fanout.PlanValidationError, match="reconciliation_policy"):
        fanout.FanoutPlanV1.from_dict(amended_wire_plan)
    amended_wire_plan["tasks"][0]["reconciliation_policy"] = []
    with pytest.raises(fanout.PlanValidationError, match="reconciliation_policy"):
        fanout.FanoutPlanV1.from_dict(amended_wire_plan)

    object.__setattr__(repository.tasks[0], "reconciliation_policy", "select-or-synthesize")
    with pytest.raises(fanout.PlanValidationError, match="synthesis"):
        fanout.validate_plan(repository)

    read_only_required = ordinary.to_dict()
    read_only_required["tasks"][0]["reconciliation_policy"] = "synthesis-required"
    assert fanout.FanoutPlanV1.from_dict(read_only_required).to_dict()["tasks"][0]["reconciliation_policy"] == "synthesis-required"

    downgraded = _task("write", execution_class="repo-write")
    downgraded["reconciliation_policy"] = "select-or-synthesize"
    with pytest.raises(fanout.PlanValidationError, match="synthesis"):
        _plan(downgraded)


def test_verified_read_only_synthesis_unlocks_dependant_with_exact_final_sources(tmp_path):
    runtime = _runtime(tmp_path, _plan(_task("work"), _task("next", depends_on=("work",))))
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    barrier = next(item for item in runtime.coordinator.barriers.values() if item.task_id == "work")
    synthesis = runtime.service.verify_read_only_synthesis(
        "work", source_seat_ids=tuple(terminal.seat_id for terminal in barrier.valid_terminals),
        synthesizer_id="orchestrator-codex",
        answer=b"a new combined answer\n",
        owner=runtime.owner,
    )

    result = runtime.service.submit("work", synthesis, owner=runtime.owner)

    assert result.artifact == synthesis.answer_ref
    assert runtime.artifacts.read_bytes(result.artifact) == b"a new combined answer\n"
    assert runtime.scheduler.task_phase("next") == "scheduled"


def test_restarted_service_rejects_missing_answer_synthesis_receipt(tmp_path):
    plan = _plan(_task("work"), _task("next", depends_on=("work",)))
    runtime = _runtime(tmp_path, plan)
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    barrier = next(item for item in runtime.coordinator.barriers.values() if item.task_id == "work")
    synthesis = fanout.verify_answer_synthesis(
        runtime.artifacts, run_id=runtime.inputs.run_id, task_id="work",
        plan_sha256=runtime.inputs.compiled_plan_sha256,
        plan_revision=runtime.scheduler.plan_revision,
        sources={terminal.seat_id: terminal.answer_ref for terminal in barrier.valid_terminals},
        synthesizer_id="orchestrator-codex", answer=b"combined answer\n",
        checks=(), controller=runtime.controller,
    )
    runtime.service.submit("work", synthesis, owner=runtime.owner)
    resumed, coordinator = _restart_before_profile_recovery(runtime, plan)
    coordinator.barriers = dict(runtime.coordinator.barriers)
    assert resumed.status().tasks[0].phase == "completed"
    (runtime.controller.root / "receipts" / synthesis.evidence_name).unlink()

    with pytest.raises(fanout.ExecutionConflictError, match="synthesis|receipt"):
        resumed.status()
    with pytest.raises(fanout.ExecutionConflictError, match="synthesis|receipt"):
        resumed.resume(owner=runtime.owner)
    assert coordinator.provider_dispatches == []


@pytest.mark.parametrize("remove_old", (False, True))
def test_pending_amendment_status_keeps_completed_answer_proof_on_cold_reopen(
    tmp_path, monkeypatch, remove_old,
):
    original = _plan(
        _task("work"),
        _task("approve", execution_class="orchestrator-action"),
        _task("later", depends_on=("approve",)),
    )
    runtime = _runtime(tmp_path, original)
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    barrier = next(item for item in runtime.coordinator.barriers.values() if item.task_id == "work")
    synthesis = fanout.verify_answer_synthesis(
        runtime.artifacts, run_id=runtime.inputs.run_id, task_id="work",
        plan_sha256=runtime.inputs.compiled_plan_sha256,
        plan_revision=1,
        sources={terminal.seat_id: terminal.answer_ref for terminal in barrier.valid_terminals},
        synthesizer_id="orchestrator-codex", answer=b"combined answer\n",
        checks=(), controller=runtime.controller,
    )
    runtime.service.submit("work", synthesis, owner=runtime.owner)
    replacement_data = original.to_dict()
    replacement_data["tasks"][2]["objective"] = "Complete the amended later work."
    if remove_old:
        replacement_data["tasks"][2]["id"] = "fresh"
        replacement_data["tasks"][2]["title"] = "fresh"
    replacement = fanout.FanoutPlanV1.from_dict(replacement_data)
    new_inputs = _inputs(replacement, repo_digest=runtime.baseline.digest)
    preparation = _read_only_preparation(
        tmp_path / "amended", replacement, new_inputs,
        replacement.tasks[2], runtime.artifacts, runtime=runtime,
    )
    original_accept = runtime.scheduler.accept_amendment

    def interrupted_accept(*args, **kwargs):
        original_accept(*args, **kwargs)
        raise SystemExit("after scheduler acceptance")

    monkeypatch.setattr(runtime.scheduler, "accept_amendment", interrupted_accept)
    with pytest.raises(SystemExit):
        runtime.service.submit_amendment(
            replacement, new_inputs, {replacement.tasks[2].id: preparation},
            dict(new_inputs.provider_profiles), expected_plan_revision=1,
            owner=runtime.owner,
        )
    monkeypatch.undo()
    resumed, coordinator = _restart_before_profile_recovery(runtime, original)
    status = resumed.status()
    assert status.tasks[0].phase == "completed"
    assert status.plan_revision == 2
    assert coordinator.provider_dispatches == []


def test_stale_or_forged_read_only_synthesis_stays_pending(tmp_path):
    runtime = _runtime(tmp_path, _plan(_task("work"), _task("next", depends_on=("work",))))
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    barrier = next(item for item in runtime.coordinator.barriers.values() if item.task_id == "work")
    sources = {terminal.seat_id: terminal.answer_ref for terminal in barrier.valid_terminals}
    stale = dict(sources)
    stale["codex"] = runtime.artifacts.write_bytes("other/answer.txt", b"foreign answer")
    synthesis = fanout.verify_answer_synthesis(
        runtime.artifacts,
        run_id=runtime.inputs.run_id,
        task_id="work",
        plan_sha256=runtime.inputs.compiled_plan_sha256,
        plan_revision=runtime.scheduler.plan_revision,
        sources=stale,
        synthesizer_id="orchestrator-codex",
        answer=b"combined answer\n",
        checks=(),
        controller=runtime.controller,
    )
    with pytest.raises(fanout.ExecutionPendingError, match="final-barrier|source"):
        runtime.service.submit("work", synthesis, owner=runtime.owner)
    with pytest.raises(fanout.ExecutionPendingError, match="unauthenticated|evidence"):
        runtime.service.submit(
            "work", dataclasses.replace(synthesis, synthesizer_id="forged"), owner=runtime.owner,
        )
    assert runtime.scheduler.task_phase("work") == "reconciliation-pending"
    assert runtime.scheduler.task_phase("next") == "unscheduled"


def test_synthesis_cannot_disguise_an_unlisted_final_seat_answer(tmp_path):
    task = _task("work")
    task["provider_policy"]["executor_ids"] = ["claude", "codex", "agy"]
    runtime = _runtime(tmp_path, _plan(task, _task("next", depends_on=("work",))))
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    barrier = next(item for item in runtime.coordinator.barriers.values() if item.task_id == "work")
    answers = {terminal.seat_id: terminal.answer_ref for terminal in barrier.valid_terminals}
    result_bytes = runtime.artifacts.read_bytes(answers["agy"])
    synthesis = fanout.verify_answer_synthesis(
        runtime.artifacts, run_id=runtime.inputs.run_id, task_id="work",
        plan_sha256=runtime.inputs.compiled_plan_sha256,
        plan_revision=runtime.scheduler.plan_revision,
        sources={"claude": answers["claude"], "codex": answers["codex"]},
        synthesizer_id="orchestrator-codex", answer=result_bytes,
        checks=(), controller=runtime.controller,
    )
    with pytest.raises(fanout.ExecutionPendingError, match="seat answer|new synthesis"):
        runtime.service.submit("work", synthesis, owner=runtime.owner)
    assert runtime.scheduler.task_phase("next") == "unscheduled"


def test_failed_declared_answer_check_cannot_unlock_dependant(tmp_path):
    check = fanout.PlanCheckV1(
        argv=(sys.executable, "-c", "raise SystemExit(3)"),
        cwd="", env_allowlist=(), timeout=5,
        accepted_exit_codes=(0,), expected_artifacts=(),
    )
    runtime = _runtime(tmp_path, _plan(_task("work", checks=(check,)),
                                       _task("next", depends_on=("work",))))
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    barrier = next(item for item in runtime.coordinator.barriers.values() if item.task_id == "work")
    sources = {terminal.seat_id: terminal.answer_ref for terminal in barrier.valid_terminals}
    synthesis = fanout.verify_answer_synthesis(
        runtime.artifacts, run_id=runtime.inputs.run_id, task_id="work",
        plan_sha256=runtime.inputs.compiled_plan_sha256,
        plan_revision=runtime.scheduler.plan_revision, sources=sources,
        synthesizer_id="orchestrator-codex", answer=b"combined answer\n",
        checks=(check,), controller=runtime.controller,
    )
    assert not synthesis.valid
    with pytest.raises(fanout.ExecutionPendingError, match="declared check"):
        runtime.service.submit("work", synthesis, owner=runtime.owner)
    assert runtime.scheduler.task_phase("work") == "reconciliation-pending"
    assert runtime.scheduler.task_phase("next") == "unscheduled"


def test_declared_checks_prevent_unverified_single_seat_selection(tmp_path):
    check = fanout.PlanCheckV1(
        argv=(sys.executable, "-c", "raise SystemExit(3)"),
        cwd="", env_allowlist=(), timeout=5,
        accepted_exit_codes=(0,), expected_artifacts=(),
    )
    runtime = _runtime(tmp_path, _plan(_task("work", checks=(check,)),
                                       _task("next", depends_on=("work",))))
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    barrier = next(item for item in runtime.coordinator.barriers.values() if item.task_id == "work")
    with pytest.raises(fanout.ExecutionPendingError, match="declared checks"):
        runtime.service.submit("work", barrier.valid_terminals[0].answer_ref, owner=runtime.owner)
    assert runtime.scheduler.task_phase("work") == "reconciliation-pending"
    assert runtime.scheduler.task_phase("next") == "unscheduled"


def test_identical_checked_answers_allow_fresh_explicit_selection_and_restart_proof(tmp_path):
    check = fanout.PlanCheckV1(
        argv=(sys.executable, "-c", "from pathlib import Path; assert Path('answer.md').read_text() == 'same safe answer\\n'"),
        cwd="", env_allowlist=(), timeout=5,
        accepted_exit_codes=(0,), expected_artifacts=(),
    )
    plan = _plan(_task("work", checks=(check,)), _task("next", depends_on=("work",)))
    runtime = _runtime(tmp_path, plan)
    runtime.coordinator.identical_answers = True
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    barrier = next(item for item in runtime.coordinator.barriers.values() if item.task_id == "work")
    assert len({terminal.answer_ref.digest for terminal in barrier.valid_terminals}) == 1
    selected = barrier.valid_terminals[0]
    receipt = runtime.service.verify_read_only_selection(
        "work", selected.seat_id, owner=runtime.owner,
    )
    assert receipt.valid
    with pytest.raises(fanout.ExecutionPendingError, match="selection evidence"):
        runtime.service.submit(
            "work", dataclasses.replace(receipt, seat_id="forged"), owner=runtime.owner,
        )
    assert runtime.scheduler.task_phase("next") == "unscheduled"
    result = runtime.service.submit("work", receipt, owner=runtime.owner)
    assert runtime.artifacts.read_bytes(result.artifact) == b"same safe answer\n"
    assert runtime.scheduler.task_phase("next") == "scheduled"
    restarted, coordinator = _restart_before_profile_recovery(runtime, plan)
    assert restarted.status().tasks[0].phase == "completed"
    (runtime.controller.root / "receipts" / receipt.evidence_name).unlink()
    with pytest.raises(fanout.ExecutionConflictError, match="selection|receipt"):
        restarted.status()
    assert coordinator.provider_dispatches == []


def test_failed_checked_selection_remains_pending(tmp_path):
    check = fanout.PlanCheckV1(
        argv=(sys.executable, "-c", "raise SystemExit(3)"),
        cwd="", env_allowlist=(), timeout=5,
        accepted_exit_codes=(0,), expected_artifacts=(),
    )
    runtime = _runtime(tmp_path, _plan(_task("work", checks=(check,))))
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    barrier = next(item for item in runtime.coordinator.barriers.values() if item.task_id == "work")
    receipt = runtime.service.verify_read_only_selection(
        "work", barrier.valid_terminals[0].seat_id, owner=runtime.owner,
    )
    assert not receipt.valid
    with pytest.raises(fanout.ExecutionPendingError, match="fresh task verification"):
        runtime.service.submit("work", receipt, owner=runtime.owner)
    assert runtime.scheduler.task_phase("work") == "reconciliation-pending"


def test_owner_can_reconcile_durable_work_after_memory_becomes_unavailable(tmp_path):
    runtime = _runtime(tmp_path, _plan(_task("work")))
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    barrier = next(iter(runtime.coordinator.barriers.values()))
    runtime.memory.healthy = False

    receipt = runtime.service.submit(
        "work", barrier.valid_terminals[0].answer_ref, owner=runtime.owner,
    )

    assert receipt.task_id == "work"
    assert runtime.scheduler.task_phase("work") == "completed"


def test_orchestrator_actions_remain_owner_barriers_and_never_reach_maka_or_a_provider(tmp_path):
    runtime = _runtime(tmp_path, _plan(_task("approve", execution_class="orchestrator-action")))
    status = runtime.service.start(owner=runtime.owner)
    assert status.tasks[0].phase == "blocked-action"
    assert runtime.coordinator.preflights == []

    artifact = runtime.artifacts.write_bytes("actions/approve.json", fanout.canonical_json({"ok": True}))
    wrong = fanout.OwnerCapability.from_token("x" * 43)
    with pytest.raises(fanout.RunAuthorizationError):
        runtime.service.action_complete("approve", artifact, owner=wrong)
    runtime.service.action_complete("approve", artifact, owner=runtime.owner)

    assert runtime.scheduler.task_phase("approve") == "completed"
    assert runtime.coordinator.execute_calls == []


def test_bounded_cost_preflight_refuses_the_run_before_state_or_spend(tmp_path):
    runtime = _runtime(tmp_path, _plan(_task("work")))
    runtime.service = fanout.ExecutionService(
        plan=runtime.scheduler.plan,
        inputs=runtime.inputs,
        preparations=runtime.preparations,
        provider_profile_digests=dict(runtime.inputs.provider_profiles),
        budget=fanout.ExecutionBudget(max_provider_turns=1),
        scheduler=runtime.scheduler,
        journal=runtime.journal,
        coordinator=runtime.coordinator,
        artifacts=runtime.artifacts,
        backend=runtime.backend,
        memory_preflight=runtime.memory.preflight,
        baseline=runtime.baseline,
        lifecycle_controller=runtime.controller,
    )

    with pytest.raises(fanout.ExecutionPreflightError, match="budget"):
        runtime.service.start(owner=runtime.owner)

    assert runtime.backend.record is None
    assert runtime.coordinator.execute_calls == []


def _git(repository: Path, *args: str) -> bytes:
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.startswith("GIT_"):
            environment.pop(name)
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    completed = subprocess.run(
        ("git", "-C", os.fspath(repository), *args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr.decode("utf-8", "replace"))
    return completed.stdout


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "caller"
    repository.mkdir(parents=True)
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "changed.txt").write_text("before\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "baseline")
    return repository


def _repo_runtime(tmp_path: Path, plan=None):
    repository = _repository(tmp_path)
    baseline = fanout.capture_repository_baseline(repository)
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    workspaces = {
        executor: fanout.create_seat_workspace(
            baseline,
            controller.root / "workspaces",
            executor,
        )
        for executor in ("claude", "codex")
    }
    verifications = {
        executor: fanout.verify_seat_workspace(
            baseline,
            workspace,
            controller=controller,
        )
        for executor, workspace in workspaces.items()
    }
    check = fanout.PlanCheckV1(
        argv=(
            sys.executable,
            "-c",
            "from pathlib import Path; assert Path('changed.txt').read_text() == 'after\\n'",
        ),
        cwd="",
        env_allowlist=(),
        timeout=5,
        accepted_exit_codes=(0,),
        expected_artifacts=(),
    )
    if plan is None:
        plan = _plan(_task(
            "write",
            execution_class="repo-write",
            checks=(check,),
            owned_paths=("changed.txt",),
        ))
    inputs = _inputs(plan, repo_digest=baseline.digest)
    owner = fanout.OwnerCapability.from_token("o" * 43)
    artifacts = fanout.ArtifactStore(tmp_path / "artifacts")
    seats, bundle = _seats(
        tmp_path,
        "write",
        artifacts,
        workspace_verifications=verifications,
    )
    task = plan.tasks[0]
    packet = fanout.TaskPacket.for_repo_write(
        workspace_verifications=tuple(verifications.values()),
        lifecycle_controller=controller,
        run_id=inputs.run_id,
        task_id=task.id,
        attempt=1,
        compiled_plan=fanout.canonical_json(plan.to_dict()),
        compiled_plan_sha256=inputs.compiled_plan_sha256,
        source_markdown=b"source",
        task=fanout.canonical_json(task.to_dict()),
        task_sha256=_digest(fanout.canonical_json(task.to_dict())),
        skill_bundle=bundle,
        skill_manifest_sha256=inputs.skill_manifests[task.id],
        dependency_artifacts=(),
        execution_class="repo-write",
        cwd=repository,
    )
    preparation = fanout.TaskPreparation(
        packet,
        seats,
        fanout.RoundPolicy.from_provider_policy(task.provider_policy),
        inputs,
        1,
    )
    scheduler = _Scheduler(plan, inputs, artifacts)
    journal = _Journal(inputs, owner)
    memory = _Memory(artifacts)
    registry = _Registry(inputs.provider_profiles)
    coordinator = _Coordinator(
        artifacts,
        journal=journal,
        owner=owner,
        memory=memory,
        registry=registry,
        lifecycle_controller=controller,
        repository_baseline=baseline,
    )
    coordinator.repo_candidate_bytes = {
        "claude": b"source-claude\n",
        "codex": b"source-codex\n",
    }
    backend = _Backend(owner)
    service = fanout.ExecutionService(
        plan=plan,
        inputs=inputs,
        preparations={"write": preparation},
        provider_profile_digests=dict(inputs.provider_profiles),
        budget=fanout.ExecutionBudget(max_provider_turns=8),
        scheduler=scheduler,
        journal=journal,
        coordinator=coordinator,
        artifacts=artifacts,
        backend=backend,
        memory_preflight=memory.preflight,
        baseline=baseline,
        lifecycle_controller=controller,
    )
    return SimpleNamespace(
        repository=repository,
        baseline=baseline,
        controller=controller,
        workspaces=workspaces,
        verifications=verifications,
        owner=owner,
        inputs=inputs,
        artifacts=artifacts,
        backend=backend,
        coordinator=coordinator,
        preparations={"write": preparation},
        service=service,
        scheduler=scheduler,
        journal=journal,
        memory=memory,
        registry=registry,
    )


def _verified_repo_synthesis(runtime):
    collected = runtime.service.collect("write", owner=runtime.owner)
    synthesis = fanout.synthesize_candidate(
        runtime.baseline,
        sources=tuple(item.candidate for item in collected),
        entries=(fanout.CandidateEntry("changed.txt", "file", 0o644, b"after\n"),),
        deleted_paths=(),
    )
    receipt = runtime.service.verify_repo_synthesis(
        "write", synthesis, owner=runtime.owner,
    )
    assert receipt.valid
    return receipt, collected


def test_repo_write_rejects_verified_single_seat_then_accepts_fresh_synthesis(tmp_path):
    runtime = _repo_runtime(tmp_path)
    runtime.coordinator.repo_candidate_bytes["claude"] = b"after\n"
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    direct = next(item.candidate for item in runtime.service.collect("write", owner=runtime.owner)
                  if item.seat_id == "claude")
    direct_receipt = fanout.verify_candidate(
        runtime.baseline, direct, runtime.service.plan.tasks[0].checks,
        controller=runtime.controller,
    )
    assert direct_receipt.valid
    with pytest.raises(fanout.ExecutionPendingError, match="synthesis provenance"):
        runtime.service.verify_repo_synthesis("write", direct, owner=runtime.owner)
    with pytest.raises(fanout.ExecutionPendingError, match="task-bound verifier receipt"):
        runtime.service.submit("write", direct_receipt, owner=runtime.owner)
    assert runtime.scheduler.task_phase("write") == "reconciliation-pending"

    synthesis_receipt, collected = _verified_repo_synthesis(runtime)
    generic_receipt = fanout.verify_candidate(
        runtime.baseline, synthesis_receipt.candidate,
        runtime.service.plan.tasks[0].checks, controller=runtime.controller,
    )
    assert generic_receipt.valid and generic_receipt.reconciliation is None
    with pytest.raises(fanout.ExecutionPendingError, match="task-bound verifier receipt"):
        runtime.service.submit("write", generic_receipt, owner=runtime.owner)
    assert generic_receipt.evidence_digest != synthesis_receipt.evidence_digest
    result = runtime.service.submit("write", synthesis_receipt, owner=runtime.owner)
    assert runtime.artifacts.read_bytes(result.artifact) == synthesis_receipt.candidate.manifest_bytes
    assert set(synthesis_receipt.candidate.source_candidate_digests) == {
        item.candidate.digest for item in collected
    }


def test_completed_repo_synthesis_requires_its_signed_verifier_receipt_on_reopen(tmp_path):
    runtime = _repo_runtime(tmp_path)
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    receipt, _sources = _verified_repo_synthesis(runtime)
    runtime.service.submit("write", receipt, owner=runtime.owner)
    restarted, coordinator = _restart_before_profile_recovery(runtime, runtime.scheduler.plan)
    assert restarted.status().tasks[0].phase == "completed"
    (runtime.controller.root / "receipts" / receipt.evidence_name).unlink()

    with pytest.raises(fanout.ExecutionConflictError, match="synthesis|receipt"):
        restarted.status()
    with pytest.raises(fanout.ExecutionConflictError, match="synthesis|receipt"):
        restarted.resume(owner=runtime.owner)
    assert coordinator.provider_dispatches == []


def test_repo_sources_are_frozen_at_final_barrier_not_recollected_after_mutation(tmp_path):
    runtime = _repo_runtime(tmp_path)
    runtime.coordinator.repo_candidate_bytes = {
        "claude": b"at-barrier-claude\n",
        "codex": b"at-barrier-codex\n",
    }
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    (runtime.workspaces["claude"].root / "changed.txt").write_bytes(b"late mutation\n")
    collected = runtime.service.collect("write", owner=runtime.owner)
    observed = {item.seat_id: item.candidate.entries[0].data for item in collected}
    assert observed == runtime.coordinator.repo_candidate_bytes

    synthesis = fanout.synthesize_candidate(
        runtime.baseline,
        sources=tuple(item.candidate for item in collected),
        entries=(fanout.CandidateEntry("changed.txt", "file", 0o644, b"after\n"),),
        deleted_paths=(),
    )
    receipt = runtime.service.verify_repo_synthesis(
        "write", synthesis, owner=runtime.owner,
    )
    result = runtime.service.submit("write", receipt, owner=runtime.owner)
    assert runtime.artifacts.read_bytes(result.artifact) == synthesis.manifest_bytes


def test_identical_final_seat_patches_can_both_contribute_to_synthesis(tmp_path):
    runtime = _repo_runtime(tmp_path)
    runtime.coordinator.repo_candidate_bytes = {
        "claude": b"same-source\n", "codex": b"same-source\n",
    }
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    collected = runtime.service.collect("write", owner=runtime.owner)
    assert collected[0].candidate.digest == collected[1].candidate.digest
    synthesis = fanout.synthesize_candidate(
        runtime.baseline,
        sources=tuple(item.candidate for item in collected),
        entries=(fanout.CandidateEntry("changed.txt", "file", 0o644, b"after\n"),),
        deleted_paths=(),
    )
    receipt = runtime.service.verify_repo_synthesis(
        "write", synthesis, owner=runtime.owner,
    )
    result = runtime.service.submit("write", receipt, owner=runtime.owner)
    assert runtime.artifacts.read_bytes(result.artifact) == synthesis.manifest_bytes


def test_one_final_seat_cannot_be_counted_twice_as_repo_synthesis(tmp_path):
    runtime = _repo_runtime(tmp_path)
    runtime.coordinator.repo_candidate_bytes = {
        "claude": b"source-a\n", "codex": b"source-b\n",
    }
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    collected = runtime.service.collect("write", owner=runtime.owner)
    source_a = next(item.candidate for item in collected if item.seat_id == "claude")
    assert source_a.digest != next(item.candidate for item in collected if item.seat_id == "codex").digest
    synthesis = fanout.synthesize_candidate(
        runtime.baseline,
        sources=(source_a, source_a),
        entries=(fanout.CandidateEntry("changed.txt", "file", 0o644, b"after\n"),),
        deleted_paths=(),
    )
    receipt = fanout.verify_candidate(
        runtime.baseline, synthesis, runtime.service.plan.tasks[0].checks,
        controller=runtime.controller,
    )
    assert receipt.valid
    with pytest.raises(fanout.ExecutionPendingError, match="provenance"):
        runtime.service.verify_repo_synthesis("write", synthesis, owner=runtime.owner)
    with pytest.raises(fanout.ExecutionPendingError, match="task-bound verifier receipt"):
        runtime.service.submit("write", receipt, owner=runtime.owner)
    assert runtime.scheduler.task_phase("write") == "reconciliation-pending"


def test_repo_write_requires_authenticated_distinct_workspaces_and_composes_lifecycle(tmp_path):
    runtime = _repo_runtime(tmp_path)
    assert _git(runtime.workspaces["claude"].root, "remote") == b""
    assert _git(runtime.workspaces["codex"].root, "remote") == b""
    assert runtime.workspaces["claude"].root != runtime.workspaces["codex"].root

    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    recovered_workspace = fanout.resume_seat_workspace(
        runtime.workspaces["claude"],
        controller=runtime.controller,
        evidence_digest=runtime.verifications["claude"].evidence_digest,
    )
    assert recovered_workspace.workspace.root == runtime.workspaces["claude"].root
    assert (runtime.repository / "changed.txt").read_text(encoding="utf-8") == "before\n"

    receipt, collected = _verified_repo_synthesis(runtime)
    assert {item.seat_id for item in collected} == {"claude", "codex"}
    assert set(receipt.candidate.source_candidate_digests) == {
        item.candidate.digest for item in collected
    }
    runtime.service.submit("write", receipt, owner=runtime.owner)
    handover = runtime.service.handover("write", receipt, owner=runtime.owner)

    assert handover.candidate_digest == receipt.candidate.digest
    assert (runtime.repository / "changed.txt").read_text(encoding="utf-8") == "after\n"
    scratch = runtime.controller.root / "scratch"
    scratch.mkdir()
    (scratch / "owned.txt").write_text("owned\n", encoding="utf-8")
    claim = runtime.service.claim_gc_path("scratch", owner=runtime.owner)
    removed = runtime.service.gc((claim,), owner=runtime.owner)
    assert removed == (scratch,)
    assert not scratch.exists()


def test_collect_remains_pending_until_every_provider_round_is_durable(tmp_path):
    runtime = _repo_runtime(tmp_path)
    runtime.service.start(owner=runtime.owner)

    with pytest.raises(fanout.ExecutionPendingError, match="provider rounds"):
        runtime.service.collect("write", owner=runtime.owner)


def test_invalid_fresh_synthesis_verification_remains_reconciliation_pending(tmp_path):
    runtime = _repo_runtime(tmp_path)
    runtime.coordinator.repo_candidate_bytes = {
        "claude": b"wrong-claude\n", "codex": b"wrong-codex\n",
    }
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    candidate = runtime.service.collect("write", owner=runtime.owner)[0].candidate
    receipt = fanout.verify_candidate(
        runtime.baseline,
        candidate,
        runtime.service.plan.tasks[0].checks,
        controller=runtime.controller,
    )
    assert receipt.valid is False

    with pytest.raises(fanout.ExecutionPendingError, match="fresh verification"):
        runtime.service.submit("write", receipt, owner=runtime.owner)

    assert runtime.scheduler.task_phase("write") == "reconciliation-pending"
    assert runtime.journal.state.task_phases["write"] == "reconciliation-pending"


def test_valid_but_uncollected_candidate_without_source_provenance_stays_pending(tmp_path):
    runtime = _repo_runtime(tmp_path)
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    candidate = fanout.CandidateBundle(
        runtime.baseline.digest,
        (fanout.CandidateEntry("changed.txt", "file", 0o644, b"after\n"),),
        (),
    )
    receipt = fanout.verify_candidate(
        runtime.baseline,
        candidate,
        runtime.service.plan.tasks[0].checks,
        controller=runtime.controller,
    )
    assert receipt.valid

    with pytest.raises(fanout.ExecutionPendingError, match="provenance"):
        runtime.service.verify_repo_synthesis("write", candidate, owner=runtime.owner)
    with pytest.raises(fanout.ExecutionPendingError, match="task-bound verifier receipt"):
        runtime.service.submit("write", receipt, owner=runtime.owner)

    assert runtime.scheduler.task_phase("write") == "reconciliation-pending"


def test_handover_rejects_a_weaker_fresh_verification_of_the_completed_candidate(tmp_path):
    runtime = _repo_runtime(tmp_path)
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    exact, _ = _verified_repo_synthesis(runtime)
    candidate = exact.candidate
    runtime.service.submit("write", exact, owner=runtime.owner)
    weak_check = fanout.PlanCheckV1(
        argv=(sys.executable, "-c", "pass"),
        cwd="",
        env_allowlist=(),
        timeout=5,
        accepted_exit_codes=(0,),
        expected_artifacts=(),
    )
    weaker = fanout.verify_candidate(
        runtime.baseline,
        candidate,
        (weak_check,),
        controller=runtime.controller,
    )

    with pytest.raises(fanout.ExecutionPendingError, match="exact task checks"):
        runtime.service.handover("write", weaker, owner=runtime.owner)

    assert (runtime.repository / "changed.txt").read_text(encoding="utf-8") == "before\n"


def test_repo_write_packet_refuses_shared_caller_cwd_without_authenticated_workspace_set(tmp_path):
    repository = _repository(tmp_path)
    baseline = fanout.capture_repository_baseline(repository)
    plan = _plan(_task("write", execution_class="repo-write"))
    inputs = _inputs(plan, repo_digest=baseline.digest)
    task = plan.tasks[0]
    bundle = fanout.canonical_json({"schema_version": "fanout-seat-skill-bundle-v1", "skills": []})

    with pytest.raises(fanout.CollaborationValidationError, match="Task 13|repo-write"):
        fanout.TaskPacket(
            run_id=inputs.run_id,
            task_id=task.id,
            attempt=1,
            compiled_plan=fanout.canonical_json(plan.to_dict()),
            compiled_plan_sha256=inputs.compiled_plan_sha256,
            source_markdown=b"source",
            task=fanout.canonical_json(task.to_dict()),
            task_sha256=_digest(fanout.canonical_json(task.to_dict())),
            skill_bundle=bundle,
            skill_manifest_sha256=inputs.skill_manifests[task.id],
            dependency_artifacts=(),
            execution_class="repo-write",
            cwd=repository,
        )


def test_source_changing_amendment_is_rejected_before_durable_mutation(tmp_path):
    plan = _plan(
        _task("approve", execution_class="orchestrator-action"),
        _task("later", depends_on=("approve",)),
    )
    runtime = _runtime(tmp_path, plan)
    runtime.service.start(owner=runtime.owner)
    replacement = dataclasses.replace(
        plan,
        source=dataclasses.replace(plan.source, sha256=_digest(b"different approved source")),
    )
    new_inputs = _inputs(replacement, repo_digest=runtime.baseline.digest)
    before_revision = runtime.backend.record.revision

    with pytest.raises(fanout.ExecutionPreflightError, match="source.*amendment"):
        runtime.service.prepare_provider_profile_transition(
            replacement, new_inputs, runtime.coordinator.registry,
            dict(new_inputs.provider_profiles), owner=runtime.owner,
        )

    with pytest.raises(fanout.ExecutionPreflightError, match="source.*amendment"):
        runtime.service.submit_amendment(
            replacement, new_inputs, {}, dict(new_inputs.provider_profiles),
            expected_plan_revision=1, owner=runtime.owner,
        )

    assert runtime.scheduler.amendments == []
    assert runtime.backend.record.revision == before_revision
    assert 2 not in runtime.journal.state.amendments
    assert not (runtime.artifacts.root / "amendments").exists()


def test_submit_amendment_preflights_replacement_before_owner_acceptance(tmp_path):
    original = _plan(
        _task("approve", execution_class="orchestrator-action"),
        _task("later", depends_on=("approve",)),
    )
    runtime = _runtime(tmp_path, original)
    runtime.service.start(owner=runtime.owner)
    replacement = _plan(
        _task("approve", execution_class="orchestrator-action"),
        _task("later", depends_on=("approve",)),
    )
    replacement_data = replacement.to_dict()
    replacement_data["tasks"][1]["objective"] = "Complete the amended later work."
    replacement = fanout.FanoutPlanV1.from_dict(replacement_data)
    amended_inputs = _inputs(replacement, repo_digest=runtime.baseline.digest)
    task = replacement.tasks[1]
    preparation = _read_only_preparation(
        tmp_path / "amended", replacement, amended_inputs, task,
        runtime.artifacts, runtime=runtime,
    )
    runtime.coordinator.fail_task = "later"

    with pytest.raises(fanout.ExecutionPreflightError):
        runtime.service.submit_amendment(
            replacement,
            amended_inputs,
            {"later": preparation},
            dict(amended_inputs.provider_profiles),
            expected_plan_revision=1,
            owner=runtime.owner,
        )

    assert runtime.scheduler.amendments == []
    assert 2 not in runtime.journal.state.amendments
    runtime.coordinator.fail_task = None
    original_compare_and_set = runtime.backend.compare_and_set
    interrupted = True

    def interrupt_execution_commit(expected_revision, snapshot, *, owner):
        nonlocal interrupted
        if interrupted and snapshot.plan_sha256 == amended_inputs.compiled_plan_sha256:
            interrupted = False
            raise RuntimeError("injected execution-state interruption")
        return original_compare_and_set(expected_revision, snapshot, owner=owner)

    runtime.backend.compare_and_set = interrupt_execution_commit
    with pytest.raises(fanout.ExecutionConflictError, match="compare-and-set"):
        runtime.service.submit_amendment(
            replacement,
            amended_inputs,
            {"later": preparation},
            dict(amended_inputs.provider_profiles),
            expected_plan_revision=1,
            owner=runtime.owner,
        )

    assert runtime.scheduler.plan_revision == 2
    assert runtime.backend.record.snapshot.plan_sha256 != amended_inputs.compiled_plan_sha256
    runtime.backend.compare_and_set = original_compare_and_set
    amendment = runtime.service.submit_amendment(
        replacement,
        amended_inputs,
        {"later": preparation},
        dict(amended_inputs.provider_profiles),
        expected_plan_revision=1,
        owner=runtime.owner,
    )
    assert amendment.plan_revision == 2
    assert runtime.journal.state.amendments[2].phase == "plan-amendment-accepted"
    assert runtime.backend.record.snapshot.plan_sha256 == amended_inputs.compiled_plan_sha256


def _profile_amendment(tmp_path, *, profiled=False):
    plan = _plan(
        _task("approve", execution_class="orchestrator-action"),
        _task("later", depends_on=("approve",)),
    )
    initial_registry = (
        fanout.ProviderRegistry.default(version_probe=lambda _executor: "unused")
        if profiled else None
    )
    runtime = _runtime(tmp_path, plan, registry=initial_registry)
    runtime.service.start(owner=runtime.owner)
    profiles = dict(runtime.inputs.provider_profiles)
    if profiled:
        assert initial_registry is not None
        changed_profiles = tuple(
            dataclasses.replace(profile, cli_version="2.1.282", profile_sha256=None)
            if profile.key == ("claude", "read-only", "standard") else profile
            for profile in initial_registry._profiles.values()
        )
        registry = fanout.ProviderRegistry(
            changed_profiles, version_probe=lambda _executor: "unused",
        )
        profiles = dict(registry.profile_digests)
    else:
        profiles["claude"] = _digest("claude-profile-v2")
        registry = _Registry(profiles)
    new_inputs = dataclasses.replace(runtime.inputs, provider_profiles=profiles)
    preparation = _read_only_preparation(
        tmp_path / "profile-amendment",
        plan,
        new_inputs,
        plan.tasks[1],
        runtime.artifacts,
        registry=registry if profiled else None,
        runtime=runtime,
    )
    return runtime, plan, new_inputs, preparation, registry


def _restart_before_profile_recovery(runtime, plan):
    previous_scheduler = runtime.scheduler
    runtime.owner = fanout.OwnerCapability.from_token(runtime.owner.export_token())
    runtime.scheduler = _Scheduler(plan, runtime.inputs, runtime.artifacts)
    runtime.scheduler.plan = previous_scheduler.plan
    runtime.scheduler.inputs = previous_scheduler.inputs
    runtime.scheduler.plan_sha256 = previous_scheduler.plan_sha256
    runtime.scheduler.inputs_digest = previous_scheduler.inputs_digest
    runtime.scheduler.plan_revision = previous_scheduler.plan_revision
    runtime.scheduler.revision = previous_scheduler.revision
    runtime.scheduler.phases = dict(previous_scheduler.phases)
    runtime.scheduler.results = dict(previous_scheduler.results)
    runtime.scheduler.amendments = list(previous_scheduler.amendments)
    previous_state = runtime.journal.state
    runtime.journal = _Journal(runtime.inputs, runtime.owner)
    runtime.journal.state = copy.deepcopy(previous_state)
    previous_record = runtime.backend.record
    runtime.backend = _Backend(runtime.owner)
    runtime.backend.record = previous_record
    runtime.memory = _Memory(runtime.artifacts)
    runtime.registry = (
        fanout.ProviderRegistry(
            runtime.initial_registry_profiles,
            version_probe=lambda _executor: "unused",
        ) if getattr(runtime, "initial_registry_profiles", None) is not None
        else _Registry(runtime.inputs.provider_profiles)
    )
    baseline = getattr(runtime, "baseline", None)
    controller = getattr(runtime, "controller", None)
    coordinator = _Coordinator(
        runtime.artifacts,
        journal=runtime.journal,
        owner=runtime.owner,
        memory=runtime.memory,
        registry=runtime.registry,
        lifecycle_controller=controller,
        repository_baseline=baseline,
    )
    coordinator.barriers = dict(runtime.coordinator.barriers)
    coordinator.repo_candidate_bytes = runtime.coordinator.repo_candidate_bytes
    service = fanout.ExecutionService(
        plan=plan,
        inputs=runtime.inputs,
        preparations=runtime.preparations,
        provider_profile_digests=dict(runtime.inputs.provider_profiles),
        budget=fanout.ExecutionBudget(max_provider_turns=64),
        scheduler=runtime.scheduler,
        journal=runtime.journal,
        coordinator=coordinator,
        artifacts=runtime.artifacts,
        backend=runtime.backend,
        memory_preflight=runtime.memory.preflight,
        baseline=baseline,
        lifecycle_controller=controller,
    )
    runtime.service = None
    return service, coordinator


@pytest.mark.parametrize(
    "boundary",
    ("intent", "scheduler", "acceptance", "execution-cas", "cas-failure"),
)
@pytest.mark.parametrize("profiled", [False, True])
def test_profile_amendment_recovers_after_process_loss_at_each_durable_boundary(
    tmp_path, monkeypatch, boundary, profiled,
):
    runtime, plan, new_inputs, preparation, registry = _profile_amendment(
        tmp_path, profiled=profiled,
    )
    transition = runtime.service.prepare_provider_profile_transition(
        plan, new_inputs, registry, dict(new_inputs.provider_profiles),
        owner=runtime.owner,
    )
    if boundary in {"intent", "acceptance"}:
        original = runtime.journal.append_amendment
        phase = "plan-amendment-intent" if boundary == "intent" else "plan-amendment-accepted"

        def interrupt_journal(event_type, **kwargs):
            original(event_type, **kwargs)
            if event_type == phase:
                raise SystemExit(f"crash after {boundary}")

        monkeypatch.setattr(runtime.journal, "append_amendment", interrupt_journal)
    elif boundary == "scheduler":
        original = runtime.scheduler.accept_amendment

        def interrupt_scheduler(*args, **kwargs):
            original(*args, **kwargs)
            raise SystemExit("crash after scheduler commit")

        monkeypatch.setattr(runtime.scheduler, "accept_amendment", interrupt_scheduler)
    else:
        original = runtime.backend.compare_and_set

        def interrupt_execution_cas(expected_revision, snapshot, *, owner):
            if snapshot.plan_sha256 == new_inputs.compiled_plan_sha256:
                if boundary == "cas-failure":
                    raise RuntimeError("injected CAS failure")
                original(expected_revision, snapshot, owner=owner)
                raise SystemExit("crash after execution CAS")
            return original(expected_revision, snapshot, owner=owner)

        monkeypatch.setattr(runtime.backend, "compare_and_set", interrupt_execution_cas)

    error = fanout.ExecutionConflictError if boundary == "cas-failure" else SystemExit
    with pytest.raises(error):
        runtime.service.submit_amendment(
            plan, new_inputs, {"later": preparation},
            dict(new_inputs.provider_profiles), expected_plan_revision=1,
            provider_transition=transition, owner=runtime.owner,
        )
    assert runtime.coordinator.provider_dispatches == []
    if boundary == "intent":
        assert runtime.journal.state.amendments[2].phase == "plan-amendment-intent"
        assert runtime.scheduler.plan_revision == 1
        assert runtime.backend.record.snapshot.inputs_digest == runtime.inputs.digest
    assert len(runtime.journal.state.amendments[2].binding) == 6
    monkeypatch.undo()
    del transition
    service, coordinator = _restart_before_profile_recovery(runtime, plan)
    registry = (
        fanout.ProviderRegistry(
            tuple(registry._profiles.values()), version_probe=lambda _executor: "unused",
        ) if profiled else _Registry(new_inputs.provider_profiles)
    )
    if profiled:
        preparation = dataclasses.replace(preparation, registry=registry)
    recovered_transition = service.prepare_provider_profile_transition(
        plan, new_inputs, registry, dict(new_inputs.provider_profiles),
        owner=runtime.owner,
    )
    amendment = service.submit_amendment(
        plan, new_inputs, {"later": preparation},
        dict(new_inputs.provider_profiles), expected_plan_revision=1,
        provider_transition=recovered_transition, owner=runtime.owner,
    )
    assert amendment.plan_revision == 2
    assert runtime.journal.state.amendments[2].phase == "plan-amendment-accepted"
    assert runtime.backend.record.snapshot.inputs_digest == new_inputs.digest
    assert service.provider_profile_digests == new_inputs.provider_profiles
    assert coordinator.registry is registry
    assert coordinator.provider_dispatches == []


def test_profile_recovery_handles_removed_old_task_after_scheduler_commit(
    tmp_path, monkeypatch,
):
    original = _plan(
        _task("approve", execution_class="orchestrator-action"),
        _task("obsolete", depends_on=("approve",)),
        _task("later", depends_on=("approve",)),
    )
    runtime = _runtime(tmp_path, original)
    runtime.service.start(owner=runtime.owner)
    data = original.to_dict()
    removed_step = data["tasks"][1]["source_step_ids"][0]
    data["tasks"].pop(1)
    data["tasks"][1]["source_step_ids"].append(removed_step)
    replacement = fanout.FanoutPlanV1.from_dict(data)
    profiles = dict(runtime.inputs.provider_profiles)
    profiles["claude"] = _digest("claude-profile-v2")
    new_inputs = dataclasses.replace(
        _inputs(replacement, repo_digest=runtime.baseline.digest), provider_profiles=profiles,
    )
    preparation = _read_only_preparation(
        tmp_path / "removed-amendment", replacement, new_inputs,
        replacement.tasks[1], runtime.artifacts,
        runtime=runtime,
    )
    registry = _Registry(profiles)
    transition = runtime.service.prepare_provider_profile_transition(
        replacement, new_inputs, registry, profiles, owner=runtime.owner,
    )
    original_accept = runtime.scheduler.accept_amendment

    def interrupt_scheduler(*args, **kwargs):
        accepted = original_accept(*args, **kwargs)
        assert accepted.plan_revision == 2
        raise SystemExit("crash after scheduler removed obsolete")

    monkeypatch.setattr(runtime.scheduler, "accept_amendment", interrupt_scheduler)
    with pytest.raises(SystemExit):
        runtime.service.submit_amendment(
            replacement, new_inputs, {"later": preparation}, profiles,
            expected_plan_revision=1, provider_transition=transition,
            owner=runtime.owner,
        )
    monkeypatch.undo()
    del transition
    service, coordinator = _restart_before_profile_recovery(runtime, original)
    registry = _Registry(new_inputs.provider_profiles)
    recovered = service.prepare_provider_profile_transition(
        replacement, new_inputs, registry, profiles, owner=runtime.owner,
    )
    amendment = service.submit_amendment(
        replacement, new_inputs, {"later": preparation}, profiles,
        expected_plan_revision=1, provider_transition=recovered,
        owner=runtime.owner,
    )
    assert amendment.affected_task_ids == ("later", "obsolete")
    assert tuple(task.task_id for task in runtime.backend.record.snapshot.tasks) == (
        "approve", "later",
    )
    assert coordinator.provider_dispatches == []


def _repo_profile_crash_with_removed_task(tmp_path, monkeypatch, boundary):
    original = _plan(
        _task("write", execution_class="repo-write", owned_paths=("changed.txt",)),
        _task("approve", execution_class="orchestrator-action"),
        _task("obsolete", execution_class="orchestrator-action", depends_on=("approve",)),
        _task("later", execution_class="orchestrator-action", depends_on=("approve",)),
    )
    runtime = _repo_runtime(tmp_path, original)
    runtime.coordinator.repo_candidate_bytes = {
        "claude": b"after\n", "codex": b"after\n",
    }
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    assert runtime.scheduler.task_phase("write") == "reconciliation-pending"

    data = original.to_dict()
    removed_step = data["tasks"][2]["source_step_ids"][0]
    data["tasks"].pop(2)
    data["tasks"][2]["source_step_ids"].append(removed_step)
    replacement = fanout.FanoutPlanV1.from_dict(data)
    profiles = dict(runtime.inputs.provider_profiles)
    profiles["agy"] = _digest("agy-profile-v1")
    new_inputs = dataclasses.replace(
        _inputs(replacement, repo_digest=runtime.baseline.digest),
        provider_profiles=profiles,
    )
    transition = runtime.service.prepare_provider_profile_transition(
        replacement, new_inputs, _Registry(profiles), profiles,
        owner=runtime.owner,
    )

    if boundary in {"intent", "acceptance"}:
        original_append = runtime.journal.append_amendment
        phase = "plan-amendment-intent" if boundary == "intent" else "plan-amendment-accepted"

        def interrupt_journal(event_type, **kwargs):
            original_append(event_type, **kwargs)
            if event_type == phase:
                raise SystemExit(f"crash after {boundary}")

        monkeypatch.setattr(runtime.journal, "append_amendment", interrupt_journal)
    elif boundary == "scheduler":
        original_accept = runtime.scheduler.accept_amendment

        def interrupt_scheduler(*args, **kwargs):
            original_accept(*args, **kwargs)
            raise SystemExit("crash after scheduler commit")

        monkeypatch.setattr(runtime.scheduler, "accept_amendment", interrupt_scheduler)
    else:
        original_cas = runtime.backend.compare_and_set

        def interrupt_execution_cas(expected_revision, snapshot, *, owner):
            if snapshot.plan_sha256 == new_inputs.compiled_plan_sha256:
                if boundary == "cas-failure":
                    raise RuntimeError("injected CAS failure")
                original_cas(expected_revision, snapshot, owner=owner)
                raise SystemExit("crash after execution CAS")
            return original_cas(expected_revision, snapshot, owner=owner)

        monkeypatch.setattr(runtime.backend, "compare_and_set", interrupt_execution_cas)

    error = fanout.ExecutionConflictError if boundary == "cas-failure" else SystemExit
    with pytest.raises(error):
        runtime.service.submit_amendment(
            replacement, new_inputs, {}, profiles,
            expected_plan_revision=1, provider_transition=transition,
            owner=runtime.owner,
        )
    monkeypatch.undo()
    return runtime, original, replacement, new_inputs


@pytest.mark.parametrize(
    "boundary", ("intent", "scheduler", "acceptance", "cas-failure", "execution-cas"),
)
def test_pending_profile_recovery_status_survives_removed_task_after_restart(
    tmp_path, monkeypatch, boundary,
):
    runtime, original, replacement, _new_inputs = _repo_profile_crash_with_removed_task(
        tmp_path, monkeypatch, boundary,
    )
    service, coordinator = _restart_before_profile_recovery(runtime, original)
    before = (
        runtime.scheduler.revision,
        dict(runtime.scheduler.phases),
        runtime.backend.record,
        copy.deepcopy(runtime.journal.state),
    )

    status = service.status()

    current = original if boundary == "intent" else replacement
    assert status.plan_revision == (1 if boundary == "intent" else 2)
    assert tuple(task.task_id for task in status.tasks) == tuple(
        task.id for task in current.tasks
    )
    assert status.tasks[0].phase == "reconciliation-pending"
    assert status.tasks[0].completed_rounds == 1
    assert (
        runtime.scheduler.revision,
        runtime.scheduler.phases,
        runtime.backend.record,
        runtime.journal.state,
    ) == before
    assert coordinator.provider_dispatches == []


@pytest.mark.parametrize(
    "boundary", ("intent", "scheduler", "acceptance", "cas-failure", "execution-cas"),
)
def test_pending_profile_recovery_collects_unchanged_work_without_mutation(
    tmp_path, monkeypatch, boundary,
):
    runtime, original, _replacement, _new_inputs = _repo_profile_crash_with_removed_task(
        tmp_path, monkeypatch, boundary,
    )
    service, coordinator = _restart_before_profile_recovery(runtime, original)
    before = (
        runtime.scheduler.revision,
        dict(runtime.scheduler.phases),
        runtime.backend.record,
        copy.deepcopy(runtime.journal.state),
    )

    collected = service.collect("write", owner=runtime.owner)

    assert {item.seat_id for item in collected} == {"claude", "codex"}
    assert all(
        [(entry.path, entry.data) for entry in item.candidate.entries]
        == [("changed.txt", b"after\n")]
        for item in collected
    )
    assert (
        runtime.scheduler.revision,
        runtime.scheduler.phases,
        runtime.backend.record,
        runtime.journal.state,
    ) == before
    assert (runtime.repository / "changed.txt").read_bytes() == b"before\n"
    assert coordinator.provider_dispatches == []


@pytest.mark.parametrize(
    "boundary", ("scheduler", "acceptance", "cas-failure", "execution-cas"),
)
def test_pending_profile_collection_refuses_removed_work(
    tmp_path, monkeypatch, boundary,
):
    runtime, original, _replacement, _new_inputs = _repo_profile_crash_with_removed_task(
        tmp_path, monkeypatch, boundary,
    )
    service, coordinator = _restart_before_profile_recovery(runtime, original)
    before = (
        runtime.scheduler.revision,
        dict(runtime.scheduler.phases),
        runtime.backend.record,
        copy.deepcopy(runtime.journal.state),
    )

    with pytest.raises(fanout.ExecutionPendingError):
        service.collect("obsolete", owner=runtime.owner)

    assert (
        runtime.scheduler.revision,
        runtime.scheduler.phases,
        runtime.backend.record,
        runtime.journal.state,
    ) == before
    assert coordinator.provider_dispatches == []


def test_pending_profile_inspection_accepts_derived_blocked_dependency(
    tmp_path, monkeypatch,
):
    original = _plan(
        _task("write", execution_class="repo-write", owned_paths=("changed.txt",)),
        _task("approve", execution_class="orchestrator-action"),
        _task("hold", execution_class="orchestrator-action"),
        _task("obsolete", execution_class="orchestrator-action", depends_on=("hold",)),
    )
    runtime = _repo_runtime(tmp_path, original)
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    runtime.scheduler.fail_task("approve", "failed", owner=runtime.owner)
    for workspace in runtime.workspaces.values():
        (workspace.root / "changed.txt").write_text("after\n", encoding="utf-8")

    data = original.to_dict()
    data["tasks"][3].update({
        "id": "fresh",
        "title": "fresh",
        "objective": "Complete fresh.",
        "depends_on": ["approve"],
        "acceptance": ["fresh is complete."],
    })
    replacement = fanout.FanoutPlanV1.from_dict(data)
    profiles = dict(runtime.inputs.provider_profiles)
    profiles["agy"] = _digest("agy-profile-v1")
    new_inputs = dataclasses.replace(
        _inputs(replacement, repo_digest=runtime.baseline.digest),
        provider_profiles=profiles,
    )
    transition = runtime.service.prepare_provider_profile_transition(
        replacement, new_inputs, _Registry(profiles), profiles,
        owner=runtime.owner,
    )
    original_accept = runtime.scheduler.accept_amendment

    def interrupt_scheduler(*args, **kwargs):
        original_accept(*args, **kwargs)
        raise SystemExit("crash after scheduler commit")

    monkeypatch.setattr(runtime.scheduler, "accept_amendment", interrupt_scheduler)
    with pytest.raises(SystemExit):
        runtime.service.submit_amendment(
            replacement, new_inputs, {}, profiles,
            expected_plan_revision=1, provider_transition=transition,
            owner=runtime.owner,
        )
    monkeypatch.undo()

    service, coordinator = _restart_before_profile_recovery(runtime, original)
    task_phase = runtime.scheduler.task_phase

    def derived_phase(task_id):
        phase = task_phase(task_id)
        if task_id == "fresh" and phase == "unscheduled":
            return "blocked-dependency"
        return phase

    monkeypatch.setattr(runtime.scheduler, "task_phase", derived_phase)
    assert runtime.scheduler.phases["fresh"] == "unscheduled"
    assert runtime.scheduler.phases["approve"] == "failed"
    assert runtime.scheduler.task_phase("fresh") == "blocked-dependency"
    assert runtime.journal.state.amendments[2].phase == "plan-amendment-intent"
    before = (
        runtime.scheduler.revision,
        dict(runtime.scheduler.phases),
        runtime.backend.record,
        copy.deepcopy(runtime.journal.state),
    )

    status = service.status()
    collected = service.collect("write", owner=runtime.owner)

    assert status.plan_revision == 2
    assert {task.task_id: task.phase for task in status.tasks}["fresh"] == "blocked-dependency"
    assert {item.seat_id for item in collected} == {"claude", "codex"}
    assert (
        runtime.scheduler.revision,
        runtime.scheduler.phases,
        runtime.backend.record,
        runtime.journal.state,
    ) == before
    assert coordinator.provider_dispatches == []


@pytest.mark.parametrize("drift", ("journal-binding", "affected-work", "foreign-owner"))
def test_pending_profile_collection_refuses_ambiguous_or_affected_work(
    tmp_path, monkeypatch, drift,
):
    runtime, original, replacement, new_inputs = _repo_profile_crash_with_removed_task(
        tmp_path, monkeypatch, "scheduler",
    )
    service, coordinator = _restart_before_profile_recovery(runtime, original)
    owner = runtime.owner
    error = fanout.ExecutionConflictError
    if drift == "journal-binding":
        binding = runtime.journal.state.amendments[2].binding
        runtime.journal.state.amendments[2].binding = (
            *binding[:5], _digest("unbound-new-profile"),
        )
    elif drift == "affected-work":
        data = replacement.to_dict()
        data["tasks"][0]["objective"] = "Changed live write task."
        changed = fanout.FanoutPlanV1.from_dict(data)
        changed_inputs = dataclasses.replace(
            _inputs(changed, repo_digest=runtime.baseline.digest),
            provider_profiles=dict(new_inputs.provider_profiles),
        )
        runtime.scheduler.plan = changed
        runtime.scheduler.plan_sha256 = changed_inputs.compiled_plan_sha256
        runtime.scheduler.inputs = changed_inputs
        runtime.scheduler.inputs_digest = changed_inputs.digest
        binding = runtime.journal.state.amendments[2].binding
        runtime.journal.state.amendments[2].binding = (
            binding[0], binding[1], changed_inputs.compiled_plan_sha256,
            changed_inputs.digest, binding[4], binding[5],
        )
    else:
        owner = fanout.OwnerCapability.from_token("x" * 43)
        error = fanout.RunAuthorizationError
    before = (
        runtime.scheduler.revision,
        dict(runtime.scheduler.phases),
        runtime.backend.record,
        copy.deepcopy(runtime.journal.state),
    )

    with pytest.raises(error):
        service.collect("write", owner=owner)

    assert (
        runtime.scheduler.revision,
        runtime.scheduler.phases,
        runtime.backend.record,
        runtime.journal.state,
    ) == before
    assert coordinator.provider_dispatches == []


def test_pending_profile_collection_requires_old_intent_binding(
    tmp_path, monkeypatch,
):
    runtime, original, _replacement, _new_inputs = _repo_profile_crash_with_removed_task(
        tmp_path, monkeypatch, "intent",
    )
    service, coordinator = _restart_before_profile_recovery(runtime, original)
    binding = runtime.journal.state.amendments[2].binding
    runtime.journal.state.amendments[2].binding = (
        *binding[:4], _digest("unbound-old-profile"), binding[5],
    )
    before = (
        runtime.scheduler.revision,
        dict(runtime.scheduler.phases),
        runtime.backend.record,
        copy.deepcopy(runtime.journal.state),
    )

    with pytest.raises(fanout.ExecutionConflictError):
        service.collect("write", owner=runtime.owner)

    assert (
        runtime.scheduler.revision,
        runtime.scheduler.phases,
        runtime.backend.record,
        runtime.journal.state,
    ) == before
    assert coordinator.provider_dispatches == []


@pytest.mark.parametrize("command", ("status", "collect"))
def test_pending_profile_inspection_rejects_acceptance_before_scheduler_commit(
    tmp_path, monkeypatch, command,
):
    runtime, original, _replacement, _new_inputs = _repo_profile_crash_with_removed_task(
        tmp_path, monkeypatch, "intent",
    )
    service, coordinator = _restart_before_profile_recovery(runtime, original)
    runtime.journal.state.amendments[2].phase = "plan-amendment-accepted"
    before = (
        runtime.scheduler.revision,
        dict(runtime.scheduler.phases),
        runtime.backend.record,
        copy.deepcopy(runtime.journal.state),
    )

    with pytest.raises(fanout.ExecutionConflictError):
        if command == "status":
            service.status()
        else:
            service.collect("write", owner=runtime.owner)

    assert (
        runtime.scheduler.revision,
        runtime.scheduler.phases,
        runtime.backend.record,
        runtime.journal.state,
    ) == before
    assert coordinator.provider_dispatches == []


def _accepted_ahead_restart(tmp_path, command, *, rollback_scheduler=True):
    repo_command = command in {
        "submit", "handover", "recover_handover", "gc", "claim_gc_path",
    }
    if repo_command:
        check = fanout.PlanCheckV1(
            argv=(
                sys.executable, "-c",
                "from pathlib import Path; assert Path('changed.txt').read_text() == 'after\\n'",
            ),
            cwd="",
            env_allowlist=(),
            timeout=5,
            accepted_exit_codes=(0,),
            expected_artifacts=(),
        )
        original = _plan(
            _task(
                "write", execution_class="repo-write", checks=(check,),
                owned_paths=("changed.txt",),
            ),
            _task("approve", execution_class="orchestrator-action"),
            _task("later", execution_class="orchestrator-action", depends_on=("approve",)),
        )
        runtime = _repo_runtime(tmp_path, original)
    else:
        original = _plan(
            _task("ready"),
            _task("approve", execution_class="orchestrator-action"),
            _task("later", execution_class="orchestrator-action", depends_on=("approve",)),
        )
        runtime = _runtime(tmp_path, original)
    runtime.service.start(owner=runtime.owner)

    receipt = None
    transaction = None
    if command in {"submit", "handover", "recover_handover"}:
        runtime.service.resume(owner=runtime.owner)
        receipt, _ = _verified_repo_synthesis(runtime)
        if command in {"handover", "recover_handover"}:
            runtime.service.submit("write", receipt, owner=runtime.owner)
        if command == "recover_handover":
            runtime.journal.fail_event_once = "handover-complete"
            with pytest.raises(RuntimeError, match="injected handover-complete"):
                runtime.service.handover("write", receipt, owner=runtime.owner)
            transaction = next((runtime.controller.root / "transactions").iterdir())

    scratch = None
    claim = None
    if command in {"gc", "claim_gc_path"}:
        scratch = runtime.controller.root / "scratch"
        scratch.mkdir()
        (scratch / "owned.txt").write_text("owned\n", encoding="utf-8")
        if command == "gc":
            claim = runtime.service.claim_gc_path("scratch", owner=runtime.owner)
    artifact = None
    if command == "action_complete":
        artifact = runtime.artifacts.write_bytes("actions/approve.json", b"{}\n")

    old_revision = runtime.scheduler.revision
    old_phases = dict(runtime.scheduler.phases)
    old_results = dict(runtime.scheduler.results)
    old_amendments = list(runtime.scheduler.amendments)
    old_record = runtime.backend.record
    data = original.to_dict()
    data["tasks"][-1]["objective"] = "Complete the amended owner action."
    replacement = fanout.FanoutPlanV1.from_dict(data)
    new_inputs = _inputs(
        replacement,
        repo_digest=runtime.baseline.digest,
    )
    amendment = runtime.service.submit_amendment(
        replacement, new_inputs, {}, dict(runtime.inputs.provider_profiles),
        expected_plan_revision=1, owner=runtime.owner,
    )
    assert amendment.plan_revision == 2
    assert runtime.journal.state.amendments[2].phase == "plan-amendment-accepted"
    assert runtime.scheduler.plan_revision == 2
    assert runtime.backend.record.snapshot.inputs_digest == new_inputs.digest
    completed_record = runtime.backend.record

    if rollback_scheduler:
        runtime.scheduler = _Scheduler(original, runtime.inputs, runtime.artifacts)
        runtime.scheduler.revision = old_revision
        runtime.scheduler.phases = old_phases
        runtime.scheduler.results = old_results
        runtime.scheduler.amendments = old_amendments
    runtime.backend.record = old_record
    service, coordinator = _restart_before_profile_recovery(runtime, original)
    assert runtime.scheduler.plan_revision == (1 if rollback_scheduler else 2)
    assert runtime.backend.record is old_record
    return SimpleNamespace(
        runtime=runtime, service=service, coordinator=coordinator,
        replacement=replacement, new_inputs=new_inputs, receipt=receipt,
        transaction=transaction, scratch=scratch, claim=claim, artifact=artifact,
        completed_record=completed_record,
    )


def _accepted_ahead_state(case):
    runtime = case.runtime
    return (
        runtime.scheduler.plan,
        runtime.scheduler.inputs,
        runtime.scheduler.plan_revision,
        runtime.scheduler.revision,
        runtime.scheduler.plan_sha256,
        runtime.scheduler.inputs_digest,
        dict(runtime.scheduler.phases),
        dict(runtime.scheduler.results),
        tuple(runtime.scheduler.amendments),
        runtime.backend.record,
        copy.deepcopy(runtime.journal.state),
        None if case.scratch is None else (
            case.scratch.exists(),
            (case.scratch / "owned.txt").read_bytes()
            if case.scratch.exists() else None,
        ),
        (runtime.repository / "changed.txt").read_bytes()
        if hasattr(runtime, "repository") else None,
    )


@pytest.mark.parametrize(
    "command",
    (
        "start", "resume", "submit", "submit_amendment", "action_complete",
        "handover", "recover_handover", "claim_gc_path", "gc",
    ),
)
def test_accepted_amendment_ahead_of_rolled_back_stores_blocks_all_mutation(
    tmp_path, command,
):
    case = _accepted_ahead_restart(tmp_path, command)
    runtime = case.runtime
    before = _accepted_ahead_state(case)
    actions = {
        "start": lambda: case.service.start(owner=runtime.owner),
        "resume": lambda: case.service.resume(owner=runtime.owner),
        "submit": lambda: case.service.submit(
            "write", case.receipt, owner=runtime.owner,
        ),
        "submit_amendment": lambda: case.service.submit_amendment(
            case.replacement, case.new_inputs, {},
            dict(runtime.inputs.provider_profiles),
            expected_plan_revision=1, owner=runtime.owner,
        ),
        "action_complete": lambda: case.service.action_complete(
            "approve", case.artifact, owner=runtime.owner,
        ),
        "handover": lambda: case.service.handover(
            "write", case.receipt, owner=runtime.owner,
        ),
        "recover_handover": lambda: case.service.recover_handover(
            "write", case.transaction, case.receipt, owner=runtime.owner,
        ),
        "claim_gc_path": lambda: case.service.claim_gc_path(
            "scratch", owner=runtime.owner,
        ),
        "gc": lambda: case.service.gc((case.claim,), owner=runtime.owner),
    }

    with pytest.raises(fanout.ExecutionConflictError, match="accepted amendment"):
        actions[command]()

    assert case.coordinator.provider_dispatches == []
    assert _accepted_ahead_state(case) == before


@pytest.mark.parametrize("scheduler_revision", (2, 3))
def test_accepted_amendment_rejects_old_binding_at_current_or_skipped_revision(
    tmp_path, scheduler_revision,
):
    case = _accepted_ahead_restart(tmp_path, "resume")
    case.runtime.scheduler.plan_revision = scheduler_revision
    before = _accepted_ahead_state(case)

    with pytest.raises(fanout.ExecutionConflictError, match="amendment|revision"):
        case.service.resume(owner=case.runtime.owner)

    assert case.coordinator.provider_dispatches == []
    assert _accepted_ahead_state(case) == before


def test_accepted_scheduler_ahead_can_replay_exact_revision_without_spend(
    tmp_path,
):
    case = _accepted_ahead_restart(
        tmp_path, "resume", rollback_scheduler=False,
    )
    runtime = case.runtime
    assert case.service.status().plan_revision == 2

    amendment = case.service.submit_amendment(
        case.replacement, case.new_inputs, {}, dict(runtime.inputs.provider_profiles),
        expected_plan_revision=1, owner=runtime.owner,
    )

    assert amendment.plan_revision == 2
    assert runtime.scheduler.plan_revision == 2
    assert runtime.backend.record.snapshot.inputs_digest == case.new_inputs.digest
    assert case.coordinator.provider_dispatches == []
    case.service.resume(owner=runtime.owner)
    assert case.coordinator.provider_dispatches == [("ready", 1)]


def _append_next_amendment(case, *, accepted):
    runtime = case.runtime
    next_data = case.replacement.to_dict()
    next_data["tasks"][-1]["objective"] = "Complete the later owner action again."
    next_plan = fanout.FanoutPlanV1.from_dict(next_data)
    next_inputs = _inputs(next_plan, repo_digest=runtime.baseline.digest)
    profiles_sha256 = runtime.journal.state.amendments[2].binding[5]
    binding = {
        "revision": 3,
        "old_plan_sha256": case.new_inputs.compiled_plan_sha256,
        "old_inputs_digest": case.new_inputs.digest,
        "new_plan_sha256": next_inputs.compiled_plan_sha256,
        "new_inputs_digest": next_inputs.digest,
        "old_profiles_sha256": profiles_sha256,
        "new_profiles_sha256": profiles_sha256,
        "owner": runtime.owner,
    }
    documents = (
        ("plans", next_inputs.compiled_plan_sha256,
         fanout.canonical_json(next_plan.to_dict())),
        ("inputs", next_inputs.digest,
         fanout.canonical_json(next_inputs.to_dict())),
        ("profiles", profiles_sha256, fanout.canonical_json({
            "profiles": dict(sorted(next_inputs.provider_profiles.items())),
            "schema_version": "fanout-provider-profiles-v1",
        })),
    )
    for kind, digest, data in documents:
        assert _digest(data) == digest
        path = f"amendments/{kind}/{digest}.json"
        try:
            runtime.artifacts.write_bytes(path, data)
        except fanout.ArtifactExistsError:
            assert runtime.artifacts.read_bytes(
                fanout.ArtifactRef(path, digest, len(data)),
            ) == data
    runtime.journal.append_amendment("plan-amendment-intent", **binding)
    if accepted:
        runtime.journal.append_amendment("plan-amendment-accepted", **binding)
    return next_plan, next_inputs


def test_future_accepted_revision_blocks_replay_of_earlier_accepted_revision(tmp_path):
    case = _accepted_ahead_restart(
        tmp_path, "resume", rollback_scheduler=False,
    )
    runtime = case.runtime
    _append_next_amendment(case, accepted=True)
    before = _accepted_ahead_state(case)

    with pytest.raises(fanout.ExecutionConflictError, match="accepted amendment"):
        case.service.submit_amendment(
            case.replacement, case.new_inputs, {},
            dict(runtime.inputs.provider_profiles),
            expected_plan_revision=1, owner=runtime.owner,
        )

    assert case.coordinator.provider_dispatches == []
    assert _accepted_ahead_state(case) == before


@pytest.mark.parametrize(
    ("command", "execution_binding"),
    (
        ("status", "old"),
        ("collect", "old"),
        ("submit_amendment", "old"),
        ("submit_amendment", "new"),
    ),
)
def test_later_intent_refuses_earlier_unresolved_execution_or_replay(
    tmp_path, command, execution_binding,
):
    case = _accepted_ahead_restart(
        tmp_path, "submit" if command == "collect" else "resume",
        rollback_scheduler=False,
    )
    _append_next_amendment(case, accepted=False)
    if execution_binding == "new":
        case.runtime.backend.record = case.completed_record
    before = _accepted_ahead_state(case)

    with pytest.raises(fanout.ExecutionConflictError, match="amendment|revision"):
        if command == "status":
            case.service.status()
        elif command == "collect":
            case.service.collect("write", owner=case.runtime.owner)
        else:
            case.service.submit_amendment(
                case.replacement, case.new_inputs, {},
                dict(case.runtime.inputs.provider_profiles),
                expected_plan_revision=1, owner=case.runtime.owner,
            )

    assert case.coordinator.provider_dispatches == []
    assert _accepted_ahead_state(case) == before


def test_latest_intent_replays_from_settled_prior_revision(tmp_path):
    case = _accepted_ahead_restart(
        tmp_path, "resume", rollback_scheduler=False,
    )
    runtime = case.runtime
    case.service.submit_amendment(
        case.replacement, case.new_inputs, {},
        dict(runtime.inputs.provider_profiles),
        expected_plan_revision=1, owner=runtime.owner,
    )
    next_plan, next_inputs = _append_next_amendment(case, accepted=False)

    amendment = case.service.submit_amendment(
        next_plan, next_inputs, {},
        dict(next_inputs.provider_profiles),
        expected_plan_revision=2, owner=runtime.owner,
    )

    assert amendment.plan_revision == 3
    assert runtime.scheduler.plan_revision == 3
    assert runtime.journal.state.amendments[3].phase == "plan-amendment-accepted"
    assert runtime.backend.record.snapshot.inputs_digest == next_inputs.digest
    assert case.coordinator.provider_dispatches == []


def test_start_refuses_missing_execution_after_accepted_amendment(tmp_path):
    original = _plan(
        _task("approve", execution_class="orchestrator-action"),
        _task("later", execution_class="orchestrator-action", depends_on=("approve",)),
    )
    runtime = _runtime(tmp_path, original)
    runtime.service.start(owner=runtime.owner)
    data = original.to_dict()
    data["tasks"][1]["objective"] = "Complete the amended owner action."
    replacement = fanout.FanoutPlanV1.from_dict(data)
    new_inputs = _inputs(replacement, repo_digest=runtime.baseline.digest)
    runtime.service.submit_amendment(
        replacement, new_inputs, {}, dict(runtime.inputs.provider_profiles),
        expected_plan_revision=1, owner=runtime.owner,
    )
    service = fanout.ExecutionService(
        plan=replacement,
        inputs=new_inputs,
        preparations={},
        provider_profile_digests=dict(new_inputs.provider_profiles),
        budget=runtime.service.budget,
        scheduler=runtime.scheduler,
        journal=runtime.journal,
        coordinator=runtime.coordinator,
        artifacts=runtime.artifacts,
        backend=runtime.backend,
        memory_preflight=runtime.memory.preflight,
        baseline=runtime.baseline,
        lifecycle_controller=runtime.controller,
    )
    runtime.backend.record = None
    before = (
        runtime.scheduler.revision,
        dict(runtime.scheduler.phases),
        copy.deepcopy(runtime.journal.state),
    )

    with pytest.raises(fanout.ExecutionConflictError, match="execution record is missing"):
        service.start(owner=runtime.owner)

    assert runtime.backend.record is None
    assert (
        runtime.scheduler.revision,
        runtime.scheduler.phases,
        runtime.journal.state,
    ) == before
    assert runtime.coordinator.provider_dispatches == []


def test_start_refuses_prior_task_decision_when_execution_record_is_missing(tmp_path):
    plan = _plan(_task("approve", execution_class="orchestrator-action"))
    runtime = _runtime(tmp_path, plan)
    runtime.journal.append("blocked-action", task_id="approve", owner=runtime.owner)
    before = copy.deepcopy(runtime.journal.state)

    with pytest.raises(fanout.ExecutionConflictError, match="execution record is missing"):
        runtime.service.start(owner=runtime.owner)

    assert runtime.backend.record is None
    assert runtime.scheduler.revision == 1
    assert runtime.scheduler.phases == {"approve": "unscheduled"}
    assert runtime.journal.state == before
    assert runtime.coordinator.provider_dispatches == []


def test_status_does_not_trust_cached_execution_after_backend_disappears(tmp_path):
    plan = _plan(_task("approve", execution_class="orchestrator-action"))
    runtime = _runtime(tmp_path, plan)
    runtime.service.start(owner=runtime.owner)
    runtime.backend.record = None
    before = (
        runtime.scheduler.revision,
        dict(runtime.scheduler.phases),
        copy.deepcopy(runtime.journal.state),
    )

    with pytest.raises(fanout.ExecutionConflictError, match="execution record|missing"):
        runtime.service.status()

    assert runtime.backend.record is None
    assert (
        runtime.scheduler.revision,
        runtime.scheduler.phases,
        runtime.journal.state,
    ) == before
    assert runtime.coordinator.provider_dispatches == []


def _round_trip_amendment_with_rolled_back_execution(tmp_path, *, restarted):
    original = _plan(
        _task("ready"),
        _task("approve", execution_class="orchestrator-action"),
        _task("later", execution_class="orchestrator-action", depends_on=("approve",)),
    )
    runtime = _runtime(tmp_path, original)
    runtime.service.start(owner=runtime.owner)
    initial_record = runtime.backend.record
    data = original.to_dict()
    data["tasks"][-1]["objective"] = "Complete the amended owner action."
    amended = fanout.FanoutPlanV1.from_dict(data)
    amended_inputs = _inputs(amended, repo_digest=runtime.baseline.digest)
    runtime.service.submit_amendment(
        amended, amended_inputs, {}, dict(runtime.inputs.provider_profiles),
        expected_plan_revision=1, owner=runtime.owner,
    )
    runtime.service.submit_amendment(
        original, runtime.inputs, {}, dict(runtime.inputs.provider_profiles),
        expected_plan_revision=2, owner=runtime.owner,
    )
    assert runtime.scheduler.plan_revision == 3
    assert runtime.scheduler.plan == original
    assert runtime.journal.state.amendments[3].phase == "plan-amendment-accepted"
    assert runtime.backend.record.snapshot.inputs_digest == runtime.inputs.digest
    assert runtime.backend.record.revision > initial_record.revision
    completed_record = runtime.backend.record
    runtime.backend.record = initial_record
    if restarted:
        service, coordinator = _restart_before_profile_recovery(runtime, original)
    else:
        service, coordinator = runtime.service, runtime.coordinator
    return SimpleNamespace(
        runtime=runtime, service=service, coordinator=coordinator,
        original=original, completed_record=completed_record, scratch=None,
    )


@pytest.mark.parametrize("restarted", (False, True))
@pytest.mark.parametrize(
    "command", ("status", "resume", "action_complete", "submit_amendment"),
)
def test_round_trip_amendment_rejects_same_digest_execution_rollback(
    tmp_path, restarted, command,
):
    case = _round_trip_amendment_with_rolled_back_execution(
        tmp_path, restarted=restarted,
    )
    runtime = case.runtime
    artifact = runtime.artifacts.write_bytes("actions/approve.json", b"{}\n")
    data = case.original.to_dict()
    data["tasks"][-1]["objective"] = "Complete one more owner action."
    next_plan = fanout.FanoutPlanV1.from_dict(data)
    next_inputs = _inputs(next_plan, repo_digest=runtime.baseline.digest)
    before = _accepted_ahead_state(case)

    with pytest.raises(fanout.ExecutionConflictError, match="execution.*revision"):
        if command == "status":
            case.service.status()
        elif command == "resume":
            case.service.resume(owner=runtime.owner)
        elif command == "action_complete":
            case.service.action_complete("approve", artifact, owner=runtime.owner)
        else:
            case.service.submit_amendment(
                next_plan, next_inputs, {}, dict(next_inputs.provider_profiles),
                expected_plan_revision=3, owner=runtime.owner,
            )

    assert case.coordinator.provider_dispatches == []
    assert _accepted_ahead_state(case) == before


def test_round_trip_amendment_accepts_current_execution_revision(tmp_path):
    case = _round_trip_amendment_with_rolled_back_execution(
        tmp_path, restarted=True,
    )
    case.runtime.backend.record = case.completed_record

    assert case.service.status().plan_revision == 3
    case.service.resume(owner=case.runtime.owner)
    assert case.coordinator.provider_dispatches == [("ready", 1)]


def test_execution_snapshot_durably_validates_global_plan_revision(tmp_path):
    runtime = _runtime(
        tmp_path, _plan(_task("approve", execution_class="orchestrator-action")),
    )
    runtime.service.start(owner=runtime.owner)
    snapshot = runtime.backend.record.snapshot

    assert snapshot.to_dict()["plan_revision"] == 1
    with pytest.raises(fanout.ExecutionValidationError, match="plan revision"):
        dataclasses.replace(snapshot, plan_revision=0)
    with pytest.raises(fanout.ExecutionValidationError, match="schema"):
        dataclasses.replace(snapshot, schema_version="fanout-execution-v1")


@pytest.mark.parametrize("drift", ("journal-phase", "unchanged-task-state"))
def test_pending_profile_execution_snapshot_requires_exact_recovery_evidence(
    tmp_path, monkeypatch, drift,
):
    runtime, original, _replacement, _new_inputs = _repo_profile_crash_with_removed_task(
        tmp_path, monkeypatch, "execution-cas",
    )
    service, coordinator = _restart_before_profile_recovery(runtime, original)
    if drift == "journal-phase":
        runtime.journal.state.amendments[2].phase = "plan-amendment-intent"
    else:
        record = runtime.backend.record
        states = list(record.snapshot.tasks)
        states[0] = dataclasses.replace(
            states[0], preparation_sha256=_digest("wrong-unchanged-preparation"),
        )
        runtime.backend.record = fanout.ExecutionRecord(
            record.revision,
            dataclasses.replace(record.snapshot, tasks=tuple(states)),
        )
    before = (
        runtime.scheduler.revision,
        dict(runtime.scheduler.phases),
        runtime.backend.record,
        copy.deepcopy(runtime.journal.state),
    )

    with pytest.raises(fanout.ExecutionConflictError):
        service.status()
    with pytest.raises(fanout.ExecutionConflictError):
        service.collect("write", owner=runtime.owner)

    assert (
        runtime.scheduler.revision,
        runtime.scheduler.phases,
        runtime.backend.record,
        runtime.journal.state,
    ) == before
    assert coordinator.provider_dispatches == []


def test_profile_intent_only_restart_blocks_other_provider_spend_until_resolution(
    tmp_path, monkeypatch,
):
    original = _plan(
        _task("ready"),
        _task("approve", execution_class="orchestrator-action"),
        _task("later", depends_on=("approve",)),
    )
    runtime = _runtime(tmp_path, original)
    runtime.service.start(owner=runtime.owner)
    data = original.to_dict()
    data["tasks"][2]["objective"] = "Complete revised later work."
    replacement = fanout.FanoutPlanV1.from_dict(data)
    profiles = dict(runtime.inputs.provider_profiles)
    profiles["agy"] = _digest("agy-profile-v1")
    new_inputs = dataclasses.replace(
        _inputs(replacement, repo_digest=runtime.baseline.digest), provider_profiles=profiles,
    )
    preparation = _read_only_preparation(
        tmp_path / "intent-restart", replacement, new_inputs,
        replacement.tasks[2], runtime.artifacts,
        runtime=runtime,
    )
    registry = _Registry(profiles)
    transition = runtime.service.prepare_provider_profile_transition(
        replacement, new_inputs, registry, profiles, owner=runtime.owner,
    )
    original_append = runtime.journal.append_amendment

    def interrupt_intent(event_type, **kwargs):
        original_append(event_type, **kwargs)
        if event_type == "plan-amendment-intent":
            raise SystemExit("crash after intent")

    monkeypatch.setattr(runtime.journal, "append_amendment", interrupt_intent)
    with pytest.raises(SystemExit):
        runtime.service.submit_amendment(
            replacement, new_inputs, {"later": preparation}, profiles,
            expected_plan_revision=1, provider_transition=transition,
            owner=runtime.owner,
        )
    monkeypatch.undo()
    del transition
    service, coordinator = _restart_before_profile_recovery(runtime, original)
    registry = _Registry(new_inputs.provider_profiles)
    assert runtime.journal.state.amendments[2].phase == "plan-amendment-intent"
    with pytest.raises(fanout.ExecutionPreflightError, match="amendment|transition"):
        service.resume(owner=runtime.owner)
    assert coordinator.provider_dispatches == []
    recovered = service.prepare_provider_profile_transition(
        replacement, new_inputs, registry, profiles, owner=runtime.owner,
    )
    service.submit_amendment(
        replacement, new_inputs, {"later": preparation}, profiles,
        expected_plan_revision=1, provider_transition=recovered,
        owner=runtime.owner,
    )
    assert coordinator.provider_dispatches == []
    service.resume(owner=runtime.owner)
    assert coordinator.provider_dispatches == [("ready", 1)]


@pytest.mark.parametrize("command", ("submit", "action_complete", "handover", "gc"))
def test_profile_intent_only_restart_blocks_other_owner_mutations(
    tmp_path, monkeypatch, command,
):
    runtime, plan, new_inputs, preparation, registry = _profile_amendment(tmp_path)
    transition = runtime.service.prepare_provider_profile_transition(
        plan, new_inputs, registry, dict(new_inputs.provider_profiles),
        owner=runtime.owner,
    )
    original_append = runtime.journal.append_amendment

    def interrupt_intent(event_type, **kwargs):
        original_append(event_type, **kwargs)
        if event_type == "plan-amendment-intent":
            raise SystemExit("crash after intent")

    monkeypatch.setattr(runtime.journal, "append_amendment", interrupt_intent)
    with pytest.raises(SystemExit):
        runtime.service.submit_amendment(
            plan, new_inputs, {"later": preparation},
            dict(new_inputs.provider_profiles), expected_plan_revision=1,
            provider_transition=transition, owner=runtime.owner,
        )
    monkeypatch.undo()
    del transition
    service, coordinator = _restart_before_profile_recovery(runtime, plan)
    artifact = runtime.artifacts.write_bytes("actions/pending-intent.json", b"{}\n")
    revision = runtime.scheduler.revision
    phases = dict(runtime.scheduler.phases)
    backend_record = runtime.backend.record
    if command == "submit":
        action = lambda: service.submit("approve", artifact, owner=runtime.owner)
    elif command == "action_complete":
        action = lambda: service.action_complete("approve", artifact, owner=runtime.owner)
    elif command == "handover":
        action = lambda: service.handover("approve", None, owner=runtime.owner)
    else:
        action = lambda: service.gc((), owner=runtime.owner)
    with pytest.raises(fanout.ExecutionPreflightError, match="amendment|transition"):
        action()
    assert runtime.scheduler.revision == revision
    assert runtime.scheduler.phases == phases
    assert runtime.backend.record is backend_record
    assert runtime.journal.state.amendments[2].phase == "plan-amendment-intent"
    assert coordinator.provider_dispatches == []
    assert service.status().plan_revision == 1
    with pytest.raises(fanout.ExecutionValidationError, match="baseline"):
        service.collect("later", owner=runtime.owner)


def test_profile_recovery_refuses_wrong_binding_and_registry_drift_before_spend(
    tmp_path, monkeypatch,
):
    runtime, plan, new_inputs, preparation, registry = _profile_amendment(tmp_path)
    transition = runtime.service.prepare_provider_profile_transition(
        plan, new_inputs, registry, dict(new_inputs.provider_profiles),
        owner=runtime.owner,
    )
    original_accept = runtime.scheduler.accept_amendment

    def interrupt_scheduler(*args, **kwargs):
        original_accept(*args, **kwargs)
        raise SystemExit("crash after scheduler commit")

    monkeypatch.setattr(runtime.scheduler, "accept_amendment", interrupt_scheduler)
    with pytest.raises(SystemExit):
        runtime.service.submit_amendment(
            plan, new_inputs, {"later": preparation},
            dict(new_inputs.provider_profiles), expected_plan_revision=1,
            provider_transition=transition, owner=runtime.owner,
        )
    monkeypatch.undo()
    del transition
    service, coordinator = _restart_before_profile_recovery(runtime, plan)
    registry = _Registry(new_inputs.provider_profiles)
    binding = runtime.journal.state.amendments[2].binding
    runtime.journal.state.amendments[2].binding = (
        *binding[:5], _digest("wrong-durable-profile"),
    )
    with pytest.raises(fanout.ExecutionPreflightError):
        service.prepare_provider_profile_transition(
            plan, new_inputs, registry, dict(new_inputs.provider_profiles),
            owner=runtime.owner,
        )
    runtime.journal.state.amendments[2].binding = binding
    wrong_inputs = dataclasses.replace(
        new_inputs, parser_sha256=_digest("wrong-recovery-inputs"),
    )
    with pytest.raises(fanout.ExecutionPreflightError):
        service.prepare_provider_profile_transition(
            plan, wrong_inputs, registry, dict(new_inputs.provider_profiles),
            owner=runtime.owner,
        )
    registry.profile_digests["claude"] = _digest("drifted-after-crash")
    with pytest.raises(fanout.ExecutionPreflightError):
        service.prepare_provider_profile_transition(
            plan, new_inputs, registry, dict(new_inputs.provider_profiles),
            owner=runtime.owner,
        )
    assert runtime.backend.record.snapshot.inputs_digest == runtime.inputs.digest
    assert coordinator.provider_dispatches == []


def test_profile_amendment_refuses_old_only_and_premature_new_only_compositions(tmp_path):
    old_runtime, plan, new_inputs, preparation, registry = _profile_amendment(
        tmp_path / "old-only"
    )
    with pytest.raises(fanout.ExecutionPreflightError, match="profile|transition"):
        old_runtime.service.submit_amendment(
            plan,
            new_inputs,
            {"later": preparation},
            dict(new_inputs.provider_profiles),
            expected_plan_revision=1,
            owner=old_runtime.owner,
        )
    assert old_runtime.scheduler.plan_revision == 1

    new_runtime, plan, new_inputs, _preparation, registry = _profile_amendment(
        tmp_path / "premature-new"
    )
    new_runtime.coordinator.registry = registry
    with pytest.raises(fanout.ExecutionPreflightError, match="composition|profile"):
        new_runtime.service.prepare_provider_profile_transition(
            plan,
            new_inputs,
            registry,
            dict(new_inputs.provider_profiles),
            owner=new_runtime.owner,
        )
    assert new_runtime.scheduler.plan_revision == 1


def test_profile_amendment_uses_one_exact_atomic_transition(tmp_path):
    runtime, plan, new_inputs, preparation, registry = _profile_amendment(tmp_path)
    transition = runtime.service.prepare_provider_profile_transition(
        plan,
        new_inputs,
        registry,
        dict(new_inputs.provider_profiles),
        owner=runtime.owner,
    )

    amendment = runtime.service.submit_amendment(
        plan,
        new_inputs,
        {"later": preparation},
        dict(new_inputs.provider_profiles),
        expected_plan_revision=1,
        provider_transition=transition,
        owner=runtime.owner,
    )

    assert amendment.plan_revision == 2
    assert runtime.coordinator.registry is registry
    assert runtime.service.provider_profile_digests == new_inputs.provider_profiles
    artifact = runtime.artifacts.write_bytes("actions/approve-v2.json", b"{}\n")
    runtime.service.action_complete("approve", artifact, owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    assert runtime.coordinator.provider_dispatches == [("later", 1)]


def test_profile_transition_rejects_changed_binding_and_new_registry_drift(tmp_path):
    runtime, plan, new_inputs, preparation, registry = _profile_amendment(tmp_path)
    transition = runtime.service.prepare_provider_profile_transition(
        plan,
        new_inputs,
        registry,
        dict(new_inputs.provider_profiles),
        owner=runtime.owner,
    )
    registry.profile_digests["claude"] = _digest("drift-after-transition")

    with pytest.raises(fanout.ExecutionPreflightError, match="profile|transition"):
        runtime.service.submit_amendment(
            plan,
            new_inputs,
            {"later": preparation},
            dict(new_inputs.provider_profiles),
            expected_plan_revision=1,
            provider_transition=transition,
            owner=runtime.owner,
        )

    assert runtime.scheduler.plan_revision == 1
    assert runtime.backend.record.snapshot.inputs_digest == runtime.inputs.digest


def test_profile_transition_is_bound_to_the_exact_service_and_amendment(tmp_path):
    first, plan, new_inputs, preparation, registry = _profile_amendment(
        tmp_path / "first"
    )
    transition = first.service.prepare_provider_profile_transition(
        plan,
        new_inputs,
        registry,
        dict(new_inputs.provider_profiles),
        owner=first.owner,
    )
    mismatched_inputs = dataclasses.replace(
        new_inputs,
        parser_sha256=_digest("mismatched-parser"),
    )
    with pytest.raises(fanout.ExecutionPreflightError, match="profile|transition"):
        first.service.submit_amendment(
            plan,
            mismatched_inputs,
            {"later": preparation},
            dict(mismatched_inputs.provider_profiles),
            expected_plan_revision=1,
            provider_transition=transition,
            owner=first.owner,
        )
    assert first.scheduler.plan_revision == 1
    assert first.coordinator.provider_dispatches == []

    second, second_plan, second_inputs, second_preparation, _second_registry = (
        _profile_amendment(tmp_path / "second")
    )

    with pytest.raises(fanout.ExecutionPreflightError, match="profile|transition"):
        second.service.submit_amendment(
            second_plan,
            second_inputs,
            {"later": second_preparation},
            dict(second_inputs.provider_profiles),
            expected_plan_revision=1,
            provider_transition=transition,
            owner=second.owner,
        )

    assert second.scheduler.plan_revision == 1
    assert second.backend.record.snapshot.inputs_digest == second.inputs.digest
    assert second.coordinator.provider_dispatches == []


def test_profile_transition_rejects_joint_provider_boundary_substitution(tmp_path):
    runtime, plan, new_inputs, preparation, registry = _profile_amendment(tmp_path)
    transition = runtime.service.prepare_provider_profile_transition(
        plan,
        new_inputs,
        registry,
        dict(new_inputs.provider_profiles),
        owner=runtime.owner,
    )
    runtime.coordinator.registry = registry
    runtime.coordinator.provider_runner = lambda *_args, **_kwargs: None

    with pytest.raises(fanout.ExecutionPreflightError, match="composition|provider|boundary"):
        runtime.service.submit_amendment(
            plan,
            new_inputs,
            {"later": preparation},
            dict(new_inputs.provider_profiles),
            expected_plan_revision=1,
            provider_transition=transition,
            owner=runtime.owner,
        )

    assert runtime.scheduler.plan_revision == 1
    assert runtime.coordinator.provider_dispatches == []


def test_noop_amendment_is_rejected_before_journal_or_scheduler_mutation(tmp_path):
    plan = _plan(
        _task("approve", execution_class="orchestrator-action"),
        _task("later", depends_on=("approve",)),
    )
    runtime = _runtime(tmp_path, plan)
    runtime.service.start(owner=runtime.owner)

    with pytest.raises(fanout.ExecutionPreflightError, match="affect"):
        runtime.service.submit_amendment(
            plan,
            runtime.inputs,
            runtime.preparations,
            dict(runtime.inputs.provider_profiles),
            expected_plan_revision=1,
            owner=runtime.owner,
        )

    assert runtime.scheduler.amendments == []
    assert 2 not in runtime.journal.state.amendments


def test_amendment_preserves_unaffected_live_task_context_across_restart(tmp_path):
    original = _plan(
        _task("live"),
        _task("approve", execution_class="orchestrator-action"),
        _task("later", depends_on=("approve",)),
    )
    runtime = _runtime(tmp_path, original)
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    assert runtime.scheduler.task_phase("live") == "reconciliation-pending"
    old_preparation = runtime.preparations["live"]
    old_state = next(
        state for state in runtime.backend.record.snapshot.tasks
        if state.task_id == "live"
    )

    replacement_data = original.to_dict()
    replacement_data["tasks"][2]["objective"] = "Complete the amended later task."
    replacement = fanout.FanoutPlanV1.from_dict(replacement_data)
    new_inputs = _inputs(replacement, repo_digest=runtime.baseline.digest)
    proposed = {
        task.id: _read_only_preparation(
            tmp_path / "amended" / task.id,
            replacement,
            new_inputs,
            task,
            runtime.artifacts,
            runtime=runtime,
        )
        for task in replacement.tasks
        if task.execution_class == "read-only"
    }

    runtime.service.submit_amendment(
        replacement,
        new_inputs,
        proposed,
        dict(new_inputs.provider_profiles),
        expected_plan_revision=1,
        owner=runtime.owner,
    )

    current_state = next(
        state for state in runtime.backend.record.snapshot.tasks
        if state.task_id == "live"
    )
    assert runtime.service.preparations["live"] is old_preparation
    assert current_state == old_state
    assert runtime.service.preparations["later"] is proposed["later"]

    recovered = fanout.ExecutionService(
        plan=replacement,
        inputs=new_inputs,
        preparations=runtime.service.preparations,
        provider_profile_digests=dict(new_inputs.provider_profiles),
        budget=fanout.ExecutionBudget(max_provider_turns=64),
        scheduler=runtime.scheduler,
        journal=runtime.journal,
        coordinator=runtime.coordinator,
        artifacts=runtime.artifacts,
        backend=runtime.backend,
        memory_preflight=runtime.memory.preflight,
        baseline=runtime.baseline,
        lifecycle_controller=runtime.controller,
    )
    barrier = next(
        item for item in runtime.coordinator.barriers.values()
        if item.task_id == "live"
    )
    result = recovered.submit(
        "live",
        barrier.valid_terminals[0].answer_ref,
        owner=runtime.owner,
    )
    assert result.task_id == "live"


def test_amendment_cannot_replace_unaffected_live_repo_workspace_evidence(tmp_path):
    check = fanout.PlanCheckV1(
        argv=(sys.executable, "-c", "pass"),
        cwd="",
        env_allowlist=(),
        timeout=5,
        accepted_exit_codes=(0,),
        expected_artifacts=(),
    )
    original = _plan(
        _task("write", execution_class="repo-write", checks=(check,)),
        _task("approve", execution_class="orchestrator-action"),
        _task(
            "later-action",
            execution_class="orchestrator-action",
            depends_on=("approve",),
        ),
    )
    runtime = _repo_runtime(tmp_path, original)
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    old_preparation = runtime.preparations["write"]

    replacement_data = original.to_dict()
    replacement_data["tasks"][2]["objective"] = "Complete the amended action."
    replacement = fanout.FanoutPlanV1.from_dict(replacement_data)
    new_inputs = _inputs(replacement, repo_digest=runtime.baseline.digest)
    alternate_workspaces = {
        executor: fanout.create_seat_workspace(
            runtime.baseline,
            runtime.controller.root / "alternate-workspaces",
            f"alternate-{executor}",
        )
        for executor in ("claude", "codex")
    }
    alternate_verifications = {
        executor: fanout.verify_seat_workspace(
            runtime.baseline,
            workspace,
            controller=runtime.controller,
        )
        for executor, workspace in alternate_workspaces.items()
    }
    seats, bundle = _seats(
        tmp_path / "alternate",
        "write",
        runtime.artifacts,
        workspace_verifications=alternate_verifications,
    )
    task = replacement.tasks[0]
    packet = fanout.TaskPacket.for_repo_write(
        workspace_verifications=tuple(alternate_verifications.values()),
        lifecycle_controller=runtime.controller,
        run_id=new_inputs.run_id,
        task_id=task.id,
        attempt=1,
        compiled_plan=fanout.canonical_json(replacement.to_dict()),
        compiled_plan_sha256=new_inputs.compiled_plan_sha256,
        source_markdown=b"source",
        task=fanout.canonical_json(task.to_dict()),
        task_sha256=_digest(fanout.canonical_json(task.to_dict())),
        skill_bundle=bundle,
        skill_manifest_sha256=new_inputs.skill_manifests[task.id],
        dependency_artifacts=(),
        execution_class="repo-write",
        cwd=runtime.repository,
    )
    proposed = fanout.TaskPreparation(
        packet,
        seats,
        fanout.RoundPolicy.from_provider_policy(task.provider_policy),
        new_inputs,
        2,
    )

    runtime.service.submit_amendment(
        replacement,
        new_inputs,
        {"write": proposed},
        dict(new_inputs.provider_profiles),
        expected_plan_revision=1,
        owner=runtime.owner,
    )

    assert runtime.service.preparations["write"] is old_preparation
    assert {
        seat.workspace_verification.evidence_digest
        for seat in runtime.service.preparations["write"].seats
    } == {
        verification.evidence_digest
        for verification in runtime.verifications.values()
    }


@pytest.mark.parametrize(
    "mismatch",
    (
        "same-run-journal",
        "joint-journal-identity",
        "owner",
        "memory-health",
        "memory-identity",
        "provider-profiles",
        "registry-identity",
        "artifacts",
        "lifecycle-controller",
        "scheduler-identity",
        "coordinator-identity",
        "scheduler-inputs",
    ),
)
def test_composition_mismatch_fails_before_state_mutation_or_provider_spend(
    tmp_path,
    mismatch,
):
    if mismatch == "lifecycle-controller":
        runtime = _repo_runtime(tmp_path)
        runtime.coordinator.lifecycle_controller = fanout.create_lifecycle_controller(
            tmp_path / "foreign-controller"
        )
    else:
        runtime = _runtime(tmp_path, _plan(_task("work")))
        if mismatch == "same-run-journal":
            runtime.coordinator.journal = _Journal(runtime.inputs, runtime.owner)
        elif mismatch == "joint-journal-identity":
            replacement = _Journal(runtime.inputs, runtime.owner)
            runtime.journal = replacement
            runtime.coordinator.journal = replacement
            runtime.service.journal = replacement
        elif mismatch == "owner":
            runtime.coordinator.owner = fanout.OwnerCapability.from_token("x" * 43)
        elif mismatch == "memory-health":
            runtime.service.memory_preflight = lambda: True
        elif mismatch == "memory-identity":
            replacement = _Memory(runtime.artifacts)
            runtime.memory = replacement
            runtime.coordinator.memory = replacement
            runtime.service.memory_preflight = replacement.preflight
        elif mismatch == "provider-profiles":
            runtime.registry.profile_digests["claude"] = _digest("foreign-claude")
        elif mismatch == "registry-identity":
            replacement = _Registry(runtime.inputs.provider_profiles)
            runtime.registry = replacement
            runtime.coordinator.registry = replacement
        elif mismatch == "artifacts":
            runtime.coordinator.artifacts = fanout.ArtifactStore(tmp_path / "foreign-artifacts")
        elif mismatch == "scheduler-inputs":
            runtime.scheduler.inputs = _inputs(_plan(_task("foreign")))
        elif mismatch == "scheduler-identity":
            replacement = _Scheduler(
                runtime.scheduler.plan,
                runtime.inputs,
                runtime.artifacts,
            )
            runtime.scheduler = replacement
            runtime.service.scheduler = replacement
        elif mismatch == "coordinator-identity":
            replacement = _Coordinator(
                runtime.artifacts,
                journal=runtime.journal,
                owner=runtime.owner,
                memory=runtime.memory,
                registry=runtime.registry,
            )
            runtime.coordinator = replacement
            runtime.service.coordinator = replacement

    with pytest.raises(fanout.ExecutionPreflightError, match="composition|preflight"):
        runtime.service.start(owner=runtime.owner)

    assert runtime.backend.record is None
    assert runtime.coordinator.execute_calls == []
    assert runtime.coordinator.provider_dispatches == []
    assert set(runtime.scheduler.phases.values()) == {"unscheduled"}


@pytest.mark.parametrize("command", ("start", "resume"))
@pytest.mark.parametrize("boundary", ("execute_round", "provider_runner"))
def test_provider_execution_boundary_replacement_is_rejected_before_spend(
    tmp_path,
    boundary,
    command,
):
    runtime = _runtime(tmp_path, _plan(_task("work")))
    if command == "resume":
        runtime.service.start(owner=runtime.owner)
    original = getattr(runtime.coordinator, boundary)
    substituted = []

    def replacement(*args, **kwargs):
        substituted.append(boundary)
        return original(*args, **kwargs)

    setattr(runtime.coordinator, boundary, replacement)

    with pytest.raises(fanout.ExecutionPreflightError, match="composition|provider|boundary"):
        getattr(runtime.service, command)(owner=runtime.owner)

    assert substituted == []
    assert runtime.coordinator.provider_dispatches == []


def test_repo_write_drift_is_rejected_before_start_and_before_first_resume(tmp_path):
    before_start_root = tmp_path / "before-start"
    before_start_root.mkdir()
    before_start = _repo_runtime(before_start_root)
    (before_start.workspaces["claude"].root / "changed.txt").write_text(
        "drifted before start\n",
        encoding="utf-8",
    )
    with pytest.raises(fanout.ExecutionPreflightError, match="baseline|workspace|preflight"):
        before_start.service.start(owner=before_start.owner)
    assert before_start.backend.record is None
    assert before_start.coordinator.provider_dispatches == []

    before_resume_root = tmp_path / "before-resume"
    before_resume_root.mkdir()
    before_resume = _repo_runtime(before_resume_root)
    before_resume.service.start(owner=before_resume.owner)
    (before_resume.workspaces["codex"].root / "changed.txt").write_text(
        "drifted before resume\n",
        encoding="utf-8",
    )
    with pytest.raises(fanout.ExecutionPreflightError, match="baseline|workspace|preflight"):
        before_resume.service.resume(owner=before_resume.owner)
    assert before_resume.coordinator.provider_dispatches == []


def _complete_repo_reconciliation(runtime):
    runtime.service.start(owner=runtime.owner)
    runtime.service.resume(owner=runtime.owner)
    receipt, _ = _verified_repo_synthesis(runtime)
    runtime.service.submit("write", receipt, owner=runtime.owner)
    return receipt


def test_handover_recovery_closes_committed_task14_crash_window_idempotently(tmp_path):
    runtime = _repo_runtime(tmp_path)
    receipt = _complete_repo_reconciliation(runtime)
    runtime.journal.fail_event_once = "handover-complete"

    with pytest.raises(RuntimeError, match="injected handover-complete"):
        runtime.service.handover("write", receipt, owner=runtime.owner)

    transaction = next((runtime.controller.root / "transactions").iterdir())
    disposition = runtime.service.recover_handover(
        "write",
        transaction,
        receipt,
        owner=runtime.owner,
    )
    assert disposition.status == "committed"
    assert disposition.task_id == "write"
    assert runtime.journal.state.task_phases["write"] == "handover-complete"
    assert runtime.service.recover_handover(
        "write",
        transaction,
        receipt,
        owner=runtime.owner,
    ) == disposition


@pytest.mark.parametrize(
    ("interrupted_step", "expected_status", "expected_phase"),
    (
        ("stage-create-intent", "not-mutated", "handover-not-mutated"),
        ("handover-complete", "rolled-back", "handover-rolled-back"),
    ),
)
def test_handover_recovery_closes_precompletion_crash_windows(
    tmp_path,
    monkeypatch,
    interrupted_step,
    expected_status,
    expected_phase,
):
    runtime = _repo_runtime(tmp_path)
    receipt = _complete_repo_reconciliation(runtime)
    original_record = lifecycle._HandoverJournal.record
    interrupted = False

    def interrupt_record(journal, step, *args, **kwargs):
        nonlocal interrupted
        if not interrupted and step == interrupted_step:
            interrupted = True
            raise RuntimeError(f"injected Task 14 crash before {step}")
        return original_record(journal, step, *args, **kwargs)

    monkeypatch.setattr(lifecycle._HandoverJournal, "record", interrupt_record)
    with pytest.raises(fanout.HandoverError):
        runtime.service.handover("write", receipt, owner=runtime.owner)
    monkeypatch.setattr(lifecycle._HandoverJournal, "record", original_record)

    transaction = next((runtime.controller.root / "transactions").iterdir())
    disposition = runtime.service.recover_handover(
        "write",
        transaction,
        receipt,
        owner=runtime.owner,
    )
    assert disposition.status == expected_status
    assert runtime.journal.state.task_phases["write"] == expected_phase


def test_handover_recovery_is_idempotent_after_both_completion_records(tmp_path):
    runtime = _repo_runtime(tmp_path)
    receipt = _complete_repo_reconciliation(runtime)
    completed = runtime.service.handover("write", receipt, owner=runtime.owner)

    recovered = runtime.service.recover_handover(
        "write",
        completed.transaction_root,
        receipt,
        owner=runtime.owner,
    )

    assert recovered == completed
    assert recovered.status == "committed"
    assert runtime.journal.state.task_phases["write"] == "handover-complete"


def test_stale_handover_transaction_cannot_terminalize_the_current_retry(
    tmp_path,
    monkeypatch,
):
    runtime = _repo_runtime(tmp_path)
    receipt = _complete_repo_reconciliation(runtime)
    original_record = lifecycle._HandoverJournal.record
    interrupted = False

    def interrupt_first_transaction(journal, step, *args, **kwargs):
        nonlocal interrupted
        if not interrupted and step == "stage-create-intent":
            interrupted = True
            raise RuntimeError("injected transaction A interruption")
        return original_record(journal, step, *args, **kwargs)

    monkeypatch.setattr(
        lifecycle._HandoverJournal,
        "record",
        interrupt_first_transaction,
    )
    with pytest.raises(fanout.HandoverError):
        runtime.service.handover("write", receipt, owner=runtime.owner)
    monkeypatch.setattr(lifecycle._HandoverJournal, "record", original_record)

    transactions = set((runtime.controller.root / "transactions").iterdir())
    transaction_a = transactions.pop()
    first = runtime.service.recover_handover(
        "write",
        transaction_a,
        receipt,
        owner=runtime.owner,
    )
    assert first.status == "not-mutated"

    runtime.journal.fail_event_once = "handover-complete"
    with pytest.raises(RuntimeError, match="injected handover-complete"):
        runtime.service.handover("write", receipt, owner=runtime.owner)
    transaction_b = next(
        item
        for item in (runtime.controller.root / "transactions").iterdir()
        if item != transaction_a
    )
    assert runtime.journal.state.task_phases["write"] == "handover-intent"

    with pytest.raises(fanout.ExecutionConflictError, match="transaction|association"):
        runtime.service.recover_handover(
            "write",
            transaction_a,
            receipt,
            owner=runtime.owner,
        )
    assert runtime.journal.state.task_phases["write"] == "handover-intent"

    current = runtime.service.recover_handover(
        "write",
        transaction_b,
        receipt,
        owner=runtime.owner,
    )
    assert current.status == "committed"
    assert runtime.journal.state.task_phases["write"] == "handover-complete"
    assert runtime.service.recover_handover(
        "write",
        transaction_b,
        receipt,
        owner=runtime.owner,
    ) == current


@pytest.mark.parametrize(
    "task_phase",
    ("scheduled", "blocked-action", "reconciliation-pending", "completed"),
)
def test_amendment_records_never_collide_with_legal_task_ids(tmp_path, task_phase):
    colliding_id = "plan-revision-2"
    original = _plan(
        _task(colliding_id, execution_class="orchestrator-action"),
        _task("gate", execution_class="orchestrator-action"),
        _task("later", depends_on=("gate",)),
    )
    runtime = _runtime(tmp_path, original)
    runtime.service.start(owner=runtime.owner)
    runtime.scheduler.phases[colliding_id] = task_phase
    runtime.journal.state.task_phases[colliding_id] = task_phase
    replacement_data = original.to_dict()
    replacement_data["tasks"][2]["objective"] = "Amended later work."
    replacement = fanout.FanoutPlanV1.from_dict(replacement_data)
    new_inputs = _inputs(replacement, repo_digest=runtime.baseline.digest)
    later = replacement.tasks[2]
    proposed = _read_only_preparation(
        tmp_path / "amended",
        replacement,
        new_inputs,
        later,
        runtime.artifacts,
        runtime=runtime,
    )

    amendment = runtime.service.submit_amendment(
        replacement,
        new_inputs,
        {"later": proposed},
        dict(new_inputs.provider_profiles),
        expected_plan_revision=1,
        owner=runtime.owner,
    )

    assert amendment.plan_revision == 2
    assert runtime.journal.state.task_phases[colliding_id] == task_phase
    record = runtime.journal.state.amendments[2]
    assert record.phase == "plan-amendment-accepted"
    profiles_sha256 = _digest(fanout.canonical_json({
        "profiles": {
            "claude": _digest("claude"),
            "codex": _digest("codex"),
        },
        "schema_version": "fanout-provider-profiles-v1",
    }))
    assert record.binding == (
        _digest(fanout.canonical_json(original.to_dict())),
        runtime.inputs.digest,
        _digest(fanout.canonical_json(replacement.to_dict())),
        new_inputs.digest,
        profiles_sha256,
        profiles_sha256,
    )


def test_execution_limits_reject_plan_status_snapshot_and_backend_overflow(tmp_path):
    plan = _plan(_task("one"), _task("two"))
    runtime = _runtime(tmp_path, plan)
    limits = fanout.ExecutionLimits(
        max_tasks=1,
        max_artifact_path_bytes=128,
        max_snapshot_bytes=64 * 1024,
        max_backend_record_bytes=64 * 1024,
        max_status_bytes=64 * 1024,
    )

    with pytest.raises(fanout.ExecutionValidationError, match="task.*limit|bounded"):
        fanout.ExecutionService(
            plan=plan,
            inputs=runtime.inputs,
            preparations=runtime.preparations,
            provider_profile_digests=dict(runtime.inputs.provider_profiles),
            budget=fanout.ExecutionBudget(max_provider_turns=64),
            scheduler=runtime.scheduler,
            journal=runtime.journal,
            coordinator=runtime.coordinator,
            artifacts=runtime.artifacts,
            backend=runtime.backend,
            memory_preflight=runtime.memory.preflight,
            baseline=runtime.baseline,
            lifecycle_controller=runtime.controller,
            limits=limits,
        )

    assert runtime.backend.record is None
    assert runtime.coordinator.execute_calls == []


def test_artifact_reference_path_limit_is_rechecked_on_backend_read(tmp_path):
    runtime = _runtime(tmp_path, _plan(_task("work")))
    limits = fanout.ExecutionLimits(
        max_tasks=8,
        max_artifact_path_bytes=32,
        max_snapshot_bytes=64 * 1024,
        max_backend_record_bytes=64 * 1024,
        max_status_bytes=64 * 1024,
    )
    service = fanout.ExecutionService(
        plan=runtime.scheduler.plan,
        inputs=runtime.inputs,
        preparations=runtime.preparations,
        provider_profile_digests=dict(runtime.inputs.provider_profiles),
        budget=fanout.ExecutionBudget(max_provider_turns=64),
        scheduler=runtime.scheduler,
        journal=runtime.journal,
        coordinator=runtime.coordinator,
        artifacts=runtime.artifacts,
        backend=runtime.backend,
        memory_preflight=runtime.memory.preflight,
        baseline=runtime.baseline,
        lifecycle_controller=runtime.controller,
        limits=limits,
    )
    service.start(owner=runtime.owner)
    long_ref = runtime.artifacts.write_bytes(
        "over/long/artifact/reference/path/barrier.json",
        b"barrier",
    )
    original = runtime.backend.record
    forged_state = dataclasses.replace(
        original.snapshot.tasks[0],
        barriers=(long_ref,),
    )
    forged_snapshot = dataclasses.replace(
        original.snapshot,
        backend_revision=2,
        tasks=(forged_state,),
    )
    runtime.backend.record = fanout.ExecutionRecord(2, forged_snapshot)
    recovered = fanout.ExecutionService(
        plan=runtime.scheduler.plan,
        inputs=runtime.inputs,
        preparations=runtime.preparations,
        provider_profile_digests=dict(runtime.inputs.provider_profiles),
        budget=fanout.ExecutionBudget(max_provider_turns=64),
        scheduler=runtime.scheduler,
        journal=runtime.journal,
        coordinator=runtime.coordinator,
        artifacts=runtime.artifacts,
        backend=runtime.backend,
        memory_preflight=runtime.memory.preflight,
        baseline=runtime.baseline,
        lifecycle_controller=runtime.controller,
        limits=limits,
    )

    with pytest.raises(fanout.ExecutionConflictError, match="artifact|path|bounded"):
        recovered.status()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("max_snapshot_bytes", 128, "snapshot"),
        ("max_status_bytes", 64, "status"),
        ("max_backend_record_bytes", 128, "backend|record"),
    ),
)
def test_execution_aggregate_byte_limits_fail_before_initialization(
    tmp_path,
    field,
    value,
    message,
):
    runtime = _runtime(tmp_path, _plan(_task("work")))
    values = {
        "max_tasks": 8,
        "max_artifact_path_bytes": 256,
        "max_snapshot_bytes": 64 * 1024,
        "max_backend_record_bytes": 64 * 1024,
        "max_status_bytes": 64 * 1024,
    }
    values[field] = value
    limits = fanout.ExecutionLimits(**values)

    with pytest.raises(fanout.ExecutionValidationError, match=message):
        fanout.ExecutionService(
            plan=runtime.scheduler.plan,
            inputs=runtime.inputs,
            preparations=runtime.preparations,
            provider_profile_digests=dict(runtime.inputs.provider_profiles),
            budget=fanout.ExecutionBudget(max_provider_turns=64),
            scheduler=runtime.scheduler,
            journal=runtime.journal,
            coordinator=runtime.coordinator,
            artifacts=runtime.artifacts,
            backend=runtime.backend,
            memory_preflight=runtime.memory.preflight,
            baseline=runtime.baseline,
            lifecycle_controller=runtime.controller,
            limits=limits,
        )

    assert runtime.backend.record is None
