"""Receipt adoption is an explicit unverified exception, not recertification."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JUDGE_CONFIG = {"model": "requested-evaluator", "thinking": "xhigh"}
sys.path[:0] = [str(ROOT / "scripts/lib"), str(ROOT / "scripts")]
import checks
import eval_harness


def test_evaluation_snapshot_api_available_before_campaign_spend():
    assert callable(getattr(checks, "capture_evaluation_snapshot", None)), "campaign has no pre-spend source snapshot"


@pytest.mark.parametrize("change", ("file-bytes", "destination"))
def test_regular_file_bundle_and_destination_are_delivered_behavior(tmp_path, change):
    put(tmp_path, "shared/skills/alpha/SKILL.md", "Read the bundled asset.\n")
    source = put(tmp_path, "shared/runtime/asset.txt", "original\n")
    manifest = put(tmp_path, "capabilities.toml",
        '[skill_delivery.skill_bundles.alpha]\n"asset.txt" = "shared/runtime/asset.txt"\n')
    before = checks.delivered_behavior_manifest(tmp_path, "alpha")
    if change == "file-bytes":
        source.write_text("changed\n")
    else:
        manifest.write_text(manifest.read_text().replace('"asset.txt"', '"renamed.txt"'))
    assert checks.delivered_behavior_manifest(tmp_path, "alpha") != before


def put(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def adopted(tmp_path, monkeypatch):
    put(tmp_path, "capabilities.toml", '[historical_only]\nskills = ["llm-council", "llm-forge"]\nlibraries = ["council", "forge"]\n')
    put(tmp_path, "shared/skills/alpha/SKILL.md", "Answer carefully.\n")
    put(tmp_path, "scripts/lib/reconcile.py", "# bundled helper\n")
    put(tmp_path, "scripts/eval_harness.py", "# measuring instrument\n")
    put(tmp_path, "evals/alpha/evals.json", '{"evals": []}\n')
    put(tmp_path, "evals/alpha/fixtures/input.txt", "original fixture\n")
    receipt = put(tmp_path, "evals/alpha/receipt.json", json.dumps({
        "schema_version": 2, "source_hash": "historically-stale", "eval_set_hash": "old",
        "provenance": "eval", "certified_by": "delta-gate",
    }))
    snapshot = checks.capture_evaluation_snapshot(tmp_path, "alpha")
    baseline = {"schema_version": 1, "baseline_commit": checks.ADOPTION_BASELINE_COMMIT,
                "status": "HISTORICAL_UNVERIFIED", "skills": {"alpha": {
                    "source_manifest": snapshot["source_manifest"],
                    "criteria_manifest": snapshot["criteria_manifest"],
                    "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                }}}
    path = put(tmp_path, "evals/legacy-adoption-baseline.json", json.dumps(baseline))
    monkeypatch.setattr(checks, "ADOPTION_BASELINE_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    write_proof(tmp_path)
    return tmp_path


def write_proof(root):
    manifest = checks.instrument_manifest(root)
    proof = {"schema_version": 1, "instrument_manifest": manifest,
             "instrument_hash": checks.manifest_hash(manifest),
             "tests": [{"command": ["pytest", "tests/test_contract.py"],
                        "counts": {"tests_run": 3, "failed": 0, "skipped": 0},
                        "evidence_sha256": "a" * 64}],
             "native_sessions": {provider: {"valid": True, "session_id": provider + "-session",
                 "role": "evaluator", "judge_config": {**JUDGE_CONFIG, "thinking": "high" if provider == "agy" else "xhigh"},
                 "cli_version": "observed-version", "raw_capture_sha256": "b" * 64,
                 "read_tool_evidence_sha256": "c" * 64} for provider in ("claude", "codex", "agy")},
             "private_artifact_manifest_sha256": "d" * 64}
    return put(root, "evals/instrument-proof.json", json.dumps(proof))


def test_adoption_warns_but_never_passes_final(adopted):
    with pytest.warns(UserWarning, match="HISTORICAL_UNVERIFIED"):
        assert checks.validate_receipt(adopted, "alpha") == []
    assert any("HISTORICAL_UNVERIFIED" in p for p in checks.validate_receipt(adopted, "alpha", final=True))


def test_instrument_edit_does_not_change_behavior_but_stales_shared_proof(adopted):
    original = checks.delivered_behavior_manifest(adopted, "alpha")
    put(adopted, "scripts/eval_harness.py", "# changed measurement\n")
    assert checks.delivered_behavior_manifest(adopted, "alpha") == original
    assert any("instrument proof" in p for p in checks.validate_receipt(adopted, "alpha"))
    write_proof(adopted)
    with pytest.warns(UserWarning, match="HISTORICAL_UNVERIFIED"):
        assert checks.validate_receipt(adopted, "alpha") == []


@pytest.mark.parametrize("relative", ["shared/skills/alpha/SKILL.md", "scripts/lib/reconcile.py",
    "evals/alpha/evals.json", "evals/alpha/fixtures/input.txt", "evals/alpha/receipt.json"])
def test_changed_delivery_criteria_fixture_or_old_receipt_refuses_adoption(adopted, relative):
    path = adopted / relative
    path.write_bytes(path.read_bytes() + b"\n")
    assert checks.validate_receipt(adopted, "alpha")


def test_forged_baseline_commit_refused_even_with_matching_file_digest(adopted, monkeypatch):
    path = adopted / "evals/legacy-adoption-baseline.json"
    document = json.loads(path.read_text())
    document["baseline_commit"] = "f" * 40
    path.write_text(json.dumps(document))
    monkeypatch.setattr(checks, "ADOPTION_BASELINE_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    assert checks.validate_receipt(adopted, "alpha")


def test_missing_old_receipt_cannot_be_adopted(adopted):
    (adopted / "evals/alpha/receipt.json").unlink()
    assert any("no receipt" in p for p in checks.validate_receipt(adopted, "alpha"))


def test_target_deterministic_suite_is_criteria_not_only_instrument(adopted):
    put(adopted, "scripts/eval_harness.py", 'DETERMINISTIC_GATED = {"alpha": ["pytest", "tests/test_alpha.py"]}\n')
    test = put(adopted, "tests/test_alpha.py", "def test_contract(): assert True\n")
    before = checks.criteria_manifest(adopted, "alpha")
    test.write_text("def test_contract(): pass\n")
    assert checks.criteria_manifest(adopted, "alpha") != before


def test_legacy_restamp_is_not_an_alternate_adoption_route(adopted):
    path = adopted / "evals/alpha/receipt.json"
    receipt = json.loads(path.read_text())
    receipt.update(source_hash=checks.source_hash(adopted, "alpha"),
                   eval_set_hash=checks.eval_set_hash(adopted, "alpha"))
    path.write_text(json.dumps(receipt))
    assert checks.validate_receipt(adopted, "alpha")


@pytest.mark.parametrize("change", ["zero_tests", "missing_cli", "missing_read", "missing_manifest"])
def test_shared_proof_requires_actual_counts_and_all_native_evidence(adopted, change):
    path = adopted / "evals/instrument-proof.json"
    proof = json.loads(path.read_text())
    if change == "zero_tests":
        proof["tests"][0]["counts"]["tests_run"] = 0
    elif change == "missing_cli":
        del proof["native_sessions"]["agy"]
    elif change == "missing_read":
        proof["native_sessions"]["codex"]["read_tool_evidence_sha256"] = None
    else:
        proof["private_artifact_manifest_sha256"] = None
    path.write_text(json.dumps(proof))
    assert any("instrument proof" in p for p in checks.validate_receipt(adopted, "alpha"))


def test_snapshot_change_before_issuance_preserves_existing_receipt(adopted, monkeypatch):
    snapshot = checks.capture_evaluation_snapshot(adopted, "alpha", judge_config=JUDGE_CONFIG, providers=["codex"])
    previous = (adopted / "evals/alpha/receipt.json").read_bytes()
    put(adopted, "shared/skills/alpha/SKILL.md", "changed during campaign\n")
    monkeypatch.setattr(eval_harness, "ROOT", adopted)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", adopted / "evals")
    with pytest.raises(SystemExit, match="changed during"):
        eval_harness._write_receipt("alpha", providers=["codex"], mode="normal", judge="claude",
            delta=0, seeded=False, evaluated_snapshot=snapshot, judge_config=JUDGE_CONFIG,
            cli_versions={"codex": "observed-version", "claude": "observed-judge-version"},
            raw_refs=[{"path": "workspace/raw.json", "sha256": "e" * 64}])
    assert (adopted / "evals/alpha/receipt.json").read_bytes() == previous


def test_fresh_v3_preserves_evaluated_snapshots_and_unknown_native_counts(adopted, monkeypatch):
    snapshot = checks.capture_evaluation_snapshot(adopted, "alpha", judge_config=JUDGE_CONFIG, providers=["codex"])
    raw = put(adopted, "evals/alpha/workspace/raw.json", '{"actual": "capture"}\n')
    raw_refs = [{"path": "workspace/raw.json", "sha256": hashlib.sha256(raw.read_bytes()).hexdigest()}]
    monkeypatch.setattr(eval_harness, "ROOT", adopted)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", adopted / "evals")
    eval_harness._write_receipt("alpha", providers=["codex"], mode="normal", judge="claude",
        delta=0, seeded=False, evaluated_snapshot=snapshot,
        judge_config=JUDGE_CONFIG,
        raw_refs=raw_refs,
        cli_versions={"codex": "observed-version", "claude": "observed-judge-version"})
    receipt = json.loads((adopted / "evals/alpha/receipt.json").read_text())
    assert receipt["schema_version"] == 3
    assert receipt["source_manifest"] == snapshot["source_manifest"]
    assert receipt["criteria_manifest"] == snapshot["criteria_manifest"]
    assert receipt["instrument_manifest"] == snapshot["instrument_manifest"]
    assert receipt["raw_refs"] == raw_refs
    assert receipt["native_counts"] is None
    assert receipt["judge_config"] == JUDGE_CONFIG
    assert checks.validate_receipt(adopted, "alpha") == []


def test_executor_canary_is_not_shared_evaluator_proof(adopted):
    path = adopted / "evals/instrument-proof.json"
    proof = json.loads(path.read_text())
    proof["native_sessions"]["claude"]["role"] = "executor"
    path.write_text(json.dumps(proof))
    assert checks.instrument_proof_problems(adopted)


def test_wiki_test_edit_changes_criteria_not_delivered_runtime(adopted):
    put(adopted, "shared/lib/wikisync/runtime.py", "# actual bundled runtime\n")
    test = put(adopted, "shared/lib/wikisync/tests/test_runtime.py", "# not bundled\n")
    before = checks.delivered_behavior_manifest(adopted, "khenrix-wiki-add")
    test.write_text("# changed unbundled test\n")
    assert checks.delivered_behavior_manifest(adopted, "khenrix-wiki-add") == before


def test_stale_rendered_template_cannot_be_measured_as_current_source(adopted):
    put(adopted, "shared/skill-templates/beta/SKILL.md.tmpl", "Hello $name\n")
    put(adopted, "capabilities.toml", '[skill_facts.beta.codex]\nname = "Current"\n')
    body = put(adopted, "marketplaces/codex/plugins/khenrix-utils/skills/beta/SKILL.md", "Hello Stale\n")
    with pytest.raises(ValueError, match="rendered"):
        checks.capture_evaluation_snapshot(adopted, "beta", providers=["codex"])
    body.write_text("Hello Current\n")
    snapshot = checks.capture_evaluation_snapshot(adopted, "beta", providers=["codex"])
    assert snapshot["selected_input_manifest"] == [["codex:SKILL.md", hashlib.sha256(b"Hello Current\n").hexdigest()]]


def test_fresh_issuance_requires_observed_versions_for_executor_and_judge(adopted, monkeypatch):
    raw = put(adopted, "evals/alpha/workspace/raw.json", "actual raw\n")
    snapshot = checks.capture_evaluation_snapshot(adopted, "alpha", judge_config=JUDGE_CONFIG, providers=["codex"])
    monkeypatch.setattr(eval_harness, "ROOT", adopted)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", adopted / "evals")
    previous = (adopted / "evals/alpha/receipt.json").read_bytes()
    with pytest.raises(SystemExit, match="CLI version"):
        eval_harness._write_receipt("alpha", providers=["codex"], mode="normal", judge="claude",
            delta=0, seeded=False, judge_config=JUDGE_CONFIG, evaluated_snapshot=snapshot,
            cli_versions={"codex": "observed-version"},
            raw_refs=[{"path": "workspace/raw.json", "sha256": hashlib.sha256(raw.read_bytes()).hexdigest()}])
    assert (adopted / "evals/alpha/receipt.json").read_bytes() == previous


def test_final_receipt_rechecks_actual_rendered_provider_input(adopted):
    put(adopted, "shared/skill-templates/beta/SKILL.md.tmpl", "Hello $name\n")
    put(adopted, "capabilities.toml", '[skill_facts.beta.codex]\nname = "Current"\n')
    body = put(adopted, "marketplaces/codex/plugins/khenrix-utils/skills/beta/SKILL.md", "Hello Current\n")
    put(adopted, "evals/beta/evals.json", "{}\n")
    snapshot = checks.capture_evaluation_snapshot(adopted, "beta", providers=["codex"], judge_config=JUDGE_CONFIG)
    receipt = {**snapshot, "schema_version": 3, "providers": ["codex"], "judge": "claude",
        "provenance": "eval", "certified_by": "delta-gate", "per_provider": {"codex": {"delta": 0}},
        "cli_versions": {"codex": "observed-executor", "claude": "observed-judge"},
        "raw_refs": [{"path": "workspace/raw.json", "sha256": "e" * 64}]}
    put(adopted, "evals/beta/receipt.json", json.dumps(receipt))
    write_proof(adopted)
    assert checks.validate_receipt(adopted, "beta", final=True, panel=["codex"]) == []
    body.write_text("Hello Unmeasured\n")
    assert checks.validate_receipt(adopted, "beta", final=True, panel=["codex"])


@pytest.mark.parametrize("provider", ["claude", "codex", "agy"])
def test_shared_evaluator_proof_refuses_executor_effort(adopted, provider):
    path = adopted / "evals/instrument-proof.json"
    proof = json.loads(path.read_text())
    proof["native_sessions"][provider]["judge_config"] = {"model": "explicit-approved-model", "thinking": "medium"}
    path.write_text(json.dumps(proof))
    assert checks.instrument_proof_problems(adopted)
