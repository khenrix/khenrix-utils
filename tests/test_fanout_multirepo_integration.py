"""Disposable two-repository fanout handover integration contracts."""
from __future__ import annotations

import hashlib
import base64
import copy
import dataclasses
import importlib.util
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_fanout_scheduler as cases
import test_fanout_execute_skill as cli_cases


fanout = cases.fanout


class _NativeFakeAdapter(fanout.ProviderAdapter):
    def __init__(self, executor_id: str, repositories: dict[str, Path],
                 expected_dependency: dict[str, str]):
        self.executor_id = executor_id
        self.capabilities = fanout.ProviderCapabilities(
            executor_id == "claude", True, True,
        )
        self.repositories = repositories
        self.expected_dependency = expected_dependency

    def build_command(self, request):
        other = next(root for target_id, root in self.repositories.items()
                     if target_id != request.target_id)
        session = request.session_id or f"local-{request.executor_id}-{request.seat_id}"
        script = (
            "IFS= read -r header; IFS= read -r guidance; "
            "IFS= read -r context_digest; IFS= read -r marker; IFS= read -r context; "
            "if [ \"$4\" != no-dependency ]; then "
            "case \"$context\" in *\"$4\"*) ;; *) exit 96;; esac; fi; "
            "while IFS= read -r rest || [ -n \"$rest\" ]; do :; done; "
            "if IFS= read -r blocked < \"$1\"; then exit 97; fi; "
            "printf 'verified %s change\\n' \"$2\" > README.md; "
            "printf '{\"session_id\":\"%s\",\"answer\":\"local fake complete\","
            "\"context_ok\":true}\\n' \"$3\""
        )
        return self._command(request, (
            "/bin/sh", "-c", script, "sh", str(other / "README.md"),
            request.target_id, session,
            self.expected_dependency.get(request.target_id, "no-dependency"),
        ))

    def parse(self, stdout):
        lines = stdout.decode("utf-8").splitlines()
        result = json.loads(lines[-1])
        return SimpleNamespace(
            session_id=result["session_id"], answer=result["answer"],
            usage=None, final=True, error=None, observed_model=None,
        )


