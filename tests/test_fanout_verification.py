"""Candidate-bundle and fresh-verifier contracts for llm-fanout."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_verification_contracts",
    FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)


CandidateBundle = fanout.CandidateBundle
CandidateEntry = fanout.CandidateEntry
CandidateValidationError = fanout.CandidateValidationError
CandidateVerification = fanout.CandidateVerification
CheckOutcome = fanout.CheckOutcome
PlanCheckV1 = fanout.PlanCheckV1
canonical_json = fanout.canonical_json
capture_repository_baseline = fanout.capture_repository_baseline
create_candidate = fanout.create_candidate
create_seat_workspace = fanout.create_seat_workspace
create_lifecycle_controller = fanout.create_lifecycle_controller
synthesize_candidate = fanout.synthesize_candidate
validate_candidate_verification = fanout.validate_candidate_verification
verify_candidate = fanout.verify_candidate


def _git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.startswith("GIT_"):
            environment.pop(name)
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return environment


def _git(repository: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ("git", "-C", os.fspath(repository), *args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_environment(),
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr.decode("utf-8", "replace"))
    return completed.stdout


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "caller"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "keep.txt").write_text("unchanged\n", encoding="utf-8")
    (repository / "changed.bin").write_bytes(b"before\x00")
    (repository / "remove.txt").write_text("remove me\n", encoding="utf-8")
    (repository / "mode.sh").write_text("#!/bin/sh\necho before\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "baseline")
    return repository


def _seat(tmp_path: Path, baseline: object, seat_id: str = "claude") -> object:
    return create_seat_workspace(baseline, tmp_path / "run", seat_id)


def _controller(tmp_path: Path) -> object:
    return create_lifecycle_controller(tmp_path / "controller")


def _check(source: str, *, expected_artifacts: tuple[str, ...] = ()) -> object:
    return PlanCheckV1(
        argv=(sys.executable, "-c", source),
        cwd="",
        env_allowlist=(),
        timeout=5,
        accepted_exit_codes=(0,),
        expected_artifacts=expected_artifacts,
    )


def _target_evidence_context(tmp_path):
    source = b"# Target plan\n"
    spec = fanout.TargetSpec(
        "booking", "github.com/example/booking", "TASK-123",
        "refs/heads/feat/TASK-123-booking",
    )
    plan = fanout.plan.FanoutPlanV2.from_dict({
        "schema_version": "v2",
        "source": {"path": "plan.md", "sha256": hashlib.sha256(source).hexdigest(),
                   "parser_version": "v1"},
        "defaults": {"executor_ids": ["claude", "codex"], "rounds": 1,
                     "timeout": 120, "retries": 0, "minimum_success": 2},
        "targets": [spec.to_dict()],
        "source_steps": [{"id": "Task 1/Step 1", "sha256": "a" * 64,
                          "target_id": "booking"}],
        "tasks": [{
            "id": "work", "kind": "work", "parent_id": None, "title": "Work",
            "objective": "Finish.", "source_step_ids": ["Task 1/Step 1"],
            "depends_on": [], "execution_class": "read-only", "required_skills": [],
            "none_reason": "No specialist skill is needed.", "owned_paths": [],
            "acceptance": ["Finished."], "checks": [], "provider_policy": None,
            "target_id": "booking", "dependency_modes": {},
        }],
    })
    binding = fanout.TargetBinding(spec, tmp_path, tmp_path, 1, 1, "a" * 40, None, "b" * 64,
                                   "refs/heads/main")
    inputs = fanout.RunInputs(
        "run-target", hashlib.sha256(canonical_json(plan.to_dict())).hexdigest(),
        plan.source.sha256, "c" * 64, "d" * 64, "e" * 64,
        {"claude/read-only/standard": "f" * 64,
         "codex/read-only/standard": "f" * 64},
        {"work": "8" * 64}, profile_shape="class-tier", targets={"booking": binding},
    )
    return plan, inputs, binding


def test_target_candidate_loader_binds_identical_manifest_to_target_identity(tmp_path):
    plan, inputs, binding = _target_evidence_context(tmp_path)
    bundle = CandidateBundle(binding.baseline_sha256, (), ())
    with fanout.ArtifactStore(tmp_path / "artifacts") as store:
        controller = _controller(tmp_path)
        envelope = fanout.issue_target_candidate(
            bundle, task_id="work", plan=plan, inputs=inputs,
            store=store, controller=controller,
        )
        target = fanout.load_target_candidate(
            envelope, plan=plan, inputs=inputs, store=store, controller=controller,
        )
        assert target.candidate == bundle
        wrong = replace(envelope, target_id="address")
        with pytest.raises(CandidateValidationError, match="target"):
            fanout.load_target_candidate(
                wrong, plan=plan, inputs=inputs, store=store, controller=controller,
            )


def test_target_evidence_wrong_baseline_blocks_before_missing_artifact_read(tmp_path):
    plan, inputs, binding = _target_evidence_context(tmp_path)
    ref = fanout.ArtifactRef("missing.json", "a" * 64, 1)
    envelope = fanout.TargetEvidenceEnvelope(
        inputs.run_id, "work", "booking", binding.spec.repository,
        binding.spec.branch_ref, binding.base_oid, "0" * 64,
        "candidate", ref,
    )
    with fanout.ArtifactStore(tmp_path / "artifacts") as store:
        with pytest.raises(CandidateValidationError, match="baseline"):
            fanout.load_target_candidate(
                envelope, plan=plan, inputs=inputs, store=store,
            )


def test_same_candidate_bytes_cannot_be_relabelled_as_another_target(tmp_path):
    plan, inputs, booking = _target_evidence_context(tmp_path)
    address_spec = fanout.TargetSpec(
        "address", "github.com/example/address", "TASK-123",
        "refs/heads/feat/TASK-123-address",
    )
    document = plan.to_dict()
    document["targets"].append(address_spec.to_dict())
    document["source_steps"].append({
        "id": "Task 2/Step 1", "sha256": "c" * 64, "target_id": "address",
    })
    address_task = {**document["tasks"][0], "id": "address-work",
                    "source_step_ids": ["Task 2/Step 1"], "target_id": "address"}
    document["tasks"].append(address_task)
    plan = fanout.plan.FanoutPlanV2.from_dict(document)
    address = fanout.TargetBinding(
        address_spec, tmp_path, tmp_path, 2, 2, "c" * 40, None,
        booking.baseline_sha256, "refs/heads/main",
    )
    inputs = replace(
        inputs, compiled_plan_sha256=hashlib.sha256(canonical_json(plan.to_dict())).hexdigest(),
        targets={"booking": booking, "address": address},
        skill_manifests={"work": "8" * 64, "address-work": "8" * 64},
    )
    bundle = CandidateBundle(booking.baseline_sha256, (), ())
    with fanout.ArtifactStore(tmp_path / "artifacts") as store:
        controller = _controller(tmp_path)
        booking_envelope = fanout.issue_target_candidate(
            bundle, task_id="work", plan=plan, inputs=inputs,
            store=store, controller=controller,
        )
        booking_wrapper = booking_envelope.payload
        booking_document = json.loads(store.read_bytes(booking_wrapper))
        booking_ref = fanout.ArtifactRef(**booking_document["candidate"])
        copied_ref = store.write_bytes(
            "target-evidence/address/candidate/copied.json", store.read_bytes(booking_wrapper),
        )
        forged = fanout.TargetEvidenceEnvelope(
            inputs.run_id, "address-work", "address", address.spec.repository,
            address.spec.branch_ref, address.base_oid, address.baseline_sha256,
            "candidate", copied_ref,
        )
        with pytest.raises(CandidateValidationError, match="target"):
            fanout.load_target_candidate(
                forged, plan=plan, inputs=inputs, store=store, controller=controller,
            )
        foreign_ref = store.write_bytes(
            "target-evidence/address/candidate/foreign-ref.json", canonical_json({
                "schema_version": "fanout-target-candidate-v1",
                "target": {
                    "run_id": inputs.run_id, "task_id": "address-work", "target_id": "address",
                    "repository": address.spec.repository,
                    "branch_ref": address.spec.branch_ref,
                    "base_oid": address.base_oid,
                    "baseline_sha256": address.baseline_sha256,
                },
                "candidate": {"path": booking_ref.path, "digest": booking_ref.digest,
                              "size": booking_ref.size},
                "controller_evidence": booking_document["controller_evidence"],
            }),
        )
        with pytest.raises(CandidateValidationError, match="manifest target"):
            fanout.load_target_candidate(
                replace(forged, payload=foreign_ref), plan=plan, inputs=inputs,
                store=store, controller=controller,
            )
        address_manifest = store.write_bytes(
            "target-evidence/address/candidate/manifest.json", bundle.manifest_bytes,
        )
        relabelled_ref = store.write_bytes(
            "target-evidence/address/candidate/relabelled.json", canonical_json({
                "schema_version": "fanout-target-candidate-v1",
                "target": {
                    "run_id": inputs.run_id, "task_id": "address-work", "target_id": "address",
                    "repository": address.spec.repository,
                    "branch_ref": address.spec.branch_ref,
                    "base_oid": address.base_oid,
                    "baseline_sha256": address.baseline_sha256,
                },
                "candidate": {"path": address_manifest.path, "digest": address_manifest.digest,
                              "size": address_manifest.size},
                "controller_evidence": booking_document["controller_evidence"],
            }),
        )
        with pytest.raises(CandidateValidationError, match="controller"):
            fanout.load_target_candidate(
                replace(forged, payload=relabelled_ref), plan=plan, inputs=inputs,
                store=store, controller=controller,
            )


def test_target_fresh_verification_and_checks_share_exact_target_envelope(tmp_path):
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    check = _check("import sys; sys.exit(0)")
    candidate = CandidateBundle(baseline.digest, (), ())
    controller = _controller(tmp_path)
    plan, inputs, binding = _target_evidence_context(tmp_path)
    task = replace(plan.tasks[0], checks=(check,))
    plan = replace(plan, tasks=(task,))
    git_info = (repository / ".git").stat()
    binding = fanout.TargetBinding(
        binding.spec, repository, repository / ".git",
        git_info.st_dev, git_info.st_ino, baseline.head, None, baseline.digest,
        "refs/heads/main",
    )
    inputs = replace(
        inputs, compiled_plan_sha256=hashlib.sha256(canonical_json(plan.to_dict())).hexdigest(),
        targets={"booking": binding},
    )
    with fanout.ArtifactStore(tmp_path / "artifacts") as store:
        candidate_envelope = fanout.issue_target_candidate(
            candidate, task_id="work", plan=plan, inputs=inputs,
            store=store, controller=controller,
        )
        verification_envelope, receipt = fanout.verify_target_candidate(
            candidate_envelope, baseline=baseline, plan=plan, inputs=inputs,
            store=store, controller=controller,
        )
        assert receipt.valid
        wrapper_ref = verification_envelope.payload
        restored = fanout.load_target_verification(
            verification_envelope, plan=plan, inputs=inputs, store=store,
            controller=controller,
        )
        assert restored.candidate_digest == receipt.candidate_digest
        outcome = receipt.outcomes[0]
        check_ref = store.write_bytes("target-evidence/booking/check/outcomes.json", canonical_json({
            "schema_version": "fanout-target-checks-v1",
            "candidate_sha256": candidate.digest,
            "checks": [check.to_dict()],
            "outcomes": [{
                "index": outcome.index, "argv_digest": outcome.argv_digest,
                "status": outcome.status, "returncode": outcome.returncode,
                "artifacts": [], "failure": outcome.failure,
            }],
        }))
        check_envelope = replace(verification_envelope, evidence_kind="check", payload=check_ref)
        assert fanout.load_target_checks(
            check_envelope, plan=plan, inputs=inputs, store=store,
            controller=controller, verification_envelope=verification_envelope,
        ) == receipt.outcomes
        wrong = replace(verification_envelope, target_id="address")
        with pytest.raises(CandidateValidationError, match="target"):
            fanout.load_target_checks(
                check_envelope, plan=plan, inputs=inputs, store=store,
                controller=controller, verification_envelope=wrong,
            )
        invalid_ref = store.write_bytes(
            "target-evidence/booking/verification/invalid.json",
            store.read_bytes(wrapper_ref).replace(
                f'"size":{len(candidate.manifest_bytes)}'.encode(), b'"size":NaN',
            ),
        )
        with pytest.raises(CandidateValidationError, match="wrapper"):
            fanout.load_target_verification(
                replace(verification_envelope, payload=invalid_ref),
                plan=plan, inputs=inputs, store=store, controller=controller,
            )


def test_target_verification_rejects_relabelled_v1_receipt_with_equal_baseline_and_checks(tmp_path):
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    check = _check("import sys; sys.exit(0)")
    candidate = CandidateBundle(baseline.digest, (), ())
    controller = _controller(tmp_path)
    plan, inputs, booking = _target_evidence_context(tmp_path)
    address_spec = fanout.TargetSpec(
        "address", "github.com/example/address", "TASK-123",
        "refs/heads/feat/TASK-123-address",
    )
    document = plan.to_dict()
    document["targets"].append(address_spec.to_dict())
    document["source_steps"].append({
        "id": "Task 2/Step 1", "sha256": "c" * 64, "target_id": "address",
    })
    document["tasks"][0]["checks"] = [check.to_dict()]
    document["tasks"].append({
        **document["tasks"][0], "id": "address-work", "target_id": "address",
        "source_step_ids": ["Task 2/Step 1"],
    })
    plan = fanout.plan.FanoutPlanV2.from_dict(document)
    git_info = (repository / ".git").stat()
    booking = fanout.TargetBinding(
        booking.spec, repository, repository / ".git",
        git_info.st_dev, git_info.st_ino, baseline.head, None, baseline.digest,
        "refs/heads/main",
    )
    address = fanout.TargetBinding(
        address_spec, tmp_path, tmp_path, 2, 2, "c" * 40, None, baseline.digest,
        "refs/heads/main",
    )
    inputs = replace(
        inputs, compiled_plan_sha256=hashlib.sha256(canonical_json(plan.to_dict())).hexdigest(),
        targets={"booking": booking, "address": address},
        skill_manifests={"work": "8" * 64, "address-work": "8" * 64},
    )
    with fanout.ArtifactStore(tmp_path / "artifacts") as store:
        booking_candidate = fanout.issue_target_candidate(
            candidate, task_id="work", plan=plan, inputs=inputs,
            store=store, controller=controller,
        )
        booking_verification, receipt = fanout.verify_target_candidate(
            booking_candidate, baseline=baseline, plan=plan, inputs=inputs,
            store=store, controller=controller,
        )
        address_candidate = fanout.issue_target_candidate(
            candidate, task_id="address-work", plan=plan, inputs=inputs,
            store=store, controller=controller,
        )
        booking_wrapper = json.loads(store.read_bytes(booking_verification.payload))
        copied_ref = store.write_bytes(
            fanout.candidate_result_path("address-work", candidate.digest, receipt.evidence_digest),
            candidate.manifest_bytes,
        )
        wrapper_ref = store.write_bytes(
            "target-evidence/address/verification/relabelled.json", canonical_json({
                "schema_version": "fanout-target-verification-v1",
                "target": {
                    "run_id": inputs.run_id, "task_id": "address-work",
                    "target_id": "address", "repository": address.spec.repository,
                    "branch_ref": address.spec.branch_ref, "base_oid": address.base_oid,
                    "baseline_sha256": address.baseline_sha256,
                },
                "candidate": {"path": copied_ref.path, "digest": copied_ref.digest,
                              "size": copied_ref.size},
                "candidate_evidence": {
                    "path": address_candidate.payload.path,
                    "digest": address_candidate.payload.digest,
                    "size": address_candidate.payload.size,
                },
                "controller_evidence": booking_wrapper["controller_evidence"],
            }),
        )
        envelope = fanout.TargetEvidenceEnvelope(
            inputs.run_id, "address-work", "address", address.spec.repository,
            address.spec.branch_ref, address.base_oid, address.baseline_sha256,
            "verification", wrapper_ref,
        )
        with pytest.raises(CandidateValidationError, match="controller"):
            fanout.load_target_verification(
                envelope, plan=plan, inputs=inputs, store=store,
                controller=controller,
            )


def test_controller_issues_independent_target_evidence_for_identical_candidate_content(tmp_path):
    booking_root = _repository(tmp_path)
    address_root = tmp_path / "address-repo"
    _git(tmp_path, "clone", "--quiet", "--no-hardlinks", str(booking_root), str(address_root))
    booking_baseline = capture_repository_baseline(booking_root)
    address_baseline = capture_repository_baseline(address_root)
    assert booking_baseline.digest == address_baseline.digest
    check = _check("import sys; sys.exit(0)")
    plan, inputs, booking = _target_evidence_context(tmp_path)
    address_spec = fanout.TargetSpec(
        "address", "github.com/example/address", "TASK-123",
        "refs/heads/feat/TASK-123-address",
    )
    document = plan.to_dict()
    document["targets"].append(address_spec.to_dict())
    document["source_steps"].append({
        "id": "Task 2/Step 1", "sha256": "c" * 64, "target_id": "address",
    })
    document["tasks"][0]["checks"] = [check.to_dict()]
    document["tasks"].append({
        **document["tasks"][0], "id": "address-work", "target_id": "address",
        "source_step_ids": ["Task 2/Step 1"],
    })
    plan = fanout.plan.FanoutPlanV2.from_dict(document)
    booking_git = (booking_root / ".git").stat()
    address_git = (address_root / ".git").stat()
    booking = fanout.TargetBinding(
        booking.spec, booking_root, booking_root / ".git",
        booking_git.st_dev, booking_git.st_ino, booking_baseline.head,
        None, booking_baseline.digest, "refs/heads/main",
    )
    address = fanout.TargetBinding(
        address_spec, address_root, address_root / ".git",
        address_git.st_dev, address_git.st_ino, address_baseline.head,
        None, address_baseline.digest, "refs/heads/main",
    )
    inputs = replace(
        inputs, compiled_plan_sha256=hashlib.sha256(canonical_json(plan.to_dict())).hexdigest(),
        targets={"booking": booking, "address": address},
        skill_manifests={"work": "8" * 64, "address-work": "8" * 64},
    )
    candidate = CandidateBundle(booking_baseline.digest, (), ())
    controller = _controller(tmp_path)
    with fanout.ArtifactStore(tmp_path / "artifacts") as store:
        booking_candidate = fanout.issue_target_candidate(
            candidate, task_id="work", plan=plan, inputs=inputs,
            store=store, controller=controller,
        )
        address_candidate = fanout.issue_target_candidate(
            candidate, task_id="address-work", plan=plan, inputs=inputs,
            store=store, controller=controller,
        )
        assert booking_candidate.payload.digest != address_candidate.payload.digest
        assert fanout.load_target_candidate(
            booking_candidate, plan=plan, inputs=inputs, store=store,
            controller=controller,
        ).candidate == candidate
        assert fanout.load_target_candidate(
            address_candidate, plan=plan, inputs=inputs, store=store,
            controller=controller,
        ).candidate == candidate
        with pytest.raises(CandidateValidationError, match="baseline.*target"):
            fanout.verify_target_candidate(
                booking_candidate, baseline=address_baseline, plan=plan,
                inputs=inputs, store=store, controller=controller,
            )
        booking_verification, booking_receipt = fanout.verify_target_candidate(
            booking_candidate, baseline=booking_baseline, plan=plan,
            inputs=inputs, store=store, controller=controller,
        )
        address_verification, address_receipt = fanout.verify_target_candidate(
            address_candidate, baseline=address_baseline, plan=plan,
            inputs=inputs, store=store, controller=controller,
        )
        assert booking_receipt.evidence_digest != address_receipt.evidence_digest
        for envelope in (booking_verification, address_verification):
            assert fanout.load_target_verification(
                envelope, plan=plan, inputs=inputs, store=store,
                controller=controller,
            ).valid


def test_candidate_bundle_captures_full_bytes_modes_raw_links_and_deletions(tmp_path):
    """Dropping a changed byte, mode, raw target, or deletion would hand over another result."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = _seat(tmp_path, baseline)
    (seat.root / "changed.bin").write_bytes(b"after\x00\xff")
    (seat.root / "mode.sh").chmod(0o755)
    (seat.root / "remove.txt").unlink()
    (seat.root / "raw-link").symlink_to("mode.sh")

    candidate = create_candidate(baseline, seat.root)

    entries = {entry.path: entry for entry in candidate.entries}
    assert set(entries) == {"changed.bin", "mode.sh", "raw-link"}
    assert entries["changed.bin"].data == b"after\x00\xff"
    assert entries["changed.bin"].digest == hashlib.sha256(b"after\x00\xff").hexdigest()
    assert entries["mode.sh"].mode == 0o755
    assert entries["raw-link"].kind == "symlink"
    assert entries["raw-link"].data == b"mode.sh"
    assert candidate.deleted_paths == ("remove.txt",)
    assert candidate.digest == hashlib.sha256(candidate.manifest_bytes).hexdigest()
    assert CandidateBundle.from_manifest(candidate.manifest_bytes) == candidate

    (seat.root / "changed.bin").write_bytes(b"later seat mutation")
    assert entries["changed.bin"].data == b"after\x00\xff"


