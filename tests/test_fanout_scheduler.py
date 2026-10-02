"""Deterministic dependency-ready scheduling contracts."""
from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_scheduler_contracts", FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)


ActionBarrier = fanout.ActionBarrier
ArtifactRef = fanout.ArtifactRef
ArtifactStore = fanout.ArtifactStore
AnchorRevision = fanout.AnchorRevision
BackendRecord = fanout.BackendRecord
OwnerCapability = fanout.OwnerCapability
ReconciledResult = fanout.ReconciledResult
RunInputs = fanout.RunInputs
RunAuthorizationError = fanout.RunAuthorizationError
Scheduler = fanout.Scheduler
SchedulerConflictError = fanout.SchedulerConflictError
SchedulerStateError = fanout.SchedulerStateError
WorkDispatch = fanout.WorkDispatch
scheduler_module = sys.modules[f"{SPEC.name}.scheduler"]
runstate_module = sys.modules[f"{SPEC.name}.runstate"]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _work(task_id: str, *, depends_on: tuple[str, ...] = (), parent_id: str | None = None,
          execution_class: str = "read-only", seats: int = 3) -> dict[str, object]:
    policy = None
    if execution_class != "orchestrator-action":
        executors = ["claude", "codex", "agy"]
        executors.extend(f"executor-{index}" for index in range(4, seats + 1))
        policy = {
            "executor_ids": executors[:seats],
            "rounds": 2, "timeout": 120, "retries": 0,
            "minimum_success": 2,
        }
    return {
        "id": task_id, "kind": "work", "parent_id": parent_id,
        "title": task_id.title(), "objective": f"Complete {task_id}.",
        "source_step_ids": [], "depends_on": list(depends_on),
        "execution_class": execution_class, "required_skills": [],
        "none_reason": ("Owner performs this action." if execution_class == "orchestrator-action"
                        else "No specialist skill is needed."),
        "owned_paths": [], "acceptance": [f"{task_id} is complete."],
        "checks": [], "provider_policy": policy,
    }


def _group(task_id: str, *, parent_id: str | None = None,
           skills: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "id": task_id, "kind": "group", "parent_id": parent_id,
        "title": task_id.title(), "objective": f"Organize {task_id}.",
        "required_skills": list(skills),
    }


def _plan(*tasks: dict[str, object]):
    data: dict[str, object] = {
        "schema_version": "v1",
        "source": {
            "path": "docs/superpowers/plans/scheduler.md",
            "sha256": _hash("scheduler source"), "parser_version": "parser-v1",
        },
        "defaults": {
            "executor_ids": ["claude", "codex", "agy"], "rounds": 2,
            "timeout": 120, "retries": 0, "minimum_success": 2,
        },
        "source_steps": [],
        "tasks": copy.deepcopy(list(tasks)),
    }
    step_number = 0
    for task in data["tasks"]:  # type: ignore[index]
        if task["kind"] != "work":
            continue
        step_number += 1
        source_id = f"Task 1/Step {step_number}"
        data["source_steps"].append({"id": source_id, "sha256": _hash(source_id)})  # type: ignore[union-attr]
        task["source_step_ids"] = [source_id]
    return fanout.FanoutPlanV1.from_dict(data)


def _result(task_id: str, text: str | None = None) -> ArtifactRef:
    payload = (text or task_id).encode()
    return ArtifactRef(
        path=f"results/{task_id}.json", digest=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
    )


class MemoryBackend:
    """Exact CAS backend; authorization remains outside scheduler process state."""

    def __init__(self, owner: OwnerCapability) -> None:
        self.owner = owner
        self.record: BackendRecord | None = None
        self.force_conflict = False
        self.fail_after_mutation = False
        self.records: list[BackendRecord] = []
        self._temporary = tempfile.TemporaryDirectory(
            prefix="fanout-scheduler-test-", dir="/private/tmp",
        )
        self.artifacts = ArtifactStore(Path(self._temporary.name) / "artifacts")
        self.artifact_index = 0

    def identity(self) -> str:
        return "memory-scheduler-backend/v1"

    def key(self) -> str:
        return "scheduler-state/run"

    def read(self) -> BackendRecord | None:
        return self.record

    def compare_and_set(self, expected_revision: int, snapshot, *, owner) -> BackendRecord:
        if owner is not self.owner:
            raise RunAuthorizationError("invalid scheduler owner")
        actual = 0 if self.record is None else self.record.revision
        if self.force_conflict or expected_revision != actual:
            raise SchedulerConflictError("injected backend conflict")
        self.record = BackendRecord(actual + 1, snapshot)
        self.records.append(self.record)
        if self.fail_after_mutation:
            self.fail_after_mutation = False
            raise RuntimeError("ambiguous backend response")
        return self.record


class MemoryAnchorStore:
    """Process-external monotonic authority behind Task 8's private test adapter."""

    def __init__(self) -> None:
        self.identity = "scheduler-anchor-service"
        self.records: dict[str, AnchorRevision] = {}
        self.cas_calls = 0
        self.fail_create = False
        self.crash_create_before_mutation = False
        self.crash_create_after_mutation = False
        self.fail_cas_call: int | None = None
        self.mutate_then_fail_cas_call: int | None = None
        self.skip_after_mutation_cas_call: int | None = None

    def create(self, key: str, value: bytes) -> AnchorRevision:
        if self.fail_create or self.crash_create_before_mutation:
            if self.crash_create_before_mutation:
                raise SimulatedCrash("crash before authority create mutation")
            raise RuntimeError("injected authority create failure")
        if key in self.records:
            raise RuntimeError("exists")
        result = AnchorRevision(1, value)
        self.records[key] = result
        if self.crash_create_after_mutation:
            raise SimulatedCrash("crash after authority create mutation")
        return result

    def read(self, key: str) -> AnchorRevision:
        return self.records[key]

    def compare_and_set(self, key: str, expected_revision: int, value: bytes) -> AnchorRevision:
        self.cas_calls += 1
        if self.fail_cas_call == self.cas_calls:
            raise RuntimeError("injected authority CAS failure")
        current = self.records[key]
        if current.revision != expected_revision:
            raise RuntimeError("stale")
        result = AnchorRevision(expected_revision + 1, value)
        if self.skip_after_mutation_cas_call == self.cas_calls:
            result = AnchorRevision(expected_revision + 2, value)
        self.records[key] = result
        if self.mutate_then_fail_cas_call == self.cas_calls:
            raise RuntimeError("ambiguous authority CAS response")
        return result


class SimulatedCrash(BaseException):
    """Process death after a durable mutation, outside normal exception recovery."""


@pytest.fixture
def owner() -> OwnerCapability:
    return OwnerCapability.from_token("o" * 43)


def _inputs(plan, **overrides) -> RunInputs:
    executor_ids = {
        executor_id
        for task in plan.tasks if task.kind == "work" and task.execution_class != "orchestrator-action"
        for executor_id in (task.provider_policy or plan.defaults).executor_ids
    }
    values = {
        "run_id": "run-1",
        "compiled_plan_sha256": hashlib.sha256(fanout.canonical_json(plan.to_dict())).hexdigest(),
        "source_sha256": plan.source.sha256,
        "draft_sha256": _hash("draft"),
        "compiler_sha256": _hash("compiler"),
        "parser_sha256": _hash("parser"),
        "provider_profiles": (
            {item: _hash(f"profile:{item}") for item in executor_ids}
            or {"orchestrator": _hash("profile:orchestrator")}
        ),
        "skill_manifests": {
            task.id: _hash(f"skills:{task.id}")
            for task in plan.tasks if task.kind == "work" and task.execution_class != "orchestrator-action"
        } or {"orchestrator": _hash("skills:orchestrator")},
        "repo_baseline_sha256": _hash("repo"),
    }
    values.update(overrides)
    return RunInputs(**values)


