"""Complete release proofs reuse the existing senders, capture and receipt rules."""
from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_fanout_readonly_canaries import canaries, _forbid_reopen_sends_and_probes
from test_fanout_container_upstream import connection_factory  # noqa: F401
from test_fanout_container_certify import (certifier, company_manifest, complete_receipt,
    _completed_quality_root)

ROOT = Path(__file__).resolve().parents[1]


def _helper():
    path = ROOT / "scripts/fanout_release_evaluations.py"
    assert path.is_file(), "release issuance needs its fixed complete semantic dependency"
    spec = importlib.util.spec_from_file_location("_release_evaluation_tests", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def release_root(tmp_path, monkeypatch):
    helper = _helper()
    from scripts import fanout_baseline
    checkout = tmp_path / "owned-checkout"
    checkout.mkdir(mode=0o700)
    for relative in ("shared/lib/fanout", "components/memory", "components/fanout-container",
                     "shared/skills/llm-fanout-plan", "shared/skills/llm-fanout-execute"):
        shutil.copytree(ROOT / relative, checkout / relative,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for relative in ("Makefile", "scripts/lib/checks.py", "scripts/render.py",
                     "capabilities.toml", "mise.toml", "mise.lock",
                     *(path.relative_to(ROOT).as_posix() for path in ROOT.glob("scripts/fanout_*.py"))):
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / relative).read_bytes())
    lock = checkout / fanout_baseline.HISTORICAL_LOCK
    lock.parent.mkdir(parents=True)
    lock.write_bytes((ROOT / fanout_baseline.HISTORICAL_LOCK).read_bytes())
    historical = tmp_path / "historical" / "snapshot"
    fanout_baseline.prepare_historical(ROOT, historical,
        source=ROOT / "tests/fixtures/llm-fanout-historical-original")
    monkeypatch.setenv("KHENRIX_HISTORICAL_BASELINE_ROOT", str(historical))
    original_checks = tmp_path / "original-checks.py"
    original_checks.write_bytes(Path(os.environ["FANOUT_QUALITY_ORIGINAL_CHECKS"]).read_bytes())
    original_checks.chmod(0o600)
    monkeypatch.setenv("FANOUT_QUALITY_ARCHIVE_ROOT", str(historical))
    monkeypatch.setenv("FANOUT_QUALITY_ORIGINAL_CHECKS", str(original_checks))
    for skill in helper.SKILLS:
        evals = checkout / "evals" / skill
        evals.mkdir(parents=True)
        (evals / "evals.json").write_text("[]\n")
        receipt = {"schema_version": 2, "certified_by": "synthetic-panel",
            "provenance": "eval", "providers": ["claude", "codex", "agy"],
            "per_provider": {"claude": {}, "codex": {}, "agy": {}},
            "source_hash": helper.checks.source_hash(checkout, skill),
            "eval_set_hash": helper.checks.eval_set_hash(checkout, skill)}
        (evals / "receipt.json").write_bytes(helper.canonical_json(receipt))
    monkeypatch.setattr(helper, "ROOT", checkout)
    root = tmp_path / "release"
    root.mkdir(mode=0o700)
    controller = helper.create_lifecycle_controller(root / "controller")
    store = helper.ArtifactStore(root / "raw")
    yield helper, root, controller, store
    store.close()