def test_candidate_capture_rejects_special_files_traversal_and_tampered_manifests(tmp_path):
    """A candidate must not turn a special inode, path escape, or detached bytes into a bundle."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = _seat(tmp_path, baseline)
    fifo = seat.root / "unsafe.fifo"
    os.mkfifo(fifo)

    with pytest.raises(CandidateValidationError, match="special"):
        create_candidate(baseline, seat.root)

    with pytest.raises(CandidateValidationError, match="path"):
        CandidateEntry("../escape", "file", 0o644, b"blocked")
    with pytest.raises(CandidateValidationError, match="kind"):
        CandidateEntry("unsafe", "fifo", 0o644, b"")

    valid = CandidateBundle(
        baseline.digest,
        (CandidateEntry("changed.bin", "file", 0o644, b"after"),),
        (),
    )
    manifest = json.loads(valid.manifest_bytes)
    manifest["entries"][0]["data_sha256"] = "0" * 64
    with pytest.raises(CandidateValidationError, match="digest"):
        CandidateBundle.from_manifest(manifest)


@pytest.mark.parametrize("path", [".GIT/config", ".GiT/config", ".ＧＩＴ/config", ".git./config"])
def test_candidate_rejects_filesystem_equivalent_git_metadata_aliases(path):
    """Case aliases must fail before a verifier can resolve them to the real Git directory."""
    with pytest.raises(CandidateValidationError, match="Git metadata"):
        CandidateEntry(path, "file", 0o644, b"blocked")


def test_candidate_explicitly_represents_an_empty_directory_transition(tmp_path):
    """Replacing a tracked file with an empty directory must not silently erase the directory."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = _seat(tmp_path, baseline)
    (seat.root / "changed.bin").unlink()
    (seat.root / "changed.bin").mkdir()
    (seat.root / "changed.bin").chmod(0o750)

    candidate = create_candidate(baseline, seat.root)

    assert candidate.deleted_paths == ("changed.bin",)
    assert [(entry.path, entry.kind, entry.mode) for entry in candidate.entries] == [
        ("changed.bin", "directory", 0o750),
    ]