def _scheduler(plan, owner: OwnerCapability, *, inputs: RunInputs | None = None,
               backend: MemoryBackend | None = None,
               anchor_store: MemoryAnchorStore | None = None) -> tuple[Scheduler, MemoryBackend]:
    backend = backend or MemoryBackend(owner)
    inputs = inputs or _inputs(plan)
    anchor_store = anchor_store or MemoryAnchorStore()
    backend.inputs = inputs
    backend.anchor_store = anchor_store
    backend.authority = runstate_module._test_anchor_authority(anchor_store)
    scheduler = Scheduler._create_for_test(
        plan, inputs, backend, backend.artifacts, owner=owner,
        anchor_store=backend.authority,
    )
    return scheduler, backend


def _resume(plan, backend: MemoryBackend, *, inputs: RunInputs | None = None,
            owner: OwnerCapability | None = None) -> Scheduler:
    resumed = Scheduler._resume_for_test(
        plan, inputs or backend.inputs, backend, backend.artifacts,
        owner=owner or backend.owner, anchor_store=backend.authority,
    )
    return resumed


def _receipt(scheduler: Scheduler, backend: MemoryBackend, task_id: str,
             text: str | None = None) -> ReconciledResult:
    backend.artifact_index += 1
    payload = (text or task_id).encode()
    ref = backend.artifacts.write_bytes(
        f"results/{task_id}-{backend.artifact_index}.json", payload,
    )
    return scheduler.result_receipt(task_id, ref)