def test_cold_delivered_validator_reopens_authenticated_producer_evidence(release_root, tmp_path, monkeypatch):
    helper, root, controller, store = release_root
    monkeypatch.setattr(helper, "run_command", lambda _, **__: helper.ProcessResult(
        helper.ProcessStatus.EXIT, 0, b"complete synthetic gate output\n", b"retained stderr\n",
        capture_complete=True, pid=12345))
    strict = helper.capture_strict_gate(controller, store)
    skills = helper.capture_skill_evaluations(controller, store)
    proofs_ref = store.write_json("test-proofs.json", {"strict": strict, "skills": skills})
    helper.probe._write_private(root / "proofs.json", helper.canonical_json(dataclasses.asdict(proofs_ref)))
    helper.probe._write_private(root.with_name(root.name + ".controller-capability"),
                                controller.capability.export_token().encode("ascii"))
    spec = importlib.util.spec_from_file_location("_release_cold_delivery", ROOT / "components/skills/skillctl.py")
    delivery = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = delivery
    spec.loader.exec_module(delivery)
    config = delivery.load_configuration(ROOT, tmp_path / "home", None)
    delivery.apply(config, expect=delivery.plan(config)[0], as_json=True)
    installed = config.home / ".agents/skills/llm-fanout-execute"
    command = '''
import importlib, importlib.util, pathlib, sys
entry, evidence_root, producer_root = map(pathlib.Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location("installed_helper", entry)
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
runtime = helper._runtime()
evaluations = importlib.import_module("scripts.fanout_release_evaluations")
source = importlib.import_module(runtime.__name__ + ".container_source")
assert evaluations.ROOT == entry.parent.parent / ".runtime"
proofs_ref, _ = evaluations._read_owned_json(evidence_root / "proofs.json")
controller = evaluations.probe._quality_controller(evidence_root)
with runtime.ArtifactStore.open_existing(evidence_root / "raw") as store:
    proofs = store.read_json(runtime.ArtifactRef(**proofs_ref))
    producer = evaluations.validate_strict_gate(controller, store, proofs["strict"], source.release_source_binding())
    assert producer == producer_root
    evaluations.validate_skill_evaluations(controller, store, proofs["skills"],
                                          source.release_source_binding(), producer_root=producer)
    assert store.read_bytes(runtime.ArtifactRef(**proofs["strict"]["stdout_ref"])) == b"complete synthetic gate output\\n"
    assert store.read_bytes(runtime.ArtifactRef(**proofs["strict"]["stderr_ref"])) == b"retained stderr\\n"
assert not (evaluations.ROOT / "evals").exists()
assert all(not getattr(module, "__file__", "") or not pathlib.Path(module.__file__).is_relative_to(producer_root)
           for module in sys.modules.values())
print("authenticated producer evidence reopened")
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", command,
                             str(installed / "scripts/execute.py"), str(root), str(helper.ROOT)],
                            cwd=config.home, env={"PATH": os.environ["PATH"], "HOME": str(config.home)},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "authenticated producer evidence reopened"
    for path in (helper.ROOT / "shared/lib/fanout/container_boundary.py",
                 helper.ROOT / "Makefile", helper.ROOT / "evals/llm-fanout-plan/receipt.json",
                 helper.ROOT / "evals/llm-fanout-plan/evals.json",
                 tmp_path / "historical/snapshot/shared/lib/council/engine.py",
                 root / "raw/strict-gate/stdout.bin", root / "raw/strict-gate/process.json"):
        original = path.read_bytes()
        path.write_bytes(original + b"\nchanged evidence\n")
        refused = subprocess.run(result.args, cwd=config.home,
                                 env={"PATH": os.environ["PATH"], "HOME": str(config.home)},
                                 capture_output=True, text=True)
        assert refused.returncode != 0, str(path)
        assert "proof is incomplete or stale" in refused.stderr
        path.write_bytes(original)
    renamed = helper.ROOT.with_name("hidden-producer")
    helper.ROOT.rename(renamed)
    for symlink in (False, True):
        if symlink:
            helper.ROOT.symlink_to(renamed, target_is_directory=True)
        refused = subprocess.run(result.args, cwd=config.home,
                                 env={"PATH": os.environ["PATH"], "HOME": str(config.home)},
                                 capture_output=True, text=True)
        assert refused.returncode != 0
        assert "strict gate proof is incomplete or stale" in refused.stderr
    helper.ROOT.unlink()
    renamed.rename(helper.ROOT)


@pytest.mark.parametrize("reseal", (False, True))
def test_changed_proof_cannot_redirect_before_producer_reads(release_root, tmp_path, monkeypatch, reseal):
    helper, root, controller, store = release_root
    monkeypatch.setattr(helper, "run_command", lambda _, **__: helper.ProcessResult(
        helper.ProcessStatus.EXIT, 0, b"synthetic complete gate", b"", capture_complete=True, pid=12345))
    proof = helper.capture_strict_gate(controller, store)
    foreign = tmp_path / "unrelated-producer"
    proof["cwd"] = str(foreign)
    if reseal:
        unsigned = {key: value for key, value in proof.items()
                    if key not in {"completion_evidence_name", "completion_evidence_digest"}}
        proof = helper._seal(controller, "release-strict-gate-complete", unsigned)
        (root / "raw/strict-gate/process.json").write_bytes(helper.canonical_json(proof))
    resolve = Path.resolve
    read_bytes = Path.read_bytes
    def refuse_external_resolution(path, *args, **kwargs):
        if path.is_relative_to(foreign):
            pytest.fail("unverified producer path was resolved")
        return resolve(path, *args, **kwargs)
    def refuse_external_read(path):
        if path.is_relative_to(foreign):
            pytest.fail("unverified producer evidence was read")
        return read_bytes(path)
    monkeypatch.setattr(Path, "resolve", refuse_external_resolution)
    monkeypatch.setattr(Path, "read_bytes", refuse_external_read)
    with pytest.raises(ValueError, match="strict gate proof"):
        helper.validate_strict_gate(controller, store, proof, helper.release_source_binding())


@pytest.mark.parametrize("damage", ("missing", "cwd", "argv", "binding", "gate", "subject",
                                    "mode", "symlink", "noncanonical"))
def test_strict_gate_requires_unchanged_original_attempt(release_root, monkeypatch, damage):
    helper, root, controller, store = release_root
    monkeypatch.setattr(helper, "run_command", lambda _, **__: helper.ProcessResult(
        helper.ProcessStatus.EXIT, 0, b"synthetic complete gate", b"", capture_complete=True, pid=12345))
    proof = helper.capture_strict_gate(controller, store)
    path = root / "strict-gate-attempt.json"
    attempt = json.loads(path.read_bytes())
    if damage == "missing":
        path.unlink()
    elif damage == "mode":
        path.chmod(0o644)
    elif damage == "symlink":
        replacement = path.with_name("other-attempt.json")
        path.rename(replacement)
        path.symlink_to(replacement)
    elif damage == "noncanonical":
        path.write_text(json.dumps(attempt, indent=2))
    else:
        key = {"binding": "release_source_binding", "gate": "gate_inputs",
               "subject": "historical_subject"}.get(damage, damage)
        attempt[key] = "/unrelated/producer" if damage == "cwd" else {}
        path.write_bytes(helper.canonical_json(attempt))
    with pytest.raises(ValueError, match="strict gate proof"):
        helper.validate_strict_gate(controller, store, proof, helper.release_source_binding())


@pytest.mark.parametrize("pid", (None, 0, "12345", True))
def test_strict_gate_refuses_resealed_invalid_process_pid(release_root, monkeypatch, pid):
    helper, root, controller, store = release_root
    monkeypatch.setattr(helper, "run_command", lambda _, **__: helper.ProcessResult(
        helper.ProcessStatus.EXIT, 0, b"synthetic complete gate", b"", capture_complete=True, pid=12345))
    original = helper.capture_strict_gate(controller, store)
    unsigned = {key: value for key, value in original.items()
                if key not in {"completion_evidence_name", "completion_evidence_digest"}}
    unsigned["pid"] = pid
    changed = helper._seal(controller, "release-strict-gate-complete", unsigned)
    (root / "raw/strict-gate/process.json").write_bytes(helper.canonical_json(changed))
    with pytest.raises(ValueError, match="strict gate proof"):
        helper.validate_strict_gate(controller, store, changed, helper.release_source_binding())


@pytest.mark.parametrize("leaf", ("Makefile", "evals/llm-fanout-plan/receipt.json",
                                  "evals/llm-fanout-plan/evals.json"))
def test_producer_evidence_refuses_identical_bytes_through_symlink(release_root, tmp_path, monkeypatch, leaf):
    helper, root, controller, store = release_root
    monkeypatch.setattr(helper, "run_command", lambda _, **__: helper.ProcessResult(
        helper.ProcessStatus.EXIT, 0, b"synthetic complete gate", b"", capture_complete=True, pid=12345))
    strict = helper.capture_strict_gate(controller, store)
    skills = helper.capture_skill_evaluations(controller, store)
    path = helper.ROOT / leaf
    outside = tmp_path / "outside-evidence.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError):
        producer = helper.validate_strict_gate(controller, store, strict, helper.release_source_binding())
        helper.validate_skill_evaluations(controller, store, skills, helper.release_source_binding(),
                                          producer_root=producer)


def test_strict_gate_transports_and_reopens_only_its_explicit_historical_subject(release_root, monkeypatch):
    from shared.lib.fanout import process
    helper, root, controller, store = release_root
    historical = root.parent / "historical/snapshot"
    launches = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-not-a-credential")

    def supervise(command, environment, **_):
        launches.append((command, environment))
        assert tuple(command.argv) == helper.STRICT_ARGV
        assert environment.get("KHENRIX_HISTORICAL_BASELINE_ROOT") == str(historical)
        assert environment.get("FANOUT_QUALITY_ARCHIVE_ROOT") == str(historical)
        assert environment.get("FANOUT_QUALITY_ORIGINAL_CHECKS") == os.environ["FANOUT_QUALITY_ORIGINAL_CHECKS"]
        assert "ANTHROPIC_API_KEY" not in environment
        return helper.ProcessResult(helper.ProcessStatus.EXIT, 0, b"bounded fake gate", b"", pid=12345)

    monkeypatch.setattr(process, "_supervise_command", supervise)
    proof = helper.capture_strict_gate(controller, store)
    assert len(launches) == 1
    subject = proof["historical_subject"]
    assert subject["root"] == str(historical)
    assert subject["provenance"] == json.loads((historical.parent / "provenance.json").read_bytes())
    assert proof["before_original_sources"] == proof["after_original_sources"]
    assert len(proof["original_source_refs"]["archive"]) == 60
    assert json.loads((root / "strict-gate-attempt.json").read_bytes())["historical_subject"] == subject
    monkeypatch.delenv("KHENRIX_HISTORICAL_BASELINE_ROOT")
    helper.validate_strict_gate(controller, store, proof, helper.release_source_binding())
    (historical / "shared/lib/council/engine.py").write_bytes(b"changed original subject")
    with pytest.raises(ValueError, match="strict gate proof"):
        helper.validate_strict_gate(controller, store, proof, helper.release_source_binding())


@pytest.mark.parametrize("damage", ("retained", "after", "association", "dependency"))
def test_strict_original_inputs_refuse_mutation_and_resealed_wrong_associations(release_root, monkeypatch, damage):
    helper, root, controller, store = release_root
    monkeypatch.setattr(helper, "run_command", lambda _, **__: helper.ProcessResult(
        helper.ProcessStatus.EXIT, 0, b"unit transport, not a gate observation", b"", pid=12345))
    proof = helper.capture_strict_gate(controller, store)
    helper.validate_strict_gate(controller, store, proof, helper.release_source_binding())
    if damage == "retained":
        (store.root / proof["original_source_refs"]["checks"]["path"]).write_bytes(b"changed")
    elif damage == "dependency":
        Path(proof["before_original_sources"]["checks"]["path"]).write_bytes(b"changed")
    else:
        unsigned = json.loads(helper.canonical_json({key: value for key, value in proof.items()
            if key not in {"completion_evidence_name", "completion_evidence_digest"}}))
        if damage == "after":
            unsigned["after_original_sources"]["checks"]["sha256"] = "a" * 64
        else:
            unsigned["before_original_sources"]["archive"]["root"] = str(root)
        proof = helper._seal(controller, "release-strict-gate-complete", unsigned)
        (store.root / "strict-gate/process.json").write_bytes(helper.canonical_json(proof))
    with pytest.raises(ValueError, match="strict gate proof"):
        helper.validate_strict_gate(controller, store, proof, helper.release_source_binding())


def test_strict_gate_refuses_an_unselected_historical_subject_before_launch(release_root, monkeypatch):
    helper, _, controller, store = release_root
    monkeypatch.delenv("KHENRIX_HISTORICAL_BASELINE_ROOT")
    calls = []
    monkeypatch.setattr(helper, "run_command", lambda command: calls.append(command))
    with pytest.raises(ValueError, match="explicit historical"):
        helper.capture_strict_gate(controller, store)
    assert calls == []


def test_historical_selector_does_not_widen_provider_or_native_environments(release_root):
    from shared.lib.fanout import process
    helper, root, _, _ = release_root
    historical = root.parent / "historical/snapshot"
    assert "KHENRIX_HISTORICAL_BASELINE_ROOT" not in process.build_child_environment()
    with pytest.raises(process.ProcessValidationError, match="not allowlisted"):
        process.build_child_environment(overrides={"KHENRIX_HISTORICAL_BASELINE_ROOT": str(historical)})
    for argv, native in ((["true"], None), (helper.STRICT_ARGV, object())):
        command = helper.ProcessCommand(argv, b"", cwd=helper.ROOT, native_boundary=native)
        with pytest.raises(process.ProcessValidationError, match="fixed strict gate"):
            process.run_command(command, strict_gate_historical_root=historical)


@pytest.mark.parametrize("damage", [None, "returncode", "capture", "argv", "stdout", "source"])
def test_strict_gate_reopens_fixed_command_complete_process_output_and_source(release_root, monkeypatch, damage):
    helper, root, controller, store = release_root
    calls = []

    def command_run(command, **_):
        calls.append(command)
        assert tuple(command.argv) == ("mise", "exec", "--", "make", "precommit")
        assert Path(command.cwd) == helper.ROOT
        return helper.ProcessResult(helper.ProcessStatus.EXIT, 0,
            b"full gate stdout\n" + b"x" * 8192, b"full gate stderr\x00\n", capture_complete=True, pid=12345)

    monkeypatch.setattr(helper, "run_command", command_run)
    proof = helper.capture_strict_gate(controller, store)
    assert len(calls) == 1
    assert store.read_bytes(helper.ArtifactRef(**proof["stdout_ref"])).endswith(b"x" * 8192)
    assert store.read_bytes(helper.ArtifactRef(**proof["stderr_ref"])) == b"full gate stderr\x00\n"
    if damage == "returncode":
        proof["returncode"] = 1
    elif damage == "capture":
        proof["capture_complete"] = False
    elif damage == "argv":
        proof["argv"] = ["true"]
    elif damage == "stdout":
        (root / "raw" / proof["stdout_ref"]["path"]).write_bytes(b"truncated")
    elif damage == "source":
        (helper.ROOT / "Makefile").write_bytes(b"changed after gate")
    if damage:
        with pytest.raises(ValueError):
            helper.validate_strict_gate(controller, store, proof, helper.release_source_binding())
    else:
        helper.validate_strict_gate(controller, store, proof, helper.release_source_binding())


@pytest.mark.parametrize("damage", [None, "seeded", "panel", "raw", "stale", "deterministic"])
def test_skill_evidence_uses_existing_final_receipt_rulebook(release_root, damage):
    helper, root, controller, store = release_root
    path = helper.ROOT / "evals" / helper.SKILLS[0] / "receipt.json"
    receipt = json.loads(path.read_bytes())
    if damage == "seeded":
        receipt["provenance"] = "seeded"
    elif damage == "panel":
        receipt["providers"] = ["codex", "agy"]
    elif damage == "deterministic":
        receipt.update(self_test=True, provenance="deterministic", providers=[])
        receipt.pop("per_provider")
    path.write_bytes(helper.canonical_json(receipt))
    if damage in {"seeded", "panel"}:
        with pytest.raises(ValueError):
            helper.capture_skill_evaluations(controller, store)
        return
    proof = helper.capture_skill_evaluations(controller, store)
    if damage == "raw":
        (root / "raw" / proof["skills"][helper.SKILLS[0]]["receipt_ref"]["path"]).write_bytes(b"{}")
    elif damage == "stale":
        (helper.ROOT / "shared/skills" / helper.SKILLS[0] / "SKILL.md").write_text("drift")
    if damage in {"raw", "stale"}:
        with pytest.raises(ValueError):
            helper.validate_skill_evaluations(controller, store, proof, helper.release_source_binding())
    else:
        helper.validate_skill_evaluations(controller, store, proof, helper.release_source_binding())


def test_release_semantics_refuses_authenticated_scalar_pass_maps(release_root):
    helper, root, controller, store = release_root
    decision = helper.validate_release_evaluations(controller, root,
        {name: {"passed": True} for name in helper.RELEASE_CHECKS},
        helper.release_source_binding(), root / "trial")
    assert not decision.passed


@pytest.mark.parametrize("status,returncode,capture", [
    ("exit", 1, True), ("timeout", None, False), ("output-limit", -15, False),
])
def test_actual_failed_or_incomplete_gate_result_is_retained_but_refused(
    release_root, monkeypatch, status, returncode, capture,
):
    helper, root, controller, store = release_root
    monkeypatch.setattr(helper, "run_command", lambda _, **__: helper.ProcessResult(
        helper.ProcessStatus(status), returncode, b"partial or failed stdout", b"complete retained error",
        capture_complete=capture))
    proof = helper.capture_strict_gate(controller, store)
    assert (root / "strict-gate-attempt.json").is_file()
    assert store.read_bytes(helper.ArtifactRef(**proof["stdout_ref"])) == b"partial or failed stdout"
    with pytest.raises(ValueError):
        helper.validate_strict_gate(controller, store, proof, helper.release_source_binding())


@pytest.mark.parametrize("damage", [None, "ballot", "missing-ballots", "boundary", "source"])
def test_complete_maka_producer_proof_reopens_answers_and_all_six_ballots(
    certifier, complete_receipt, tmp_path, monkeypatch, damage,
):
    helper = _helper()
    root, _, _ = _completed_quality_root(certifier, complete_receipt, tmp_path, monkeypatch)

    class Maka:
        target_id = "maka"
        no_tools_characterization_sha256 = "e" * 64
        def __init__(self, **_):
            pass
        def judge(self, request):
            return {"winner": "tie", "auth_route": request.auth_route,
                "requested_model": request.requested_model, "observed_model": request.requested_model,
                "effort": request.effort, "tool_calls": 0, "tool_boundary_sha256": "e" * 64}

    monkeypatch.setattr(certifier, "HarborMakaTransport", Maka)
    certifier._quality_run(root, allow_paid=True, approval_reference="synthetic-owner-123",
        observation_date="2026-09-30", launcher=tmp_path / "never-executed", boundary_sha256="e" * 64)
    # The helper uses the exact production module, not a replay transport.
    monkeypatch.setattr(helper, "probe", certifier)
    proof = helper.maka_evidence(root)
    if damage == "ballot":
        proof["quality"]["ballots"][0]["observed_model"] = "wrong"
    elif damage == "missing-ballots":
        proof["quality"].pop("ballots")
    elif damage == "boundary":
        proof["quality"]["boundary_sha256"] = "f" * 64
    elif damage == "source":
        proof["quality"]["release_source_binding"]["components"]["memory"] = "0" * 64
    if damage:
        with pytest.raises(ValueError):
            helper.validate_maka_evidence(proof, helper.release_source_binding())
    else:
        helper.validate_maka_evidence(proof, helper.release_source_binding())


def test_all_five_release_dependencies_reopen_complete_original_proofs(
    release_root, canaries, certifier, complete_receipt, tmp_path, monkeypatch,
):
    helper, root, controller, store = release_root
    canary_helper, (standard, deep) = canaries
    _forbid_reopen_sends_and_probes(canary_helper, monkeypatch)
    maka_root, _, _ = _completed_quality_root(certifier, complete_receipt, tmp_path, monkeypatch)

    class Maka:
        target_id = "maka"
        no_tools_characterization_sha256 = "e" * 64
        def __init__(self, **_):
            pass
        def judge(self, request):
            return {"winner": "tie", "auth_route": request.auth_route,
                "requested_model": request.requested_model, "observed_model": request.requested_model,
                "effort": request.effort, "tool_calls": 0, "tool_boundary_sha256": "e" * 64}

    monkeypatch.setattr(certifier, "HarborMakaTransport", Maka)
    monkeypatch.setattr(helper, "probe", certifier)
    certifier._quality_run(maka_root, allow_paid=True, approval_reference="synthetic-owner-123",
        observation_date="2026-09-30", launcher=tmp_path / "never-executed", boundary_sha256="e" * 64)
    summary = canary_helper.summarize_readonly_canaries(standard, deep)
    values = {"maka": helper.maka_evidence(maka_root)}
    for tier, receipt in (("standard", standard), ("deep", deep)):
        values[tier + "_canary"] = {"schema_version": "fanout-release-canary-v1", "tier": tier,
            "receipt": receipt, "raw_receipt_sha256": summary["tiers"][tier]["raw_receipt_sha256"]}
    values["skill_evaluations"] = helper.capture_skill_evaluations(controller, store)
    monkeypatch.setattr(helper, "run_command", lambda _, **__: helper.ProcessResult(
        helper.ProcessStatus.EXIT, 0, b"full synthetic gate result", b"", capture_complete=True, pid=12345))
    values["strict_gate"] = helper.capture_strict_gate(controller, store)
    assert helper.validate_release_evaluations(controller, root, values,
        helper.release_source_binding(), tmp_path / "trial").passed
    for name in helper.RELEASE_CHECKS:
        damaged = dict(values)
        damaged[name] = {"passed": True}
        assert not helper.validate_release_evaluations(controller, root, damaged,
            helper.release_source_binding(), tmp_path / "trial").passed


def test_release_runner_reopens_current_version_canaries_without_host_probes(
    release_root, canaries, tmp_path, monkeypatch,
):
    helper, _, _, _ = release_root
    canary_helper, (standard, deep) = canaries
    _forbid_reopen_sends_and_probes(canary_helper, monkeypatch)
    # Isolate the canary seam from unrelated synthetic trial/Maka dependencies.
    # Skill receipts and strict-gate captures still use their actual validators.
    trial_root = tmp_path / "trial"
    trial_root.mkdir(mode=0o700)
    trial_controller = helper.create_lifecycle_controller(trial_root / "controller")
    with helper.ArtifactStore(trial_root / "raw") as store:
        checks = {name: {"artifact": dataclasses.asdict(store.write_bytes(
            f"trial/{name}.json", helper.canonical_json({"synthetic": True})))}
            for name in helper.TRIAL_CHECKS}
    trial = {"private_run_root": str(trial_root),
             "release_source_binding": helper.release_source_binding().to_dict(), "checks": checks}
    trial["completion_evidence_name"], trial["completion_evidence_digest"] = helper.write_evidence(
        trial_controller, "container-synthetic-trial-complete",
        {"trial_result_sha256": hashlib.sha256(helper.canonical_json(trial)).hexdigest()})
    (trial_root / "trial-result.json").write_bytes(helper.canonical_json(trial))
    (trial_root / "trial-result.json").chmod(0o600)
    monkeypatch.setattr(helper.probe, "_quality_controller", lambda _: trial_controller)
    from shared.lib.fanout import collaboration
    monkeypatch.setattr(collaboration, "validate_container_trial_result",
                        lambda *_: helper.CertificationDecision(True, ()))
    monkeypatch.setattr(helper, "maka_evidence", lambda _: {"synthetic": True})
    monkeypatch.setattr(helper, "validate_maka_evidence", lambda *_: None)
    monkeypatch.setattr(helper, "run_command", lambda _, **__: helper.ProcessResult(
        helper.ProcessStatus.EXIT, 0, b"synthetic complete strict gate", b"", capture_complete=True, pid=12345))
    destination = tmp_path / "new-release"
    result = helper.run_release_evaluations(destination, trial_root=trial_root,
        maka_root=tmp_path / "synthetic-maka", standard_root=Path(standard["private_run_root"]),
        deep_root=Path(deep["private_run_root"]))
    assert result["schema_version"] == "fanout-release-evaluations-v1"
    assert set(result["checks"]) == set(helper.RELEASE_CHECKS)
    with helper.ArtifactStore.open_existing(destination / "raw") as store:
        for tier, receipt in (("standard", standard), ("deep", deep)):
            ref = helper.ArtifactRef(**result["checks"][tier + "_canary"]["artifact"])
            proof = json.loads(store.read_bytes(ref))
            assert proof["receipt"] == receipt
            assert proof["raw_receipt_sha256"] == hashlib.sha256(
                (Path(receipt["private_run_root"]) / "receipt.json").read_bytes()).hexdigest()