def test_candidate_manifest_requires_canonical_bytes(tmp_path):
    """An alternate wire spelling must not identify a candidate."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    candidate = CandidateBundle(
        baseline.digest,
        (CandidateEntry("changed.bin", "file", 0o644, b"after"),),
        (),
    )

    with pytest.raises(CandidateValidationError, match="canonical"):
        CandidateBundle.from_manifest(candidate.manifest_bytes + b" ")


def test_verification_preserves_a_changed_symlink_mode_when_the_platform_supports_it(tmp_path):
    """A candidate must retain the lstat mode it content-addresses for a raw link."""
    if not hasattr(os, "lchmod"):
        pytest.skip("platform cannot change a symlink mode")
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = _seat(tmp_path, baseline)
    link = seat.root / "raw-link"
    link.symlink_to("changed.bin")
    os.lchmod(link, 0o700)
    if stat.S_IMODE(link.lstat().st_mode) != 0o700:
        pytest.skip("filesystem does not retain distinct symlink modes")
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)

    receipt = verify_candidate(
        baseline,
        candidate,
        (_check("import sys; sys.exit(0)"),),
        controller=controller,
    )

    assert receipt.valid
    validate_candidate_verification(controller, receipt)
    assert stat.S_IMODE((receipt.workspace / "raw-link").lstat().st_mode) == 0o700


def test_candidate_capture_represents_file_to_directory_transitions_as_a_delete_and_new_entry(tmp_path):
    """A replacement directory must not hide deletion of the baseline file it replaces."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = _seat(tmp_path, baseline)
    (seat.root / "changed.bin").unlink()
    (seat.root / "changed.bin").mkdir()
    (seat.root / "changed.bin" / "nested.txt").write_bytes(b"nested\x00bytes")

    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)

    assert candidate.deleted_paths == ("changed.bin",)
    assert [(entry.path, entry.kind, entry.data) for entry in candidate.entries] == [
        ("changed.bin", "directory", b""),
        ("changed.bin/nested.txt", "file", b"nested\x00bytes"),
    ]
    receipt = verify_candidate(
        baseline,
        candidate,
        (_check("from pathlib import Path; assert Path('changed.bin/nested.txt').read_bytes() == b'nested\\x00bytes'"),),
        controller=controller,
    )
    assert receipt.valid


