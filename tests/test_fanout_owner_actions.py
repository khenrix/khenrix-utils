"""Public owner synthesis and native read recovery contracts."""
import dataclasses
import hashlib
import importlib.util
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_fanout_execute_skill import (
    ROOT, PLANNER, execute, fanout, _v2_write_packet, _v2_target_root,
    _registry, _NeverMemory, _v2_owner_approval, _git, _git_output,
    _question_packet, _repository, _write_packet,
)


def test_public_v2_blocked_memory_status_is_read_only_without_provider_replay(execute, tmp_path, capsys):
    packet = _v2_read_packet(tmp_path)
    repo = _v2_target_root(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
        memory_preflight=lambda: True,
        provider_runner=lambda *_args, **_kwargs: pytest.fail("status launched a provider"))
    execute.start_v2_run(packet, target_roots={"address": repo}, run_root=run,
        authority_root=authority, private_root=private, skill_roots=(tmp_path / "skills",),
        budget=6, runtime=fanout, registry=_registry(), provider_runner=common["provider_runner"])
    with execute.open_run(private, **common) as opened:
        opened.service.start(owner=opened.owner)
        opened.journal.append("blocked-memory", task_id="change", owner=opened.owner)
    before = {str(path): path.read_bytes() for root in (private, run, authority)
              for path in root.rglob("*") if path.is_file()}
    for _ in range(2):
        assert execute.main(["status", "--private-root", str(private)], **common) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["tasks"][0]["phase"] == "blocked-memory"
        assert status["read_only"] is True
    assert {str(path): path.read_bytes() for root in (private, run, authority)
            for path in root.rglob("*") if path.is_file()} == before


def test_v2_read_only_cli_dispatch_admits_trusted_native_profiles_but_refuses_worker_writes(execute):
    plan = SimpleNamespace(schema_version="v2", tasks=(
        SimpleNamespace(kind="work", execution_class="read-only"),
    ))
    execute._require_native_seat_boundary(plan)
    execute._require_native_seat_boundary(SimpleNamespace(
        schema_version="v1", tasks=plan.tasks,
    ))
    with pytest.raises(ValueError, match="repo-write"):
        execute._require_native_seat_boundary(SimpleNamespace(schema_version="v2", tasks=(
            SimpleNamespace(kind="work", execution_class="repo-write"),
        )))


def test_question_draft_requires_every_configured_final_seat(execute, tmp_path):
    packet, resolver = _question_packet(tmp_path)
    admitted = execute.verify_admission(packet, fanout, resolver)
    assert admitted.plan.defaults.minimum_success == len(admitted.plan.defaults.executor_ids)



