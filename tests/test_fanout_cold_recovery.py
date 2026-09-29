"""Cold-start amendment recovery over exact durable inputs."""
from __future__ import annotations

import hashlib
import dataclasses
import json
import os
import subprocess
import shutil
import sys
from pathlib import Path

import pytest


FROZEN_V1 = Path(__file__).resolve().parent / "fixtures/fanout-v1-pre-multirepo"


def test_frozen_preupgrade_v1_status_authenticates_without_current_compilation(tmp_path):
    """Changing current compiler bytes must not erase authenticated historical status."""
    import importlib.util

    cli = Path(__file__).resolve().parents[1] / "shared/skills/llm-fanout-execute/scripts/execute.py"
    spec = importlib.util.spec_from_file_location("frozen_status_execute", cli)
    assert spec and spec.loader
    execute = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = execute
    spec.loader.exec_module(execute)
    manifest = json.loads((FROZEN_V1 / "manifest.json").read_bytes())
    assert manifest["runtime_sha256"] == "fa5b0823a016f854ab9cb7bbdcb53599fe38b5e1e92e5d79d81611c95eb78bfd"
    assert manifest["compiler_sha256"] == "6012e5badb8748b56720816552a7d08fce8274a7e274a987cb6410004803c36c"
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((FROZEN_V1 / name).read_bytes()).hexdigest() == expected
    status = execute.inspect_historical_run(FROZEN_V1)
    assert status.schema_version == "fanout-run-inputs-v2"
    assert status.scheduler_revision >= 1
    assert status.amendments[2] == "plan-amendment-accepted"
    assert status.handovers["answer"] == "handover-complete"
    assert "branch_handovers" not in status.to_dict()
    with pytest.raises(RuntimeError, match="original runtime"):
        execute.resume_run(FROZEN_V1)