def test_verification_materializes_children_before_finalizing_a_nonwritable_directory_mode(tmp_path):
    """A captured readable-but-nonwritable directory must preserve its final mode after its child bytes."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = _seat(tmp_path, baseline)
    sealed = seat.root / "sealed"
    sealed.mkdir()
    (sealed / "value.txt").write_text("sealed\n", encoding="utf-8")
    sealed.chmod(0o500)
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)

    receipt = verify_candidate(
        baseline,
        candidate,
        (_check("from pathlib import Path; assert Path('sealed/value.txt').read_text() == 'sealed\\n'"),),
        controller=controller,
    )

    assert receipt.valid
    assert stat.S_IMODE((receipt.workspace / "sealed").stat().st_mode) == 0o500


def test_verification_uses_a_new_workspace_for_each_candidate_and_immutable_argv_checks(tmp_path):
    """Reusing a peer checkout would let an earlier verifier's residue satisfy a later check."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = _seat(tmp_path, baseline)
    (seat.root / "changed.bin").write_bytes(b"verified\x00bytes")
    candidate = create_candidate(baseline, seat.root)
    check = _check(
        "from pathlib import Path; import sys; "
        "sys.exit(0 if Path('changed.bin').read_bytes() == b'verified\\x00bytes' else 9)"
    )
    caller_before = (repository / "changed.bin").read_bytes()
    index_before = (repository / ".git" / "index").read_bytes()
    controller = _controller(tmp_path)

    first = verify_candidate(baseline, candidate, (check,), controller=controller)
    second = verify_candidate(baseline, candidate, (check,), controller=controller)

    assert first.valid and second.valid
    assert first.workspace != second.workspace
    assert first.workspace.is_dir() and second.workspace.is_dir()
    assert first.outcomes[0].returncode == 0
    assert (repository / "changed.bin").read_bytes() == caller_before
    assert (repository / ".git" / "index").read_bytes() == index_before


