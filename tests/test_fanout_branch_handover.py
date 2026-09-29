"""Local, disposable Git proof and handover boundaries."""
from __future__ import annotations

import importlib
import dataclasses
import hashlib
import os
import subprocess
from types import SimpleNamespace

import pytest

import test_fanout_scheduler as cases


fanout = cases.fanout


def _settled_writer(tmp_path, *, changed: bool = False, candidate_entries=None,
                    existing_branch: bool = False, file_backend: bool = False,
                    dependency_mode: str = "handover", independent_count: int = 0,
                    descendant: bool = False, recoverable_amendments: bool = False,
                    baseline_mode: int | None = None,
                    baseline_directory_mode: int | None = None,
                    baseline_file_count: int = 0,
                    candidate_deleted_paths: tuple[str, ...] = ()):
    scheduler, backend, journal, controller, baseline, owner = cases._v2_handover_fixture(
        tmp_path, existing_branch=existing_branch, file_backend=file_backend,
        dependency_mode=dependency_mode, independent_count=independent_count,
        descendant=descendant, recoverable_amendments=recoverable_amendments,
        baseline_mode=baseline_mode, baseline_directory_mode=baseline_directory_mode,
        baseline_file_count=baseline_file_count,
    )
    scheduler.schedule_ready(owner=owner)
    scheduler.mark_active("address", owner=owner)
    scheduler.begin_reconciliation("address", owner=owner)
    entries = (candidate_entries if candidate_entries is not None else
               (fanout.CandidateEntry("README.md", "file", 0o644, b"verified change\n"),)
               if changed else ())
    candidate = fanout.CandidateBundle(baseline.digest, entries, candidate_deleted_paths)
    issued = fanout.issue_target_candidate(
        candidate, task_id="address", plan=scheduler.plan, inputs=scheduler.inputs,
        store=backend.artifacts, controller=controller,
    )
    verified, _receipt = fanout.verify_target_candidate(
        issued, baseline=baseline, plan=scheduler.plan, inputs=scheduler.inputs,
        store=backend.artifacts, controller=controller,
    )
    cases._reconcile_v2_candidate(scheduler, backend, candidate, owner)
    return scheduler, backend, journal, controller, owner, issued, verified