def test_frozen_v1_cli_status_is_read_only_and_tampering_blocks(tmp_path):
    cli = Path(__file__).resolve().parents[1] / "shared/skills/llm-fanout-execute/scripts/execute.py"
    result = subprocess.run(
        (sys.executable, str(cli), "status", "--private-root", str(FROZEN_V1)),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["read_only"] is True
    copied = tmp_path / "frozen"
    shutil.copytree(FROZEN_V1, copied)
    with (copied / "events.jsonl").open("ab") as handle:
        handle.write(b"{}\n")
    altered = subprocess.run(
        (sys.executable, str(cli), "status", "--private-root", str(copied)),
        capture_output=True, text=True, check=False,
    )
    assert altered.returncode == 2
    assert "historical frozen fixture changed" in altered.stderr


def test_frozen_v1_cli_rejects_rehashed_scheduler_status(tmp_path):
    """A co-located manifest cannot authorize a forged scheduler revision."""
    cli = Path(__file__).resolve().parents[1] / "shared/skills/llm-fanout-execute/scripts/execute.py"
    copied = tmp_path / "frozen"
    shutil.copytree(FROZEN_V1, copied)
    record_path = copied / "scheduler-record.json"
    record = json.loads(record_path.read_bytes())
    record["revision"] += 1
    record["snapshot"]["backend_revision"] += 1
    record["snapshot"]["tasks"][0]["phase"] = "active"
    canonical = lambda value: (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    record["snapshot_sha256"] = hashlib.sha256(canonical(record["snapshot"])).hexdigest()
    record_path.write_bytes(canonical(record))
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"]["scheduler-record.json"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
    manifest_path.write_bytes(canonical(manifest))
    altered = subprocess.run(
        (sys.executable, str(cli), "status", "--private-root", str(copied)),
        capture_output=True, text=True, check=False,
    )
    assert altered.returncode == 2, altered.stdout
    assert "historical frozen fixture changed" in altered.stderr

from test_fanout_execute import (
    _Registry, _plan, _profile_amendment, _restart_before_profile_recovery,
    _repository, _task, fanout,
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_amendment_documents_are_durable_before_intent(tmp_path: Path, monkeypatch):
    """Removing the pre-intent writes must make this test fail."""
    runtime, plan, new_inputs, preparation, registry = _profile_amendment(
        tmp_path, profiled=True,
    )
    transition = runtime.service.prepare_provider_profile_transition(
        plan, new_inputs, registry, new_inputs.provider_profiles, owner=runtime.owner,
    )
    original_append = runtime.journal.append_amendment

    def observe_intent(event_type, **kwargs):
        if event_type == "plan-amendment-intent":
            plan_bytes = fanout.canonical_json(plan.to_dict())
            input_bytes = fanout.canonical_json(new_inputs.to_dict())
            assert (runtime.artifacts.root / "amendments" / "plans" /
                    f"{_digest(plan_bytes)}.json").read_bytes() == plan_bytes
            assert (runtime.artifacts.root / "amendments" / "inputs" /
                    f"{_digest(input_bytes)}.json").read_bytes() == input_bytes
            profile_bytes = fanout.canonical_json({
                "profiles": dict(sorted(new_inputs.provider_profiles.items())),
                "schema_version": "fanout-provider-profiles-v1",
            })
            assert (runtime.artifacts.root / "amendments" / "profiles" /
                    f"{_digest(profile_bytes)}.json").read_bytes() == profile_bytes
            for profile in registry._profiles.values():
                path = (runtime.artifacts.root / "amendments" / "executor-profiles" /
                        f"{profile.digest}.json")
                assert _digest(path.read_bytes()) == profile.digest
        return original_append(event_type, **kwargs)

    monkeypatch.setattr(runtime.journal, "append_amendment", observe_intent)
    runtime.service.submit_amendment(
        plan, new_inputs, {"later": preparation}, new_inputs.provider_profiles,
        expected_plan_revision=1, provider_transition=transition, owner=runtime.owner,
    )


def test_cold_recovery_loads_replacement_without_caller_plan_or_inputs(
    tmp_path: Path, monkeypatch,
):
    """Dropping the document loader must block this replay after an intent crash."""
    runtime, plan, new_inputs, preparation, registry = _profile_amendment(
        tmp_path, profiled=True,
    )
    transition = runtime.service.prepare_provider_profile_transition(
        plan, new_inputs, registry, new_inputs.provider_profiles, owner=runtime.owner,
    )
    original_append = runtime.journal.append_amendment

    def crash_after_intent(event_type, **kwargs):
        original_append(event_type, **kwargs)
        if event_type == "plan-amendment-intent":
            raise SystemExit("intent was durable")

    monkeypatch.setattr(runtime.journal, "append_amendment", crash_after_intent)
    with pytest.raises(SystemExit):
        runtime.service.submit_amendment(
            plan, new_inputs, {"later": preparation}, new_inputs.provider_profiles,
            expected_plan_revision=1, provider_transition=transition,
            owner=runtime.owner,
        )
    monkeypatch.undo()
    del transition
    service, coordinator = _restart_before_profile_recovery(runtime, plan)
    # No replacement registry object survives the restart. The descriptors are
    # reloaded from the content-addressed store using the old adapter catalog.
    recovered_registry = service.load_amendment_registry(owner=runtime.owner)
    preparation = dataclasses.replace(
        preparation, registry=recovered_registry,
    )
    amendment = service.recover_pending_amendment(
        {"later": preparation}, owner=runtime.owner,
    )
    assert amendment.plan_revision == 2
    assert runtime.backend.record.snapshot.inputs_digest == new_inputs.digest
    assert coordinator.provider_dispatches == []
    assert coordinator.registry.profile_digests == new_inputs.provider_profiles


def test_cold_registry_rebinds_new_executor_from_explicit_adapter_catalog(
    tmp_path: Path, monkeypatch,
):
    """A newly admitted CLI must not require its adapter in the old registry."""
    runtime, plan, new_inputs, preparation, registry = _profile_amendment(
        tmp_path, profiled=True,
    )

    class ExtraAdapter(fanout.ProviderAdapter):
        executor_id = "extra"
        capabilities = fanout.ProviderCapabilities(True, True, True)

    adapter = ExtraAdapter()
    extra = fanout.ExecutorProfile(
        "extra", adapter, adapter.capabilities, "read-only", "standard",
        "extra-model", "high", "1.0.0", ("extra", "run"),
        ("extra", "resume", "{session_id}"), 600, 600,
    )
    expanded_registry = fanout.ProviderRegistry(
        (*registry._profiles.values(), extra), version_probe=lambda _executor: "unused",
    )
    expanded_inputs = dataclasses.replace(
        new_inputs, provider_profiles=expanded_registry.profile_digests,
    )
    preparation = dataclasses.replace(
        preparation, inputs=expanded_inputs, registry=expanded_registry,
    )
    transition = runtime.service.prepare_provider_profile_transition(
        plan, expanded_inputs, expanded_registry, expanded_inputs.provider_profiles,
        owner=runtime.owner,
    )
    original = runtime.journal.append_amendment

    def stop_after_intent(event_type, **kwargs):
        original(event_type, **kwargs)
        if event_type == "plan-amendment-intent":
            raise SystemExit("durable intent")

    monkeypatch.setattr(runtime.journal, "append_amendment", stop_after_intent)
    with pytest.raises(SystemExit):
        runtime.service.submit_amendment(
            plan, expanded_inputs, {"later": preparation},
            expanded_inputs.provider_profiles, expected_plan_revision=1,
            provider_transition=transition, owner=runtime.owner,
        )
    monkeypatch.undo()
    service, _ = _restart_before_profile_recovery(runtime, plan)
    restored = service.load_amendment_registry(
        owner=runtime.owner, adapter_catalog=expanded_registry,
    )
    assert restored.require("extra") is adapter
    assert restored.profile_digests == expanded_inputs.provider_profiles


def test_equivocated_document_path_blocks_intent_and_provider_spend(tmp_path):
    """An occupied digest name with different bytes cannot publish an intent."""
    runtime, plan, new_inputs, preparation, registry = _profile_amendment(
        tmp_path, profiled=True,
    )
    transition = runtime.service.prepare_provider_profile_transition(
        plan, new_inputs, registry, new_inputs.provider_profiles, owner=runtime.owner,
    )
    plan_sha256 = _digest(fanout.canonical_json(plan.to_dict()))
    runtime.artifacts.write_bytes(
        f"amendments/plans/{plan_sha256}.json", b"{}\n",
    )
    with pytest.raises(fanout.ExecutionConflictError, match="equivocat"):
        runtime.service.submit_amendment(
            plan, new_inputs, {"later": preparation}, new_inputs.provider_profiles,
            expected_plan_revision=1, provider_transition=transition,
            owner=runtime.owner,
        )
    assert not runtime.journal.state.amendments
    assert runtime.scheduler.plan_revision == 1
    assert runtime.coordinator.provider_dispatches == []


def test_existing_intent_never_recreates_a_missing_document(tmp_path, monkeypatch):
    """Retrying with caller copies cannot silently repair missing durable evidence."""
    runtime, plan, new_inputs, preparation, registry = _profile_amendment(
        tmp_path, profiled=True,
    )
    transition = runtime.service.prepare_provider_profile_transition(
        plan, new_inputs, registry, new_inputs.provider_profiles, owner=runtime.owner,
    )
    original = runtime.journal.append_amendment

    def stop_after_intent(event_type, **kwargs):
        original(event_type, **kwargs)
        if event_type == "plan-amendment-intent":
            raise SystemExit("durable intent")

    monkeypatch.setattr(runtime.journal, "append_amendment", stop_after_intent)
    with pytest.raises(SystemExit):
        runtime.service.submit_amendment(
            plan, new_inputs, {"later": preparation}, new_inputs.provider_profiles,
            expected_plan_revision=1, provider_transition=transition,
            owner=runtime.owner,
        )
    monkeypatch.undo()
    (runtime.artifacts.root / "amendments" / "inputs" /
     f"{new_inputs.digest}.json").unlink()
    with pytest.raises(fanout.ExecutionConflictError, match="amendment document"):
        runtime.service.submit_amendment(
            plan, new_inputs, {"later": preparation}, new_inputs.provider_profiles,
            expected_plan_revision=1, provider_transition=transition,
            owner=runtime.owner,
        )
    assert runtime.journal.state.amendments[2].phase == "plan-amendment-intent"
    assert runtime.scheduler.plan_revision == 1
    assert runtime.coordinator.provider_dispatches == []


def test_startup_document_loader_rejects_wrong_old_journal_binding(tmp_path, monkeypatch):
    """A valid new document set cannot authorize an unrelated old revision."""
    runtime, plan, new_inputs, preparation, registry = _profile_amendment(
        tmp_path, profiled=True,
    )
    transition = runtime.service.prepare_provider_profile_transition(
        plan, new_inputs, registry, new_inputs.provider_profiles, owner=runtime.owner,
    )
    original = runtime.journal.append_amendment

    def stop_after_intent(event_type, **kwargs):
        original(event_type, **kwargs)
        if event_type == "plan-amendment-intent":
            raise SystemExit("durable intent")

    monkeypatch.setattr(runtime.journal, "append_amendment", stop_after_intent)
    with pytest.raises(SystemExit):
        runtime.service.submit_amendment(
            plan, new_inputs, {"later": preparation}, new_inputs.provider_profiles,
            expected_plan_revision=1, provider_transition=transition,
            owner=runtime.owner,
        )
    record = runtime.journal.state.amendments[2]
    record.binding = (_digest(b"unrelated old plan"),) + record.binding[1:]
    with pytest.raises(fanout.ExecutionConflictError, match="journal.*chain"):
        runtime.service.recovery_documents(runtime.journal, runtime.artifacts)


class _Memory:
    def __init__(self, artifacts):
        self.artifacts = artifacts

    def preflight(self):
        return True

    def publish(self, *_args, **_kwargs):
        raise AssertionError("provider publication is forbidden in this fixture")

    def recover(self, *_args, **_kwargs):
        raise AssertionError("provider recovery is forbidden in this fixture")

    def verify_existing(self, *_args, **_kwargs):
        raise AssertionError("provider verification is forbidden in this fixture")

    def fetch_verified(self, *_args, **_kwargs):
        raise AssertionError("provider fetch is forbidden in this fixture")


class _NoProviderCoordinator:
    def __init__(self, artifacts, journal, owner, registry):
        self.artifacts = artifacts
        self.journal = journal
        self.owner = owner
        self.registry = registry
        self.memory = _Memory(artifacts)
        self.lifecycle_controller = None
        self.repository_baseline = None

    def preflight_round(self, *_args, **_kwargs):
        raise AssertionError("action-only fixture cannot preflight providers")

    def execute_round(self, *_args, **_kwargs):
        raise AssertionError("action-only fixture cannot spend provider turns")

    def restore_barrier(self, *_args, **_kwargs):
        raise AssertionError("action-only fixture has no provider barriers")


def _changed_registry(old):
    changed = (
        dataclasses.replace(profile, default_timeout=850, profile_sha256=None)
        if profile.key == ("claude", "read-only", "standard") else profile
        for profile in old._profiles.values()
    )
    return fanout.ProviderRegistry(changed, version_probe=_version_probe)


def _version_probe(executor):
    return {"claude": "2.1.281", "codex": "0.157.1", "agy": "1.2.12"}[executor]


def _pending_preparation(
    root, plan, inputs, artifacts, registry, revision, role, *, baseline, controller,
):
    task = next(item for item in plan.tasks if item.id == "later")
    source = root / "skill-source"
    source.mkdir(exist_ok=True)
    stage_parent = root / f"staging-{role}"
    stage_parent.mkdir(exist_ok=True)
    resolver = fanout.SkillResolver((fanout.SkillRoot("tests", source, 0),))
    bundle = fanout.canonical_json({
        "schema_version": "fanout-seat-skill-bundle-v1", "skills": [],
    })
    seats = []
    for executor in ("claude", "codex"):
        workspace_parent = controller.root / "workspaces" / f"revision-{revision}"
        workspace_path = workspace_parent / "seats" / executor
        workspace = (
            fanout.SeatWorkspace(workspace_path, executor, baseline.digest)
            if workspace_path.is_dir()
            else fanout.create_seat_workspace(baseline, workspace_parent, executor)
        )
        verification = fanout.verify_seat_workspace(
            baseline, workspace, controller=controller,
        )
        admission = resolver.admit(
            (), task_id="later", seat_id=executor, provider=executor,
            session_id=f"session-{executor}",
        )
        admission = resolver.stage(admission, stage_parent / executor)
        admission = resolver.verify_engine_delivery(admission, ())
        seats.append(fanout.SeatAssignment.from_admission(
            admission, skill_bundle=bundle, artifacts=artifacts,
            workspace_verification=verification,
        ))
    packet = fanout.TaskPacket(
        run_id=inputs.run_id, task_id="later", attempt=1,
        compiled_plan=fanout.canonical_json(plan.to_dict()),
        compiled_plan_sha256=inputs.compiled_plan_sha256,
        source_markdown=b"source",
        task=fanout.canonical_json(task.to_dict()),
        task_sha256=_digest(fanout.canonical_json(task.to_dict())),
        skill_bundle=bundle, skill_manifest_sha256=inputs.skill_manifests["later"],
        dependency_artifacts=(), execution_class="read-only",
        cwd=baseline.repository,
    )
    return fanout.TaskPreparation(
        packet, tuple(seats),
        fanout.RoundPolicy.from_provider_policy(task.provider_policy or plan.defaults),
        inputs, revision,
        registry=registry if isinstance(registry, fanout.ProviderRegistry) else None,
    )


def _coordinator(
    artifacts, journal, owner, registry, *, with_work, baseline=None, controller=None,
):
    if not with_work:
        return _NoProviderCoordinator(artifacts, journal, owner, registry)
    memory = _Memory(artifacts)

    def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("provider launch is forbidden in cold recovery")

    return fanout.CollaborationCoordinator(
        artifacts=artifacts, journal=journal, owner=owner, memory=memory,
        registry=registry, provider_runner=forbidden_provider,
        repository_baseline=baseline, lifecycle_controller=controller,
    )


def _cold_fixture(tmp_path, *, old_shape="class-tier", with_work=False):
    original = _plan(
        _task("approval", execution_class="orchestrator-action"),
        _task("obsolete", execution_class="orchestrator-action", depends_on=("approval",)),
        _task("later", execution_class=("read-only" if with_work else "orchestrator-action"),
              depends_on=("approval",)),
    )
    data = original.to_dict()
    removed_step = data["tasks"][1]["source_step_ids"][0]
    data["tasks"].pop(1)
    data["tasks"][1]["source_step_ids"].append(removed_step)
    data["tasks"][1]["objective"] = "Complete the replacement action."
    replacement = fanout.FanoutPlanV1.from_dict(data)
    repo_root = _repository(tmp_path) if with_work else tmp_path / "repo"
    if not with_work:
        repo_root.mkdir(mode=0o700)
    baseline = fanout.capture_repository_baseline(repo_root) if with_work else None
    old_registry = fanout.ProviderRegistry.default(version_probe=_version_probe)
    new_registry = _changed_registry(old_registry)
    from test_fanout_execute import _inputs
    initial_inputs = (
        _inputs(original, repo_digest=None if baseline is None else baseline.digest,
                profile_digests=old_registry.profile_digests)
        if old_shape == "class-tier" else _inputs(
            original, repo_digest=None if baseline is None else baseline.digest,
        )
    )
    replacement_inputs = dataclasses.replace(
        initial_inputs,
        compiled_plan_sha256=_digest(fanout.canonical_json(replacement.to_dict())),
        provider_profiles=new_registry.profile_digests,
        profile_shape="class-tier",
    )
    run_root, authority_root = tmp_path / "run", tmp_path / "authority"
    authority = fanout.LocalAnchorAuthority.bootstrap(
        authority_root, run_root=run_root, repo_root=repo_root,
    )
    journal, owner = fanout.RunJournal.create(
        run_root, initial_inputs, anchor_store=authority,
    )
    artifacts = fanout.ArtifactStore(run_root / "artifacts")
    controller = (
        fanout.create_lifecycle_controller(run_root / "lifecycle")
        if with_work else None
    )
    scheduler_backend = fanout.FileSchedulerBackend.create(
        run_root, run_id=initial_inputs.run_id, owner=owner,
    )
    execution_backend = fanout.FileExecutionBackend.create(
        run_root, run_id=initial_inputs.run_id, owner=owner,
    )
    scheduler = fanout.Scheduler.create(
        original, initial_inputs, scheduler_backend, artifacts,
        owner=owner, anchor_store=authority,
    )
    old_catalog = (
        old_registry if old_shape == "class-tier"
        else _Registry(initial_inputs.provider_profiles)
    )
    coordinator = _coordinator(
        artifacts, journal, owner,
        old_catalog, with_work=with_work, baseline=baseline, controller=controller,
    )
    preparations = (
        {"later": _pending_preparation(
            run_root, original, initial_inputs, artifacts, old_catalog, 1, "initial",
            baseline=baseline, controller=controller,
        )} if with_work else {}
    )
    service = fanout.ExecutionService(
        plan=original, inputs=initial_inputs, preparations=preparations,
        provider_profile_digests=initial_inputs.provider_profiles,
        budget=fanout.ExecutionBudget(64), scheduler=scheduler, journal=journal,
        coordinator=coordinator, artifacts=artifacts, backend=execution_backend,
        memory_preflight=coordinator.memory.preflight,
        baseline=baseline, lifecycle_controller=controller,
    )
    assert service.start(owner=owner).tasks[0].phase == "blocked-action"
    journal.close()
    artifacts.close()
    scheduler_backend.close()
    execution_backend.close()
    return {
        "repo_root": str(repo_root), "run_root": str(run_root),
        "authority_root": str(authority_root), "owner_token": owner.export_token(),
        "initial_plan": original.to_dict(), "initial_inputs": initial_inputs.to_dict(),
        "replacement_plan": replacement.to_dict(),
        "replacement_inputs": replacement_inputs.to_dict(),
        "with_work": with_work,
        "controller_token": (
            controller.capability.export_token() if controller is not None else None
        ),
    }


def _reopen(data, *, replacement_scheduler=False):
    original = fanout.FanoutPlanV1.from_dict(data["initial_plan"])
    initial_inputs = fanout.RunInputs.from_dict(data["initial_inputs"])
    owner = fanout.OwnerCapability.from_token(data["owner_token"])
    run_root = Path(data["run_root"])
    baseline = (
        fanout.capture_repository_baseline(data["repo_root"])
        if data["with_work"] else None
    )
    controller = (
        fanout.resume_lifecycle_controller(
            run_root / "lifecycle",
            fanout.LifecycleCapability(data["controller_token"]),
        ) if data["with_work"] else None
    )
    authority = fanout.LocalAnchorAuthority(
        data["authority_root"], run_root=run_root, repo_root=data["repo_root"],
    )
    journal = fanout.RunJournal.resume(
        run_root, initial_inputs, owner, anchor_store=authority,
    )
    artifacts = fanout.ArtifactStore(run_root / "artifacts")
    scheduler_backend = fanout.FileSchedulerBackend.resume(
        run_root, run_id=initial_inputs.run_id, owner=owner,
    )
    execution_backend = fanout.FileExecutionBackend.resume(
        run_root, run_id=initial_inputs.run_id, owner=owner,
    )
    if replacement_scheduler:
        scheduler = fanout.resume_scheduler_from_amendments(
            initial_plan=original, journal=journal, backend=scheduler_backend,
            artifacts=artifacts, owner=owner, anchor_store=authority,
        )
    else:
        scheduler = fanout.Scheduler.resume(
            original, initial_inputs, scheduler_backend, artifacts,
            owner=owner, anchor_store=authority,
        )
    registry = (
        fanout.ProviderRegistry.default(version_probe=_version_probe)
        if initial_inputs.profile_shape == "class-tier"
        else _Registry(initial_inputs.provider_profiles)
    )
    coordinator = _coordinator(
        artifacts, journal, owner, registry, with_work=data["with_work"],
        baseline=baseline, controller=controller,
    )
    old_preparations = (
        {"later": _pending_preparation(
            run_root, original, initial_inputs, artifacts, registry, 1,
            "old-recover" if replacement_scheduler else "old-crash",
            baseline=baseline, controller=controller,
        )} if data["with_work"] else {}
    )
    service = fanout.ExecutionService(
        plan=original, inputs=initial_inputs, preparations=old_preparations,
        provider_profile_digests=initial_inputs.provider_profiles,
        budget=fanout.ExecutionBudget(64), scheduler=scheduler, journal=journal,
        coordinator=coordinator, artifacts=artifacts, backend=execution_backend,
        memory_preflight=coordinator.memory.preflight,
        baseline=baseline, lifecycle_controller=controller,
    )
    return service, scheduler_backend


def _child_stage(data):
    stage = data["stage"]
    service, scheduler_backend = _reopen(
        data, replacement_scheduler=stage in {"recover", "abandon"},
    )
    owner = fanout.OwnerCapability.from_token(data["owner_token"])
    if stage == "probe-old":
        assert not service.journal.state.amendments
        assert service.resume(owner=owner).plan_revision == 1
        return
    if stage == "abandon":
        if data["boundary"] == "intent":
            service.abandon_pending_amendment(owner=owner)
            assert not service.journal.state.amendments
            assert service.resume(owner=owner).plan_revision == 1
        else:
            with pytest.raises(fanout.ExecutionConflictError):
                service.abandon_pending_amendment(owner=owner)
            assert service.journal.state.amendments[2].phase == "plan-amendment-intent"
        return
    if stage == "recover":
        with pytest.raises(fanout.ExecutionPreflightError):
            service.resume(owner=owner)
        catalog = (
            fanout.ProviderRegistry.default(version_probe=_version_probe)
            if service.inputs.profile_shape == "flat" else None
        )
        replacement_preparations = {}
        registry = None
        if data["with_work"]:
            registry = service.load_amendment_registry(
                owner=owner, adapter_catalog=catalog,
            )
            documents = service.recovery_documents(service.journal, service.artifacts)
            replacement_preparations["later"] = _pending_preparation(
                Path(data["run_root"]), documents.plan, documents.inputs,
                service.artifacts, registry, 2, "replacement-recover",
                baseline=service.baseline,
                controller=service.lifecycle_controller,
            )
        amendment = service.recover_pending_amendment(
            replacement_preparations, registry=registry,
            adapter_catalog=catalog, owner=owner,
        )
        assert amendment.affected_task_ids == ("later", "obsolete")
        assert service.scheduler.plan_revision == 2
        assert service.backend.read().snapshot.inputs_digest == service.inputs.digest
        assert service.coordinator.registry.profile_digests == service.inputs.provider_profiles
        assert service.status().tasks[0].phase == "blocked-action"
        return
    replacement = fanout.FanoutPlanV1.from_dict(data["replacement_plan"])
    inputs = fanout.RunInputs.from_dict(data["replacement_inputs"])
    catalog = fanout.ProviderRegistry.default(version_probe=_version_probe)
    new_registry = _changed_registry(catalog)
    transition = service.prepare_provider_profile_transition(
        replacement, inputs, new_registry, inputs.provider_profiles, owner=owner,
    )
    boundary = data["boundary"]
    if boundary in {"intent", "acceptance"}:
        original = service.journal.append_amendment
        trigger = ("plan-amendment-intent" if boundary == "intent"
                   else "plan-amendment-accepted")

        def crash_journal(event_type, **kwargs):
            original(event_type, **kwargs)
            if event_type == trigger:
                os._exit(42)

        service.journal.append_amendment = crash_journal
    elif boundary == "scheduler":
        original = service.scheduler.accept_amendment

        def crash_scheduler(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(42)

        service.scheduler.accept_amendment = crash_scheduler
    elif boundary == "scheduler-authority-pending":
        original = scheduler_backend.compare_and_set

        def crash_scheduler_cas(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(42)

        scheduler_backend.compare_and_set = crash_scheduler_cas
    elif boundary in {"execution-cas", "execution-cas-failure"}:
        original = service.backend.compare_and_set

        def crash_execution_cas(expected_revision, snapshot, *, owner):
            if snapshot.plan_revision == 2:
                if boundary == "execution-cas-failure":
                    os._exit(42)
                original(expected_revision, snapshot, owner=owner)
                os._exit(42)
            return original(expected_revision, snapshot, owner=owner)

        service.backend.compare_and_set = crash_execution_cas
    new_preparations = (
        {"later": _pending_preparation(
            Path(data["run_root"]), replacement, inputs, service.artifacts,
            new_registry, 2, "replacement-crash",
            baseline=service.baseline,
            controller=service.lifecycle_controller,
        )} if data["with_work"] else {}
    )
    service.submit_amendment(
        replacement, inputs, new_preparations, inputs.provider_profiles,
        expected_plan_revision=1, provider_transition=transition, owner=owner,
    )
    raise AssertionError("injected boundary did not interrupt amendment")


@pytest.mark.parametrize("old_shape", ("class-tier", "flat"))
@pytest.mark.parametrize("boundary", (
    "intent", "scheduler-authority-pending", "scheduler", "acceptance",
    "execution-cas-failure", "execution-cas",
))
def test_fresh_process_recovers_every_amendment_boundary(tmp_path, boundary, old_shape):
    """Replacing the old/new authority replay with in-memory state must fail."""
    data = _cold_fixture(tmp_path, old_shape=old_shape)
    child = subprocess.run(
        [sys.executable, __file__], input=json.dumps({**data, "stage": "crash", "boundary": boundary}),
        text=True, capture_output=True, check=False,
    )
    assert child.returncode == 42, child.stderr
    child = subprocess.run(
        [sys.executable, __file__], input=json.dumps({**data, "stage": "recover"}),
        text=True, capture_output=True, check=False,
    )
    assert child.returncode == 0, child.stderr


@pytest.mark.parametrize("boundary", ("intent", "scheduler", "execution-cas"))
@pytest.mark.parametrize("old_shape", ("class-tier", "flat"))
def test_cold_recovery_recreates_pending_seat_preparation_without_launch(
    tmp_path, boundary, old_shape,
):
    """A pending real preflight must survive object loss and profile change."""
    data = _cold_fixture(tmp_path, with_work=True, old_shape=old_shape)
    crashed = subprocess.run(
        [sys.executable, __file__],
        input=json.dumps({**data, "stage": "crash", "boundary": boundary}),
        text=True, capture_output=True, check=False,
    )
    assert crashed.returncode == 42, crashed.stderr
    reopened = subprocess.run(
        [sys.executable, __file__], input=json.dumps({**data, "stage": "recover"}),
        text=True, capture_output=True, check=False,
    )
    assert reopened.returncode == 0, reopened.stderr


@pytest.mark.parametrize("kind,damage", (
    ("plans", "missing"), ("inputs", "tampered"),
    ("profiles", "missing"), ("executor-profiles", "tampered"),
))
def test_cold_recovery_refuses_missing_or_changed_documents(tmp_path, kind, damage):
    """Removing any replacement document must stop transition and all provider spend."""
    data = _cold_fixture(tmp_path)
    child = subprocess.run(
        [sys.executable, __file__],
        input=json.dumps({**data, "stage": "crash", "boundary": "intent"}),
        text=True, capture_output=True, check=False,
    )
    assert child.returncode == 42, child.stderr
    plan = fanout.FanoutPlanV1.from_dict(data["replacement_plan"])
    inputs = fanout.RunInputs.from_dict(data["replacement_inputs"])
    digests = {
        "plans": _digest(fanout.canonical_json(plan.to_dict())),
        "inputs": inputs.digest,
        "profiles": _digest(fanout.canonical_json({
            "profiles": dict(sorted(inputs.provider_profiles.items())),
            "schema_version": "fanout-provider-profiles-v1",
        })),
        "executor-profiles": next(iter(inputs.provider_profiles.values())),
    }
    target = (Path(data["run_root"]) / "artifacts" / "amendments" /
              kind / f"{digests[kind]}.json")
    assert target.is_file()
    if damage == "missing":
        target.unlink()
    else:
        target.write_bytes(b"{}\n")
    child = subprocess.run(
        [sys.executable, __file__], input=json.dumps({**data, "stage": "recover"}),
        text=True, capture_output=True, check=False,
    )
    assert child.returncode != 0
    assert "amendment document" in child.stderr


@pytest.mark.parametrize("boundary", ("intent", "scheduler"))
def test_abandon_requires_both_authorities_old(tmp_path, boundary):
    """A scheduler CAS makes owner abandonment illegal even before execution CAS."""
    data = _cold_fixture(tmp_path)
    crashed = subprocess.run(
        [sys.executable, __file__],
        input=json.dumps({**data, "stage": "crash", "boundary": boundary}),
        text=True, capture_output=True, check=False,
    )
    assert crashed.returncode == 42, crashed.stderr
    reopened = subprocess.run(
        [sys.executable, __file__],
        input=json.dumps({**data, "stage": "abandon", "boundary": boundary}),
        text=True, capture_output=True, check=False,
    )
    assert reopened.returncode == 0, reopened.stderr
    if boundary == "intent":
        replay = subprocess.run(
            [sys.executable, __file__],
            input=json.dumps({**data, "stage": "probe-old"}),
            text=True, capture_output=True, check=False,
        )
        assert replay.returncode == 0, replay.stderr


def test_abandon_refuses_scheduler_cas_success_with_lost_response(tmp_path, monkeypatch):
    """A stale scheduler object must not erase a durable new revision's intent."""
    data = _cold_fixture(tmp_path)
    service, scheduler_backend = _reopen(data)
    owner = fanout.OwnerCapability.from_token(data["owner_token"])
    replacement = fanout.FanoutPlanV1.from_dict(data["replacement_plan"])
    inputs = fanout.RunInputs.from_dict(data["replacement_inputs"])
    catalog = fanout.ProviderRegistry.default(version_probe=_version_probe)
    new_registry = _changed_registry(catalog)
    transition = service.prepare_provider_profile_transition(
        replacement, inputs, new_registry, inputs.provider_profiles, owner=owner,
    )
    original = scheduler_backend.compare_and_set

    def lost_response(*args, **kwargs):
        original(*args, **kwargs)
        raise fanout.SchedulerConflictError("response lost after durable CAS")

    monkeypatch.setattr(scheduler_backend, "compare_and_set", lost_response)
    with pytest.raises(fanout.SchedulerConflictError, match="response lost"):
        service.submit_amendment(
            replacement, inputs, {}, inputs.provider_profiles,
            expected_plan_revision=1, provider_transition=transition, owner=owner,
        )
    assert service.scheduler.plan_revision == 1
    assert scheduler_backend.read().snapshot.plan_revision == 2
    status = service.status()
    assert status.authority_state == "unresolved"
    assert status.plan_revision == 0
    assert status.scheduler_revision == 0
    assert status.tasks == ()
    with pytest.raises(fanout.ExecutionConflictError, match="scheduler"):
        service.abandon_pending_amendment(owner=owner)
    assert service.journal.state.amendments[2].phase == "plan-amendment-intent"


def test_missing_descriptor_blocks_pending_authority_resolution(tmp_path):
    """Startup must authenticate all docs before it rolls authority forward."""
    data = _cold_fixture(tmp_path)
    crashed = subprocess.run(
        [sys.executable, __file__],
        input=json.dumps({**data, "stage": "crash", "boundary": "scheduler-authority-pending"}),
        text=True, capture_output=True, check=False,
    )
    assert crashed.returncode == 42, crashed.stderr
    digest = next(iter(data["replacement_inputs"]["provider_profiles"].values()))
    descriptor = (Path(data["run_root"]) / "artifacts" / "amendments" /
                  "executor-profiles" / f"{digest}.json")
    descriptor.unlink()
    authority_root = Path(data["authority_root"])
    before = {item.name: item.read_bytes() for item in authority_root.iterdir()
              if item.is_file()}
    reopened = subprocess.run(
        [sys.executable, __file__], input=json.dumps({**data, "stage": "recover"}),
        text=True, capture_output=True, check=False,
    )
    after = {item.name: item.read_bytes() for item in authority_root.iterdir()
             if item.is_file()}
    assert reopened.returncode != 0
    assert "amendment document" in reopened.stderr
    assert after == before


if __name__ == "__main__":
    _child_stage(json.load(sys.stdin))