def test_verification_refuses_a_controller_root_inside_the_caller_repository(tmp_path):
    """A fresh verifier workspace must never materialize beneath caller-controlled worktree bytes."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = _seat(tmp_path, baseline)
    (seat.root / "changed.bin").write_bytes(b"candidate")
    candidate = create_candidate(baseline, seat.root)
    controller = create_lifecycle_controller(repository / ".fanout-controller")
    index_before = (repository / ".git" / "index").read_bytes()

    with pytest.raises(fanout.CandidateVerificationError, match="caller repository"):
        verify_candidate(baseline, candidate, (_check("import sys; sys.exit(0)"),), controller=controller)

    assert (repository / ".git" / "index").read_bytes() == index_before


def test_failed_candidate_receipt_cannot_claim_a_passed_check(tmp_path):
    """Treating a nonzero verifier command as evidence would unlock failed provider output."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = _seat(tmp_path, baseline)
    (seat.root / "changed.bin").write_bytes(b"candidate")
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)

    receipt = verify_candidate(
        baseline,
        candidate,
        (_check("import sys; sys.exit(7)"),),
        controller=controller,
    )

    assert not receipt.valid
    assert receipt.outcomes[0].returncode == 7
    assert receipt.failure is not None


def test_verification_requires_an_immutable_argv_check_before_it_can_issue_a_receipt(tmp_path):
    """An empty check list would turn materialization alone into an unsafe verification claim."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = _seat(tmp_path, baseline)
    (seat.root / "changed.bin").write_bytes(b"candidate")
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)

    with pytest.raises(CandidateValidationError, match="at least one"):
        verify_candidate(baseline, candidate, (), controller=controller)

    empty_digest = hashlib.sha256(canonical_json([])).hexdigest()
    with pytest.raises(CandidateValidationError, match="at least one"):
        CandidateVerification(
            candidate=candidate,
            candidate_digest=candidate.digest,
            baseline_digest=baseline.digest,
            checks_digest=empty_digest,
            environment_digest=empty_digest,
            checks=(),
            environment=(),
            valid=True,
            outcomes=(),
            workspace=tmp_path.resolve(),
            verifier_root=tmp_path.resolve(),
        )


def test_verification_receipt_is_digest_bound_to_the_candidate_it_checked(tmp_path):
    """Replacing a receipt's candidate after verification would let another delta borrow its checks."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = _seat(tmp_path, baseline)
    (seat.root / "changed.bin").write_bytes(b"first")
    first = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)
    receipt = verify_candidate(
        baseline,
        first,
        (_check("import sys; sys.exit(0)"),),
        controller=controller,
    )
    replacement = CandidateBundle(
        baseline.digest,
        (CandidateEntry("changed.bin", "file", 0o644, b"other"),),
        (),
    )

    with pytest.raises(CandidateValidationError, match="candidate digest"):
        replace(receipt, candidate=replacement)