class _NativeFakeMemory:
    def __init__(self, artifacts):
        self.artifacts = artifacts
        self.receipts = {}

    def preflight(self):
        return True

    def publish(self, checkpoint, *, journal, owner):
        prefix = f"memory/{hashlib.sha256(checkpoint.identity.key.encode()).hexdigest()}"
        checkpoint_ref = self.artifacts.write_bytes(
            f"{prefix}/checkpoint.json", checkpoint.text.encode(),
        )
        intent_ref = self.artifacts.write_bytes(
            f"{prefix}/intent.json",
            fanout.canonical_json({"checkpoint_key": checkpoint.identity.key}),
        )
        publication = fanout.CheckpointPublication(
            checkpoint.identity, checkpoint.identity.key, checkpoint.digest,
            checkpoint.project, len(self.receipts) + 1, "c" * 64,
            checkpoint_ref, intent_ref,
        )
        publication = dataclasses.replace(
            publication, publication_ref=self.artifacts.write_bytes(
                f"{prefix}/published.json", fanout.canonical_json(publication.to_dict()),
            ),
        )
        receipt = fanout.CheckpointReceipt(
            publication, f"local-{checkpoint.project}", "d" * 64,
        )
        receipt = dataclasses.replace(
            receipt, verification_ref=self.artifacts.write_bytes(
                f"{prefix}/verified.json", fanout.canonical_json(receipt.to_dict()),
            ),
        )
        journal.append(
            "publication-intent", task_id=checkpoint.identity.task_id,
            seat_id=checkpoint.identity.seat_id, attempt=checkpoint.identity.attempt,
            round=checkpoint.identity.round,
        )
        for phase, digest in (
            ("checkpoint-published", receipt.publication_ref.digest),
            ("checkpoint-verified", receipt.verification_ref.digest),
        ):
            journal.append(
                phase, task_id=checkpoint.identity.task_id,
                seat_id=checkpoint.identity.seat_id, attempt=checkpoint.identity.attempt,
                round=checkpoint.identity.round, evidence_sha256=digest,
            )
        self.receipts[checkpoint.identity.key] = receipt
        return receipt

    def recover(self, checkpoint, publication, *, journal, owner):
        raise AssertionError("local fake checkpoint recovery was not expected")

    def verify_existing(self, checkpoint, receipt):
        assert self.receipts[checkpoint.identity.key] == receipt
        return receipt

    def fetch_verified(self, pairs):
        return tuple(self.verify_existing(checkpoint, receipt)
                     for checkpoint, receipt in pairs)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _admitted_two_writer_packet(tmp_path: Path):
    packet_root = tmp_path / "admission"
    packet_root.mkdir()
    original, _resolver = cli_cases._v2_write_packet(packet_root)
    source = original["source_markdown"].replace("src/change.py", "README.md")
    source += "\n".join([
        "### Task 2: Update booking", "", "**Target:** booking", "", "**Files:**",
        "- Modify: `README.md`", "", "- [ ] **Step 1: Implement and test booking**",
        "  **Depends on:** Task 1/Step 1",
        "  **Dependency modes:** Task 1/Step 1=handover", "",
        "Update booking after address handover.", "",
        "### Task 3: Integrate", "", "**Target:** address", "", "**Files:**", "",
        "- [ ] **Step 1: Review both delivery results**",
        "  **Depends on:** Task 1/Step 1, Task 2/Step 1",
        "  **Dependency modes:** Task 1/Step 1=artifact, Task 2/Step 1=artifact", "",
        "Review the two verified results without an executor workspace.", "",
    ])
    draft = copy.deepcopy(original["draft"])
    draft["source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
    draft["tasks"][0]["owned_paths"] = ["README.md"]
    check = {
        "argv": ["/bin/test", "-f", "README.md"], "cwd": "", "env_allowlist": [],
        "timeout": 5, "accepted_exit_codes": [0], "expected_artifacts": [],
    }
    draft["tasks"][0]["checks"] = [check]
    draft["targets"].append({
        "id": "booking", "repository": "github.com/example/booking-service",
        "ticket_key": "TASK-123", "branch_ref": "refs/heads/feat/TASK-123-booking",
    })
    draft["tasks"].extend((
        {
            "id": "booking", "kind": "work", "parent_id": None,
            "title": "Update booking", "objective": "Update booking after address.",
            "source_step_ids": ["Task 2/Step 1"], "depends_on": ["change"],
            "execution_class": "repo-write", "required_skills": [],
            "none_reason": "No specialist skill applies to this fixture.",
            "owned_paths": ["README.md"], "acceptance": ["Booking result is verified."],
            "checks": [check], "provider_policy": None, "target_id": "booking",
            "dependency_modes": {"change": "handover"},
        },
        {
            "id": "integration", "kind": "work", "parent_id": None,
            "title": "Integrate", "objective": "Review both delivery results.",
            "source_step_ids": ["Task 3/Step 1"], "depends_on": ["change", "booking"],
            "execution_class": "orchestrator-action", "required_skills": [],
            "none_reason": "Owner reviews the two results.", "owned_paths": [],
            "acceptance": ["Both results are reviewed."], "checks": [],
            "provider_policy": None, "target_id": "address",
            "dependency_modes": {"change": "artifact", "booking": "artifact"},
        },
    ))
    ingress = {
        "schema_version": "fanout-bundle-ingress-v2", "quality_tier": "normal",
        "source_path": "docs/change.md", "source_markdown": source, "draft": draft,
    }
    review = {
        "schema_version": "fanout-owner-review-v2", "reviewer": "repository-owner",
        "binding_sha256": hashlib.sha256(fanout.canonical_json({
            "schema_version": "fanout-owner-review-binding-v2", "bundle": ingress,
        })).hexdigest(),
    }
    bundle_path = packet_root / "two-writer-ingress.json"
    review_path = packet_root / "two-writer-review.json"
    bundle_path.write_bytes(fanout.canonical_json(ingress))
    review_path.write_bytes(fanout.canonical_json(review))
    result = subprocess.run(
        [sys.executable, str(cli_cases.PLANNER), "from-bundle", "--bundle", str(bundle_path),
         "--owner-review-file", str(review_path), "--skill-root", str(packet_root / "skills")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout), packet_root / "skills"


@contextmanager
def _admitted_native_fake_service(tmp_path, packet, skill_root, repositories, monkeypatch):
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        pytest.skip("macOS Seatbelt is required for the native fake-seat test")
    probe = subprocess.run(
        ("/usr/bin/sandbox-exec", "-p", "(version 1) (allow default)", "/usr/bin/true"),
        capture_output=True, check=False,
    )
    if probe.returncode == 71 and b"sandbox_apply: Operation not permitted" in probe.stderr:
        pytest.skip("enclosing sandbox denied native Seatbelt application")
    assert probe.returncode == 0, probe.stderr

    fake_bin = tmp_path / "native-fake-bin"
    fake_bin.mkdir()
    for executor_id in ("claude", "codex", "agy"):
        (fake_bin / executor_id).symlink_to("/bin/sh")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    spec = importlib.util.spec_from_file_location("multirepo_native_execute", cli_cases.CLI)
    assert spec is not None and spec.loader is not None
    execute = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = execute
    spec.loader.exec_module(execute)
    provider_module = importlib.import_module(f"{fanout.__name__}.providers")
    profiles = []
    expected_dependency = {}
    for executor_id in ("claude", "codex", "agy"):
        adapter = _NativeFakeAdapter(executor_id, repositories, expected_dependency)
        profiles.append(fanout.ExecutorProfile(
            executor_id, adapter, adapter.capabilities, "repo-write", "standard",
            "local-fake", "diagnostic", "local-fake-v1",
            (executor_id, "--local-fake"),
            (executor_id, "--local-fake-resume", "{session_id}"),
            120, 120,
        ))
    registry = fanout.ProviderRegistry(
        profiles, version_probe=lambda _executor_id: "local-fake-v1",
    )
    turns = []
    turn_lock = threading.Lock()

    def provider_runner(request, *, registry):
        command = registry.require(request.executor_id).build_command(request)
        fanout.validate_native_boundary(
            request.native_boundary, request, command,
            request.native_controller, request.native_verification,
        )
        assert "(deny network-outbound)" in request.native_boundary.profile_path.read_text()
        observed = []

        def native_runner(exact):
            assert exact == command
            wrapped = fanout.wrap_native_command(
                exact, request.native_boundary, request=request,
                controller=request.native_controller,
                verification=request.native_verification,
            )
            result = fanout.run_command(wrapped)
            observed.append(result)
            return result

        result = provider_module._run_provider_with_runner(
            request, registry=registry, runner=native_runner,
        )
        assert len(observed) == 1
        lines = observed[0].stdout.splitlines()
        context_ok = bool(lines and json.loads(lines[-1]).get("context_ok"))
        with turn_lock:
            turns.append(SimpleNamespace(
                target_id=request.target_id, task_id=request.task_id,
                executor_id=request.executor_id, seat_id=request.seat_id,
                resume=request.resume, request_session_id=request.session_id,
                result_session_id=result.session_id,
                prompt_bytes=request.prompt_bytes, context_ok=context_ok,
                process=observed[0], result=result,
            ))
        return result

    private, run_root, authority_root = (
        tmp_path / name for name in ("native-private", "native-run", "native-authority")
    )
    private.mkdir(mode=0o700)
    started = execute.start_v2_run(
        packet, target_roots=repositories, run_root=run_root,
        authority_root=authority_root, private_root=private,
        skill_roots=(skill_root,), budget=12, runtime=fanout,
        registry=registry, provider_runner=provider_runner,
    )
    assert started["provider_turns"] == 0
    descriptor = execute.load_private_descriptor(private)
    plan = fanout.plan.FanoutPlanV2.from_dict(packet["compiled"]["plan"])
    inputs = fanout.RunInputs.from_dict(descriptor["inputs"])
    owner = fanout.OwnerCapability.from_token(descriptor["owner_token"])
    anchor = fanout.LocalAnchorAuthority(
        authority_root, run_root=run_root, repo_root=repositories["address"],
        target_bindings=inputs.targets,
    )
    journal = fanout.RunJournal.resume(run_root, inputs, owner, anchor_store=anchor)
    controller = fanout.resume_lifecycle_controller(
        run_root / "lifecycle", fanout.LifecycleCapability(descriptor["controller_token"]),
    )
    try:
        with (fanout.ArtifactStore.open_existing(run_root / "artifacts") as artifacts,
              fanout.FileSchedulerBackend.resume(
                  run_root, run_id=inputs.run_id, owner=owner,
              ) as scheduler_backend,
              fanout.FileExecutionBackend.resume(
                  run_root, run_id=inputs.run_id, owner=owner,
              ) as execution_backend):
            baselines = {
                target_id: execute._load_baseline(
                    fanout, artifacts, descriptor["baseline_manifest_refs"][target_id],
                    root, target_id=target_id,
                )
                for target_id, root in repositories.items()
            }
            scheduler = fanout.Scheduler.create(
                plan, inputs, scheduler_backend, artifacts, owner=owner,
                anchor_store=anchor, journal=journal, lifecycle_controller=controller,
            )
            resolver = execute._resolver(fanout, (skill_root,))
            compiled_plan = fanout.canonical_json(plan.to_dict())
            preparations = {}
            for task in plan.tasks:
                if task.execution_class == "orchestrator-action":
                    continue
                baseline = baselines[task.target_id]
                policy = task.provider_policy or plan.defaults
                skill_bundle = execute._task_skill_bundle(fanout, resolver, plan, task.id)
                stage_group = run_root / "skill-stage" / task.id
                stage_group.mkdir(parents=True)
                seats = []
                for executor_id in policy.executor_ids:
                    seat_id = execute._task_seat_id(task.id, executor_id)
                    workspace = fanout.create_seat_workspace(
                        baseline, controller.root / "workspaces", seat_id,
                    )
                    verification = fanout.verify_seat_workspace(
                        baseline, workspace, controller=controller,
                    )
                    admission = resolver.admit_plan(
                        plan, task.id, seat_id=seat_id, provider=executor_id,
                        session_id=execute._admission_session_id(
                            inputs.run_id, task.id, executor_id,
                        ),
                    )
                    admission = resolver.stage(admission, stage_group / seat_id)
                    admission = resolver.verify_engine_delivery(
                        admission, tuple(fanout.SkillLoadEvidence.engine(skill, admission)
                                         for skill in admission.skills),
                    )
                    seats.append(fanout.SeatAssignment.from_admission(
                        admission, skill_bundle=skill_bundle, artifacts=artifacts,
                        workspace_verification=verification,
                    ))
                records = tuple(
                    fanout.DependencyRecord(
                        inputs.run_id, task.id, task.target_id, source_id,
                        next(item.target_id for item in plan.tasks if item.id == source_id),
                        task.dependency_modes[source_id], 1, "0" * 64,
                        fanout.ArtifactRef(f"unsettled/{source_id}", "0" * 64, 0),
                    )
                    for source_id in task.depends_on
                )
                task_bytes = fanout.canonical_json(task.to_dict())
                packet_values = dict(
                    run_id=inputs.run_id, task_id=task.id, attempt=1,
                    compiled_plan=compiled_plan,
                    compiled_plan_sha256=inputs.compiled_plan_sha256,
                    source_markdown=packet["source_markdown"].encode(),
                    task=task_bytes, task_sha256=hashlib.sha256(task_bytes).hexdigest(),
                    skill_bundle=skill_bundle,
                    skill_manifest_sha256=inputs.skill_manifests[task.id],
                    dependency_artifacts=(), dependency_records=records,
                    execution_class=task.execution_class, cwd=repositories[task.target_id],
                    target_binding=inputs.targets[task.target_id], run_inputs=inputs,
                )
                task_packet = fanout.TaskPacket.for_repo_write(
                    workspace_verifications=tuple(
                        seat.workspace_verification for seat in seats
                    ),
                    lifecycle_controller=controller, **packet_values,
                )
                preparations[task.id] = fanout.TaskPreparation(
                    task_packet, tuple(seats),
                    fanout.RoundPolicy.from_provider_policy(policy),
                    inputs, 1, registry=registry,
                )
            memory = _NativeFakeMemory(artifacts)
            coordinator = fanout.CollaborationCoordinator(
                artifacts=artifacts, journal=journal, owner=owner, memory=memory,
                registry=registry, provider_runner=provider_runner,
                lifecycle_controller=controller, repository_baselines=baselines,
                slot_root=run_root / "slots", dependency_scheduler=scheduler,
            )
            service = fanout.ExecutionService(
                plan=plan, inputs=inputs, preparations=preparations,
                provider_profile_digests=dict(inputs.provider_profiles),
                budget=fanout.ExecutionBudget(12), scheduler=scheduler,
                journal=journal, coordinator=coordinator, artifacts=artifacts,
                backend=execution_backend, memory_preflight=memory.preflight,
                repository_baselines=baselines, lifecycle_controller=controller,
            )
            yield SimpleNamespace(
                execute=execute, private_root=private, plan=plan, inputs=inputs,
                owner=owner, journal=journal, controller=controller, artifacts=artifacts,
                scheduler=scheduler, service=service, baselines=baselines,
                registry=registry, provider_runner=provider_runner, turns=turns,
                expected_dependency=expected_dependency,
            )
    finally:
        journal.close()


@pytest.fixture
def two_repo_fixture(tmp_path: Path, request):
    dependency_mode = getattr(request, "param", "handover")
    repositories = {}
    baselines = {}
    specs = {}
    bindings = {}
    for target_id in ("address", "booking"):
        root = tmp_path / target_id
        root.mkdir()
        cases._git(root, "init", "-q", "-b", "main")
        cases._git(root, "config", "user.name", "Fixture")
        cases._git(root, "config", "user.email", "fixture@example.invalid")
        cases._git(root, "config", "remote.origin.url",
                   f"https://github.com/example/{target_id}-service.git")
        (root / "README.md").write_text("shared initial bytes\n")
        cases._git(root, "add", "README.md")
        cases._git(root, "commit", "-qm", "initial")
        repositories[target_id] = root
        baselines[target_id] = fanout.capture_repository_baseline(root)
        specs[target_id] = fanout.TargetSpec(
            target_id, f"github.com/example/{target_id}-service", "TASK-123",
            f"refs/heads/feat/TASK-123-{target_id}",
        )
        bindings[target_id] = fanout.bind_captured_baseline(
            fanout.resolve_target(specs[target_id], root), baselines[target_id],
        )

    tasks = [
        cases._work("address", execution_class="repo-write", seats=2),
        cases._work("booking", depends_on=("address",), execution_class="repo-write", seats=2),
        cases._work("integration", depends_on=("address", "booking"),
                    execution_class="orchestrator-action"),
    ]
    source = cases._plan(*tasks)
    data = source.to_dict()
    data["schema_version"] = "v2"
    data["targets"] = [spec.to_dict() for spec in specs.values()]
    for index, task in enumerate(data["tasks"]):
        target_id = task["id"] if task["id"] != "integration" else "address"
        task["target_id"] = target_id
        task["dependency_modes"] = (
            {"address": dependency_mode} if task["id"] == "booking" else
            {"address": "artifact", "booking": "artifact"}
            if task["id"] == "integration" else {}
        )
        source_id = f"Task {index + 1}/Step 1"
        task["source_step_ids"] = [source_id]
        data["source_steps"][index]["id"] = source_id
        data["source_steps"][index]["target_id"] = target_id
        if task["execution_class"] == "repo-write":
            task["owned_paths"] = ["README.md"]
            task["checks"] = [fanout.PlanCheckV1(
                argv=(sys.executable, "-c", "pass"), cwd="", env_allowlist=(),
                timeout=5, accepted_exit_codes=(0,), expected_artifacts=(),
            ).to_dict()]
    plan = fanout.plan.FanoutPlanV2.from_dict(data)
    inputs = fanout.RunInputs(
        "run-two-repositories", hashlib.sha256(fanout.canonical_json(plan.to_dict())).hexdigest(),
        plan.source.sha256, _digest("draft"), _digest("compiler"), _digest("parser"),
        {f"{executor}/repo-write/standard": _digest(f"profile:{executor}")
         for executor in ("claude", "codex")},
        {target_id: _digest(f"skills:{target_id}") for target_id in repositories},
        profile_shape="class-tier", targets=bindings,
    )
    owner = fanout.OwnerCapability.from_token("o" * 43)
    run = tmp_path / "run"
    anchor = fanout.LocalAnchorAuthority.bootstrap(
        tmp_path / "authority", run_root=run, repo_root=repositories["address"],
        target_bindings=inputs.targets,
    )
    journal, _ = fanout.RunJournal.create(
        run, inputs, anchor_store=anchor, owner_capability=owner,
    )
    artifacts = fanout.ArtifactStore(run / "artifacts")
    backend = fanout.FileSchedulerBackend.create(run, run_id=inputs.run_id, owner=owner)
    controller = fanout.create_lifecycle_controller(run / "lifecycle")
    scheduler = fanout.Scheduler.create(
        plan, inputs, backend, artifacts, owner=owner, anchor_store=anchor,
        journal=journal, lifecycle_controller=controller,
    )
    try:
        yield repositories, baselines, plan, inputs, owner, journal, artifacts, controller, scheduler
    finally:
        journal.close()
        artifacts.close()
        backend.close()


def _reconcile(target_id, fixture):
    _repositories, baselines, plan, inputs, owner, _journal, artifacts, controller, scheduler = fixture
    scheduler.mark_active(target_id, owner=owner)
    scheduler.begin_reconciliation(target_id, owner=owner)
    candidate = fanout.CandidateBundle(
        baselines[target_id].digest,
        (fanout.CandidateEntry("README.md", "file", 0o644,
                               f"verified {target_id} change\n".encode()),), (),
    )
    issued = fanout.issue_target_candidate(
        candidate, task_id=target_id, plan=plan, inputs=inputs,
        store=artifacts, controller=controller,
    )
    verified, receipt = fanout.verify_target_candidate(
        issued, baseline=baselines[target_id], plan=plan, inputs=inputs,
        store=artifacts, controller=controller,
    )
    assert receipt.valid
    result = artifacts.write_bytes(
        f"results/{target_id}/candidate.json", candidate.manifest_bytes,
    )
    scheduler.complete_reconciliation(
        target_id, scheduler.result_receipt(target_id, result), owner=owner,
    )
    return issued, verified


def _deliver(target_id, fixture):
    _repositories, _baselines, plan, inputs, owner, journal, artifacts, controller, scheduler = fixture
    issued, verified = _reconcile(target_id, fixture)
    handover = __import__(f"{fanout.__name__}.branch_handover", fromlist=["branch_handover"])
    prepared = handover.prepare_branch_handover(
        inputs.targets[target_id], issued, verified, plan=plan, inputs=inputs,
        artifacts=artifacts, scheduler=scheduler, controller=controller,
        journal=journal, owner=owner, task_id=target_id,
    )
    terminal = handover.deliver_branch_candidate(
        prepared, plan=plan, inputs=inputs, artifacts=artifacts, scheduler=scheduler,
        controller=controller, journal=journal, owner=owner,
    )
    assert isinstance(terminal, fanout.HandoverTerminalV2)
    return terminal


def _reconcile_and_deliver_native_writer(run, task_id):
    task = next(item for item in run.plan.tasks if item.id == task_id)
    baseline = run.baselines[task.target_id]
    state = run.service._execution_state(task_id)
    barrier = run.service._restore_barrier(
        state.barriers[-1], run.service._packet_for(task),
    )
    sources = tuple(
        fanout.CandidateBundle.from_manifest(run.artifacts.read_bytes(ref))
        for _seat_id, ref in barrier.candidate_sources
    )
    assert len(sources) == len(task.provider_policy.executor_ids if task.provider_policy
                               else run.plan.defaults.executor_ids)
    assert all(source.entries == (
        fanout.CandidateEntry(
            "README.md", "file", 0o644,
            f"verified {task.target_id} change\n".encode(),
        ),
    ) for source in sources)
    candidate = fanout.synthesize_candidate(
        baseline, sources=sources,
        entries=(fanout.CandidateEntry(
            "README.md", "file", 0o644,
            f"verified {task.target_id} change\n".encode(),
        ),), deleted_paths=(),
    )
    verification = run.service.verify_repo_synthesis(
        task_id, candidate, owner=run.owner,
    )
    assert verification.valid
    settled = run.service.submit(task_id, verification, owner=run.owner)
    assert settled.artifact.digest == candidate.digest
    issued = fanout.issue_target_candidate(
        candidate, task_id=task_id, plan=run.plan, inputs=run.inputs,
        store=run.artifacts, controller=run.controller,
    )
    verified, receipt = fanout.verify_target_candidate(
        issued, baseline=baseline, plan=run.plan, inputs=run.inputs,
        store=run.artifacts, controller=run.controller,
    )
    assert receipt.valid
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    prepared = handover.prepare_branch_handover(
        run.inputs.targets[task.target_id], issued, verified,
        plan=run.plan, inputs=run.inputs, artifacts=run.artifacts,
        scheduler=run.scheduler, controller=run.controller,
        journal=run.journal, owner=run.owner, task_id=task_id,
    )
    terminal = handover.deliver_branch_candidate(
        prepared, plan=run.plan, inputs=run.inputs, artifacts=run.artifacts,
        scheduler=run.scheduler, controller=run.controller,
        journal=run.journal, owner=run.owner,
    )
    assert isinstance(terminal, fanout.HandoverTerminalV2)
    return terminal


def test_two_repositories_one_run_two_ticket_branches(two_repo_fixture):
    fixture = two_repo_fixture
    repositories, _baselines, _plan, _inputs, owner, _journal, artifacts, _controller, scheduler = fixture
    assert [decision.task_id for decision in scheduler.schedule_ready(owner=owner)] == ["address"]
    assert all(cases._git(root, "for-each-ref", "--format=%(objectname)",
                          f"refs/heads/feat/TASK-123-{target_id}") == ""
               for target_id, root in repositories.items())
    address_terminal = _deliver("address", fixture)
    decision, = scheduler.schedule_ready(owner=owner)
    assert decision.task_id == "booking"
    assert decision.dependencies[0].kind == "handover"
    booking_context = json.loads(fanout.build_verified_dependency_context(
        (scheduler.dependency_record_for("booking", "address"),),
        store=artifacts, scheduler=scheduler, max_bytes=4096,
    ))
    assert booking_context[0]["record"]["source_target_id"] == "address"
    assert booking_context[0]["record"]["recipient_target_id"] == "booking"
    assert booking_context[0]["trust"] == "untrusted"
    assert base64.b64decode(booking_context[0]["content_base64"]) == artifacts.read_bytes(
        address_terminal.evidence,
    )
    _deliver("booking", fixture)
    barrier, = scheduler.schedule_ready(owner=owner)
    assert barrier.task_id == "integration" and barrier.barrier_id
    assert not hasattr(barrier, "seat_count")
    assert {dependency.source_task_id for dependency in barrier.dependencies} == {
        "address", "booking",
    }
    integration_context = json.loads(fanout.build_verified_dependency_context(
        tuple(scheduler.dependency_record_for("integration", source)
              for source in ("address", "booking")),
        store=artifacts, scheduler=scheduler, max_bytes=4096,
    ))
    assert {item["record"]["source_target_id"] for item in integration_context} == {
        "address", "booking",
    }
    assert fanout.ExecutionService.v2_status(scheduler).overall_state == "pending"
    action = artifacts.write_bytes("results/integration/owner-review.json", b"{}\n")
    scheduler.complete_action(
        "integration", scheduler.result_receipt("integration", action), owner=owner,
    )
    assert fanout.ExecutionService.v2_status(scheduler).overall_state == "delivered"
    for target_id, root in repositories.items():
        branch = f"refs/heads/feat/TASK-123-{target_id}"
        assert cases._git(root, "symbolic-ref", "--no-recurse", "HEAD") == branch
        assert cases._git(root, "rev-list", "--count", "main..HEAD") == "1"
        assert (root / "README.md").read_text() == f"verified {target_id} change\n"


def test_failed_integration_barrier_does_not_relabel_delivered_repositories(two_repo_fixture):
    fixture = two_repo_fixture
    _repositories, _baselines, _plan, _inputs, owner, _journal, _artifacts, _controller, scheduler = fixture
    scheduler.schedule_ready(owner=owner)
    _deliver("address", fixture)
    scheduler.schedule_ready(owner=owner)
    _deliver("booking", fixture)
    scheduler.schedule_ready(owner=owner)
    scheduler.fail_task("integration", "owner review failed", owner=owner)
    status = fanout.ExecutionService.v2_status(scheduler)
    assert status.target_states == {"address": "delivered", "booking": "delivered"}
    assert status.overall_state == "blocked"


def test_two_writer_admission_starts_dormant_owner_run(two_repo_fixture, tmp_path, capsys):
    repositories = two_repo_fixture[0]
    packet, skill_root = _admitted_two_writer_packet(tmp_path)
    spec = importlib.util.spec_from_file_location("multirepo_execute_skill", cli_cases.CLI)
    assert spec is not None and spec.loader is not None
    execute = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = execute
    spec.loader.exec_module(execute)
    private = tmp_path / "cli-private"
    private.mkdir(mode=0o700)
    result = execute.start_v2_run(
        packet, target_roots=repositories,
        run_root=tmp_path / "cli-run", authority_root=tmp_path / "cli-authority",
        private_root=private, skill_roots=(skill_root,), budget=12,
        runtime=cli_cases.fanout, registry=cli_cases._registry(),
        provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"),
    )
    assert result["provider_turns"] == 0
    assert result["authority_state"] == "dormant"
    assert execute.main(
        ["status", "--private-root", str(private)], runtime=cli_cases.fanout,
        registry=cli_cases._registry(),
        provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"),
    ) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["target_states"] == {"address": "pending", "booking": "pending"}
    assert all(cases._git(root, "for-each-ref", "--format=%(objectname)",
                          f"refs/heads/feat/TASK-123-{target_id}") == ""
               for target_id, root in repositories.items())


def test_admitted_packet_runs_native_fake_seats_through_both_handovers(
    two_repo_fixture, tmp_path, capsys, monkeypatch,
):
    repositories = two_repo_fixture[0]
    packet, skill_root = _admitted_two_writer_packet(tmp_path)
    with _admitted_native_fake_service(
        tmp_path, packet, skill_root, repositories, monkeypatch,
    ) as run:
        assert run.service.plan.to_dict() == packet["compiled"]["plan"]
        assert all(cases._git(root, "status", "--porcelain") == ""
                   for root in repositories.values())
        assert run.service.start(owner=run.owner).tasks[0].phase == "scheduled"
        assert run.turns == []
        run.service.resume(owner=run.owner)
        assert run.scheduler.task_phase("change") == "reconciliation-pending"
        assert not any(turn.target_id == "booking" for turn in run.turns)
        assert all(cases._git(root, "status", "--porcelain") == ""
                   for root in repositories.values())
        assert all(cases._git(root, "symbolic-ref", "--no-recurse", "HEAD") ==
                   "refs/heads/main" for root in repositories.values())
        address_terminal = _reconcile_and_deliver_native_writer(run, "change")
        run.expected_dependency["booking"] = base64.b64encode(
            run.artifacts.read_bytes(address_terminal.evidence),
        ).decode()
        run.service.resume(owner=run.owner)
        assert run.scheduler.task_phase("booking") == "reconciliation-pending"
        booking_turns = [turn for turn in run.turns if turn.target_id == "booking"]
        assert len(booking_turns) == 6
        assert len(run.turns) == 12
        assert sum(turn.resume for turn in run.turns) == 6
        for task_id in ("change", "booking"):
            for executor_id in ("claude", "codex", "agy"):
                initial, resumed = sorted(
                    (turn for turn in run.turns if turn.task_id == task_id
                     and turn.executor_id == executor_id),
                    key=lambda turn: turn.resume,
                )
                assert not initial.resume and resumed.resume
                assert resumed.request_session_id == initial.result_session_id
                assert resumed.result_session_id == initial.result_session_id
        evidence = base64.b64encode(run.artifacts.read_bytes(address_terminal.evidence))
        assert all(evidence in turn.prompt_bytes for turn in booking_turns)
        assert all(turn.context_ok and turn.process.returncode == 0
                   for turn in booking_turns)
        assert all(b"Operation not permitted" in turn.process.stderr
                   for turn in run.turns)
        _reconcile_and_deliver_native_writer(run, "booking")
        barrier, = run.scheduler.pending_actions()
        assert barrier.task_id == "integration" and barrier.barrier_id
        action = run.artifacts.write_bytes("results/integration/native-owner-review.json", b"{}\n")
        run.scheduler.complete_action(
            "integration", run.scheduler.result_receipt("integration", action),
            owner=run.owner,
        )
        assert fanout.ExecutionService.v2_status(run.scheduler).overall_state == "delivered"
        before = len(run.turns)
        assert run.execute.main(
            ["resume", "--private-root", str(run.private_root)],
            runtime=fanout, registry=run.registry, provider_runner=run.provider_runner,
        ) == 2
        assert len(run.turns) == before
        assert "validated native seat boundary" in capsys.readouterr().err
        for target_id, root in repositories.items():
            assert cases._git(root, "symbolic-ref", "--no-recurse", "HEAD") == (
                f"refs/heads/feat/TASK-123-{target_id}"
            )
            assert cases._git(root, "rev-list", "--count", "main..HEAD") == "1"
            assert (root / "README.md").read_text() == f"verified {target_id} change\n"


@pytest.mark.parametrize("two_repo_fixture", ["artifact"], indirect=True)
def test_cross_repository_artifact_unlocks_before_branch_handover(two_repo_fixture):
    fixture = two_repo_fixture
    repositories, _baselines, _plan, _inputs, owner, _journal, artifacts, _controller, scheduler = fixture
    scheduler.schedule_ready(owner=owner)
    _reconcile("address", fixture)
    decision, = scheduler.schedule_ready(owner=owner)
    assert decision.task_id == "booking"
    assert decision.dependencies[0].kind == "artifact"
    assert decision.dependencies[0].source_target_id == "address"
    context = json.loads(fanout.build_verified_dependency_context(
        (scheduler.dependency_record_for("booking", "address"),),
        store=artifacts, scheduler=scheduler, max_bytes=4096,
    ))
    assert context[0]["record"]["kind"] == "artifact"
    assert context[0]["record"]["recipient_target_id"] == "booking"
    assert base64.b64decode(context[0]["content_base64"]) == artifacts.read_bytes(
        decision.dependencies[0].artifact,
    )
    assert cases._git(repositories["address"], "for-each-ref", "--format=%(objectname)",
                      "refs/heads/feat/TASK-123-address") == ""


def test_failed_booking_after_address_delivery_cold_reopens_as_partial(two_repo_fixture):
    fixture = two_repo_fixture
    repositories, _baselines, plan, inputs, owner, journal, _artifacts, controller, scheduler = fixture
    scheduler.schedule_ready(owner=owner)
    _deliver("address", fixture)
    decision, = scheduler.schedule_ready(owner=owner)
    assert decision.task_id == "booking"
    scheduler.fail_task("booking", "disposable booking failure", owner=owner)
    assert fanout.ExecutionService.v2_status(scheduler).target_states == {
        "address": "delivered", "booking": "blocked",
    }
    run = repositories["address"].parent / "run"
    authority = repositories["address"].parent / "authority"
    before = tuple(sorted(
        (str(path.relative_to(repositories["address"].parent)), path.read_bytes())
        for root in (run, authority) for path in root.rglob("*") if path.is_file()
    ))
    commit = cases._git(repositories["address"], "rev-parse", "HEAD")
    journal.close()
    inspected = fanout.LocalAnchorAuthority.inspect(
        authority, run_root=run, repo_root=repositories["address"],
        target_bindings=inputs.targets,
    )
    cold_controller = fanout.resume_lifecycle_controller(
        controller.root, controller.capability,
    )
    with fanout.ArtifactStore.open_existing(run / "artifacts") as cold_artifacts:
        statuses = [fanout.Scheduler.inspect_status(
            plan, inputs, run, cold_artifacts, owner=owner,
            original_journal_inputs=inputs, anchor_store=inspected,
            lifecycle_controller=cold_controller,
        ).to_dict() for _ in range(2)]
    assert statuses[0] == statuses[1]
    assert statuses[0]["overall_state"] == "partial"
    assert statuses[0]["target_states"] == {"address": "delivered", "booking": "blocked"}
    after = tuple(sorted(
        (str(path.relative_to(repositories["address"].parent)), path.read_bytes())
        for root in (run, authority) for path in root.rglob("*") if path.is_file()
    ))
    assert before == after
    assert cases._git(repositories["address"], "rev-parse", "HEAD") == commit
    assert cases._git(repositories["address"], "rev-list", "--count", "main..HEAD") == "1"
    assert cases._git(repositories["booking"], "for-each-ref", "--format=%(objectname)",
                      "refs/heads/feat/TASK-123-booking") == ""