def _git(repo: Path, *args: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    result = subprocess.run(("git", "-C", str(repo), *args), check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    return result.stdout.decode().strip()


def _v2_handover_fixture(tmp_path: Path, *, dependency_mode: str = "handover",
                         read_only_only: bool = False, existing_branch: bool = False,
                         file_backend: bool = False, independent_count: int = 0,
                         descendant: bool = False, recoverable_amendments: bool = False,
                         baseline_mode: int | None = None,
                         baseline_directory_mode: int | None = None,
                         baseline_file_count: int = 0):
    repo = tmp_path / "address"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "remote.origin.url", "https://github.com/example/address.git")
    (repo / "README.md").write_text("initial\n")
    if baseline_file_count:
        (repo / "bulk").mkdir()
        for number in range(baseline_file_count):
            (repo / "bulk" / f"{number:04d}-{'x' * 150}.txt").write_text("pinned\n")
    if baseline_directory_mode is not None:
        (repo / "notes").mkdir()
        (repo / "notes" / "keep.txt").write_text("keep\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    if baseline_mode is not None:
        (repo / "README.md").chmod(baseline_mode)
    if baseline_directory_mode is not None:
        (repo / "notes").chmod(baseline_directory_mode)
    if existing_branch:
        _git(repo, "switch", "-q", "-c", "feat/TASK-123-address")
    baseline = fanout.capture_repository_baseline(repo)
    address_work = _work("address", execution_class="repo-write", seats=2)
    address_work["checks"] = [fanout.PlanCheckV1(
        argv=(sys.executable, "-c", "pass"), cwd="", env_allowlist=(),
        timeout=5, accepted_exit_codes=(0,), expected_artifacts=(),
    ).to_dict()]
    extra_ids = ("analytics", "reports")[:independent_count]
    source = (_plan(_work("events", seats=2)) if read_only_only else
              _plan(address_work,
                    _work("booking", depends_on=("address",), seats=2),
                    _work("events", seats=2),
                    *(_work(task_id, seats=2) for task_id in extra_ids),
                    *([_work("shipping", depends_on=("booking",), seats=2)] if descendant else [])))
    document = source.to_dict()
    specs = {
        target_id: fanout.TargetSpec(
            target_id, f"github.com/example/{target_id}", "TASK-123",
            f"refs/heads/feat/TASK-123-{target_id}",
        ) for target_id in (("events",) if read_only_only else
                            ("address", "booking", "events", *extra_ids,
                             *(("shipping",) if descendant else ())))
    }
    document["schema_version"] = "v2"
    document["targets"] = [spec.to_dict() for spec in specs.values()]
    for index, task in enumerate(document["tasks"]):
        task["target_id"] = task["id"]
        task["dependency_modes"] = ({"address": dependency_mode} if task["id"] == "booking" else
                                    {"booking": "artifact"} if task["id"] == "shipping" else {})
        source_id = f"Task {index + 1}/Step 1"
        task["source_step_ids"] = [source_id]
        document["source_steps"][index]["id"] = source_id
        document["source_steps"][index]["target_id"] = task["id"]
    plan = fanout.plan.FanoutPlanV2.from_dict(document)
    bindings = {
        target_id: fanout.TargetBinding(
            spec, repo if target_id == "address" else tmp_path / target_id,
            repo / ".git" if target_id == "address" else tmp_path / target_id / ".git",
            1, index + 1, _git(repo, "rev-parse", "HEAD"), None,
            baseline.digest if target_id == "address" else _hash(target_id),
            "refs/heads/main",
        ) for index, (target_id, spec) in enumerate(specs.items())
    }
    if "address" in bindings:
        bindings["address"] = fanout.bind_captured_baseline(
            fanout.resolve_target(specs["address"], repo), baseline,
        )
    descriptors = {
        f"{executor}/{execution_class}/standard": {
            "executor_id": executor, "execution_class": execution_class,
            "quality_tier": "standard",
        }
        for executor in ("claude", "codex") for execution_class in ("repo-write", "read-only")
    }
    profiles = {
        key: (hashlib.sha256(fanout.canonical_json(value)).hexdigest()
              if recoverable_amendments else _hash(f"{value['executor_id']}:{value['execution_class']}"))
        for key, value in descriptors.items()
    }
    inputs = RunInputs(
        "run-v2-scheduler", hashlib.sha256(fanout.canonical_json(plan.to_dict())).hexdigest(),
        plan.source.sha256, _hash("draft"), _hash("compiler"), _hash("parser"),
        profiles, {task.id: _hash(task.id) for task in plan.tasks if task.kind == "work"},
        profile_shape="class-tier", targets=bindings,
    )
    owner = OwnerCapability.from_token("o" * 43)
    journal_authority = runstate_module._test_anchor_authority(MemoryAnchorStore())
    journal, _ = fanout.RunJournal._create_for_test(
        tmp_path / "run", inputs, anchor_store=journal_authority, owner_capability=owner,
    )
    backend = (fanout.FileSchedulerBackend.create(tmp_path / "run", run_id=inputs.run_id, owner=owner)
               if file_backend else MemoryBackend(owner))
    backend.artifacts = ArtifactStore(tmp_path / "run" / "artifacts")
    if recoverable_amendments:
        for key, descriptor in descriptors.items():
            digest = profiles[key]
            backend.artifacts.write_bytes(
                f"amendments/executor-profiles/{digest}.json",
                fanout.canonical_json(descriptor),
            )
    controller = fanout.create_lifecycle_controller(tmp_path / "controller")
    scheduler = Scheduler._create_for_test(
        plan, inputs, backend, backend.artifacts, owner=owner,
        anchor_store=runstate_module._test_anchor_authority(MemoryAnchorStore()),
        journal=journal, lifecycle_controller=controller,
    )
    return scheduler, backend, journal, controller, baseline, owner


def _v2_terminal(scheduler, backend, journal, controller, baseline, owner):
    binding = scheduler.inputs.targets["address"]
    bundle = fanout.CandidateBundle(binding.baseline_sha256, (), ())
    candidate = fanout.issue_target_candidate(
        bundle, task_id="address", plan=scheduler.plan, inputs=scheduler.inputs,
        store=backend.artifacts, controller=controller,
    )
    verification, receipt = fanout.verify_target_candidate(
        candidate, baseline=baseline, plan=scheduler.plan, inputs=scheduler.inputs,
        store=backend.artifacts, controller=controller,
    )
    intent = fanout.BranchHandoverIntentV2(
        scheduler.inputs.run_id, scheduler.plan_revision, scheduler.plan_sha256,
        scheduler.inputs_digest, "address", "address", binding.spec.repository,
        binding.spec.branch_ref, binding.base_oid, "0" * 40,
        candidate.payload, verification.payload, binding.base_oid,
        _hash("index"), "a" * 40,
    )
    evidence = backend.artifacts.write_bytes("handover/address.json", b"delivered")
    unsigned = {
        "schema_version": "fanout-branch-handover-terminal-v2",
        "intent_sha256": intent.sha256, "commit_oid": "b" * 40,
        "evidence": {"path": evidence.path, "digest": evidence.digest, "size": evidence.size},
        "candidate_sha256": bundle.digest,
    }
    terminal = fanout.HandoverTerminalV2(
        intent.sha256, "b" * 40, evidence, bundle.digest,
        hashlib.sha256(fanout.canonical_json(unsigned)).hexdigest(),
    )
    return intent, terminal, bundle


def _reconcile_v2_candidate(scheduler, backend, bundle, owner):
    result = backend.artifacts.write_bytes(
        f"results/address/{bundle.digest}/candidate.json", bundle.manifest_bytes,
    )
    scheduler.complete_reconciliation(
        "address", scheduler.result_receipt("address", result), owner=owner,
    )


def _deliver_v2_candidate(scheduler, backend, journal, controller, baseline, owner):
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    bundle = fanout.CandidateBundle(baseline.digest, (), ())
    candidate = fanout.issue_target_candidate(
        bundle, task_id="address", plan=scheduler.plan, inputs=scheduler.inputs,
        store=backend.artifacts, controller=controller,
    )
    verification, _receipt = fanout.verify_target_candidate(
        candidate, baseline=baseline, plan=scheduler.plan, inputs=scheduler.inputs,
        store=backend.artifacts, controller=controller,
    )
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
    return terminal


def test_v2_handover_edge_waits_for_authenticated_terminal(tmp_path):
    scheduler, backend, journal, controller, baseline, owner = _v2_handover_fixture(tmp_path)
    assert [item.task_id for item in scheduler.schedule_ready(owner=owner)] == ["address", "events"]
    scheduler.mark_active("address", owner=owner)
    scheduler.begin_reconciliation("address", owner=owner)
    bundle = fanout.CandidateBundle(baseline.digest, (), ())
    _reconcile_v2_candidate(scheduler, backend, bundle, owner)
    assert scheduler.schedule_ready(owner=owner) == ()
    terminal = _deliver_v2_candidate(scheduler, backend, journal, controller, baseline, owner)
    decision, = scheduler.schedule_ready(owner=owner)
    assert decision.task_id == "booking"
    assert decision.dependencies[0].kind == "handover"
    assert decision.dependencies[0].artifact == terminal.evidence
    assert decision.dependencies[0].receipt_sha256 == terminal.terminal_sha256


def test_v2_unjournaled_terminal_cannot_unlock_dependency(tmp_path):
    scheduler, backend, journal, controller, baseline, owner = _v2_handover_fixture(tmp_path)
    scheduler.schedule_ready(owner=owner)
    scheduler.mark_active("address", owner=owner)
    scheduler.begin_reconciliation("address", owner=owner)
    _intent, terminal, bundle = _v2_terminal(scheduler, backend, journal, controller, baseline, owner)
    _reconcile_v2_candidate(scheduler, backend, bundle, owner)
    revision = scheduler.revision
    with pytest.raises(SchedulerStateError, match="journal|terminal"):
        scheduler.mark_handover_terminal("address", terminal, owner=owner)
    assert scheduler.revision == revision
    assert scheduler.schedule_ready(owner=owner) == ()


def test_v2_artifact_edge_unlocks_on_reconciliation_without_handover(tmp_path):
    scheduler, backend, _journal, _controller, _baseline, owner = _v2_handover_fixture(
        tmp_path, dependency_mode="artifact",
    )
    scheduler.schedule_ready(owner=owner)
    scheduler.mark_active("address", owner=owner)
    scheduler.begin_reconciliation("address", owner=owner)
    receipt = _receipt(scheduler, backend, "address", "verified address bytes")
    scheduler.complete_reconciliation("address", receipt, owner=owner)
    decision, = scheduler.schedule_ready(owner=owner)
    assert decision.task_id == "booking"
    assert decision.dependencies[0].kind == "artifact"
    assert decision.dependencies[0].artifact == receipt.artifact
    assert decision.dependencies[0].receipt_sha256 == fanout.reconciled_receipt_sha256(receipt)


def test_v2_failed_target_blocks_dependant_but_keeps_independent_work(tmp_path):
    scheduler, _backend, _journal, _controller, _baseline, owner = _v2_handover_fixture(tmp_path)
    scheduler.schedule_ready(owner=owner)
    scheduler.fail_task("address", "verified failure", owner=owner)
    assert scheduler.task_phase("booking") == "blocked-dependency"
    assert scheduler.task_phase("events") == "scheduled"
    assert scheduler.schedule_ready(owner=owner) == ()


def test_v2_amendment_cannot_retarget_completed_handover(tmp_path):
    scheduler, backend, journal, controller, baseline, owner = _v2_handover_fixture(tmp_path)
    scheduler.schedule_ready(owner=owner)
    scheduler.mark_active("address", owner=owner)
    scheduler.begin_reconciliation("address", owner=owner)
    bundle = fanout.CandidateBundle(baseline.digest, (), ())
    _reconcile_v2_candidate(scheduler, backend, bundle, owner)
    _deliver_v2_candidate(scheduler, backend, journal, controller, baseline, owner)
    replacement = scheduler.plan.to_dict()
    replacement["tasks"][0]["target_id"] = "events"
    replacement["source_steps"][0]["target_id"] = "events"
    plan = fanout.plan.FanoutPlanV2.from_dict(replacement)
    inputs = replace(
        scheduler.inputs,
        compiled_plan_sha256=hashlib.sha256(fanout.canonical_json(plan.to_dict())).hexdigest(),
    )
    revision = scheduler.revision
    with pytest.raises(SchedulerStateError, match="retarget|live work"):
        scheduler.accept_amendment(
            plan, inputs, expected_plan_revision=scheduler.plan_revision, owner=owner,
        )
    assert scheduler.revision == revision


def test_v2_amendment_rejects_changed_unscheduled_target_binding(tmp_path):
    scheduler, _backend, _journal, _controller, _baseline, owner = _v2_handover_fixture(
        tmp_path, dependency_mode="artifact",
    )
    replacement = scheduler.plan.to_dict()
    replacement["tasks"][2]["objective"] = "Improved event processing."
    plan = fanout.plan.FanoutPlanV2.from_dict(replacement)
    changed_binding = replace(
        scheduler.inputs.targets["events"], baseline_sha256=_hash("different baseline"),
    )
    targets = dict(scheduler.inputs.targets)
    targets["events"] = changed_binding
    inputs = replace(
        scheduler.inputs, targets=targets,
        compiled_plan_sha256=hashlib.sha256(fanout.canonical_json(plan.to_dict())).hexdigest(),
    )
    revision = scheduler.revision
    with pytest.raises(SchedulerStateError, match="target binding"):
        scheduler.accept_amendment(
            plan, inputs, expected_plan_revision=scheduler.plan_revision, owner=owner,
        )
    assert scheduler.revision == revision


def test_v2_same_target_amendment_reopens_with_unchanged_bindings(tmp_path):
    scheduler, backend, journal, controller, _baseline, owner = _v2_handover_fixture(
        tmp_path, dependency_mode="artifact",
    )
    replacement = scheduler.plan.to_dict()
    replacement["tasks"][2]["objective"] = "Improved event processing."
    plan = fanout.plan.FanoutPlanV2.from_dict(replacement)
    inputs = replace(
        scheduler.inputs,
        compiled_plan_sha256=hashlib.sha256(fanout.canonical_json(plan.to_dict())).hexdigest(),
    )
    amendment = scheduler.accept_amendment(
        plan, inputs, expected_plan_revision=1, owner=owner,
    )
    assert amendment.plan_revision == 2
    reopened = Scheduler._resume_for_test(
        plan, inputs, backend, backend.artifacts, owner=owner,
        anchor_store=scheduler._authority, journal=journal,
        lifecycle_controller=controller,
    )
    assert reopened.plan_revision == 2
    assert [item.task_id for item in reopened.schedule_ready(owner=owner)] == ["address", "events"]


def test_v2_artifact_only_test_seam_cannot_admit_handover_plan_or_terminal(tmp_path):
    scheduler, _backend, _journal, _controller, _baseline, owner = _v2_handover_fixture(
        tmp_path, dependency_mode="artifact",
    )
    plain_backend = MemoryBackend(owner)
    seam = Scheduler._create_for_test(
        scheduler.plan, scheduler.inputs, plain_backend, plain_backend.artifacts,
        owner=owner, anchor_store=runstate_module._test_anchor_authority(MemoryAnchorStore()),
    )
    seam.schedule_ready(owner=owner)
    seam.mark_active("address", owner=owner)
    seam.begin_reconciliation("address", owner=owner)
    seam.complete_reconciliation("address", _receipt(seam, plain_backend, "address"), owner=owner)
    revision = seam.revision
    with pytest.raises(SchedulerStateError, match="journal and controller"):
        seam.mark_handover_terminal("address", object(), owner=owner)
    assert seam.revision == revision
    handover_document = scheduler.plan.to_dict()
    handover_document["tasks"][1]["dependency_modes"] = {"address": "handover"}
    handover_plan = fanout.plan.FanoutPlanV2.from_dict(handover_document)
    with pytest.raises(SchedulerStateError, match="journal, artifacts, and controller"):
        Scheduler._create_for_test(
            handover_plan, scheduler.inputs, MemoryBackend(owner), plain_backend.artifacts,
            owner=owner, anchor_store=runstate_module._test_anchor_authority(MemoryAnchorStore()),
        )


def test_v2_scheduler_rejects_incomplete_target_registry_before_state(tmp_path):
    scheduler, _backend, _journal, _controller, _baseline, owner = _v2_handover_fixture(
        tmp_path, dependency_mode="artifact",
    )
    incomplete = replace(
        scheduler.inputs,
        targets={key: value for key, value in scheduler.inputs.targets.items() if key != "events"},
    )
    backend = MemoryBackend(owner)
    with pytest.raises(SchedulerStateError, match="target registry"):
        Scheduler._create_for_test(
            scheduler.plan, incomplete, backend, backend.artifacts,
            owner=owner, anchor_store=runstate_module._test_anchor_authority(MemoryAnchorStore()),
        )
    assert backend.record is None


def test_v2_journal_terminal_projects_once_after_scheduler_reopen(tmp_path, monkeypatch):
    scheduler, backend, journal, controller, baseline, owner = _v2_handover_fixture(tmp_path)
    scheduler.schedule_ready(owner=owner)
    scheduler.mark_active("address", owner=owner)
    scheduler.begin_reconciliation("address", owner=owner)
    bundle = fanout.CandidateBundle(baseline.digest, (), ())
    _reconcile_v2_candidate(scheduler, backend, bundle, owner)
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    candidate = fanout.issue_target_candidate(
        bundle, task_id="address", plan=scheduler.plan, inputs=scheduler.inputs,
        store=backend.artifacts, controller=controller,
    )
    verification, _receipt = fanout.verify_target_candidate(
        candidate, baseline=baseline, plan=scheduler.plan, inputs=scheduler.inputs,
        store=backend.artifacts, controller=controller,
    )
    prepared = handover.prepare_branch_handover(
        scheduler.inputs.targets["address"], candidate, verification,
        plan=scheduler.plan, inputs=scheduler.inputs, artifacts=backend.artifacts,
        scheduler=scheduler, controller=controller, journal=journal, owner=owner,
        task_id="address",
    )
    class Crash(BaseException):
        pass
    def stop(phase):
        if phase == "after-journal-terminal":
            raise Crash()
    monkeypatch.setattr(handover, "_checkpoint", stop)
    with pytest.raises(Crash):
        handover.deliver_branch_candidate(
            prepared, plan=scheduler.plan, inputs=scheduler.inputs,
            artifacts=backend.artifacts, scheduler=scheduler, controller=controller,
            journal=journal, owner=owner,
        )
    monkeypatch.setattr(handover, "_checkpoint", lambda _phase: None)
    _intent, terminal = journal.branch_handover_state("address")
    assert terminal is not None
    before = scheduler.revision
    reopened = Scheduler._resume_for_test(
        scheduler.plan, scheduler.inputs, backend, backend.artifacts,
        owner=owner, anchor_store=scheduler._authority,
        journal=journal, lifecycle_controller=controller,
    )
    assert reopened.revision == before
    fanout.ExecutionService.project_v2_handover_terminal(
        reopened, journal, "address", owner=owner,
    )
    assert reopened.revision == before + 1
    assert [item.task_id for item in reopened.schedule_ready(owner=owner)] == ["booking"]
    after = reopened.revision
    reopened = Scheduler._resume_for_test(
        scheduler.plan, scheduler.inputs, backend, backend.artifacts,
        owner=owner, anchor_store=scheduler._authority,
        journal=journal, lifecycle_controller=controller,
    )
    fanout.ExecutionService.project_v2_handover_terminal(
        reopened, journal, "address", owner=owner,
    )
    assert reopened.revision == after


def _finish(scheduler: Scheduler, backend: MemoryBackend, task_id: str,
            owner: OwnerCapability, result: ReconciledResult | None = None) -> ReconciledResult:
    scheduler.mark_active(task_id, owner=owner)
    scheduler.begin_reconciliation(task_id, owner=owner)
    scheduler.complete_reconciliation(
        task_id, result or _receipt(scheduler, backend, task_id), owner=owner,
    )
    completed = scheduler.result_for(task_id)
    assert completed is not None
    return completed


def test_only_dependency_ready_leaves_are_scheduled_with_exact_results(owner):
    """A dependant must never run early or receive a guessed dependency artifact."""
    plan = _plan(_work("prepare"), _work("implement", depends_on=("prepare",)))
    scheduler, backend = _scheduler(plan, owner)

    first = scheduler.schedule_ready(owner=owner)
    assert first == (
        WorkDispatch("prepare", 1, 3, (), first[0].dispatch_id),
    )
    assert scheduler.schedule_ready(owner=owner) == ()

    prepared = _finish(
        scheduler, backend, "prepare", owner,
        _receipt(scheduler, backend, "prepare", "exact bytes"),
    )
    second = scheduler.schedule_ready(owner=owner)
    assert second[0].task_id == "implement"
    assert second[0].dependencies == (prepared,)
    assert second[0].seat_count == 3


def test_scheduler_enforces_two_task_and_six_seat_global_caps(owner):
    """A third ready task must wait rather than over-admit provider processes."""
    scheduler, _ = _scheduler(_plan(_work("a"), _work("b"), _work("c")), owner)

    decisions = scheduler.schedule_ready(owner=owner)

    assert [decision.task_id for decision in decisions] == ["a", "b"]
    assert sum(decision.seat_count for decision in decisions if isinstance(decision, WorkDispatch)) == 6
    assert scheduler.active_task_count == 2
    assert scheduler.active_seat_count == 6


def test_reconciliation_releases_seats_but_keeps_the_active_task_barrier(owner):
    """Owner reconciliation must not reserve executor capacity after providers exit."""
    scheduler, _ = _scheduler(_plan(_work("a", seats=3), _work("c", seats=4)), owner)
    assert [item.task_id for item in scheduler.schedule_ready(owner=owner)] == ["a"]
    scheduler.mark_active("a", owner=owner)

    scheduler.begin_reconciliation("a", owner=owner)

    assert scheduler.active_task_count == 1
    assert scheduler.active_seat_count == 0
    assert [item.task_id for item in scheduler.schedule_ready(owner=owner)] == ["c"]
    assert scheduler.active_task_count == 2
    assert scheduler.active_seat_count == 4


def test_failure_releases_seats_for_an_independent_wider_task(owner):
    """A failed provider branch must not retain executor seats."""
    scheduler, backend = _scheduler(_plan(_work("a", seats=3), _work("c", seats=4)), owner)
    assert [item.task_id for item in scheduler.schedule_ready(owner=owner)] == ["a"]

    scheduler.fail_task("a", "provider failed", owner=owner)

    assert scheduler.active_task_count == 0
    assert scheduler.active_seat_count == 0
    assert backend.record is not None
    assert backend.record.snapshot.tasks[0].seat_count == 0
    assert [item.task_id for item in scheduler.schedule_ready(owner=owner)] == ["c"]


def test_hierarchy_is_structural_and_never_adds_ordering(owner):
    """Sibling groups and declaration adjacency must not become hidden dependencies."""
    plan = _plan(
        _group("phase-a"),
        _work("a", parent_id="phase-a"),
        _group("phase-b"),
        _work("b", parent_id="phase-b"),
    )
    scheduler, backend = _scheduler(plan, owner)

    decisions = scheduler.schedule_ready(owner=owner)

    assert [decision.task_id for decision in decisions] == ["a", "b"]
    assert all(not decision.task_id.startswith("phase-") for decision in decisions)


def test_failed_branch_does_not_block_independent_ready_progress(owner):
    """Failure propagation must remain local to dependency descendants."""
    plan = _plan(
        _work("a"), _work("b"),
        _work("after-a", depends_on=("a",)),
        _work("after-b", depends_on=("b",)),
    )
    scheduler, backend = _scheduler(plan, owner)
    scheduler.schedule_ready(owner=owner)
    scheduler.mark_active("a", owner=owner)
    scheduler.fail_task("a", "verification failed", owner=owner)
    _finish(scheduler, backend, "b", owner)

    decisions = scheduler.schedule_ready(owner=owner)

    assert [decision.task_id for decision in decisions] == ["after-b"]
    assert scheduler.task_phase("after-a") == "blocked-dependency"
    assert scheduler.task_phase("after-b") == "scheduled"


def test_actions_and_reconciliation_are_owner_controlled_barriers(owner):
    """Seats without the external owner capability must not unlock dependants."""
    plan = _plan(
        _work("approve", execution_class="orchestrator-action"),
        _work("execute", depends_on=("approve",)),
    )
    scheduler, backend = _scheduler(plan, owner)
    decisions = scheduler.schedule_ready(owner=owner)

    assert decisions == (ActionBarrier("approve", 1, (), decisions[0].barrier_id),)
    assert scheduler.active_seat_count == 0
    wrong_owner = OwnerCapability.from_token("x" * 43)
    with pytest.raises(RunAuthorizationError):
        scheduler.complete_action(
            "approve", _receipt(scheduler, backend, "approve"), owner=wrong_owner,
        )
    assert backend.record is not None
    assert scheduler.task_phase("approve") == "blocked-action"

    completed = scheduler.complete_action(
        "approve", _receipt(scheduler, backend, "approve"), owner=owner,
    )
    assert scheduler.schedule_ready(owner=owner)[0].dependencies == (completed,)

    scheduler.mark_active("execute", owner=owner)
    with pytest.raises(RunAuthorizationError):
        scheduler.begin_reconciliation("execute", owner=wrong_owner)
    assert scheduler.task_phase("execute") == "active"


def test_illegal_transitions_and_invalid_results_fail_without_mutation(owner):
    """A malformed or premature completion must not unlock downstream work."""
    scheduler, backend = _scheduler(_plan(_work("a")), owner)
    scheduler.schedule_ready(owner=owner)
    before = backend.record

    with pytest.raises(SchedulerStateError, match="reconciliation"):
        scheduler.complete_reconciliation(
            "a", ReconciledResult(
                "run-1", "a", 1, scheduler.plan_sha256, scheduler.inputs_digest,
                _result("a"),
            ), owner=owner,
        )
    with pytest.raises(SchedulerStateError, match="blocked-action"):
        scheduler.complete_action("a", _receipt(scheduler, backend, "a"), owner=owner)
    assert backend.record == before


def test_result_artifacts_reuse_the_contained_artifact_path_contract(owner):
    """A scheduler must not bless an artifact reference the artifact store cannot read."""
    scheduler, backend = _scheduler(
        _plan(_work("approve", execution_class="orchestrator-action")), owner,
    )
    scheduler.schedule_ready(owner=owner)

    with pytest.raises(SchedulerStateError, match="artifact path"):
        scheduler.complete_action("approve", ReconciledResult(
            "run-1", "approve", 1, scheduler.plan_sha256, scheduler.inputs_digest,
            ArtifactRef(path="results\\escape", digest="a" * 64, size=1),
        ), owner=owner)


def test_resume_never_reschedules_scheduled_or_terminal_work(owner):
    """A controller restart must not create a second provider dispatch or spend."""
    plan = _plan(_work("a"), _work("after", depends_on=("a",)))
    scheduler, backend = _scheduler(plan, owner)
    first = scheduler.schedule_ready(owner=owner)[0]

    resumed = _resume(plan, backend)
    assert resumed.schedule_ready(owner=owner) == ()
    assert resumed.pending_work() == (first,)
    _finish(resumed, backend, "a", owner)
    resumed = _resume(plan, backend)
    assert resumed.task_phase("a") == "completed"
    assert resumed.schedule_ready(owner=owner)[0].task_id == "after"
    assert all(item.task_id != "a" for item in resumed.pending_work())


def test_resume_recovers_an_action_barrier_without_creating_another(owner):
    """A crash after blocking an owner action must preserve its exact barrier identity."""
    plan = _plan(_work("approve", execution_class="orchestrator-action"))
    scheduler, backend = _scheduler(plan, owner)
    barrier = scheduler.schedule_ready(owner=owner)[0]

    resumed = _resume(plan, backend)

    assert resumed.schedule_ready(owner=owner) == ()
    assert resumed.pending_actions() == (barrier,)


def test_resume_rejects_plan_drift_foreign_inputs_and_cap_inconsistency(owner):
    """Backend corruption or a different plan must fail closed before dispatch."""
    plan = _plan(_work("a"), _work("after", depends_on=("a",)))
    scheduler, backend = _scheduler(plan, owner)
    scheduler.schedule_ready(owner=owner)
    assert backend.record is not None

    drifted = _plan(_work("renamed"), _work("after", depends_on=("renamed",)))
    with pytest.raises(SchedulerStateError, match="compiled plan|plan drift"):
        _resume(drifted, backend)

    snapshot = backend.record.snapshot
    states = list(snapshot.tasks)
    states[0] = replace(states[0], seat_count=7)
    backend.record = BackendRecord(backend.record.revision, replace(snapshot, tasks=tuple(states)))
    with pytest.raises(SchedulerStateError, match="seat cap"):
        _resume(plan, backend)


def test_resume_rejects_boolean_task_revision_and_foreign_dependency_result(owner):
    """JSON booleans and substituted result identities must not pass integer/equality checks."""
    plan = _plan(_work("a"), _work("after", depends_on=("a",)))
    scheduler, backend = _scheduler(plan, owner)
    scheduler.schedule_ready(owner=owner)
    _finish(scheduler, backend, "a", owner)
    scheduler.schedule_ready(owner=owner)
    assert backend.record is not None
    snapshot = backend.record.snapshot
    states = list(snapshot.tasks)
    states[1] = replace(states[1], plan_revision=True)
    backend.record = BackendRecord(backend.record.revision, replace(snapshot, tasks=tuple(states)))
    with pytest.raises(SchedulerStateError, match="plan revision"):
        _resume(plan, backend)

    states[1] = replace(
        snapshot.tasks[1],
        dependencies=(replace(snapshot.tasks[0].result, task_id="foreign"),),
    )
    backend.record = BackendRecord(backend.record.revision, replace(snapshot, tasks=tuple(states)))
    with pytest.raises(SchedulerStateError, match="foreign"):
        _resume(plan, backend)


def test_resume_rejects_malformed_backend_collections_and_result_types(owner):
    """Type annotations alone must not admit corrupt durable backend values."""
    plan = _plan(_work("a"))
    scheduler, backend = _scheduler(plan, owner)
    scheduler.schedule_ready(owner=owner)
    _finish(scheduler, backend, "a", owner)
    assert backend.record is not None
    snapshot = backend.record.snapshot
    backend.record = BackendRecord(
        backend.record.revision,
        replace(snapshot, tasks=list(snapshot.tasks)),  # type: ignore[arg-type]
    )
    with pytest.raises(SchedulerStateError, match="task collection"):
        _resume(plan, backend)

    malformed = replace(snapshot.tasks[0], result=_result("a"))  # type: ignore[arg-type]
    backend.record = BackendRecord(
        backend.record.revision, replace(snapshot, tasks=(malformed,)),
    )
    with pytest.raises(SchedulerStateError, match="reconciled result"):
        _resume(plan, backend)


def test_scheduler_rejects_a_single_task_above_the_six_seat_cap(owner):
    """Skipping an impossible task forever would hide a configuration error."""
    data = _plan(_work("wide")).to_dict()
    data["tasks"][0]["provider_policy"]["executor_ids"] = [  # type: ignore[index]
        f"executor-{index}" for index in range(7)
    ]
    wide = fanout.FanoutPlanV1.from_dict(data)

    with pytest.raises(SchedulerStateError, match="six-seat cap"):
        _scheduler(wide, owner)


def test_backend_compare_and_set_conflict_does_not_advance_local_state(owner):
    """An ambiguous concurrent-controller conflict must not be treated as committed."""
    scheduler, backend = _scheduler(_plan(_work("a")), owner)
    backend.force_conflict = True

    with pytest.raises(SchedulerConflictError):
        scheduler.schedule_ready(owner=owner)

    assert scheduler.task_phase("a") == "unscheduled"
    assert scheduler.revision == 1


def test_amendment_accepts_only_wholly_unscheduled_hierarchy_subtrees(owner):
    """Changing group inheritance after any descendant was scheduled changes live work."""
    original = _plan(
        _work("active"),
        _group("later"),
        _work("u1", depends_on=("active",), parent_id="later"),
        _work("u2", depends_on=("u1",), parent_id="later"),
    )
    scheduler, _ = _scheduler(original, owner)
    scheduler.schedule_ready(owner=owner)
    replacement_data = original.to_dict()
    replacement_data["tasks"][1]["objective"] = "Revised later work."  # type: ignore[index]
    replacement = fanout.FanoutPlanV1.from_dict(replacement_data)

    accepted = scheduler.accept_amendment(
        replacement, _inputs(replacement), expected_plan_revision=1, owner=owner,
    )

    assert accepted.plan_revision == 2
    assert accepted.affected_task_ids == ("u1", "u2")
    assert scheduler.plan_revision == 2
    assert scheduler.task_phase("active") == "scheduled"

    live_group_change = scheduler.plan.to_dict()
    live_group_change["tasks"].insert(0, _group("root"))  # type: ignore[union-attr]
    live_group_change["tasks"][1]["parent_id"] = "root"  # type: ignore[index]
    with pytest.raises(SchedulerStateError, match="unscheduled subtree"):
        scheduler.accept_amendment(
            fanout.FanoutPlanV1.from_dict(live_group_change),
            _inputs(fanout.FanoutPlanV1.from_dict(live_group_change)),
            expected_plan_revision=2, owner=owner,
        )


def test_amendment_refuses_stale_revision_invalid_plan_and_consumed_work(owner):
    """A stale or partial amendment must not rewrite work already used as an input."""
    original = _plan(_work("a"), _work("b", depends_on=("a",)), _work("c", depends_on=("b",)))
    scheduler, backend = _scheduler(original, owner)
    scheduler.schedule_ready(owner=owner)
    _finish(scheduler, backend, "a", owner)
    scheduler.schedule_ready(owner=owner)

    changed_a = scheduler.plan.to_dict()
    changed_a["tasks"][0]["objective"] = "Changed after consumption."  # type: ignore[index]
    with pytest.raises(SchedulerStateError, match="unscheduled subtree"):
        scheduler.accept_amendment(
            changed_a, _inputs(fanout.FanoutPlanV1.from_dict(changed_a)),
            expected_plan_revision=1, owner=owner,
        )

    before = backend.record
    invalid = scheduler.plan.to_dict()
    invalid["tasks"][2]["depends_on"] = ["c"]  # type: ignore[index]
    with pytest.raises(fanout.PlanValidationError):
        scheduler.accept_amendment(invalid, backend.inputs, expected_plan_revision=1, owner=owner)
    assert backend.record == before

    untouched = scheduler.plan.to_dict()
    with pytest.raises(SchedulerConflictError, match="stale plan revision"):
        scheduler.accept_amendment(
            untouched, _inputs(fanout.FanoutPlanV1.from_dict(untouched)),
            expected_plan_revision=0, owner=owner,
        )


def test_amendment_backend_conflict_keeps_previous_plan_and_revision(owner):
    """A failed CAS must not make the in-memory scheduler diverge from durable state."""
    original = _plan(_work("a"))
    scheduler, backend = _scheduler(original, owner)
    replacement = original.to_dict()
    replacement["tasks"][0]["objective"] = "A revised unscheduled objective."  # type: ignore[index]
    backend.force_conflict = True

    with pytest.raises(SchedulerConflictError):
        scheduler.accept_amendment(
            replacement, _inputs(fanout.FanoutPlanV1.from_dict(replacement)),
            expected_plan_revision=1, owner=owner,
        )

    assert scheduler.plan == original
    assert scheduler.plan_revision == 1


def test_amendment_versions_deterministic_work_order_changes(owner):
    """Reordering ready peers changes scheduling and must be an explicit plan revision."""
    original = _plan(_work("a"), _work("b"))
    scheduler, _ = _scheduler(original, owner)
    reordered = original.to_dict()
    reordered["tasks"] = list(reversed(reordered["tasks"]))  # type: ignore[arg-type]

    amendment = scheduler.accept_amendment(
        reordered, _inputs(fanout.FanoutPlanV1.from_dict(reordered)),
        expected_plan_revision=1, owner=owner,
    )

    assert amendment.plan_revision == 2
    assert amendment.affected_task_ids == ("b", "a")
    assert [item.task_id for item in scheduler.schedule_ready(owner=owner)] == ["b", "a"]


def test_amendment_can_remove_an_unscheduled_task_without_touching_a_live_peer(owner):
    """Removing one future leaf must not classify every declaration after it as changed."""
    original = _plan(
        _work("active"),
        _work("remove", depends_on=("active",)),
        _work("keep", depends_on=("active",)),
    )
    scheduler, _ = _scheduler(original, owner)
    scheduler.schedule_ready(owner=owner)
    replacement = original.to_dict()
    removed = replacement["tasks"].pop(1)  # type: ignore[union-attr]
    replacement["tasks"][1]["source_step_ids"].extend(removed["source_step_ids"])  # type: ignore[index,union-attr]

    amendment = scheduler.accept_amendment(
        replacement, _inputs(fanout.FanoutPlanV1.from_dict(replacement)),
        expected_plan_revision=1, owner=owner,
    )

    assert amendment.affected_task_ids == ("keep", "remove")
    assert scheduler.task_phase("active") == "scheduled"


def test_exact_backend_prefix_rollback_and_fabricated_revisions_are_rejected(owner):
    """A valid old local record must not override the external monotonic high-water mark."""
    plan = _plan(_work("a"))
    scheduler, backend = _scheduler(plan, owner)
    old_record = backend.record
    scheduler.schedule_ready(owner=owner)
    latest = backend.record
    assert old_record is not None and latest is not None

    backend.record = old_record
    with pytest.raises(SchedulerStateError, match="authority|rollback|revision"):
        _resume(plan, backend)

    fabricated = replace(latest.snapshot, plan_revision=99)
    backend.record = BackendRecord(latest.revision, fabricated)
    with pytest.raises(SchedulerStateError, match="authority|digest|revision"):
        _resume(plan, backend)

    skipped = replace(latest.snapshot, backend_revision=latest.revision + 2)
    backend.record = BackendRecord(latest.revision + 2, skipped)
    with pytest.raises(SchedulerStateError, match="authority|revision"):
        _resume(plan, backend)


def test_authority_revision_is_owner_signed_against_advanced_prefix_replay(owner):
    """Old authenticated authority bytes cannot become a new high-water record."""
    plan = _plan(_work("a"))
    scheduler, backend = _scheduler(plan, owner)
    authority_key = next(iter(backend.anchor_store.records))
    old_backend = backend.record
    old_authority = backend.anchor_store.records[authority_key]
    scheduler.schedule_ready(owner=owner)
    latest_authority = backend.anchor_store.records[authority_key]
    assert old_backend is not None

    backend.record = old_backend
    backend.anchor_store.records[authority_key] = AnchorRevision(
        latest_authority.revision + 1, old_authority.value,
    )

    with pytest.raises(SchedulerStateError, match="authority.*revision"):
        _resume(plan, backend)


def test_authority_rejects_same_value_revision_skips_and_skipped_cas_results(owner):
    """The attested service revision must equal the owner-signed target revision."""
    plan = _plan(_work("a"))
    scheduler, backend = _scheduler(plan, owner)
    authority_key = next(iter(backend.anchor_store.records))
    current = backend.anchor_store.records[authority_key]
    backend.anchor_store.records[authority_key] = AnchorRevision(
        current.revision + 2, current.value,
    )
    with pytest.raises(SchedulerStateError, match="authority.*revision"):
        _resume(plan, backend)

    clean_scheduler, clean_backend = _scheduler(plan, owner)
    clean_backend.anchor_store.skip_after_mutation_cas_call = (
        clean_backend.anchor_store.cas_calls + 1
    )
    with pytest.raises(SchedulerStateError, match="authority.*revision"):
        clean_scheduler.schedule_ready(owner=owner)
    assert clean_scheduler.task_phase("a") == "unscheduled"
    with pytest.raises(SchedulerStateError, match="authority.*revision"):
        _resume(plan, clean_backend)


@pytest.mark.parametrize("after_mutation", [False, True])
def test_initial_authority_create_crash_is_recoverable(owner, after_mutation):
    """Fresh controllers recover whether the pending-initial create mutated or not."""
    plan = _plan(_work("a"))
    backend = MemoryBackend(owner)
    anchor_store = MemoryAnchorStore()
    if after_mutation:
        anchor_store.crash_create_after_mutation = True
    else:
        anchor_store.crash_create_before_mutation = True
    with pytest.raises(SimulatedCrash):
        _scheduler(plan, owner, backend=backend, anchor_store=anchor_store)
    anchor_store.crash_create_after_mutation = False
    anchor_store.crash_create_before_mutation = False

    if after_mutation:
        recovered = _resume(plan, backend)
    else:
        recovered, backend = _scheduler(
            plan, owner, backend=backend, anchor_store=anchor_store,
        )
    assert recovered.revision == 1
    assert recovered.task_phase("a") == "unscheduled"
    assert len(backend.records) == 1


@pytest.mark.parametrize("after_mutation", [False, True])
def test_initial_backend_cas_failure_is_recoverable(owner, after_mutation):
    """Pending initialization survives either backend CAS failure window."""
    plan = _plan(_work("a"))
    backend = MemoryBackend(owner)
    anchor_store = MemoryAnchorStore()
    if after_mutation:
        backend.fail_after_mutation = True
    else:
        backend.force_conflict = True
    with pytest.raises(SchedulerConflictError):
        _scheduler(plan, owner, backend=backend, anchor_store=anchor_store)
    backend.force_conflict = False

    recovered = _resume(plan, backend)

    assert recovered.revision == 1
    assert recovered.task_phase("a") == "unscheduled"
    assert len(backend.records) == 1


@pytest.mark.parametrize("after_mutation", [False, True])
def test_initial_final_authority_cas_failure_is_recoverable(owner, after_mutation):
    """Fresh resume resolves both final initialization authority reply windows."""
    plan = _plan(_work("a"))
    backend = MemoryBackend(owner)
    anchor_store = MemoryAnchorStore()
    if after_mutation:
        anchor_store.mutate_then_fail_cas_call = 1
    else:
        anchor_store.fail_cas_call = 1
    with pytest.raises(SchedulerConflictError):
        _scheduler(plan, owner, backend=backend, anchor_store=anchor_store)

    recovered = _resume(plan, backend)

    assert recovered.revision == 1
    assert recovered.task_phase("a") == "unscheduled"
    assert len(backend.records) == 1


def test_create_retry_returns_exact_committed_initial_state(owner):
    """An idempotent create retry neither overwrites nor duplicates revision one."""
    plan = _plan(_work("a"))
    first, backend = _scheduler(plan, owner)

    retried, _ = _scheduler(
        plan, owner, backend=backend, anchor_store=backend.anchor_store,
    )

    assert retried.revision == first.revision == 1
    assert retried.task_phase("a") == "unscheduled"
    assert len(backend.records) == 1


def test_pending_authority_reconciles_old_and_exact_new_backend_states(owner):
    """Crash windows clear an unapplied decision or commit exactly one applied decision."""
    plan = _plan(_work("a"))
    scheduler, backend = _scheduler(plan, owner)
    backend.force_conflict = True
    with pytest.raises(SchedulerConflictError):
        scheduler.schedule_ready(owner=owner)
    backend.force_conflict = False
    resumed = _resume(plan, backend)
    assert resumed.task_phase("a") == "unscheduled"

    backend.fail_after_mutation = True
    with pytest.raises(SchedulerConflictError):
        resumed.schedule_ready(owner=owner)
    recovered = _resume(plan, backend)
    assert recovered.task_phase("a") == "scheduled"
    assert len(recovered.pending_work()) == 1


@pytest.mark.parametrize("mutated_before_error", [False, True])
def test_resume_resolves_both_final_authority_cas_crash_windows(
    owner, mutated_before_error,
):
    """An uncertain final authority reply cannot lose or repeat an applied decision."""
    plan = _plan(_work("a"))
    anchor_store = MemoryAnchorStore()
    scheduler, backend = _scheduler(plan, owner, anchor_store=anchor_store)
    final_cas_call = anchor_store.cas_calls + 2
    if mutated_before_error:
        anchor_store.mutate_then_fail_cas_call = final_cas_call
    else:
        anchor_store.fail_cas_call = final_cas_call

    with pytest.raises(SchedulerConflictError):
        scheduler.schedule_ready(owner=owner)

    recovered = _resume(plan, backend)
    assert recovered.task_phase("a") == "scheduled"
    assert len(recovered.pending_work()) == 1


def test_concurrent_scheduler_cannot_advance_after_another_controller(owner):
    """The remote authority and backend CAS reject a controller holding a stale prefix."""
    plan = _plan(_work("a"), _work("b"))
    first, backend = _scheduler(plan, owner)
    second = _resume(plan, backend)
    first.schedule_ready(owner=owner)

    with pytest.raises(SchedulerConflictError):
        second.schedule_ready(owner=owner)

    assert [item.task_id for item in _resume(plan, backend).pending_work()] == ["a", "b"]


@pytest.mark.parametrize("changed", [
    "source", "provider", "skill", "compiler", "parser", "repo",
])
def test_resume_rejects_every_run_input_drift_class(owner, changed):
    """The same plan cannot conceal changed source, profiles, skills, toolchain, or repository."""
    plan = _plan(_work("a"))
    _, backend = _scheduler(plan, owner)
    values = _inputs(plan).to_dict()
    values.pop("schema_version")
    if changed == "source":
        values["source_sha256"] = _hash("changed source")
    elif changed == "provider":
        values["provider_profiles"] = {"claude": _hash("changed")}
    elif changed == "skill":
        values["skill_manifests"] = {"a": _hash("changed")}
    elif changed == "compiler":
        values["compiler_sha256"] = _hash("changed")
    elif changed == "parser":
        values["parser_sha256"] = _hash("changed")
    else:
        values["repo_baseline_sha256"] = _hash("changed")

    with pytest.raises(SchedulerStateError, match="input|source|profile"):
        _resume(plan, backend, inputs=RunInputs(**values))


def test_missing_mutated_and_foreign_result_receipts_never_complete(owner):
    """Only exact durable bytes bound to this run/task/revision may unlock dependants."""
    plan = _plan(_work("a"), _work("after", depends_on=("a",)))
    scheduler, backend = _scheduler(plan, owner)
    scheduler.schedule_ready(owner=owner)
    scheduler.mark_active("a", owner=owner)
    scheduler.begin_reconciliation("a", owner=owner)
    missing = ReconciledResult(
        "run-1", "a", 1, scheduler.plan_sha256, scheduler.inputs_digest,
        ArtifactRef("results/missing.json", "a" * 64, 1),
    )
    with pytest.raises(SchedulerStateError, match="artifact"):
        scheduler.complete_reconciliation("a", missing, owner=owner)

    valid = _receipt(scheduler, backend, "a", "durable")
    artifact_path = backend.artifacts.root / valid.artifact.path
    artifact_path.write_bytes(b"mutated")
    with pytest.raises(SchedulerStateError, match="artifact"):
        scheduler.complete_reconciliation("a", valid, owner=owner)

    replacement_ref = backend.artifacts.write_bytes("results/replacement.json", b"replacement")
    valid = scheduler.result_receipt("a", replacement_ref)
    for foreign in (
        replace(valid, run_id="run-2"),
        replace(valid, task_id="after"),
        replace(valid, plan_revision=2),
    ):
        with pytest.raises(SchedulerStateError, match="receipt"):
            scheduler.complete_reconciliation("a", foreign, owner=owner)


def test_result_receipt_rejects_traversal_digest_size_and_boolean_size(owner):
    """Receipt construction must preserve the ArtifactStore reference contract exactly."""
    plan = _plan(_work("a"))
    scheduler, _ = _scheduler(plan, owner)
    for ref in (
        ArtifactRef("../escape", "a" * 64, 1),
        ArtifactRef("result", "bad", 1),
        ArtifactRef("result", "a" * 64, -1),
        ArtifactRef("result", "a" * 64, True),
    ):
        with pytest.raises(SchedulerStateError, match="artifact"):
            ReconciledResult(
                "run-1", "a", 1, scheduler.plan_sha256, scheduler.inputs_digest, ref,
            )


def test_resume_and_dependency_consumption_reverify_result_bytes(owner):
    """Post-completion mutation is detected both at restart and before downstream dispatch."""
    plan = _plan(_work("a"), _work("after", depends_on=("a",)))
    scheduler, backend = _scheduler(plan, owner)
    scheduler.schedule_ready(owner=owner)
    completed = _finish(scheduler, backend, "a", owner)
    (backend.artifacts.root / completed.artifact.path).write_bytes(b"tampered")

    with pytest.raises(SchedulerStateError, match="artifact"):
        _resume(plan, backend)
    with pytest.raises(SchedulerStateError, match="artifact"):
        scheduler.schedule_ready(owner=owner)


def test_decision_ids_bind_run_inputs_plan_policy_kind_and_dependencies(owner):
    """Every material decision input changes the canonical decision namespace."""
    base = _plan(_work("a"))
    base_scheduler, base_backend = _scheduler(base, owner)
    base_id = base_scheduler.schedule_ready(owner=owner)[0].dispatch_id
    assert _resume(base, base_backend).pending_work()[0].dispatch_id == base_id

    source_data = base.to_dict()
    source_data["source"]["sha256"] = _hash("other source")  # type: ignore[index]
    source = fanout.FanoutPlanV1.from_dict(source_data)
    source_id = _scheduler(source, owner, inputs=_inputs(source))[0].schedule_ready(owner=owner)[0].dispatch_id

    skill_data = base.to_dict()
    skill_data["tasks"][0]["required_skills"] = ["code-search"]  # type: ignore[index]
    skill_data["tasks"][0]["none_reason"] = None  # type: ignore[index]
    skill = fanout.FanoutPlanV1.from_dict(skill_data)
    skill_id = _scheduler(skill, owner, inputs=_inputs(skill))[0].schedule_ready(owner=owner)[0].dispatch_id

    provider_inputs = _inputs(base, provider_profiles={
        item: _hash(f"changed:{item}") for item in ("claude", "codex", "agy")
    })
    provider_id = _scheduler(base, owner, inputs=provider_inputs)[0].schedule_ready(owner=owner)[0].dispatch_id

    policy_data = base.to_dict()
    policy_data["tasks"][0]["provider_policy"]["executor_ids"] = ["claude", "codex"]  # type: ignore[index]
    policy_data["tasks"][0]["provider_policy"]["minimum_success"] = 2  # type: ignore[index]
    policy = fanout.FanoutPlanV1.from_dict(policy_data)
    policy_id = _scheduler(policy, owner, inputs=_inputs(policy))[0].schedule_ready(owner=owner)[0].dispatch_id

    other_run = _inputs(base, run_id="run-2")
    run_id = _scheduler(base, owner, inputs=other_run)[0].schedule_ready(owner=owner)[0].dispatch_id

    action = _plan(_work("a", execution_class="orchestrator-action"))
    action_id = _scheduler(action, owner)[0].schedule_ready(owner=owner)[0].barrier_id

    dependent = _plan(_work("seed"), _work("a", depends_on=("seed",)))
    dependency_ids = []
    for payload in ("first result", "second result"):
        dependency_scheduler, dependency_backend = _scheduler(dependent, owner)
        dependency_scheduler.schedule_ready(owner=owner)
        _finish(dependency_scheduler, dependency_backend, "seed", owner,
                _receipt(dependency_scheduler, dependency_backend, "seed", payload))
        dependency_ids.append(
            dependency_scheduler.schedule_ready(owner=owner)[0].dispatch_id
        )

    assert len({base_id, source_id, skill_id, provider_id, policy_id, run_id, action_id}) == 7
    assert dependency_ids[0] != dependency_ids[1]


def test_public_scheduler_rejects_a_structural_local_authority(owner):
    """Production callers cannot replace Task 8's remote authority with a replayable local store."""
    plan = _plan(_work("a"))
    backend = MemoryBackend(owner)
    with pytest.raises(SchedulerStateError, match="trusted remote|authority"):
        Scheduler.create(
            plan, _inputs(plan), backend, backend.artifacts, owner=owner,
            anchor_store=MemoryAnchorStore(),
        )