def test_verification_receipt_rejects_rebound_baseline_or_immutable_checks(tmp_path):
    """A changed baseline or argv declaration must not borrow a prior pass receipt."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = _seat(tmp_path, baseline)
    (seat.root / "changed.bin").write_bytes(b"first")
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)
    receipt = verify_candidate(
        baseline,
        candidate,
        (_check("import sys; sys.exit(0)"),),
        controller=controller,
    )

    with pytest.raises(CandidateValidationError, match="baseline"):
        replace(receipt, baseline_digest="0" * 64)
    with pytest.raises(CandidateValidationError, match="check"):
        replace(receipt, checks=(_check("import sys; sys.exit(9)"),))


def test_verification_receipt_rejects_nonpassing_outcome_claimed_as_valid(tmp_path):
    """A timeout or nonzero result cannot be relabeled as a verifier pass."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    seat = _seat(tmp_path, baseline)
    (seat.root / "changed.bin").write_bytes(b"first")
    candidate = create_candidate(baseline, seat.root)
    controller = _controller(tmp_path)
    receipt = verify_candidate(
        baseline,
        candidate,
        (_check("import sys; sys.exit(0)"),),
        controller=controller,
    )

    with pytest.raises(CandidateValidationError, match="outcome"):
        replace(receipt, outcomes=(replace(receipt.outcomes[0], status="timeout"),))