def test_prepare_requires_controller_issued_envelopes_and_settled_decision(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    with pytest.raises(fanout.HandoverError, match="controller|envelope"):
        handover.prepare_branch_handover(
            binding, fanout.CandidateBundle(binding.baseline_sha256, (), ()), verification,
            plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
            scheduler=scheduler, controller=controller, journal=journal, owner=owner,
            task_id="address",
        )
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    assert prepared.intent.candidate_ref == candidate.payload
    assert prepared.intent.verification_ref == verification.payload
    assert cases._git(
        binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref,
    ) == ""


def _loose_object_count(root):
    return int(next(line.split(": ", 1)[1] for line in
                    cases._git(root, "count-objects", "-v").splitlines()
                    if line.startswith("count: ")))


def _stage_index_only(root, tmp_path):
    source = tmp_path / "index-only-source.txt"
    source.write_text("index-only bytes\n")
    oid = cases._git(root, "hash-object", "-w", str(source))
    cases._git(root, "update-index", "--add", "--cacheinfo", f"100644,{oid},README.md")
    return oid


def test_index_only_drift_pre_intent_is_read_only(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    _stage_index_only(binding.root, tmp_path)
    assert (binding.root / "README.md").read_text() == "initial\n"
    before = _loose_object_count(binding.root)

    with pytest.raises(fanout.HandoverError, match="dirty|index"):
        handover.prepare_branch_handover(
            binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner, task_id="address",
        )
    assert _loose_object_count(binding.root) == before
    assert "address" not in journal.state.branch_handovers
    assert cases._git(binding.root, "symbolic-ref", "--no-recurse", "HEAD") == binding.head_ref
    assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""


def test_index_only_drift_before_cas_does_not_publish_ref_or_tree(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    observed = []
    def drift(at):
        if at == "after-prepared-commit":
            _stage_index_only(binding.root, tmp_path)
            observed.append(_loose_object_count(binding.root))
    monkeypatch.setattr(handover, "_checkpoint", drift)
    blocked = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert observed and _loose_object_count(binding.root) == observed[0]
    assert blocked.reason == "delivery-failed"
    assert journal.state.branch_blocked["address"] == (prepared.intent.sha256, "delivery-failed")
    assert (binding.root / "README.md").read_text() == "initial\n"
    assert cases._git(binding.root, "symbolic-ref", "--no-recurse", "HEAD") == binding.head_ref
    assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""


def test_index_only_drift_post_cas_recovery_never_switches_head(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def crash(at):
        if at == "after-ref-cas":
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    _stage_index_only(binding.root, tmp_path)
    before = _loose_object_count(binding.root)
    blocked = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert blocked.reason == "git-proof-invalid"
    assert journal.state.branch_blocked["address"] == (prepared.intent.sha256, "git-proof-invalid")
    assert _loose_object_count(binding.root) == before
    assert cases._git(binding.root, "symbolic-ref", "--no-recurse", "HEAD") == binding.head_ref
    assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == handover._prepared_commit_receipt(
        prepared.intent, journal, controller,
    )["commit_oid"]


def test_index_only_drift_cold_proof_is_read_only(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    terminal = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(terminal, fanout.HandoverTerminalV2)
    _stage_index_only(binding.root, tmp_path)
    before = _loose_object_count(binding.root)
    anchor = journal._anchor_store
    journal.close()
    cold_journal = fanout.RunJournal._resume_for_test(
        journal.root, scheduler.inputs, owner, anchor_store=anchor,
    )
    cold_controller = fanout.resume_lifecycle_controller(controller.root, controller.capability)
    with pytest.raises(fanout.HandoverError, match="proof|index|checkout"):
        handover.verify_git_handover_proof(
            prepared.intent, terminal, inputs=scheduler.inputs, artifacts=backend.artifacts,
            controller=cold_controller, journal=cold_journal,
        )
    assert _loose_object_count(binding.root) == before
    assert cases._git(binding.root, "symbolic-ref", "--no-recurse", "HEAD") == binding.spec.branch_ref
    assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == terminal.commit_oid
    cold_journal.close()


def test_staged_drift_pre_intent_does_not_write_git_tree(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    (binding.root / "README.md").write_text("staged drift\n")
    cases._git(binding.root, "add", "README.md")
    before = _loose_object_count(binding.root)

    with pytest.raises(fanout.HandoverError, match="dirty|checkout"):
        handover.prepare_branch_handover(
            binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner, task_id="address",
        )
    assert _loose_object_count(binding.root) == before
    assert "address" not in journal.state.branch_handovers


def test_absent_branch_same_oid_head_switch_after_admission_blocks_prepare(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    cases._git(binding.root, "switch", "-q", "-c", "other")
    assert cases._git(binding.root, "rev-parse", "HEAD") == binding.base_oid

    with pytest.raises(fanout.HandoverError, match="HEAD|binding"):
        handover.prepare_branch_handover(
            binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner, task_id="address",
        )
    assert "address" not in journal.state.branch_handovers


def test_staged_drift_cold_proof_does_not_write_git_tree(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    terminal = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(terminal, fanout.HandoverTerminalV2)
    (binding.root / "README.md").write_text("staged drift\n")
    cases._git(binding.root, "add", "README.md")
    before = _loose_object_count(binding.root)

    with pytest.raises(fanout.HandoverError, match="proof|index|checkout"):
        handover.verify_git_handover_proof(
            prepared.intent, terminal, inputs=scheduler.inputs, artifacts=backend.artifacts,
            controller=controller, journal=journal,
        )
    assert _loose_object_count(binding.root) == before


def test_journaled_terminal_without_matching_git_commit_keeps_handover_edge_locked(tmp_path):
    scheduler, backend, journal, controller, baseline, owner = cases._v2_handover_fixture(tmp_path)
    scheduler.schedule_ready(owner=owner)
    scheduler.mark_active("address", owner=owner)
    scheduler.begin_reconciliation("address", owner=owner)
    intent, terminal, bundle = cases._v2_terminal(
        scheduler, backend, journal, controller, baseline, owner,
    )
    cases._reconcile_v2_candidate(scheduler, backend, bundle, owner)
    journal.append_branch_intent(intent, owner=owner)
    journal.append_branch_terminal("address", terminal, owner=owner)
    with pytest.raises(fanout.SchedulerStateError, match="Git|commit|proof"):
        scheduler.mark_handover_terminal("address", terminal, owner=owner)
    assert scheduler.schedule_ready(owner=owner) == ()


def test_blocked_valid_git_terminal_cannot_project_after_journal_crash(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    prepared = handover.prepare_branch_handover(
        scheduler.inputs.targets["address"], candidate, verification,
        plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
        task_id="address",
    )
    def stop(at):
        if at == "after-journal-terminal":
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", stop)
    with pytest.raises(SimulatedCrash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
            scheduler=scheduler, controller=controller, journal=journal, owner=owner,
        )
    intent, terminal = journal.branch_handover_state("address")
    assert terminal is not None
    handover.verify_git_handover_proof(
        intent, terminal, inputs=scheduler.inputs, artifacts=backend.artifacts,
        controller=controller, journal=journal,
    )
    journal.append_branch_blocked("address", "git-proof-invalid", owner=owner)
    revision = scheduler.revision
    with pytest.raises(fanout.SchedulerStateError, match="blocked"):
        scheduler.mark_handover_terminal("address", terminal, owner=owner)
    assert scheduler.revision == revision
    assert scheduler.schedule_ready(owner=owner) == ()


def test_blocked_projected_terminal_cannot_issue_handover_dependency(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    prepared = handover.prepare_branch_handover(
        scheduler.inputs.targets["address"], candidate, verification,
        plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
        task_id="address",
    )
    terminal = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    assert isinstance(terminal, fanout.HandoverTerminalV2)
    journal.append_branch_blocked("address", "git-proof-invalid", owner=owner)
    with pytest.raises(fanout.SchedulerStateError, match="blocked"):
        scheduler.dependency_record_for("booking", "address")
    assert scheduler.schedule_ready(owner=owner) == ()
    assert fanout.ExecutionService.v2_status(scheduler).target_states["address"] == "blocked"


def _scheduled_handover_dependant(tmp_path, *, independent_count=0, descendant=False,
                                  file_backend=False):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, independent_count=independent_count, descendant=descendant,
        file_backend=file_backend,
    )
    scheduler.mark_active("events", owner=owner)
    scheduler.begin_reconciliation("events", owner=owner)
    result = backend.artifacts.write_bytes("results/events.json", b"events")
    receipt = scheduler.result_receipt("events", result)
    scheduler.complete_reconciliation("events", receipt, owner=owner)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    prepared = handover.prepare_branch_handover(
        scheduler.inputs.targets["address"], candidate, verification,
        plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
        task_id="address",
    )
    handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    assert [decision.task_id for decision in scheduler.schedule_ready(owner=owner)] == (
        ["booking", "analytics"] if independent_count else ["booking"]
    )
    return scheduler, backend, journal, controller, owner


def test_warm_block_quarantines_scheduled_handover_dependant(tmp_path):
    scheduler, _backend, journal, _controller, owner = _scheduled_handover_dependant(tmp_path)
    journal.append_branch_blocked("address", "git-proof-invalid", owner=owner)
    assert scheduler.pending_work() == ()
    with pytest.raises(fanout.SchedulerStateError, match="blocked handover dependency"):
        scheduler.mark_active("booking", owner=owner)
    assert scheduler.schedule_ready(owner=owner) == ()
    assert scheduler.task_phase("booking") == "failed"
    assert scheduler.active_task_count == 0
    assert scheduler.active_seat_count == 0


def test_warm_block_refuses_already_active_handover_dependant(tmp_path):
    scheduler, _backend, journal, _controller, owner = _scheduled_handover_dependant(tmp_path)
    scheduler.mark_active("booking", owner=owner)
    journal.append_branch_blocked("address", "git-proof-invalid", owner=owner)
    with pytest.raises(fanout.SchedulerStateError, match="blocked handover dependency"):
        scheduler.assert_dispatchable("booking")
    with pytest.raises(fanout.SchedulerStateError, match="blocked handover dependency"):
        scheduler.begin_reconciliation("booking", owner=owner)
    scheduler.schedule_ready(owner=owner)
    assert scheduler.task_phase("booking") == "failed"
    assert scheduler.active_seat_count == 0


def test_blocked_handover_frees_capacity_for_independent_target_and_descendants(tmp_path):
    scheduler, _backend, journal, _controller, owner = _scheduled_handover_dependant(
        tmp_path, independent_count=2, descendant=True,
    )
    assert scheduler.active_task_count == 2
    journal.append_branch_blocked("address", "git-proof-invalid", owner=owner)
    assert scheduler.task_phase("shipping") == "blocked-dependency"
    assert [decision.task_id for decision in scheduler.pending_work()] == ["analytics"]
    assert [decision.task_id for decision in scheduler.schedule_ready(owner=owner)] == ["reports"]
    assert scheduler.task_phase("booking") == "failed"
    assert scheduler.active_task_count == 2
    assert scheduler.active_seat_count == 4
    scheduler.mark_active("analytics", owner=owner)
    scheduler.mark_active("reports", owner=owner)


def test_blocked_writer_artifact_edge_stays_eligible(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, dependency_mode="artifact",
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    prepared = handover.prepare_branch_handover(
        scheduler.inputs.targets["address"], candidate, verification,
        plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
        task_id="address",
    )
    handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    journal.append_branch_blocked("address", "git-proof-invalid", owner=owner)
    scheduler.mark_active("events", owner=owner)
    scheduler.begin_reconciliation("events", owner=owner)
    scheduler.complete_reconciliation(
        "events", cases._receipt(scheduler, backend, "events"), owner=owner,
    )
    assert [decision.task_id for decision in scheduler.schedule_ready(owner=owner)] == ["booking"]
    assert scheduler.task_phase("booking") == "scheduled"
    scheduler.mark_active("booking", owner=owner)


def test_cold_resume_quarantines_blocked_handover_and_preserves_independent_work(tmp_path):
    scheduler, backend, journal, controller, owner = _scheduled_handover_dependant(
        tmp_path, file_backend=True, independent_count=2,
    )
    journal.append_branch_blocked("address", "git-proof-invalid", owner=owner)
    revision = scheduler.revision
    journal_anchor = journal._anchor_store
    scheduler_anchor = scheduler._authority
    artifacts = backend.artifacts
    journal.close()
    backend.close()
    reopened_journal = fanout.RunJournal._resume_for_test(
        tmp_path / "run", scheduler.inputs, owner, anchor_store=journal_anchor,
    )
    reopened_backend = fanout.FileSchedulerBackend.resume(
        tmp_path / "run", run_id=scheduler.inputs.run_id, owner=owner,
    )
    reopened = fanout.Scheduler._resume_for_test(
        scheduler.plan, scheduler.inputs, reopened_backend, artifacts,
        owner=owner, anchor_store=scheduler_anchor,
        journal=reopened_journal, lifecycle_controller=controller,
    )
    assert reopened.revision == revision + 1
    assert reopened.task_phase("booking") == "failed"
    assert reopened.task_phase("events") == "completed"
    assert reopened.active_task_count == 1
    assert [item.task_id for item in reopened.pending_work()] == ["analytics"]
    assert [item.task_id for item in reopened.schedule_ready(owner=owner)] == ["reports"]


def test_cold_blocked_status_inspection_is_read_only_and_authority_bound(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, file_backend=True,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    prepared = handover.prepare_branch_handover(
        scheduler.inputs.targets["address"], candidate, verification,
        plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
        task_id="address",
    )
    handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    journal.append_branch_blocked("address", "git-proof-invalid", owner=owner)
    record_path = journal.root / ".fanout-scheduler.record.json"
    before = record_path.read_bytes()
    revision = scheduler.revision
    journal_anchor = journal._anchor_store
    scheduler_anchor = scheduler._authority
    journal.close()
    backend.close()
    status = fanout.Scheduler._inspect_blocked_for_test(
        scheduler.plan, scheduler.inputs, journal.root, backend.artifacts,
        owner=owner, original_journal_inputs=journal.inputs,
        journal_anchor=journal_anchor, scheduler_anchor=scheduler_anchor,
        lifecycle_controller=controller,
    )
    assert status.blocked_target_ids == ("address",)
    assert status.backend_revision == revision
    assert record_path.read_bytes() == before
    assert not tuple(journal.root.glob(".fanout-scheduler-*-tmp-*"))
    with fanout.FileSchedulerBackend.inspect(
        journal.root, run_id=scheduler.inputs.run_id, owner=owner,
    ) as inspected_backend:
        with pytest.raises(fanout.SchedulerStateError, match="cannot mutate"):
            inspected_backend.compare_and_set(
                revision, inspected_backend.read().snapshot, owner=owner,
            )
    assert record_path.read_bytes() == before
    with fanout.FileSchedulerBackend.resume(
        journal.root, run_id=scheduler.inputs.run_id, owner=owner,
    ) as displaced_backend:
        stored = displaced_backend.read()
        displaced_backend.compare_and_set(
            stored.revision,
            dataclasses.replace(stored.snapshot, backend_revision=stored.revision + 1),
            owner=owner,
        )
    displaced = record_path.read_bytes()
    with pytest.raises(fanout.SchedulerStateError, match="committed authority binding"):
        fanout.Scheduler._inspect_blocked_for_test(
            scheduler.plan, scheduler.inputs, journal.root, backend.artifacts,
            owner=owner, original_journal_inputs=journal.inputs,
            journal_anchor=journal_anchor, scheduler_anchor=scheduler_anchor,
            lifecycle_controller=controller,
        )
    assert record_path.read_bytes() == displaced


def test_cold_inspection_reports_intent_only_block_without_claiming_delivery(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, file_backend=True,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    prepared = handover.prepare_branch_handover(
        scheduler.inputs.targets["address"], candidate, verification,
        plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
        task_id="address",
    )
    assert prepared.intent.sha256 == journal.branch_handover_state("address")[0].sha256
    assert journal.branch_handover_state("address")[1] is None
    assert scheduler.handover_terminal_for("address") is None
    journal.append_branch_blocked("address", "pre-cas-interrupted", owner=owner)
    journal_anchor = journal._anchor_store
    scheduler_anchor = scheduler._authority
    record_path = journal.root / ".fanout-scheduler.record.json"
    before = record_path.read_bytes()
    journal.close()
    backend.close()
    status = fanout.Scheduler._inspect_blocked_for_test(
        scheduler.plan, scheduler.inputs, journal.root, backend.artifacts,
        owner=owner, original_journal_inputs=journal.inputs,
        journal_anchor=journal_anchor, scheduler_anchor=scheduler_anchor,
        lifecycle_controller=controller,
    )
    assert status.blocked_target_ids == ("address",)
    assert status.backend_revision == scheduler.revision
    assert record_path.read_bytes() == before


@pytest.mark.parametrize("document_damage", ("missing", "tampered"))
def test_cold_blocked_inspection_authenticates_amended_plan_documents(tmp_path, document_damage):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, file_backend=True, recoverable_amendments=True,
    )
    original_inputs = journal.inputs
    replacement = scheduler.plan.to_dict()
    replacement["tasks"][1]["objective"] = "Revised booking objective."
    amended_plan = fanout.plan.FanoutPlanV2.from_dict(replacement)
    amended_inputs = dataclasses.replace(
        scheduler.inputs,
        compiled_plan_sha256=hashlib.sha256(fanout.canonical_json(amended_plan.to_dict())).hexdigest(),
    )
    profiles_document = fanout.canonical_json({
        "profiles": dict(sorted(amended_inputs.provider_profiles.items())),
        "schema_version": "fanout-provider-profiles-v1",
    })
    profiles_sha256 = hashlib.sha256(profiles_document).hexdigest()
    documents = (
        ("plans", amended_inputs.compiled_plan_sha256, fanout.canonical_json(amended_plan.to_dict())),
        ("inputs", amended_inputs.digest, fanout.canonical_json(amended_inputs.to_dict())),
        ("profiles", profiles_sha256, profiles_document),
    )
    for kind, digest, data in documents:
        backend.artifacts.write_bytes(f"amendments/{kind}/{digest}.json", data)
    old_plan_sha256 = scheduler.plan_sha256
    old_inputs_digest = scheduler.inputs_digest
    amendment = scheduler.accept_amendment(
        amended_plan, amended_inputs, expected_plan_revision=1, owner=owner,
    )
    binding = dict(
        revision=amendment.plan_revision,
        old_plan_sha256=old_plan_sha256, old_inputs_digest=old_inputs_digest,
        new_plan_sha256=scheduler.plan_sha256, new_inputs_digest=scheduler.inputs_digest,
        old_profiles_sha256=profiles_sha256, new_profiles_sha256=profiles_sha256,
        owner=owner,
    )
    journal.append_amendment("plan-amendment-intent", **binding)
    journal.append_amendment("plan-amendment-accepted", **binding)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    prepared = handover.prepare_branch_handover(
        scheduler.inputs.targets["address"], candidate, verification,
        plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
        task_id="address",
    )
    journal.append_branch_blocked("address", "pre-cas-interrupted", owner=owner)
    journal_anchor = journal._anchor_store
    scheduler_anchor = scheduler._authority
    journal.close()
    backend.close()
    status = fanout.Scheduler._inspect_blocked_for_test(
        scheduler.plan, scheduler.inputs, journal.root, backend.artifacts,
        owner=owner, original_journal_inputs=original_inputs,
        journal_anchor=journal_anchor, scheduler_anchor=scheduler_anchor,
        lifecycle_controller=controller,
    )
    assert status.blocked_target_ids == ("address",)
    assert prepared.intent.plan_revision == 1
    assert original_inputs.digest != scheduler.inputs.digest
    plan_document = backend.artifacts.root / f"amendments/plans/{amended_inputs.compiled_plan_sha256}.json"
    if document_damage == "missing":
        plan_document.unlink()
    else:
        plan_document.write_bytes(b"{}")
    with pytest.raises(fanout.SchedulerStateError, match="amendment documents"):
        fanout.Scheduler._inspect_blocked_for_test(
            scheduler.plan, scheduler.inputs, journal.root, backend.artifacts,
            owner=owner, original_journal_inputs=original_inputs,
            journal_anchor=journal_anchor, scheduler_anchor=scheduler_anchor,
            lifecycle_controller=controller,
        )


@pytest.mark.parametrize("recovering", (False, True))
def test_blocked_active_handover_refuses_both_provider_round_sites(tmp_path, recovering):
    scheduler, backend, journal, _controller, owner = _scheduled_handover_dependant(tmp_path)
    scheduler.mark_active("booking", owner=owner)
    journal.append_branch_blocked("address", "git-proof-invalid", owner=owner)
    task = next(task for task in scheduler.plan.tasks if task.id == "booking")
    Packet = dataclasses.make_dataclass("Packet", ["dependency_artifacts", "context_sha256"])
    barrier = backend.artifacts.write_bytes("barriers/blocked.json", b"blocked")
    calls = []

    class Coordinator:
        def execute_round(self, *_args, **_kwargs):
            calls.append("provider")
            raise AssertionError("blocked dependent reached provider")

    service = SimpleNamespace(
        scheduler=scheduler,
        preparations={"booking": SimpleNamespace(
            packet=Packet((), "context"), seats=(), policy=SimpleNamespace(rounds=1),
            inputs=scheduler.inputs,
        )},
        coordinator=Coordinator(),
        _execution_state=lambda _task_id: SimpleNamespace(
            barriers=(barrier,) if recovering else (),
        ),
        _restore_barrier=lambda _ref, _packet: SimpleNamespace(
            status="blocked-memory", valid_terminals=(), barrier_ref=barrier,
        ),
    )
    with pytest.raises(fanout.SchedulerStateError, match="blocked handover dependency"):
        fanout.ExecutionService._drive_task(service, task, owner)
    assert calls == []


def test_deliver_creates_one_ticket_commit_and_unlocks_dependant(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, changed=True,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    terminal = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    assert isinstance(terminal, fanout.HandoverTerminalV2)
    assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == terminal.commit_oid
    assert cases._git(binding.root, "show", "-s", "--format=%P", terminal.commit_oid) == binding.base_oid
    assert cases._git(binding.root, "show", "-s", "--format=%s", terminal.commit_oid).startswith("TASK-123")
    message = cases._git(binding.root, "show", "-s", "--format=%B", terminal.commit_oid)
    assert f"Fanout-Baseline: {binding.baseline_sha256}" in message
    assert (binding.root / "README.md").read_bytes() == b"verified change\n"
    assert cases._git(binding.root, "status", "--porcelain=v1") == ""
    proof = backend.artifacts.read_bytes(terminal.evidence)
    assert str(controller.root).encode() not in proof
    assert str(prepared.transaction.transaction_root).encode() not in proof
    assert b"verified change" not in proof
    assert [item.task_id for item in scheduler.schedule_ready(owner=owner)] == ["booking"]


@pytest.mark.parametrize("distortion", ("filemode", "clean-filter", "empty-directory"))
def test_git_stage_must_represent_exact_verified_candidate(tmp_path, distortion, monkeypatch):
    entries = (
        (fanout.CandidateEntry("README.md", "file", 0o755, b"verified change\n"),)
        if distortion == "filemode" else
        (fanout.CandidateEntry("README.md", "file", 0o644, b"verified change\n"),)
        if distortion == "clean-filter" else
        (fanout.CandidateEntry("empty", "directory", 0o755, b""),)
    )
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, candidate_entries=entries,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    if distortion == "filemode":
        cases._git(binding.root, "config", "core.filemode", "false")
    elif distortion == "clean-filter":
        marker = binding.common_dir / "filter-ran"
        cases._git(binding.root, "config", "filter.fanout.clean",
                   f"sh -c 'touch {marker}; sed s/verified/transformed/'")
        (binding.common_dir / "info" / "attributes").write_text("README.md filter=fanout\n")
        invoked = []
        original_run = subprocess.run
        def observe_run(args, *positional, **keywords):
            before = marker.exists()
            result = original_run(args, *positional, **keywords)
            if not before and marker.exists():
                invoked.append(args)
            return result
        monkeypatch.setattr(subprocess, "run", observe_run)
    blocked = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    if distortion in {"filemode", "clean-filter"}:
        assert isinstance(blocked, fanout.HandoverTerminalV2)
        expected_mode = "100755" if distortion == "filemode" else "100644"
        assert cases._git(binding.root, "ls-tree", blocked.commit_oid, "README.md").startswith(f"{expected_mode} blob ")
        assert cases._git(binding.root, "show", f"{blocked.commit_oid}:README.md") == "verified change"
    else:
        assert isinstance(blocked, handover.BlockedHandover)
        assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""
    if distortion == "clean-filter":
        assert not marker.exists(), f"clean filter launched by {invoked}"


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize("phase,expected", [
    ("after-file-application", "blocked"),
    ("after-commit-tree", "blocked"),
    ("after-prepared-commit", "blocked"),
    ("after-ref-cas", "delivered"),
    ("after-head-switch", "delivered"),
    ("after-journal-terminal", "delivered"),
    ("after-scheduler-cas", "delivered"),
])
def test_interrupted_handover_never_replays_commit(tmp_path, monkeypatch, phase, expected):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def crash(at):
        if at == phase:
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
            scheduler=scheduler, controller=controller, journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    before = cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref)
    recovered = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    if expected == "blocked":
        assert isinstance(recovered, handover.BlockedHandover)
        assert before == ""
        assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""
        assert fanout.ExecutionService.v2_status(scheduler).target_states["address"] == "blocked"
        assert scheduler.schedule_ready(owner=owner) == ()
    else:
        assert isinstance(recovered, fanout.HandoverTerminalV2)
        assert before == recovered.commit_oid
        assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == before
        assert cases._git(binding.root, "rev-list", "--count", binding.spec.branch_ref, "--not", binding.base_oid) == "1"
        assert [item.task_id for item in scheduler.schedule_ready(owner=owner)] == ["booking"]


def test_ref_drift_after_prepare_records_blocked_disposition(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    cases._git(binding.root, "branch", binding.spec.branch_ref.removeprefix("refs/heads/"))
    blocked = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert journal.state.branch_blocked["address"][0] == prepared.intent.sha256
    assert fanout.ExecutionService.v2_status(scheduler).target_states["address"] == "blocked"


@pytest.mark.parametrize("alias", ("refs/heads/main", "refs/heads/hidden", "prepared"))
def test_ticket_ref_alias_inserted_at_cas_cannot_redirect_commit(tmp_path, monkeypatch, alias):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    original_git = handover._git
    def insert_alias(root, *args, **kwargs):
        if args[:1] == ("update-ref",):
            target = "refs/heads/hidden" if alias == "prepared" else alias
            if alias == "prepared":
                cases._git(root, "update-ref", target, args[3])
            cases._git(root, "symbolic-ref", binding.spec.branch_ref, target)
        return original_git(root, *args, **kwargs)
    monkeypatch.setattr(handover, "_git", insert_alias)

    blocked = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert journal.state.branch_blocked["address"][0] == prepared.intent.sha256
    assert cases._git(binding.root, "symbolic-ref", binding.spec.branch_ref) == (
        "refs/heads/hidden" if alias == "prepared" else alias
    )
    if alias == "refs/heads/main":
        assert cases._git(binding.root, "rev-parse", "main") == binding.base_oid
    elif alias == "refs/heads/hidden":
        assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", alias) == ""


def test_blocked_prepared_handle_cannot_retry_after_cold_recovery(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    blocked = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    retried = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    assert retried == blocked
    assert cases._git(
        binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref,
    ) == ""


def test_dirty_target_refuses_before_intent(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    binding = scheduler.inputs.targets["address"]
    (binding.root / "README.md").write_text("outside edit\n")
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    with pytest.raises(fanout.HandoverError, match="dirty|baseline"):
        handover.prepare_branch_handover(
            binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner, task_id="address",
        )
    assert "address" not in journal.state.branch_handovers
    assert fanout.ExecutionService.v2_status(scheduler).target_states["address"] == "candidate"


def test_existing_ticket_branch_receives_one_exact_commit(tmp_path):
    scheduler, backend, journal, controller, baseline, owner = cases._v2_handover_fixture(
        tmp_path, existing_branch=True,
    )
    scheduler.schedule_ready(owner=owner)
    scheduler.mark_active("address", owner=owner)
    scheduler.begin_reconciliation("address", owner=owner)
    bundle = fanout.CandidateBundle(baseline.digest, (), ())
    cases._reconcile_v2_candidate(scheduler, backend, bundle, owner)
    terminal = cases._deliver_v2_candidate(scheduler, backend, journal, controller, baseline, owner)
    binding = scheduler.inputs.targets["address"]
    assert binding.branch_oid == binding.base_oid
    assert cases._git(binding.root, "symbolic-ref", "HEAD") == binding.spec.branch_ref
    assert cases._git(binding.root, "rev-parse", "HEAD") == terminal.commit_oid


def test_verification_for_another_candidate_issuance_is_rejected(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, _verification = _settled_writer(tmp_path)
    binding = scheduler.inputs.targets["address"]
    second = fanout.issue_target_candidate(
        fanout.CandidateBundle(binding.baseline_sha256, (), ()),
        task_id="address", plan=scheduler.plan, inputs=scheduler.inputs,
        store=backend.artifacts, controller=controller,
    )
    verified, _receipt = fanout.verify_target_candidate(
        second, baseline=fanout.capture_repository_baseline(binding.root),
        plan=scheduler.plan, inputs=scheduler.inputs,
        store=backend.artifacts, controller=controller,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    with pytest.raises(fanout.HandoverError, match="issuance"):
        handover.prepare_branch_handover(
            binding, candidate, verified, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner, task_id="address",
        )
    assert "address" not in journal.state.branch_handovers or journal.state.branch_handovers["address"][1] is None


def test_blocked_disposition_survives_journal_and_scheduler_reopen(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    anchor = journal._anchor_store
    journal.close()
    reopened_journal = fanout.RunJournal._resume_for_test(
        journal.root, scheduler.inputs, owner, anchor_store=anchor,
    )
    reopened_controller = fanout.resume_lifecycle_controller(controller.root, controller.capability)
    reopened_scheduler = fanout.Scheduler._resume_for_test(
        scheduler.plan, scheduler.inputs, backend, backend.artifacts,
        owner=owner, anchor_store=scheduler._authority, journal=reopened_journal,
        lifecycle_controller=reopened_controller,
    )
    blocked = handover.recover_branch_handover(
        prepared.intent, plan=reopened_scheduler.plan, inputs=reopened_scheduler.inputs,
        artifacts=backend.artifacts, scheduler=reopened_scheduler,
        controller=reopened_controller, journal=reopened_journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    reopened_journal.close()
    cold_journal = fanout.RunJournal._resume_for_test(
        journal.root, scheduler.inputs, owner, anchor_store=anchor,
    )
    cold_scheduler = fanout.Scheduler._resume_for_test(
        scheduler.plan, scheduler.inputs, backend, backend.artifacts,
        owner=owner, anchor_store=scheduler._authority, journal=cold_journal,
        lifecycle_controller=reopened_controller,
    )
    assert fanout.ExecutionService.v2_status(cold_scheduler).target_states["address"] == "blocked"
    assert cold_scheduler.schedule_ready(owner=owner) == ()
    cold_journal.close()


@pytest.mark.parametrize("phase", ["after-intent", "after-association"])
def test_crash_during_prepare_reopens_as_blocked_without_git_mutation(tmp_path, monkeypatch, phase):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    def crash(at):
        if at == phase:
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        handover.prepare_branch_handover(
            binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner, task_id="address",
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    intent, terminal = journal.branch_handover_state("address")
    assert terminal is None
    blocked = handover.recover_branch_handover(
        intent, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""
    assert cases._git(binding.root, "rev-parse", "HEAD") == binding.base_oid


def test_lookalike_commit_cannot_replace_exact_prepared_oid(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def crash(at):
        if at == "after-ref-cas":
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    prepared_oid = cases._git(binding.root, "rev-parse", binding.spec.branch_ref)
    tree = cases._git(binding.root, "show", "-s", "--format=%T", prepared_oid)
    message = cases._git(binding.root, "show", "-s", "--format=%B", prepared_oid)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00 +0000",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00 +0000",
    })
    lookalike = subprocess.run(
        ("git", "-C", str(binding.root), "commit-tree", tree, "-p", binding.base_oid),
        input=(message + "\n").encode(), capture_output=True, check=True, env=environment,
    ).stdout.decode().strip()
    assert lookalike != prepared_oid
    cases._git(binding.root, "update-ref", binding.spec.branch_ref, lookalike, prepared_oid)
    blocked = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == lookalike
    assert scheduler.schedule_ready(owner=owner) == ()


def test_cold_reopen_rechecks_real_git_proof_and_ref_drift(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    terminal = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    anchor = journal._anchor_store
    journal.close()
    reopened_journal = fanout.RunJournal._resume_for_test(
        journal.root, scheduler.inputs, owner, anchor_store=anchor,
    )
    reopened_controller = fanout.resume_lifecycle_controller(controller.root, controller.capability)
    reopened_scheduler = fanout.Scheduler._resume_for_test(
        scheduler.plan, scheduler.inputs, backend, backend.artifacts,
        owner=owner, anchor_store=scheduler._authority,
        journal=reopened_journal, lifecycle_controller=reopened_controller,
    )
    assert reopened_scheduler.handover_terminal_for("address") == terminal
    assert fanout.ExecutionService.v2_status(reopened_scheduler).target_states["address"] == "delivered"
    cases._git(binding.root, "update-ref", binding.spec.branch_ref, binding.base_oid, terminal.commit_oid)
    with pytest.raises(fanout.SchedulerStateError, match="Git|proof"):
        reopened_scheduler.handover_terminal_for("address")
    reopened_journal.close()
    cold_journal = fanout.RunJournal._resume_for_test(
        journal.root, scheduler.inputs, owner, anchor_store=anchor,
    )
    with pytest.raises(fanout.SchedulerStateError, match="Git|proof"):
        fanout.Scheduler._resume_for_test(
            scheduler.plan, scheduler.inputs, backend, backend.artifacts,
            owner=owner, anchor_store=scheduler._authority,
            journal=cold_journal, lifecycle_controller=reopened_controller,
        )
    cold_journal.close()


@pytest.mark.parametrize("baseline_file_count", (0, 450))
def test_cold_recovery_after_ref_cas_finishes_exact_commit(tmp_path, monkeypatch, baseline_file_count):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, baseline_file_count=baseline_file_count,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def crash(at):
        if at == "after-ref-cas":
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    oid = cases._git(binding.root, "rev-parse", binding.spec.branch_ref)
    anchor = journal._anchor_store
    journal.close()
    cold_journal = fanout.RunJournal._resume_for_test(
        journal.root, scheduler.inputs, owner, anchor_store=anchor,
    )
    cold_controller = fanout.resume_lifecycle_controller(controller.root, controller.capability)
    cold_scheduler = fanout.Scheduler._resume_for_test(
        scheduler.plan, scheduler.inputs, backend, backend.artifacts,
        owner=owner, anchor_store=scheduler._authority, journal=cold_journal,
        lifecycle_controller=cold_controller,
    )
    terminal = handover.recover_branch_handover(
        prepared.intent, plan=cold_scheduler.plan, inputs=cold_scheduler.inputs,
        artifacts=backend.artifacts, scheduler=cold_scheduler,
        controller=cold_controller, journal=cold_journal, owner=owner,
    )
    assert terminal.commit_oid == oid
    assert cases._git(binding.root, "rev-list", "--count", binding.spec.branch_ref, "--not", binding.base_oid) == "1"
    assert cold_scheduler.handover_terminal_for("address") == terminal
    cold_journal.close()


@pytest.mark.parametrize("tamper", ("clean-filter", "filemode", "mode-only"))
def test_post_cas_recovery_checks_raw_worktree_before_head_switch(tmp_path, monkeypatch, tamper):
    mode = 0o755 if tamper in {"filemode", "mode-only"} else 0o644
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, candidate_entries=(fanout.CandidateEntry(
            "README.md", "file", mode, b"verified change\n",
        ),),
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def crash(at):
        if at == "after-ref-cas":
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    if tamper == "clean-filter":
        (binding.root / "README.md").write_text("tampered change\n")
        cases._git(binding.root, "config", "filter.fanout.clean", "sed s/tampered/verified/")
        (binding.common_dir / "info" / "attributes").write_text("README.md filter=fanout\n")
    elif tamper == "filemode":
        (binding.root / "README.md").chmod(0o644)
        cases._git(binding.root, "config", "core.filemode", "false")
    else:
        (binding.root / "README.md").chmod(0o700)
    assert cases._git(binding.root, "symbolic-ref", "--no-recurse", "HEAD") == "refs/heads/main"

    blocked = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert cases._git(binding.root, "symbolic-ref", "--no-recurse", "HEAD") == "refs/heads/main"
    assert journal.state.branch_blocked["address"][0] == prepared.intent.sha256


def test_pre_delivery_same_oid_symbolic_head_switch_is_not_overwritten(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    cases._git(binding.root, "switch", "-q", "-c", "external")
    assert cases._git(binding.root, "rev-parse", "HEAD") == binding.base_oid
    blocked = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert cases._git(binding.root, "symbolic-ref", "HEAD") == "refs/heads/external"
    assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""


def test_post_cas_recovery_preserves_external_same_oid_symbolic_head(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def crash(at):
        if at == "after-ref-cas":
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    commit = cases._git(binding.root, "rev-parse", binding.spec.branch_ref)
    cases._git(binding.root, "switch", "-q", "-c", "external")
    anchor = journal._anchor_store
    journal.close()
    cold_journal = fanout.RunJournal._resume_for_test(
        journal.root, scheduler.inputs, owner, anchor_store=anchor,
    )
    cold_controller = fanout.resume_lifecycle_controller(controller.root, controller.capability)
    cold_scheduler = fanout.Scheduler._resume_for_test(
        scheduler.plan, scheduler.inputs, backend, backend.artifacts,
        owner=owner, anchor_store=scheduler._authority, journal=cold_journal,
        lifecycle_controller=cold_controller,
    )
    blocked = handover.recover_branch_handover(
        prepared.intent, plan=cold_scheduler.plan, inputs=cold_scheduler.inputs,
        artifacts=backend.artifacts, scheduler=cold_scheduler,
        controller=cold_controller, journal=cold_journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert cases._git(binding.root, "symbolic-ref", "HEAD") == "refs/heads/external"
    assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == commit


@pytest.mark.parametrize("phase", ("before-delivery", "after-ref-cas"))
def test_direct_head_alias_cannot_be_overwritten(tmp_path, monkeypatch, phase):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    if phase == "after-ref-cas":
        def crash(at):
            if at == "after-ref-cas":
                raise SimulatedCrash()
        monkeypatch.setattr(handover, "_checkpoint", crash)
        with pytest.raises(SimulatedCrash):
            handover.deliver_branch_candidate(
                prepared, plan=scheduler.plan, inputs=scheduler.inputs,
                artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
                journal=journal, owner=owner,
            )
        monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    cases._git(binding.root, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
    cases._git(binding.root, "symbolic-ref", "HEAD", "refs/heads/alias")
    assert cases._git(binding.root, "symbolic-ref", "--no-recurse", "HEAD") == "refs/heads/alias"
    assert cases._git(binding.root, "rev-parse", "HEAD") == binding.base_oid

    blocked = (
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        ) if phase == "before-delivery" else
        handover.recover_branch_handover(
            prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert cases._git(binding.root, "symbolic-ref", "--no-recurse", "HEAD") == "refs/heads/alias"
    if phase == "before-delivery":
        assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""


def test_post_cas_symbolic_ticket_ref_is_durably_blocked(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def crash(at):
        if at == "after-ref-cas":
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    prepared_oid = cases._git(binding.root, "rev-parse", binding.spec.branch_ref)
    cases._git(binding.root, "update-ref", "refs/heads/other", prepared_oid)
    cases._git(binding.root, "symbolic-ref", binding.spec.branch_ref, "refs/heads/other")

    blocked = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert journal.state.branch_blocked["address"][0] == prepared.intent.sha256
    assert cases._git(binding.root, "symbolic-ref", binding.spec.branch_ref) == "refs/heads/other"


def test_checkout_edit_before_cas_blocks_without_ref_mutation(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def edit(at):
        if at == "after-prepared-commit":
            (binding.root / "outside.txt").write_text("outside edit\n")
    monkeypatch.setattr(handover, "_checkpoint", edit)
    blocked = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert (binding.root / "outside.txt").read_text() == "outside edit\n"
    assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""


def test_mode_only_checkout_edit_before_cas_blocks_without_ref_mutation(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def change_mode(at):
        if at == "after-prepared-commit":
            (binding.root / "README.md").chmod(0o600)
    monkeypatch.setattr(handover, "_checkpoint", change_mode)

    blocked = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert journal.state.branch_blocked["address"][0] == prepared.intent.sha256
    assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""


def test_preexisting_noncanonical_baseline_mode_is_preserved_and_proof_rechecks_it(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, baseline_mode=0o600,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    assert (binding.root / "README.md").stat().st_mode & 0o777 == 0o600
    assert cases._git(binding.root, "ls-tree", "HEAD", "README.md").startswith("100644 blob ")
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    terminal = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(terminal, fanout.HandoverTerminalV2)
    assert (binding.root / "README.md").stat().st_mode & 0o777 == 0o600
    assert scheduler.handover_terminal_for("address") == terminal
    (binding.root / "README.md").chmod(0o644)
    with pytest.raises(fanout.SchedulerStateError, match="Git|proof"):
        scheduler.handover_terminal_for("address")


@pytest.mark.parametrize("phase", ("before-cas", "after-ref-cas"))
def test_baseline_directory_mode_drift_blocks_handover(tmp_path, monkeypatch, phase):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, baseline_directory_mode=0o700,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    notes = binding.root / "notes"
    assert notes.stat().st_mode & 0o777 == 0o700
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def interrupt(at):
        if phase == "before-cas" and at == "after-prepared-commit":
            notes.chmod(0o755)
        elif phase == "after-ref-cas" and at == "after-ref-cas":
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", interrupt)
    if phase == "before-cas":
        blocked = handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
        assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""
    else:
        with pytest.raises(SimulatedCrash):
            handover.deliver_branch_candidate(
                prepared, plan=scheduler.plan, inputs=scheduler.inputs,
                artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
                journal=journal, owner=owner,
            )
        monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
        notes.chmod(0o755)
        blocked = handover.recover_branch_handover(
            prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
        assert cases._git(binding.root, "symbolic-ref", "--no-recurse", "HEAD") == "refs/heads/main"
    assert isinstance(blocked, handover.BlockedHandover)
    assert journal.state.branch_blocked["address"][0] == prepared.intent.sha256


def test_preexisting_noncanonical_directory_mode_is_preserved(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, baseline_directory_mode=0o700,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    terminal = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(terminal, fanout.HandoverTerminalV2)
    assert (binding.root / "notes").stat().st_mode & 0o777 == 0o700
    assert scheduler.handover_terminal_for("address") == terminal


def test_large_baseline_modes_remain_readable_after_ref_cas(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, baseline_file_count=450,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    terminal = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(terminal, fanout.HandoverTerminalV2)
    assert scheduler.handover_terminal_for("address") == terminal
    handover.verify_git_handover_proof(
        prepared.intent, terminal, inputs=scheduler.inputs, artifacts=backend.artifacts,
        controller=controller, journal=journal,
    )


def _prepared_mode_artifact_path(journal, controller, artifacts):
    category = "branch-handover-prepared-commit"
    name, digest = journal.branch_record_for("address", "prepared-commit")
    controller_module = importlib.import_module(f"{fanout.__name__}.controller")
    record = controller_module.read_evidence(controller, category, name, digest)
    return artifacts.root / record["mode_evidence"]["path"]


def _prepared_commit_receipt_path(journal, controller):
    name, _digest = journal.branch_record_for("address", "prepared-commit")
    return controller.root / "receipts" / name


@pytest.mark.parametrize("damage", ("tamper", "delete"))
def test_mode_artifact_damage_fails_cold_proof_closed(tmp_path, damage):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    terminal = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(terminal, fanout.HandoverTerminalV2)
    artifact = _prepared_mode_artifact_path(journal, controller, backend.artifacts)
    if damage == "tamper":
        artifact.write_bytes(b"tampered mode evidence\n")
    else:
        artifact.unlink()
    with pytest.raises(fanout.SchedulerStateError, match="proof|evidence"):
        scheduler.dependency_record_for("booking", "address")
    anchor = journal._anchor_store
    journal.close()
    cold_journal = fanout.RunJournal._resume_for_test(
        journal.root, scheduler.inputs, owner, anchor_store=anchor,
    )
    cold_controller = fanout.resume_lifecycle_controller(controller.root, controller.capability)
    with pytest.raises(fanout.HandoverError, match="mode evidence"):
        handover.verify_git_handover_proof(
            prepared.intent, terminal, inputs=scheduler.inputs, artifacts=backend.artifacts,
            controller=cold_controller, journal=cold_journal,
        )
    assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == terminal.commit_oid
    cold_journal.close()


@pytest.mark.parametrize("damage", ("tamper", "delete"))
def test_mode_artifact_damage_after_ref_cas_blocks_recovery_as_invalid_proof(tmp_path, monkeypatch, damage):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def crash(at):
        if at == "after-ref-cas":
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    committed = cases._git(binding.root, "rev-parse", binding.spec.branch_ref)
    artifact = _prepared_mode_artifact_path(journal, controller, backend.artifacts)
    if damage == "tamper":
        artifact.write_bytes(b"tampered mode evidence\n")
    else:
        artifact.unlink()
    blocked = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert blocked == handover.BlockedHandover(prepared.intent.sha256, "git-proof-invalid")
    assert journal.state.branch_blocked["address"][1] == "git-proof-invalid"
    assert scheduler.handover_terminal_for("address") is None
    with pytest.raises(fanout.SchedulerStateError, match="terminal|handover"):
        scheduler.dependency_record_for("booking", "address")
    assert cases._git(binding.root, "symbolic-ref", "--no-recurse", "HEAD") == binding.head_ref
    assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == committed


@pytest.mark.parametrize("phase", ("before-cas", "after-cas"))
@pytest.mark.parametrize("damage", ("delete", "tamper"))
def test_unreadable_prepared_commit_receipt_classifies_exact_ref_state(tmp_path, monkeypatch, phase, damage):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def crash(at):
        if at == ("after-prepared-commit" if phase == "before-cas" else "after-ref-cas"):
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    receipt_path = _prepared_commit_receipt_path(journal, controller)
    if damage == "delete":
        receipt_path.unlink()
    else:
        receipt_path.write_bytes(b"tampered prepared commit receipt\n")
    ref_before = cases._git(
        binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref,
    )
    assert (ref_before == "") == (phase == "before-cas")
    blocked = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    reason = "pre-cas-interrupted" if phase == "before-cas" else "ref-changed"
    assert blocked == handover.BlockedHandover(prepared.intent.sha256, reason)
    assert journal.state.branch_blocked["address"] == (prepared.intent.sha256, reason)
    assert scheduler.handover_terminal_for("address") is None
    with pytest.raises(fanout.SchedulerStateError, match="terminal|handover"):
        scheduler.dependency_record_for("booking", "address")
    assert cases._git(
        binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref,
    ) == ref_before
    assert cases._git(binding.root, "symbolic-ref", "--no-recurse", "HEAD") == binding.head_ref


def test_unreadable_prepared_receipt_and_symbolic_ticket_ref_never_claims_pre_cas(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def crash(at):
        if at == "after-ref-cas":
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    _prepared_commit_receipt_path(journal, controller).unlink()
    cases._git(binding.root, "symbolic-ref", binding.spec.branch_ref, "refs/heads/main")
    assert cases._git(binding.root, "symbolic-ref", "--no-recurse", binding.spec.branch_ref) == "refs/heads/main"

    blocked = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert blocked == handover.BlockedHandover(prepared.intent.sha256, "ref-changed")
    assert journal.state.branch_blocked["address"] == (prepared.intent.sha256, "ref-changed")
    assert scheduler.handover_terminal_for("address") is None
    assert cases._git(binding.root, "symbolic-ref", "--no-recurse", "HEAD") == binding.head_ref


@pytest.mark.parametrize("damage", ("receipt", "mode-artifact"))
def test_existing_branch_exact_old_ref_with_damaged_prepared_evidence_is_pre_cas(tmp_path, monkeypatch, damage):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, existing_branch=True,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def crash(at):
        if at == "after-prepared-commit":
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    if damage == "receipt":
        _prepared_commit_receipt_path(journal, controller).unlink()
    else:
        _prepared_mode_artifact_path(journal, controller, backend.artifacts).unlink()
    assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == prepared.intent.old_ref_oid

    blocked = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert blocked == handover.BlockedHandover(prepared.intent.sha256, "pre-cas-interrupted")
    assert journal.state.branch_blocked["address"] == (prepared.intent.sha256, "pre-cas-interrupted")
    assert scheduler.handover_terminal_for("address") is None
    assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == prepared.intent.old_ref_oid


def test_intact_prepared_commit_with_external_direct_ref_is_not_pre_cas(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def crash(at):
        if at == "after-prepared-commit":
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    assert handover._prepared_commit_receipt(prepared.intent, journal, controller)["commit_oid"] != binding.base_oid
    cases._git(binding.root, "update-ref", binding.spec.branch_ref, binding.base_oid)
    assert prepared.intent.old_ref_oid == "0" * 40

    blocked = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert blocked == handover.BlockedHandover(prepared.intent.sha256, "ref-changed")
    assert journal.state.branch_blocked["address"] == (prepared.intent.sha256, "ref-changed")
    assert scheduler.handover_terminal_for("address") is None
    assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == binding.base_oid
    assert cases._git(binding.root, "symbolic-ref", "--no-recurse", "HEAD") == binding.head_ref


@pytest.mark.parametrize("damage", ("tamper", "delete"))
def test_post_cas_delivery_error_with_damaged_modes_requires_exact_recovery(tmp_path, monkeypatch, damage):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def damage_at_cas(at):
        if at == "after-ref-cas":
            artifact = _prepared_mode_artifact_path(journal, controller, backend.artifacts)
            if damage == "tamper":
                artifact.write_bytes(b"tampered mode evidence\n")
            else:
                artifact.unlink()
            raise RuntimeError("injected post-CAS evidence damage")
    monkeypatch.setattr(handover, "_checkpoint", damage_at_cas)
    with pytest.raises(fanout.HandoverError, match="requires exact recovery"):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    assert "address" not in journal.state.branch_blocked
    assert cases._git(binding.root, "symbolic-ref", "--no-recurse", "HEAD") == binding.head_ref
    committed = cases._git(binding.root, "rev-parse", binding.spec.branch_ref)
    assert committed != binding.base_oid
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    blocked = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert blocked == handover.BlockedHandover(prepared.intent.sha256, "git-proof-invalid")
    assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == committed


@pytest.mark.parametrize("damage", ("tamper", "delete"))
def test_post_cas_delivery_error_with_damaged_prepared_receipt_blocks_ref_changed(tmp_path, monkeypatch, damage):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def damage_at_cas(at):
        if at == "after-ref-cas":
            receipt_path = _prepared_commit_receipt_path(journal, controller)
            if damage == "tamper":
                receipt_path.write_bytes(b"tampered prepared commit receipt\n")
            else:
                receipt_path.unlink()
            raise RuntimeError("injected post-CAS receipt damage")
    monkeypatch.setattr(handover, "_checkpoint", damage_at_cas)
    blocked = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert blocked == handover.BlockedHandover(prepared.intent.sha256, "ref-changed")
    assert journal.state.branch_blocked["address"] == (prepared.intent.sha256, "ref-changed")
    published = cases._git(binding.root, "rev-parse", binding.spec.branch_ref)
    assert published != binding.base_oid
    assert scheduler.handover_terminal_for("address") is None
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    recovered = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert recovered == blocked
    assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == published
    with pytest.raises(fanout.SchedulerStateError, match="terminal|handover"):
        scheduler.dependency_record_for("booking", "address")


@pytest.mark.parametrize("phase", ("before-cas", "after-cas"))
@pytest.mark.parametrize("extra", ("empty", "git-ignored"))
def test_extra_directory_blocks_exact_checkout(tmp_path, monkeypatch, phase, extra):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    if extra == "git-ignored":
        (binding.common_dir / "info" / "exclude").write_text("unowned-extra/\n")
    def add_extra():
        directory = binding.root / "unowned-extra"
        directory.mkdir()
        if extra == "git-ignored":
            (directory / "hidden.txt").write_text("ignored\n")
        assert cases._git(binding.root, "ls-files", "--others", "--exclude-standard") == ""
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    if phase == "before-cas":
        def add_empty(at):
            if at == "after-prepared-commit":
                add_extra()
        monkeypatch.setattr(handover, "_checkpoint", add_empty)
    terminal = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    if phase == "before-cas":
        assert isinstance(terminal, handover.BlockedHandover)
        assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""
    else:
        assert isinstance(terminal, fanout.HandoverTerminalV2)
        add_extra()
        with pytest.raises(fanout.HandoverError, match="directory|worktree|checkout"):
            handover.verify_git_handover_proof(
                prepared.intent, terminal, inputs=scheduler.inputs, artifacts=backend.artifacts,
                controller=controller, journal=journal,
            )


def test_deleting_last_tracked_child_preserves_pinned_empty_parent(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, candidate_entries=(), candidate_deleted_paths=("notes/keep.txt",),
        baseline_directory_mode=0o700,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    terminal = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert isinstance(terminal, fanout.HandoverTerminalV2)
    notes = binding.root / "notes"
    assert notes.is_dir() and not tuple(notes.iterdir())
    assert notes.stat().st_mode & 0o777 == 0o700
    assert scheduler.handover_terminal_for("address") == terminal


def test_wrong_target_envelope_and_symlink_alias_refuse_before_intent(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    with pytest.raises(fanout.HandoverError, match="target|envelope"):
        handover.prepare_branch_handover(
            binding, dataclasses.replace(candidate, target_id="booking"), verification,
            plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
            scheduler=scheduler, controller=controller, journal=journal, owner=owner,
            task_id="address",
        )
    alias = tmp_path / "address-alias"
    alias.symlink_to(binding.root, target_is_directory=True)
    with pytest.raises(fanout.HandoverError, match="target"):
        handover.prepare_branch_handover(
            dataclasses.replace(binding, root=alias), candidate, verification,
            plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
            scheduler=scheduler, controller=controller, journal=journal, owner=owner,
            task_id="address",
        )
    assert "address" not in journal.state.branch_handovers


def test_unowned_file_added_under_candidate_directory_is_never_staged(tmp_path, monkeypatch):
    entries = (
        fanout.CandidateEntry("newdir", "directory", 0o755, b""),
        fanout.CandidateEntry("newdir/owned.txt", "file", 0o644, b"owned\n"),
    )
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, candidate_entries=entries,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def add_unowned(at):
        if at == "after-file-application":
            (binding.root / "newdir" / "outside.txt").write_text("unowned\n")
    monkeypatch.setattr(handover, "_checkpoint", add_unowned)
    blocked = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""
    assert (binding.root / "newdir" / "outside.txt").read_text() == "unowned\n"


def test_candidate_file_changed_between_validation_and_staging_blocks_commit(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, changed=True,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    real_git = handover._git
    changed = False
    def edit_before_stage(root, *args, **kwargs):
        nonlocal changed
        if args[:2] == ("hash-object", "-w") and not changed:
            (binding.root / "README.md").write_text("outside edit\n")
            changed = True
        return real_git(root, *args, **kwargs)
    monkeypatch.setattr(handover, "_git", edit_before_stage)
    blocked = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    assert changed
    assert isinstance(blocked, handover.BlockedHandover)
    assert journal.branch_record_for("address", "prepared-commit") is None
    assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""
    assert (binding.root / "README.md").read_text() == "outside edit\n"


def test_recoverable_post_cas_exception_preserves_exact_recovery_path(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def fail(at):
        if at == "after-ref-cas":
            raise RuntimeError("interrupted finalization")
    monkeypatch.setattr(handover, "_checkpoint", fail)
    with pytest.raises(fanout.HandoverError, match="recovery"):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    assert "address" not in journal.state.branch_blocked
    oid = cases._git(binding.root, "rev-parse", binding.spec.branch_ref)
    terminal = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert terminal.commit_oid == oid


@pytest.mark.parametrize("phase", ("after-ref-cas", "after-journal-terminal"))
def test_recovery_scheduler_cas_conflict_does_not_block_valid_git_terminal(tmp_path, monkeypatch, phase):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    def crash(at):
        if at == phase:
            raise SimulatedCrash()
    monkeypatch.setattr(handover, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    recorded, terminal = journal.branch_handover_state("address")
    assert recorded == prepared.intent
    assert (terminal is None) == (phase == "after-ref-cas")
    backend.force_conflict = True
    with pytest.raises(fanout.SchedulerConflictError):
        handover.recover_branch_handover(
            prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    assert "address" not in journal.state.branch_blocked
    _, terminal = journal.branch_handover_state("address")
    assert terminal is not None
    backend.force_conflict = False
    anchor = journal._anchor_store
    journal.close()
    cold_journal = fanout.RunJournal._resume_for_test(
        journal.root, scheduler.inputs, owner, anchor_store=anchor,
    )
    cold_controller = fanout.resume_lifecycle_controller(controller.root, controller.capability)
    cold_scheduler = fanout.Scheduler._resume_for_test(
        scheduler.plan, scheduler.inputs, backend, backend.artifacts,
        owner=owner, anchor_store=scheduler._authority,
        journal=cold_journal, lifecycle_controller=cold_controller,
    )
    recovered = handover.recover_branch_handover(
        prepared.intent, plan=cold_scheduler.plan, inputs=cold_scheduler.inputs,
        artifacts=backend.artifacts, scheduler=cold_scheduler,
        controller=cold_controller, journal=cold_journal, owner=owner,
    )
    assert recovered == terminal
    assert cold_scheduler.handover_terminal_for("address") == terminal
    assert "address" not in cold_journal.state.branch_blocked
    cold_journal.close()


def test_git_replace_ref_cannot_rewrite_the_prepared_commit_proof(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    terminal = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    tree = cases._git(binding.root, "show", "-s", "--format=%T", terminal.commit_oid)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    replacement = subprocess.run(
        ("git", "-C", str(binding.root), "commit-tree", tree, "-p", binding.base_oid),
        input=b"replacement message\n", capture_output=True, check=True, env=environment,
    ).stdout.decode().strip()
    cases._git(binding.root, "replace", terminal.commit_oid, replacement)
    assert scheduler.handover_terminal_for("address") == terminal


def test_prepare_failure_after_intent_persists_blocked_disposition(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    def fail(*_args, **_kwargs):
        raise fanout.HandoverError("transaction unavailable")
    monkeypatch.setattr(handover, "prepare_handover_transaction", fail)
    with pytest.raises(fanout.HandoverError, match="transaction unavailable"):
        handover.prepare_branch_handover(
            binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner, task_id="address",
        )
    intent, terminal = journal.branch_handover_state("address")
    assert terminal is None
    assert journal.state.branch_blocked["address"] == (intent.sha256, "association-missing")
    assert fanout.ExecutionService.v2_status(scheduler).target_states["address"] == "blocked"


@pytest.mark.parametrize("drift", ["base", "worktree", "branch"])
def test_target_drift_between_prepare_and_delivery_blocks_without_fanout_commit(tmp_path, drift):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, existing_branch=drift == "branch",
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    (binding.root / "README.md").write_text("outside edit\n")
    if drift != "worktree":
        cases._git(binding.root, "add", "README.md")
        cases._git(binding.root, "commit", "-qm", "outside commit")
    blocked = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert fanout.ExecutionService.v2_status(scheduler).target_states["address"] == "blocked"
    assert cases._git(binding.root, "show", "-s", "--format=%s", "HEAD") != "TASK-123: apply verified address candidate"
    if drift != "branch":
        assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""


@pytest.mark.parametrize("phase,lock_failure", [
    ("delivery", "common-drift"), ("delivery", "destination-lock"),
    ("recovery", "common-drift"), ("recovery", "destination-lock"),
])
def test_post_intent_lock_failure_is_durably_blocked_and_cold_visible(
    tmp_path, monkeypatch, phase, lock_failure,
):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(
        tmp_path, file_backend=True,
    )
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    if phase == "recovery":
        def crash(at):
            if at == "after-ref-cas":
                raise SimulatedCrash()
        monkeypatch.setattr(handover, "_checkpoint", crash)
        with pytest.raises(SimulatedCrash):
            handover.deliver_branch_candidate(
                prepared, plan=scheduler.plan, inputs=scheduler.inputs,
                artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
                journal=journal, owner=owner,
            )
        monkeypatch.setattr(handover, "_checkpoint", lambda _at: None)
    if lock_failure == "common-drift":
        moved = binding.root / ".git-moved"
        binding.common_dir.rename(moved)
        binding.common_dir.mkdir()
    else:
        def unavailable(_root):
            raise fanout.HandoverError("destination lock unavailable")
        monkeypatch.setattr(handover, "_destination_lock", unavailable)
    try:
        blocked = (
            handover.deliver_branch_candidate(
                prepared, plan=scheduler.plan, inputs=scheduler.inputs,
                artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
                journal=journal, owner=owner,
            ) if phase == "delivery" else
            handover.recover_branch_handover(
                prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
                artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
                journal=journal, owner=owner,
            )
        )
    finally:
        if lock_failure == "common-drift":
            binding.common_dir.rmdir()
            moved.rename(binding.common_dir)
    assert isinstance(blocked, handover.BlockedHandover)
    assert blocked.reason == "delivery-failed"
    assert journal.state.branch_blocked["address"] == (prepared.intent.sha256, "delivery-failed")
    journal_anchor = journal._anchor_store
    scheduler_anchor = scheduler._authority
    journal.close()
    backend.close()
    status = fanout.Scheduler._inspect_blocked_for_test(
        scheduler.plan, scheduler.inputs, journal.root, backend.artifacts,
        owner=owner, original_journal_inputs=journal.inputs,
        journal_anchor=journal_anchor, scheduler_anchor=scheduler_anchor,
        lifecycle_controller=controller,
    )
    assert status.blocked_target_ids == ("address",)


def test_previously_blocked_recovery_keeps_original_reason_when_lock_unavailable(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    journal.append_branch_blocked("address", "pre-cas-interrupted", owner=owner)
    def unavailable(_root):
        raise fanout.HandoverError("destination lock unavailable")
    monkeypatch.setattr(handover, "_destination_lock", unavailable)

    blocked = handover.recover_branch_handover(
        prepared.intent, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner,
    )
    assert blocked == handover.BlockedHandover(prepared.intent.sha256, "pre-cas-interrupted")


def test_unrelated_unscheduled_amendment_keeps_settled_writer_handover_available(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    source = scheduler.result_for("address")
    original_writer = scheduler.plan.tasks[0]
    old_plan_sha256 = scheduler.plan_sha256
    old_inputs_digest = scheduler.inputs_digest
    replacement = scheduler.plan.to_dict()
    replacement["tasks"][1]["objective"] = "Revised booking objective."
    amended_plan = fanout.plan.FanoutPlanV2.from_dict(replacement)
    amended_inputs = dataclasses.replace(
        scheduler.inputs,
        compiled_plan_sha256=hashlib.sha256(fanout.canonical_json(amended_plan.to_dict())).hexdigest(),
    )
    amendment = scheduler.accept_amendment(
        amended_plan, amended_inputs, expected_plan_revision=1, owner=owner,
    )
    profile_digest = hashlib.sha256(fanout.canonical_json(dict(amended_inputs.provider_profiles))).hexdigest()
    amendment_binding = dict(
        revision=amendment.plan_revision,
        old_plan_sha256=old_plan_sha256, old_inputs_digest=old_inputs_digest,
        new_plan_sha256=scheduler.plan_sha256, new_inputs_digest=scheduler.inputs_digest,
        old_profiles_sha256=profile_digest, new_profiles_sha256=profile_digest,
        owner=owner,
    )
    journal.append_amendment("plan-amendment-intent", **amendment_binding)
    journal.append_amendment("plan-amendment-accepted", **amendment_binding)
    assert scheduler.plan_revision == 2
    assert scheduler.plan.tasks[0] == original_writer
    assert scheduler.inputs.targets["address"] == binding
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    assert prepared.intent.plan_revision == source.plan_revision == 1
    assert prepared.intent.plan_sha256 == source.plan_sha256 == old_plan_sha256
    assert prepared.intent.inputs_digest == source.inputs_digest == old_inputs_digest
    terminal = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    assert isinstance(terminal, fanout.HandoverTerminalV2)
    assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == terminal.commit_oid
    assert [item.task_id for item in scheduler.schedule_ready(owner=owner)] == ["booking"]


def test_changed_reconciled_result_blocks_after_prepare(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    replacement_artifact = backend.artifacts.write_bytes("results/address/other.json", b"other candidate")
    states = list(scheduler._record.snapshot.tasks)
    states[0] = dataclasses.replace(
        states[0], result=dataclasses.replace(states[0].result, artifact=replacement_artifact),
    )
    scheduler._record = dataclasses.replace(
        scheduler._record,
        snapshot=dataclasses.replace(scheduler._record.snapshot, tasks=tuple(states)),
    )
    blocked = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""


def test_completed_handover_rejects_reuse(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    terminal = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    with pytest.raises(fanout.HandoverError, match="completed"):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
            scheduler=scheduler, controller=controller, journal=journal, owner=owner,
        )
    assert cases._git(binding.root, "rev-parse", binding.spec.branch_ref) == terminal.commit_oid


def test_changed_lifecycle_association_blocks_without_file_rollback(tmp_path):
    scheduler, backend, journal, controller, owner, candidate, verification = _settled_writer(tmp_path)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    binding = scheduler.inputs.targets["address"]
    prepared = handover.prepare_branch_handover(
        binding, candidate, verification, plan=scheduler.plan, inputs=scheduler.inputs,
        artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id="address",
    )
    name, _digest = journal.branch_record_for("address", "association")
    (controller.root / "receipts" / name).write_bytes(b"tampered\n")
    before = prepared.transaction.journal.read_bytes()
    blocked = handover.deliver_branch_candidate(
        prepared, plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
    )
    assert isinstance(blocked, handover.BlockedHandover)
    assert prepared.transaction.journal.read_bytes() == before
    assert (binding.root / "README.md").read_bytes() == b"initial\n"
    assert cases._git(binding.root, "for-each-ref", "--format=%(objectname)", binding.spec.branch_ref) == ""