def test_public_v1_owner_application_restores_authenticated_verifier_on_cold_reopen(execute, tmp_path, capsys):
    original, _ = _write_packet(tmp_path)
    source = original["source_markdown"] + (
        "- [ ] **Step 2: Apply owner synthesis**\n  **Depends on:** Task 1/Step 1\n\n"
        "The invoking owner applies the checked synthesis after reading all proposals.\n"
    )
    draft = original["draft"]
    proposal = draft["tasks"][0]
    owner = {**proposal, "id": "apply", "execution_class": "orchestrator-action",
             "source_step_ids": ["Task 1/Step 2"], "depends_on": ["change"],
             "required_skills": [], "none_reason": "The owner applies checked proposals.",
             "checks": [{"argv": [sys.executable, "-c", "from pathlib import Path; assert Path('src/change.py').read_bytes() == b'owner synthesis\\n'"],
                         "cwd": "", "env_allowlist": [], "timeout": 10,
                         "accepted_exit_codes": [0], "expected_artifacts": []}]}
    proposal.update(execution_class="read-only", owned_paths=[], checks=[])
    draft.update(source_sha256=hashlib.sha256(source.encode()).hexdigest(), tasks=[proposal, owner])
    ingress = {"schema_version": "fanout-bundle-ingress-v1", "quality_tier": "normal",
               "source_path": "docs/change.md", "source_markdown": source, "draft": draft}
    bundle, review = tmp_path / "owner-v1-ingress.json", tmp_path / "owner-v1-review.json"
    bundle.write_bytes(fanout.canonical_json(ingress))
    review.write_bytes(fanout.canonical_json({
        "schema_version": "fanout-owner-review-v1", "reviewer": "owner",
        "binding_sha256": hashlib.sha256(fanout.canonical_json(ingress)).hexdigest(),
    }))
    planned = subprocess.run([sys.executable, str(PLANNER), "from-bundle", "--bundle", str(bundle),
        "--owner-review-file", str(review), "--skill-root", str(tmp_path / "skills")],
        capture_output=True, text=True, check=False)
    assert planned.returncode == 0, planned.stderr
    packet = json.loads(planned.stdout)
    spec = importlib.util.spec_from_file_location("fanout_v1_owner_transport", ROOT / "scripts/fanout_smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = smoke
    spec.loader.exec_module(smoke)
    smoke.fanout = fanout
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    common = dict(runtime=fanout, registry=_registry(),
        memory_factory=lambda artifacts, health: smoke._FakeMemory(tmp_path, artifacts),
        memory_preflight=lambda: True, provider_runner=smoke._FakeProvider(tmp_path, poison_peer=False))
    execute.prepare_run(packet, repo_root=repo, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=6, **common)
    with execute.open_run(private, **common) as opened:
        opened.service.resume(owner=opened.owner)
        verified = opened.service.verify_read_only_synthesis(
            "change", source_seat_ids=tuple(seat.seat_id for seat in opened.service.preparations["change"].seats),
            synthesizer_id="owner", answer=b"All three proposals reconciled; dissent retained in sources.\n", owner=opened.owner)
        opened.service.submit("change", verified, owner=opened.owner)
        candidate = fanout.CandidateBundle(opened.service.baseline.digest, (
            fanout.CandidateEntry("src", "directory", 0o755, b""),
            fanout.CandidateEntry("src/change.py", "file", 0o644, b"owner synthesis\n"),
        ), ())
    candidate_file = tmp_path / "owner-v1-candidate.json"
    candidate_file.write_bytes(candidate.manifest_bytes)
    assert execute.main(["preview-owner", "--private-root", str(private), "--task-id", "apply",
        "--candidate-file", str(candidate_file)], **common) == 0
    preview = json.loads(capsys.readouterr().out)
    candidate_ref = tmp_path / "owner-v1-ref.json"
    candidate_ref.write_bytes(fanout.canonical_json(preview["candidate"]))
    approval = _v2_owner_approval(tmp_path, action="action-complete", run_id=preview["run_id"],
        task_id="apply", evidence={"artifact": preview["candidate"]})
    before = fanout.capture_repository_baseline(repo).digest
    assert execute.main(["action-complete", "--private-root", str(private), "--task-id", "apply",
        "--artifact-ref-file", str(candidate_ref), "--owner-approval-file", str(approval)], **common) == 0
    capsys.readouterr()
    with execute.open_run(private, **common) as reopened:
        assert reopened.service.scheduler.task_phase("apply") == "completed"
    assert execute.main(["status", "--private-root", str(private)], **common) == 0
    assert fanout.capture_repository_baseline(repo).digest == before


def _v2_read_packet(tmp_path):
    original, _ = _v2_write_packet(tmp_path)
    source = original["source_markdown"].replace("- Modify: `src/change.py`\n", "")
    draft = original["draft"]
    draft["source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
    draft["tasks"][0].update(execution_class="read-only", owned_paths=[], checks=[])
    ingress = {"schema_version": "fanout-bundle-ingress-v2", "quality_tier": "normal",
               "source_path": "docs/change.md", "source_markdown": source, "draft": draft}
    review = {"schema_version": "fanout-owner-review-v2", "reviewer": "owner",
              "binding_sha256": hashlib.sha256(fanout.canonical_json({
                  "schema_version": "fanout-owner-review-binding-v2", "bundle": ingress,
              })).hexdigest()}
    bundle, approval = tmp_path / "read-ingress.json", tmp_path / "read-review.json"
    bundle.write_bytes(fanout.canonical_json(ingress))
    approval.write_bytes(fanout.canonical_json(review))
    result = subprocess.run(
        [sys.executable, str(PLANNER), "from-bundle", "--bundle", str(bundle),
         "--owner-review-file", str(approval), "--skill-root", str(tmp_path / "skills")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _v2_owner_packet(tmp_path, *, two_targets=False):
    original, _ = _v2_write_packet(tmp_path)
    source = original["source_markdown"] + (
        "- [ ] **Step 2: Apply owner synthesis**\n  **Depends on:** Task 1/Step 1\n\n"
        "The invoking owner applies the checked synthesis after reading all proposals.\n"
    )
    draft = original["draft"]
    draft["source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
    proposal = draft["tasks"][0]
    owner = {**proposal, "id": "apply", "execution_class": "orchestrator-action",
             "source_step_ids": ["Task 1/Step 2"], "depends_on": ["change"],
             "required_skills": [], "none_reason": "The owner applies checked proposals.",
             "dependency_modes": {"change": "artifact"},
             "checks": [{"argv": [sys.executable, "-c", "from pathlib import Path; assert Path('src/change.py').read_bytes() == b'owner synthesis\\n'"],
                         "cwd": "", "env_allowlist": [], "timeout": 10,
                         "accepted_exit_codes": [0], "expected_artifacts": []}]}
    proposal.update(execution_class="read-only", owned_paths=[], checks=[])
    draft["tasks"].append(owner)
    if two_targets:
        source += ("\n### Task 2: Apply booking proposal\n\n**Target:** booking\n\n**Files:**\n"
                   "- Create: `src/change.py`\n\n"
                   "- [ ] **Step 1: Read booking proposals**\n  **Depends on:** Task 1/Step 2\n"
                   "  **Dependency modes:** Task 1/Step 2=handover\n\nRead the delivered address result.\n\n"
                   "- [ ] **Step 2: Apply booking synthesis**\n  **Depends on:** Task 2/Step 1\n\n"
                   "The owner applies the checked booking synthesis.\n")
        draft["source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
        draft["targets"].append({"id": "booking", "repository": "github.com/example/booking-service",
            "ticket_key": "TASK-123", "branch_ref": "refs/heads/feat/TASK-123-booking"})
        draft["tasks"].extend([
            {**proposal, "id": "read-booking", "target_id": "booking", "source_step_ids": ["Task 2/Step 1"],
             "depends_on": ["apply"], "dependency_modes": {"apply": "handover"}},
            {**owner, "id": "apply-booking", "target_id": "booking", "source_step_ids": ["Task 2/Step 2"],
             "depends_on": ["read-booking"], "dependency_modes": {"read-booking": "artifact"}},
        ])
    ingress = {"schema_version": "fanout-bundle-ingress-v2", "quality_tier": "normal",
               "source_path": "docs/change.md", "source_markdown": source, "draft": draft}
    approval = {"schema_version": "fanout-owner-review-v2", "reviewer": "owner",
                "binding_sha256": hashlib.sha256(fanout.canonical_json({
                    "schema_version": "fanout-owner-review-binding-v2", "bundle": ingress,
                })).hexdigest()}
    bundle, review = tmp_path / "owner-ingress.json", tmp_path / "owner-review.json"
    bundle.write_bytes(fanout.canonical_json(ingress))
    review.write_bytes(fanout.canonical_json(approval))
    result = subprocess.run(
        [sys.executable, str(PLANNER), "from-bundle", "--bundle", str(bundle),
         "--owner-review-file", str(review), "--skill-root", str(tmp_path / "skills")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("drift", (None, "recover", "worktree", "branch", "approval", "scheduler-scope"))
def test_public_owner_preview_completion_and_ticket_handover(execute, tmp_path, capsys, monkeypatch, drift):
    spec = importlib.util.spec_from_file_location("fanout_owner_transport_fixture", ROOT / "scripts/fanout_smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = smoke
    spec.loader.exec_module(smoke)
    smoke.fanout = fanout
    packet = _v2_owner_packet(tmp_path)
    repo = _v2_target_root(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    versions = {"claude": "2.2.0", "codex": "0.163.0", "agy": "1.4.0"}
    registry = fanout.ProviderRegistry((
        dataclasses.replace(profile, cli_version=versions[profile.executor_id], profile_sha256=None)
        for profile in _registry()._profiles.values()
    ), version_probe=lambda executor: versions[executor])
    common = dict(runtime=fanout, registry=registry,
                  memory_factory=lambda artifacts, health: smoke._FakeMemory(tmp_path, artifacts),
                  memory_preflight=lambda: True, provider_runner=smoke._FakeProvider(tmp_path, poison_peer=False))
    execute.start_v2_run(
        packet, target_roots={"address": repo}, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=6,
        runtime=fanout, registry=registry, provider_runner=common["provider_runner"],
    )
    with execute.open_run(private, for_dispatch=True, **common) as opened:
        opened.service.start(owner=opened.owner)
        opened.service.resume(owner=opened.owner)
        verified = opened.service.verify_read_only_synthesis(
            "change", source_seat_ids=tuple(seat.seat_id for seat in opened.service.preparations["change"].seats),
            synthesizer_id="owner", answer=b"All three proposals reviewed; source disagreements retained.\n", owner=opened.owner,
        )
        opened.service.submit("change", verified, owner=opened.owner)
        baseline = opened.service.repository_baselines["address"]
        candidate = fanout.CandidateBundle(baseline.digest, (
            fanout.CandidateEntry("src", "directory", 0o755, b""),
            fanout.CandidateEntry("src/change.py", "file", 0o644, b"owner synthesis\n"),
        ), ())
        if drift == "scheduler-scope":
            candidate = fanout.CandidateBundle(baseline.digest, candidate.entries + (
                fanout.CandidateEntry("outside.txt", "file", 0o644, b"unowned\n"),
            ), ())
            verified = fanout.verify_candidate(
                baseline, candidate, opened.service._task("apply").checks,
                controller=opened.service.lifecycle_controller,
                reconciliation=opened.service._owner_candidate_binding(
                    opened.service._task("apply"), persist=True),
            )
            assert verified.valid
            ref = opened.service._write_exact(
                fanout.candidate_result_path("apply", candidate.digest, verified.evidence_digest),
                candidate.manifest_bytes,
            )
            before = fanout.capture_repository_baseline(repo).digest
            refs = _git_output(repo, "show-ref")
            with pytest.raises(fanout.SchedulerStateError, match="scope|verifier"):
                receipt = opened.service.scheduler.result_receipt("apply", ref)
                opened.service.scheduler.complete_action("apply", receipt, owner=opened.owner)
            assert opened.service.scheduler.task_phase("apply") == "blocked-action"
            assert fanout.capture_repository_baseline(repo).digest == before
            assert _git_output(repo, "show-ref") == refs
            return
    candidate_file = tmp_path / "owner-candidate.json"
    candidate_file.write_bytes(candidate.manifest_bytes)
    before = fanout.capture_repository_baseline(repo).digest
    original_refs = _git_output(repo, "show-ref")
    assert execute.main(["preview-owner", "--private-root", str(private), "--task-id", "apply",
                         "--candidate-file", str(candidate_file)], **common) == 0
    preview = json.loads(capsys.readouterr().out)
    assert fanout.capture_repository_baseline(repo).digest == before
    assert _git_output(repo, "show-ref") == original_refs
    candidate_ref_file = tmp_path / "verified-ref.json"
    candidate_ref_file.write_bytes(fanout.canonical_json(preview["candidate"]))
    approval = _v2_owner_approval(tmp_path, action="action-complete", run_id=preview["run_id"],
                                  task_id="apply", evidence={"artifact": preview["candidate"]})
    assert execute.main(["action-complete", "--private-root", str(private), "--task-id", "apply",
                         "--artifact-ref-file", str(candidate_ref_file), "--owner-approval-file", str(approval)], **common) == 0
    capsys.readouterr()
    with execute.open_run(private, **common) as opened:
        assert opened.service.scheduler.handover_source_for("apply").artifact.digest == candidate.digest
    with execute.open_run(private, **common) as opened:
        source = opened.service.scheduler.handover_source_for("apply")
        binding = opened.service.inputs.targets["address"]
        evidence = {"target_id": "address", "candidate": preview["target_candidate"],
                    "verification": preview["target_verification"], "target_binding": binding.to_dict(),
                    "settled_source": {"run_id": source.run_id, "task_id": source.task_id,
                        "plan_revision": source.plan_revision, "plan_sha256": source.plan_sha256,
                        "inputs_digest": source.inputs_digest, "artifact": execute._ref_document(source.artifact)}}
    approval = _v2_owner_approval(tmp_path, action="handover", run_id=preview["run_id"], task_id="apply", evidence=evidence)
    target_candidate, target_verification = tmp_path / "target-candidate.json", tmp_path / "target-verification.json"
    target_candidate.write_bytes(fanout.canonical_json(preview["target_candidate"]))
    target_verification.write_bytes(fanout.canonical_json(preview["target_verification"]))
    if drift == "worktree":
        (repo / "source.txt").write_text("late owner edit\n")
    elif drift == "branch":
        _git(repo, "update-ref", "refs/heads/feat/TASK-123-address", _git_output(repo, "rev-parse", "HEAD"))
    elif drift == "approval":
        document = json.loads(approval.read_text())
        document["task_id"] = "other-action"
        approval.write_bytes(fanout.canonical_json(document))
    before_handover = fanout.capture_repository_baseline(repo).digest
    before_refs = _git_output(repo, "show-ref")
    handover = importlib.import_module(f"{fanout.__name__}.branch_handover")
    if drift == "recover":
        def interrupt(phase):
            if phase == "after-ref-cas":
                raise RuntimeError("interrupted handover")
        monkeypatch.setattr(handover, "_checkpoint", interrupt)
    exit_code = execute.main(["handover", "--private-root", str(private), "--task-id", "apply",
                         "--candidate-ref-file", str(target_candidate), "--verification-ref-file", str(target_verification),
                         "--owner-approval-file", str(approval)], runtime=fanout,
                         **({"registry": registry} if drift == "recover" else {}))
    if drift == "recover":
        assert exit_code == 2
        assert "exact recovery" in capsys.readouterr().err
        monkeypatch.setattr(handover, "_checkpoint", lambda _phase: None)
        descriptor = execute.load_private_descriptor(private)
        inputs = fanout.RunInputs.from_dict(descriptor["inputs"])
        owner = fanout.OwnerCapability.from_token(descriptor["owner_token"])
        anchor = fanout.LocalAnchorAuthority.inspect(
            authority, run_root=run, repo_root=repo, target_bindings=inputs.targets,
        )
        inspected = fanout.RunJournal.inspect(run, inputs, owner, anchor_store=anchor)
        intent, terminal = inspected.state.branch_handovers["apply"]
        assert terminal is None
        association = inspected.state.branch_records["apply"]["association"]
        recovery_approval = _v2_owner_approval(
            tmp_path, action="recover-handover", run_id=inputs.run_id, task_id="apply",
            evidence={"intent": intent.to_dict(),
                      "association": {"name": association[0], "digest": association[1]}},
        )
        exit_code = execute.main([
            "recover-handover", "--private-root", str(private), "--task-id", "apply",
            "--owner-approval-file", str(recovery_approval),
        ], runtime=fanout)
    if drift not in (None, "recover"):
        assert exit_code == 2
        assert fanout.capture_repository_baseline(repo).digest == before_handover
        assert _git_output(repo, "show-ref") == before_refs
        assert not (repo / "src/change.py").exists()
        return
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "committed"
    assert (repo / "src/change.py").read_bytes() == b"owner synthesis\n"
    assert _git_output(repo, "symbolic-ref", "HEAD") == "refs/heads/feat/TASK-123-address"


def test_v2_public_resume_opens_native_read_run_without_certificate_or_spend(execute, tmp_path, capsys):
    packet = _v2_read_packet(tmp_path)
    repo = _v2_target_root(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    execute.start_v2_run(
        packet, target_roots={"address": repo}, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=6,
        runtime=fanout, registry=_registry(),
    )
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True)
    assert execute.main(["resume", "--private-root", str(private)], **common) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["tasks"][0]["phase"] == "scheduled"
    assert document["projected_provider_turns"] == 6
    with execute.open_run(private, for_dispatch=True, **common) as opened:
        assert opened.service.scheduler.task_phase("change") == "scheduled"
        assert not opened.journal.state.seat_phases


def test_fresh_v2_admission_records_actual_compatible_installed_cli_versions(execute, tmp_path, monkeypatch):
    versions = {"claude": "2.2.0", "codex": "0.160.0", "agy": "1.3.0"}
    monkeypatch.setattr(fanout.providers, "_installed_cli_version", lambda executor: versions[executor])
    packet = _v2_read_packet(tmp_path)
    repo = _v2_target_root(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    started = execute.start_v2_run(packet, target_roots={"address": repo}, run_root=run,
        authority_root=authority, private_root=private, skill_roots=(tmp_path / "skills",),
        budget=6, runtime=fanout)
    descriptor = execute.load_private_descriptor(private)
    profiles = descriptor["provider_profile_descriptors"]
    observed = {profile["executor_id"]: profile["cli_version"] for profile in profiles
                if profile["execution_class"] == "read-only"}
    assert observed == {"claude": "2.2.0", "codex": "0.160.0", "agy": "1.3.0"}
    assert started["provider_turns"] == 0
    assert not (run / "slots").exists()


def test_v2_public_cold_resume_does_not_replay_uncertain_seat(execute, tmp_path, capsys):
    packet = _v2_read_packet(tmp_path)
    repo = _v2_target_root(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    launches = []
    def no_provider(request, *, registry):
        launches.append((request.executor_id, request.seat_id))
        raise AssertionError("an uncertain seat must not be replayed")
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True, provider_runner=no_provider)
    execute.start_v2_run(packet, target_roots={"address": repo}, run_root=run,
        authority_root=authority, private_root=private, skill_roots=(tmp_path / "skills",),
        budget=6, runtime=fanout, registry=_registry(), provider_runner=no_provider)
    with execute.open_run(private, **common) as opened:
        opened.service.start(owner=opened.owner)
        seat = opened.service.preparations["change"].seats[0]
        opened.journal.append("dispatch-intent", task_id="change", seat_id=seat.seat_id,
                              attempt=1, round=1, evidence_sha256="a" * 64)
        opened.journal.append("process-started", task_id="change", seat_id=seat.seat_id, attempt=1, round=1)
    assert execute.main(["resume", "--private-root", str(private)], **common) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["tasks"][0]["phase"] == "failed"
    assert (seat.executor_id, seat.seat_id) not in launches
    assert len(launches) == len(set(launches)) <= 2
    with execute.open_run(private, **common) as reopened:
        assert reopened.journal.state.seat_phase("change", seat.seat_id, 1, 1) == "uncertain-attempt"


def test_owner_actions_deliver_same_ticket_in_two_repositories_with_cold_handover_dependency(execute, tmp_path):
    spec = importlib.util.spec_from_file_location("fanout_multirepo_owner_transport", ROOT / "scripts/fanout_smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = smoke
    spec.loader.exec_module(smoke)
    smoke.fanout = fanout
    packet = _v2_owner_packet(tmp_path, two_targets=True)
    roots, providers = {}, {}
    for target_id in ("address", "booking"):
        parent = tmp_path / target_id
        parent.mkdir()
        roots[target_id] = _v2_target_root(parent, origin=f"{target_id}-service")
        providers[target_id] = smoke._FakeProvider(parent, poison_peer=False)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    common = dict(runtime=fanout, registry=_registry(),
        memory_factory=lambda artifacts, health: smoke._FakeMemory(tmp_path, artifacts),
        memory_preflight=lambda: True,
        provider_runner=lambda request, registry: providers[request.target_id](request, registry=registry))
    execute.start_v2_run(
        packet, target_roots=roots, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=12,
        runtime=fanout, registry=_registry(), provider_runner=common["provider_runner"],
    )
    handover = importlib.import_module("fanout.branch_handover")
    for index, (target_id, proposal, action) in enumerate((
        ("address", "change", "apply"), ("booking", "read-booking", "apply-booking"),
    )):
        with execute.open_run(private, for_dispatch=True, **common) as opened:
            service = opened.service
            if index == 0:
                service.start(owner=opened.owner)
            service.resume(owner=opened.owner)
            synthesis = service.verify_read_only_synthesis(
                proposal, source_seat_ids=tuple(seat.seat_id for seat in service.preparations[proposal].seats),
                synthesizer_id="owner", answer=f"All source proposals for {target_id} reconciled.\n".encode(), owner=opened.owner,
            )
            service.submit(proposal, synthesis, owner=opened.owner)
            baseline = service.repository_baselines[target_id]
            candidate = fanout.CandidateBundle(baseline.digest, (
                fanout.CandidateEntry("src", "directory", 0o755, b""),
                fanout.CandidateEntry("src/change.py", "file", 0o644, b"owner synthesis\n"),
            ), ())
            verified = service.verify_owner_candidate(action, candidate, owner=opened.owner)
            issued = fanout.issue_target_candidate(candidate, task_id=action, plan=service.plan,
                inputs=service.inputs, store=opened.artifacts, controller=service.lifecycle_controller,
                baseline=baseline)
            wrapped, _ = fanout.verify_target_candidate(issued, baseline=baseline, plan=service.plan,
                inputs=service.inputs, store=opened.artifacts, controller=service.lifecycle_controller, verification=verified)
            service.action_complete(action, verified, owner=opened.owner)
            prepared = handover.prepare_branch_handover(service.inputs.targets[target_id], issued, wrapped,
                plan=service.plan, inputs=service.inputs, artifacts=opened.artifacts, scheduler=service.scheduler,
                controller=service.lifecycle_controller, journal=opened.journal, owner=opened.owner, task_id=action,
                baseline=baseline)
            terminal = handover.deliver_branch_candidate(prepared, plan=service.plan, inputs=service.inputs,
                artifacts=opened.artifacts, scheduler=service.scheduler, controller=service.lifecycle_controller,
                journal=opened.journal, owner=opened.owner, baseline=baseline)
            assert isinstance(terminal, fanout.HandoverTerminalV2)
    status = execute.inspect_v2_status(private, runtime=fanout, registry=_registry())
    assert status["target_states"] == {"address": "delivered", "booking": "delivered"}
    for target_id, root in roots.items():
        assert _git_output(root, "symbolic-ref", "HEAD") == f"refs/heads/feat/TASK-123-{target_id}"
        assert (root / "src/change.py").read_bytes() == b"owner synthesis\n"




def test_native_readonly_start_and_reopen_need_no_agy_boundary_guard(execute, tmp_path, monkeypatch):
    packet, _ = _question_packet(tmp_path)
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    def forbidden_guard(*args, **kwargs):
        pytest.fail("native read seats requested an obsolete boundary guard")
    monkeypatch.setattr(fanout, "issue_agy_readonly_guard", forbidden_guard, raising=False)
    monkeypatch.setattr(fanout, "agy_readonly_guard_receipt_sha256", forbidden_guard, raising=False)
    status = execute.prepare_run(
        packet, repo_root=repo, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=6,
        runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
        memory_preflight=lambda: True,
    )
    assert status["tasks"][0]["phase"] == "scheduled"
    with execute.open_run(
        private, runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
        memory_preflight=lambda: True, for_dispatch=True,
    ) as opened:
        assert {seat.executor_id for seat in opened.service.preparations["answer"].seats} == {"claude", "codex", "agy"}