def test_direct_construction_cannot_forge_a_valid_verifier_receipt(tmp_path):
    """Only the controller-backed verifier may issue a receipt that consumers can trust."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    candidate = CandidateBundle(
        baseline.digest,
        (CandidateEntry("changed.bin", "file", 0o644, b"candidate"),),
        (),
    )
    failing = _check("import sys; sys.exit(1)")
    checks_digest = hashlib.sha256(canonical_json([failing.to_dict()])).hexdigest()
    environment_digest = hashlib.sha256(canonical_json([])).hexdigest()
    argv_digest = hashlib.sha256(canonical_json(list(failing.argv))).hexdigest()

    with pytest.raises(CandidateValidationError, match="issued"):
        CandidateVerification(
            candidate=candidate,
            candidate_digest=candidate.digest,
            baseline_digest=baseline.digest,
            checks_digest=checks_digest,
            environment_digest=environment_digest,
            checks=(failing,),
            environment=(),
            valid=True,
            outcomes=(CheckOutcome(0, argv_digest, "exit", 0),),
            workspace=tmp_path.resolve(),
            verifier_root=tmp_path.resolve(),
        )


def test_synthesis_records_exact_source_candidates_and_needs_its_own_fresh_verification(tmp_path):
    """Borrowing a source receipt for a synthesized delta would bypass reconciliation checks."""
    repository = _repository(tmp_path)
    baseline = capture_repository_baseline(repository)
    first_seat = _seat(tmp_path, baseline, "claude")
    second_seat = _seat(tmp_path, baseline, "codex")
    (first_seat.root / "changed.bin").write_bytes(b"first")
    (second_seat.root / "changed.bin").write_bytes(b"second")
    first = create_candidate(baseline, first_seat.root)
    second = create_candidate(baseline, second_seat.root)
    synthesis = synthesize_candidate(
        baseline,
        sources=(first, second),
        entries=(CandidateEntry("changed.bin", "file", 0o644, b"synthesized"),),
        deleted_paths=(),
    )
    check = _check(
        "from pathlib import Path; import sys; "
        "sys.exit(0 if Path('changed.bin').read_bytes() == b'synthesized' else 5)"
    )
    controller = _controller(tmp_path)

    source_receipt = verify_candidate(baseline, first, (check,), controller=controller)
    synthesis_receipt = verify_candidate(baseline, synthesis, (check,), controller=controller)

    assert not source_receipt.valid
    assert synthesis.source_candidate_digests == (first.digest, second.digest)
    assert synthesis_receipt.valid
    assert synthesis_receipt.workspace != source_receipt.workspace
    duplicated = synthesize_candidate(
        baseline,
        sources=(first, first),
        entries=(CandidateEntry("changed.bin", "file", 0o644, b"duplicate"),),
        deleted_paths=(),
    )
    assert duplicated.source_candidate_digests == (first.digest, first.digest)


def test_answer_synthesis_records_two_distinct_sources_and_fresh_declared_checks(tmp_path):
    controller = _controller(tmp_path)
    artifacts = fanout.ArtifactStore(tmp_path / "artifacts")
    first = artifacts.write_bytes("sources/claude.txt", b"first answer\n")
    second = artifacts.write_bytes("sources/codex.txt", b"second answer\n")
    check = _check(
        "from pathlib import Path; assert Path('answer.md').read_text() == 'combined answer\\n'"
    )

    receipt = fanout.verify_answer_synthesis(
        artifacts,
        run_id="run-answer",
        task_id="question",
        plan_sha256="a" * 64,
        plan_revision=1,
        sources={"claude": first, "codex": second},
        synthesizer_id="orchestrator-codex",
        answer=b"combined answer\n",
        checks=(check,),
        controller=controller,
    )

    assert receipt.valid
    assert receipt.answer_ref.digest not in {first.digest, second.digest}
    assert receipt.source_answers == (("claude", first), ("codex", second))
    assert artifacts.read_bytes(receipt.answer_ref) == b"combined answer\n"
    assert receipt.evidence_digest in receipt.answer_ref.path
    fanout.validate_answer_synthesis(controller, artifacts, receipt)
    with pytest.raises(CandidateValidationError, match="evidence"):
        fanout.validate_answer_synthesis(
            controller, artifacts, replace(receipt, synthesizer_id="forged"),
        )
    with pytest.raises(CandidateValidationError, match="evidence|reference"):
        fanout.validate_answer_synthesis(
            controller, artifacts,
            replace(receipt, answer_ref=fanout.ArtifactRef("other/answer.md", receipt.answer_ref.digest,
                                                          receipt.answer_ref.size)),
        )


def test_answer_synthesis_rejects_duplicate_source_digest_and_failed_check(tmp_path):
    controller = _controller(tmp_path)
    artifacts = fanout.ArtifactStore(tmp_path / "artifacts")
    first = artifacts.write_bytes("sources/claude.txt", b"same answer\n")
    second = artifacts.write_bytes("sources/codex.txt", b"same answer\n")
    with pytest.raises(CandidateValidationError, match="distinct"):
        fanout.verify_answer_synthesis(
            artifacts, run_id="run-answer", task_id="question",
            plan_sha256="a" * 64, plan_revision=1,
            sources={"claude": first, "codex": second},
            synthesizer_id="orchestrator-codex", answer=b"combined answer\n",
            checks=(), controller=controller,
        )

    different = artifacts.write_bytes("sources/agy.txt", b"another answer\n")
    receipt = fanout.verify_answer_synthesis(
        artifacts, run_id="run-answer", task_id="question",
        plan_sha256="a" * 64, plan_revision=1,
        sources={"claude": first, "agy": different},
        synthesizer_id="orchestrator-codex", answer=b"combined answer\n",
        checks=(_check("raise SystemExit(3)"),), controller=controller,
    )
    assert not receipt.valid
    assert receipt.failure is not None


def test_answer_checks_can_read_an_immutable_repository_baseline(tmp_path):
    baseline = capture_repository_baseline(_repository(tmp_path))
    controller = _controller(tmp_path)
    artifacts = fanout.ArtifactStore(tmp_path / "artifacts")
    first = artifacts.write_bytes("sources/claude.txt", b"first\n")
    second = artifacts.write_bytes("sources/codex.txt", b"second\n")
    check = _check(
        "from pathlib import Path; "
        "assert Path('keep.txt').read_text() == 'unchanged\\n'; "
        "assert Path('answer.md').read_text() == 'combined\\n'"
    )
    receipt = fanout.verify_answer_synthesis(
        artifacts, run_id="run-answer", task_id="question",
        plan_sha256="a" * 64, plan_revision=1,
        sources={"claude": first, "codex": second},
        synthesizer_id="orchestrator-codex", answer=b"combined\n",
        checks=(check,), controller=controller, baseline=baseline,
    )
    assert receipt.valid
    fanout.validate_answer_synthesis(controller, artifacts, receipt)


def test_answer_synthesis_overlays_baseline_answer_in_private_verifier(tmp_path):
    repository = _repository(tmp_path)
    (repository / "answer.md").write_text("previous answer\n")
    _git(repository, "add", "answer.md")
    _git(repository, "commit", "-qm", "existing answer")
    baseline = capture_repository_baseline(repository)
    controller = _controller(tmp_path)
    artifacts = fanout.ArtifactStore(tmp_path / "artifacts")
    first = artifacts.write_bytes("sources/claude.txt", b"first\n")
    second = artifacts.write_bytes("sources/codex.txt", b"second\n")

    receipt = fanout.verify_answer_synthesis(
        artifacts, run_id="run-answer", task_id="question",
        plan_sha256="a" * 64, plan_revision=1,
        sources={"claude": first, "codex": second},
        synthesizer_id="orchestrator-codex", answer=b"combined\n",
        checks=(_check("from pathlib import Path; assert Path('answer.md').read_text() == 'combined\\n'"),),
        controller=controller, baseline=baseline,
    )

    assert receipt.valid
    assert (repository / "answer.md").read_text() == "previous answer\n"
    fanout.validate_answer_synthesis(controller, artifacts, receipt)


def test_repo_verifier_receipt_is_signed_to_one_task_and_final_barrier(tmp_path):
    baseline = capture_repository_baseline(_repository(tmp_path))
    controller = _controller(tmp_path)
    artifacts = fanout.ArtifactStore(tmp_path / "artifacts")
    barrier = artifacts.write_bytes("barriers/final.json", b"final barrier")
    candidate = CandidateBundle(
        baseline.digest,
        (CandidateEntry("changed.bin", "file", 0o644, b"after\x00"),),
        (),
    )
    binding = fanout.CandidateReconciliationBinding(
        "run-one", "write-one", "a" * 64, 2, barrier,
    )
    receipt = verify_candidate(
        baseline, candidate, (_check("pass"),),
        controller=controller, reconciliation=binding,
    )
    ref = artifacts.write_bytes(
        fanout.candidate_result_path("write-one", candidate.digest, receipt.evidence_digest),
        candidate.manifest_bytes,
    )
    recovered = fanout.load_candidate_verification(
        controller, artifacts, task_id="write-one", candidate_ref=ref,
    )
    assert recovered.reconciliation == binding
    assert recovered.valid
    with pytest.raises(CandidateValidationError, match="evidence"):
        fanout.validate_candidate_verification(
            controller, replace(recovered, reconciliation=replace(binding, task_id="write-two")),
        )
