"""Fixed semantic release dependencies; no certificate issuance or provider sender."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Mapping

if __package__ in (None, ""):
    bootstrap_path = Path(__file__).resolve().parents[1] / "shared/skills/llm-fanout-execute/scripts/execute.py"
    bootstrap_spec = importlib.util.spec_from_file_location("_fanout_checkout_bootstrap", bootstrap_path)
    bootstrap = importlib.util.module_from_spec(bootstrap_spec)
    sys.modules[bootstrap_spec.name] = bootstrap
    bootstrap_spec.loader.exec_module(bootstrap)
    bootstrap._runtime()

from scripts import fanout_container_certify as probe
from scripts import fanout_readonly_canaries as canaries
from scripts import fanout_baseline as baseline
from scripts.lib import checks
from shared.lib.fanout.artifacts import ArtifactRef, ArtifactStore, canonical_json
from shared.lib.fanout.certification import CertificationDecision
from shared.lib.fanout.container_certification import RELEASE_CHECKS, TRIAL_CHECKS, _artifact_json
from shared.lib.fanout.container_routes import _open_parent, _read_owned_json
from shared.lib.fanout.container_source import (
    ReleaseSourceBinding, _MAX_TOTAL_BYTES, _read_source, release_source_binding,
)
from shared.lib.fanout.controller import create_lifecycle_controller, read_evidence, write_evidence
from shared.lib.fanout.evaluation import validate_maka_ballot
from shared.lib.fanout.process import ProcessCommand, ProcessResult, ProcessStatus, run_command

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("llm-fanout-plan", "llm-fanout-execute")
STRICT_ARGV = ("mise", "exec", "--", "make", "precommit")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _seal(controller, category: str, value: dict) -> dict:
    result = dict(value)
    result["completion_evidence_name"], result["completion_evidence_digest"] = write_evidence(
        controller, category, {"proof_sha256": _sha(canonical_json(value))})
    return result


def _authenticate(controller, category: str, value: Mapping) -> None:
    unsigned = dict(value)
    name = unsigned.pop("completion_evidence_name")
    digest = unsigned.pop("completion_evidence_digest")
    if read_evidence(controller, category, name, digest) != {"proof_sha256": _sha(canonical_json(unsigned))}:
        raise ValueError("release proof completion changed")


def _gate_inputs(producer_root: Path | None = None) -> dict:
    root = ROOT if producer_root is None else producer_root
    for skill in SKILLS:
        _read_source(root, f"evals/{skill}/evals.json", _MAX_TOTAL_BYTES)
    return {"files": {name: _sha(_read_source(root, name, _MAX_TOTAL_BYTES))
                      for name in ("Makefile", "scripts/lib/checks.py", "scripts/render.py")},
            "skills": {skill: {"source_manifest": checks.source_manifest(root, skill),
                               "eval_set_hash": checks.eval_set_hash(root, skill)} for skill in SKILLS}}


def _historical_subject(root: Path, *, producer_root: Path | None = None) -> dict:
    if not root.is_absolute():
        raise ValueError("strict gate requires an explicit historical snapshot absolute root")
    producer = ROOT if producer_root is None else producer_root
    expected = json.loads(_read_source(producer, baseline.HISTORICAL_LOCK, _MAX_TOTAL_BYTES))
    if baseline.check_historical(expected, root):
        raise ValueError("strict gate historical source subject changed")
    return {"root": str(root),
            "provenance": json.loads((root.parent / "provenance.json").read_bytes())}


def capture_strict_gate(controller, store: ArtifactStore) -> dict:
    """Run the actual fixed gate once, retaining even unsuccessful/uncertain results."""
    before = release_source_binding().to_dict()
    gate_before = _gate_inputs()
    selected = os.environ.get("KHENRIX_HISTORICAL_BASELINE_ROOT")
    if not selected:
        raise ValueError("strict gate requires an explicit historical snapshot root")
    subject = _historical_subject(Path(selected))
    probe._write_private(controller.root.parent / "strict-gate-attempt.json", canonical_json({
        "argv": list(STRICT_ARGV), "cwd": str(ROOT), "release_source_binding": before,
        "gate_inputs": gate_before, "historical_subject": subject, "retry": False}))
    result = run_command(ProcessCommand(STRICT_ARGV, b"", cwd=ROOT,
        timeout=3600, slot_root=controller.root / "locks" / "slots", max_output_bytes=64 * 1024 * 1024),
        strict_gate_historical_root=Path(subject["root"]))
    stdout = store.write_bytes("strict-gate/stdout.bin", result.stdout)
    stderr = store.write_bytes("strict-gate/stderr.bin", result.stderr)
    proof = _seal(controller, "release-strict-gate-complete", {
        "schema_version": "fanout-release-strict-gate-v1", "argv": list(STRICT_ARGV), "cwd": str(ROOT),
        "status": result.status.value, "returncode": result.returncode,
        "capture_complete": result.capture_complete,
        "timeout": dataclasses.asdict(result.timeout) if result.timeout is not None else None,
        "error": result.error, "pid": result.pid,
        "stdout_ref": dataclasses.asdict(stdout), "stderr_ref": dataclasses.asdict(stderr),
        "before_binding": before, "after_binding": release_source_binding().to_dict(),
        "before_gate_inputs": gate_before, "after_gate_inputs": _gate_inputs(),
        "historical_subject": subject,
    })
    store.write_bytes("strict-gate/process.json", canonical_json(proof))
    return proof


def validate_strict_gate(controller, store: ArtifactStore, value: Mapping,
                         source_binding: ReleaseSourceBinding) -> Path:
    try:
        _authenticate(controller, "release-strict-gate-complete", value)
        if (value["schema_version"] != "fanout-release-strict-gate-v1"
                or source_binding != release_source_binding()
                or value["argv"] != list(STRICT_ARGV)
                or value["status"] != ProcessStatus.EXIT.value or type(value["returncode"]) is not int
                or value["returncode"] != 0 or value["capture_complete"] is not True
                or type(value["pid"]) is not int or value["pid"] <= 0
                or value["timeout"] is not None or value["error"] is not None
                or value["before_binding"] != source_binding.to_dict()
                or value["after_binding"] != source_binding.to_dict()):
            raise ValueError("strict gate did not complete on current fixed inputs")
        for name in ("stdout_ref", "stderr_ref"):
            store.read_bytes(ArtifactRef(**value[name]))
        raw = store.read_bytes(ArtifactRef("strict-gate/process.json", _sha(canonical_json(value)),
                                           len(canonical_json(value))))
        if raw != canonical_json(value):
            raise ValueError("strict process capture changed")
        attempt, raw_attempt = _read_owned_json(controller.root.parent / "strict-gate-attempt.json")
        if (raw_attempt != canonical_json(attempt) or canonical_json(attempt) != canonical_json({
                "argv": list(STRICT_ARGV), "cwd": value["cwd"],
                "release_source_binding": value["before_binding"],
                "gate_inputs": value["before_gate_inputs"],
                "historical_subject": value["historical_subject"], "retry": False})):
            raise ValueError("strict original attempt changed")
        producer = Path(value["cwd"])
        if (not producer.is_absolute() or str(producer) != value["cwd"]
                or producer.resolve(strict=True) != producer):
            raise ValueError("strict producer path is not canonical")
        directory_fd = _open_parent(producer / "source")
        try:
            info = os.fstat(directory_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("strict producer directory is not owned")
        finally:
            os.close(directory_fd)
        if release_source_binding(producer) != source_binding:
            raise ValueError("strict producer source changed")
        if (value["historical_subject"] != _historical_subject(
                Path(value["historical_subject"]["root"]), producer_root=producer)
                or canonical_json(value["before_gate_inputs"]) != canonical_json(_gate_inputs(producer))
                or canonical_json(value["after_gate_inputs"]) != canonical_json(_gate_inputs(producer))
                or release_source_binding(producer) != source_binding
                or release_source_binding() != source_binding):
            raise ValueError("strict producer evidence changed")
        return producer
    except Exception as error:
        raise ValueError("strict gate proof is incomplete or stale") from error


def capture_skill_evaluations(controller, store: ArtifactStore) -> dict:
    binding = release_source_binding().to_dict()
    values = {}
    for skill in SKILLS:
        problems = checks.validate_receipt(ROOT, skill, final=True, panel=["claude", "codex", "agy"])
        if problems:
            raise ValueError(f"{skill} does not have a current final receipt")
        raw = (ROOT / "evals" / skill / "receipt.json").read_bytes()
        ref = store.write_bytes(f"skill-evaluations/{skill}/receipt.json", raw)
        values[skill] = {"receipt_ref": dataclasses.asdict(ref),
                         "source_manifest": checks.source_manifest(ROOT, skill),
                         "source_hash": checks.source_hash(ROOT, skill),
                         "eval_set_hash": checks.eval_set_hash(ROOT, skill)}
    if release_source_binding().to_dict() != binding:
        raise ValueError("release source changed while retaining final skill receipts")
    return _seal(controller, "release-skill-evaluations-complete", {
        "schema_version": "fanout-release-skill-evaluations-v1", "release_source_binding": binding,
        "skills": values})


def validate_skill_evaluations(controller, store: ArtifactStore, value: Mapping,
                               source_binding: ReleaseSourceBinding, *,
                               producer_root: Path | None = None) -> None:
    try:
        _authenticate(controller, "release-skill-evaluations-complete", value)
        producer = ROOT if producer_root is None else producer_root
        if (value["schema_version"] != "fanout-release-skill-evaluations-v1"
                or source_binding != release_source_binding()
                or source_binding != release_source_binding(producer)
                or value["release_source_binding"] != source_binding.to_dict()
                or set(value["skills"]) != set(SKILLS)):
            raise ValueError("skill closure changed")
        for skill in SKILLS:
            item = value["skills"][skill]
            raw = store.read_bytes(ArtifactRef(**item["receipt_ref"]))
            _read_source(producer, f"evals/{skill}/evals.json", _MAX_TOTAL_BYTES)
            if (raw != _read_source(producer, f"evals/{skill}/receipt.json", _MAX_TOTAL_BYTES)
                    or item["source_hash"] != checks.source_hash(producer, skill)
                    or canonical_json(item["source_manifest"]) != canonical_json(checks.source_manifest(producer, skill))
                    or item["eval_set_hash"] != checks.eval_set_hash(producer, skill)
                    or checks.validate_receipt(producer, skill, final=True, panel=["claude", "codex", "agy"])):
                raise ValueError("current raw final receipt was not proven")
        if release_source_binding(producer) != source_binding or release_source_binding() != source_binding:
            raise ValueError("source changed during receipt reopening")
    except Exception as error:
        raise ValueError("skill evaluation proof is incomplete or stale") from error


def maka_evidence(root: Path) -> dict:
    quality, raw = _read_owned_json(root / "quality.json")
    if canonical_json(quality) != raw:
        raise ValueError("Maka result is noncanonical")
    return {"schema_version": "fanout-release-maka-v1", "private_run_root": str(root),
            "quality": quality, "raw_quality_sha256": _sha(raw)}


def validate_maka_evidence(value: Mapping, source_binding: ReleaseSourceBinding) -> None:
    try:
        root = Path(value["private_run_root"])
        actual = maka_evidence(root)
        if dict(value) != actual or value["schema_version"] != "fanout-release-maka-v1":
            raise ValueError("Maka raw result changed")
        quality = value["quality"]
        if (quality["schema_version"] != "fanout-maka-quality-v1"
                or quality["private_run_root"] != str(root)
                or quality["release_source_binding"] != source_binding.to_dict()
                or quality["isolation_passed"] is not True):
            raise ValueError("Maka source/isolation changed")
        unsigned = {key: item for key, item in quality.items()
                    if key not in {"completion_evidence_name", "completion_evidence_digest"}}
        if read_evidence(probe._quality_controller(root), "container-maka-quality-complete",
                         quality["completion_evidence_name"], quality["completion_evidence_digest"]) != {
                             "quality_sha256": _sha(canonical_json(unsigned))}:
            raise ValueError("Maka completion changed")
        receipt = probe._read_private_receipt(root)
        if quality["source_receipt_sha256"] != _sha(canonical_json(receipt)):
            raise ValueError("Maka original provider receipt changed")
        answers = probe._authenticated_quality_answers(root, receipt)
        attempt, _ = _read_owned_json(root / "quality-attempt.json")
        if any(attempt[name] != quality[name] for name in (
            "approval_reference", "observation_date", "source_receipt_sha256", "boundary_sha256")):
            raise ValueError("Maka original attempt changed")
        ballots = quality["ballots"]
        if (not isinstance(ballots, list) or len(ballots) != 6 or set(answers) != set(probe.PROVIDERS)
                or quality["ballot_digest"] != _sha(canonical_json(ballots))):
            raise ValueError("six complete Maka ballots were not retained")
        for ballot, (provider, order) in zip(ballots,
            ((provider, order) for provider in probe.PROVIDERS
             for order in ("candidate-first", "baseline-first")), strict=True):
            validate_maka_ballot(probe._quality_judge_request(provider, answers[provider], order),
                                 ballot, boundary_sha256=quality["boundary_sha256"])
    except Exception as error:
        raise ValueError("Maka full stored ballot proof is incomplete or stale") from error


def validate_release_evaluations(controller, evaluation_root: Path, values: Mapping,
                                 source_binding: ReleaseSourceBinding, trial_root: Path) -> CertificationDecision:
    try:
        if (controller.root != Path(evaluation_root) / "controller"
                or Path(evaluation_root) == Path(trial_root)
                or source_binding != release_source_binding() or set(values) != set(RELEASE_CHECKS)):
            raise ValueError("release root/source/check set changed")
        validate_maka_evidence(values["maka"], source_binding)
        standard, deep = values["standard_canary"], values["deep_canary"]
        for tier, value in (("standard", standard), ("deep", deep)):
            if (set(value) != {"schema_version", "tier", "receipt", "raw_receipt_sha256"}
                    or value["schema_version"] != "fanout-release-canary-v1" or value["tier"] != tier):
                raise ValueError("complete raw canary reference is missing")
        summary = canaries.summarize_readonly_canaries(standard["receipt"], deep["receipt"])
        if any(value["raw_receipt_sha256"] != summary["tiers"][tier]["raw_receipt_sha256"]
               for tier, value in (("standard", standard), ("deep", deep))):
            raise ValueError("raw canary receipt hash changed")
        with ArtifactStore.open_existing(Path(evaluation_root) / "raw") as store:
            producer = validate_strict_gate(controller, store, values["strict_gate"], source_binding)
            validate_skill_evaluations(controller, store, values["skill_evaluations"], source_binding,
                                       producer_root=producer)
        if source_binding != release_source_binding():
            raise ValueError("source changed during semantic reopening")
        return CertificationDecision(True, ())
    except Exception:
        return CertificationDecision(False, ("complete current-source release evaluations were not proven",))


def run_release_evaluations(root: Path, *, trial_root: Path, maka_root: Path,
                            standard_root: Path, deep_root: Path) -> dict:
    root = canaries.smoke._live_root(root, new=True)
    binding = release_source_binding()
    # Trial semantics remain the existing five-core validator, not this release set.
    trial, raw = _read_owned_json(trial_root / "trial-result.json")
    unsigned = {key: value for key, value in trial.items()
                if key not in {"completion_evidence_name", "completion_evidence_digest"}}
    trial_controller = probe._quality_controller(trial_root)
    if (canonical_json(trial) != raw or trial["private_run_root"] != str(trial_root)
            or trial["release_source_binding"] != binding.to_dict()
            or read_evidence(trial_controller, "container-synthetic-trial-complete",
                trial["completion_evidence_name"], trial["completion_evidence_digest"]) != {
                    "trial_result_sha256": _sha(canonical_json(unsigned))}):
        raise ValueError("trial aggregate is stale or unauthenticated")
    from shared.lib.fanout.collaboration import validate_container_trial_result
    with ArtifactStore.open_existing(trial_root / "raw") as store:
        core = {name: _artifact_json(store, trial["checks"][name]["artifact"]) for name in TRIAL_CHECKS}
    if not validate_container_trial_result(trial_controller, trial_root, core, binding).passed:
        raise ValueError("trial core semantics were not proven")
    maka = maka_evidence(maka_root)
    validate_maka_evidence(maka, binding)
    standard, deep = (canaries.read_readonly_canary(path) for path in (standard_root, deep_root))
    summary = canaries.summarize_readonly_canaries(standard, deep)
    root.mkdir(mode=0o700)
    controller = create_lifecycle_controller(root / "controller")
    probe._write_private(root.with_name(root.name + ".controller-capability"),
                         controller.capability.export_token().encode("ascii"))
    with ArtifactStore(root / "raw") as store:
        values = {"maka": maka}
        for tier, receipt in (("standard", standard), ("deep", deep)):
            values[tier + "_canary"] = {"schema_version": "fanout-release-canary-v1", "tier": tier,
                "receipt": receipt, "raw_receipt_sha256": summary["tiers"][tier]["raw_receipt_sha256"]}
        values["skill_evaluations"] = capture_skill_evaluations(controller, store)
        values["strict_gate"] = capture_strict_gate(controller, store)
        refs = {name: {"artifact": dataclasses.asdict(store.write_bytes(
                    f"release-evaluations/{name}.json", canonical_json(values[name])))} for name in RELEASE_CHECKS}
    decision = validate_release_evaluations(controller, root, values, binding, trial_root)
    if not decision.passed:
        raise ValueError("release evaluations failed; raw attempts are retained without retry")
    aggregate = {"schema_version": "fanout-release-evaluations-v1", "private_run_root": str(root),
                 "trial_root": str(trial_root), "release_source_binding": binding.to_dict(), "checks": refs}
    aggregate["completion_evidence_name"], aggregate["completion_evidence_digest"] = write_evidence(
        controller, "container-release-evaluations-complete",
        {"release_evaluations_sha256": _sha(canonical_json(aggregate))})
    probe._write_private(root / "release-evaluations.json", canonical_json(aggregate))
    return aggregate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("private-run-root", "trial-root", "maka-root", "standard-root", "deep-root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--run-strict-gate", action="store_true")
    options = parser.parse_args(argv)
    try:
        if not options.run_strict_gate:
            raise ValueError("actual strict gate execution requires --run-strict-gate")
        run_release_evaluations(options.private_run_root, trial_root=options.trial_root,
            maka_root=options.maka_root, standard_root=options.standard_root, deep_root=options.deep_root)
        print("complete private release evaluations retained; no certificate was issued")
        return 0
    except (ValueError, OSError):
        print("release evaluations refused; existing private attempts remain unchanged", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
