"""Sealed paired quality campaigns and tool-free Maka judgment."""
from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_quality_contracts", PACKAGE / "__init__.py",
    submodule_search_locations=[str(PACKAGE)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)
quality = importlib.import_module(f"{SPEC.name}.quality")
evaluation = importlib.import_module(f"{SPEC.name}.evaluation")
JudgeRequest = evaluation.JudgeRequest
JudgeTransportError = evaluation.JudgeTransportError
MakaJudgeAdapter = evaluation.MakaJudgeAdapter
QualityError = quality.QualityError
evaluate_campaign = quality.evaluate_campaign
exact_upper_loss_bound = quality.exact_upper_loss_bound
load_casebook = quality.load_casebook
verify_receipt = quality.verify_receipt
wilson_lower_bound = quality.wilson_lower_bound


CASEBOOK = ROOT / "evals" / "llm-fanout" / "quality-cases.json"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical_hash(value: object) -> str:
    return _hash(json.dumps(value, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False) + "\n")


def _ballots(
    case_id: str, panel_id: str, verdict: str,
    candidate_digest: str, baseline_digest: str,
) -> list[dict[str, object]]:
    result = []
    for judge, model, family in (
        ("claude", "claude-opus-5-5", "claude"),
        ("codex", "gpt-6.1-sol", "openai"),
        ("maka", "gpt-6.1-sol", "openai"),
    ):
        for order in ("candidate-first", "baseline-first"):
            winner = ("tie" if verdict == "tie" else
                      "A" if (verdict == "candidate") == (order == "candidate-first") else "B")
            result.append({
                "case_id": case_id, "panel_id": panel_id,
                "judge": judge, "family": family, "order": order, "winner": winner,
                "auth_route": ("api-key-relay" if panel_id == "interactive" else "harbor-lab")
                if judge == "maka" else f"{judge}-auth",
                "requested_model": model, "observed_model": model,
                "effort": "xhigh", "tool_boundary_sha256": _hash(
                    panel_id + "boundary" if judge == "maka" else "no-tools-" + judge
                ),
                "tool_calls": 0,
                "presented_a_sha256": (
                    candidate_digest if order == "candidate-first" else baseline_digest
                ),
                "presented_b_sha256": (
                    baseline_digest if order == "candidate-first" else candidate_digest
                ),
            })
    return result


def _valid_evidence(casebook: dict[str, object]) -> dict[str, object]:
    """Build fake scorer inputs and pin their synthetic fixture catalog in the test book."""
    council = [case for case in casebook["cases"] if case["kind"] == "council"]
    forge = [case for case in casebook["cases"] if case["kind"] == "forge"]
    panels = []
    for panel_id, model, route in (
        ("interactive", "gpt-6.1-sol", "api-key-relay"),
        ("harbor", "gpt-6.1-sol", "harbor-lab"),
    ):
        results = []
        for index, case in enumerate(council):
            case_id = case["id"]
            results.append({
                "case_id": case_id,
                "baseline": {"delivered": True, "assertions": [True] * len(case["assertions"]),
                             "artifact_sha256": _hash(case_id + "baseline"),
                             "installed_consumer": True},
                "candidate": {"delivered": True, "assertions": [True] * len(case["assertions"]),
                              "artifact_sha256": _hash(case_id + "candidate"),
                              "installed_consumer": True,
                              "synthesis_sources": [_hash(case_id + "claude"),
                                                    _hash(case_id + "codex")],
                              "verification_receipt_sha256": _hash(case_id + "read-verifier"),
                              "verified_final_sha256": _hash(case_id + "candidate")},
                "blocker_regressions": [],
                "ballots": _ballots(
                    case_id, panel_id, "candidate" if index < 20 else "tie",
                    _hash(case_id + "candidate"),
                    _hash(case_id + "baseline"),
                ),
            })
        panels.append({
            "panel_id": panel_id, "auth_route": route, "requested_model": model,
            "observed_model": model, "effort": "xhigh",
            "no_tools_characterization_sha256": _hash(panel_id + "boundary"),
            "cases": results,
        })
    evidence = {
        "schema_version": 1,
        "panels": panels,
        "forge": [{
            "case_id": case["id"], "baseline_fresh_verifier": True,
            "candidate_fresh_verifier": True, "baseline_held_out_detected": True,
            "candidate_held_out_detected": True,
            "candidate_delivered": True, "candidate_synthesized": True,
            "source_contributions": [_hash(case["id"] + "claude"),
                                     _hash(case["id"] + "agy")],
            "checks_no_weaker": True, "caller_mutated": False,
            "installed_consumer": True,
            "baseline_verifier_receipt_sha256": _hash(case["id"] + "baseline-verifier"),
            "candidate_verifier_receipt_sha256": _hash(case["id"] + "candidate-verifier"),
            "candidate_final_artifact_sha256": _hash(case["id"] + "final"),
            "verified_candidate_sha256": _hash(case["id"] + "final"),
            "caller_before_sha256": _hash(case["id"] + "caller"),
            "caller_after_sha256": _hash(case["id"] + "caller"),
            "held_out_case_sha256": _canonical_hash(case),
            "baseline_held_out_evidence_sha256": _hash(case["id"] + "baseline-heldout"),
            "candidate_held_out_evidence_sha256": _hash(case["id"] + "candidate-heldout"),
            "fixture_manifest_sha256": _hash(case["id"] + "fixture-manifest"),
            "source_tree_sha256": _hash(case["id"] + "source-tree"),
            "owned_paths": [f"cases/{case['id']}/source.py"],
            "declared_checks": [{
                "id": "held-out", "spec_sha256": _hash(case["id"] + "check")
            }],
            "fixture_execution_sha256": _hash(case["id"] + "fixture-execution"),
        } for case in forge],
        "ablation": [{
            "case_id": case["id"], "assignment_nonce": f"{index + 1:032x}",
            "order": "enabled-first" if index % 2 else "disabled-first",
            "sharing_enabled_score": 0.9, "sharing_disabled_score": 0.8,
        } for index, case in enumerate(council[:12])],
        "transfers": [{
            "case_id": council[index]["id"], "fact_hex": f"{index + 1:032x}",
            "peer_received": True, "blind_leak": False,
            "poisoned_peer_adoption": False,
        } for index in range(6)],
        "operations": [{
            "sample_id": f"op-{index:02d}", "baseline_ms": 100 + index,
            "candidate_ms": 90 + index, "standard_two_round": True,
            "turns": 6, "retries": 0, "settled_replays": 0,
        } for index in range(30)],
        "cost_usd": None,
        "provenance": {"baseline_lock_sha256": _hash("baseline-lock"),
                       "run_inputs_sha256": _hash("fanout-run-inputs")},
    }
    # Synthetic fixture catalog only for scorer tests; the committed book remains unpinned.
    casebook["forge_fixture_manifest_sha256"] = _canonical_hash([
        {key: row[key] for key in (
            "case_id", "fixture_manifest_sha256", "source_tree_sha256",
            "owned_paths", "declared_checks",
        )}
        for row in evidence["forge"]
    ])
    return evidence


def test_casebook_preregisters_distinct_sealed_council_and_forge_cases():
    book = load_casebook(CASEBOOK)
    cases = book["cases"]
    council = [case for case in cases if case["kind"] == "council"]
    forge = [case for case in cases if case["kind"] == "forge"]

    assert len(council) >= 60
    assert len(forge) >= 12
    assert {case["mode"] for case in council} == {"normal", "deep"}
    assert {case["mode"] for case in forge} >= {"review", "deep-review", "ingress"}
    assert len({case["id"] for case in cases}) == len(cases)
    assert len({case["prompt"] for case in cases}) == len(cases)
    assert all(case["assertions"] for case in cases)


def test_executor_input_exposes_only_one_case_prompt_not_hidden_assertions():
    book = load_casebook(CASEBOOK)
    prompt = quality.case_prompt(book, "deep-16")
    assert prompt.startswith("A producer adds 2400 records/s")
    assert "Saturation occurs at 50 seconds" not in prompt
    assert "net growth is 900 records/s" not in prompt
    assert "quality-cases.json" not in prompt
    assert "assertions" not in prompt


def test_casebook_rejects_duplicate_content_and_unsealed_revision(tmp_path):
    book = load_casebook(CASEBOOK)
    changed = copy.deepcopy(book)
    changed["cases"][1]["prompt"] = changed["cases"][0]["prompt"]
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(QualityError, match="duplicate.*prompt"):
        load_casebook(path)

    changed["cases"][1]["prompt"] = "A changed answer case after preregistration."
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(QualityError, match="seal"):
        load_casebook(path)


def test_casebook_requires_dedicated_fusion_cases_even_if_twelve_forge_remain(tmp_path):
    book = load_casebook(CASEBOOK)
    changed = copy.deepcopy(book)
    for case in changed["cases"]:
        if case["kind"] == "forge" and case["mode"] == "fusion":
            case["mode"] = "review"
    changed["cases_sha256"] = _canonical_hash(changed["cases"])
    path = tmp_path / "no-fusion.json"
    path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(QualityError, match="fusion"):
        load_casebook(path)


def test_exact_one_sided_bound_requires_zero_losses_at_sixty_cases():
    assert exact_upper_loss_bound(0, 60) == pytest.approx(0.04870291331009746, abs=1e-12)
    assert exact_upper_loss_bound(0, 60) < 0.05
    assert exact_upper_loss_bound(1, 60) > 0.05
    assert exact_upper_loss_bound(60, 60) == 1.0
    with pytest.raises(QualityError):
        exact_upper_loss_bound(-1, 60)


def test_wilson_superiority_requires_twenty_decisive_cases():
    assert wilson_lower_bound(20, 20) > 0.8
    assert wilson_lower_bound(10, 20) < 0.5


def test_green_campaign_scores_two_independent_maka_panels_and_hashes_inputs():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)

    receipt = evaluate_campaign(book, evidence)

    assert receipt["status"] == "green"
    assert receipt["manifest_sha256"] == _hash(json.dumps(book, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False) + "\n")
    assert receipt["evidence_sha256"] == _hash(json.dumps(evidence, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False) + "\n")
    assert {panel["panel_id"] for panel in receipt["panels"]} == {"interactive", "harbor"}
    assert all(panel["status"] == "green" for panel in receipt["panels"])
    assert all(panel["distinct_cases"] == 60 for panel in receipt["panels"])
    assert all(panel["decisive_cases"] == 20 for panel in receipt["panels"])
    assert all(panel["superior"] for panel in receipt["panels"])
    assert all(panel["strata"]["normal"]["distinct_cases"] == 30 for panel in receipt["panels"])
    assert all(panel["strata"]["deep"]["distinct_cases"] == 30 for panel in receipt["panels"])
    assert receipt["forge"]["distinct_cases"] == 12
    assert receipt["ablation"]["mean_delta"] == pytest.approx(0.1)
    assert receipt["operations"]["samples"] == 30
    assert receipt["operations"]["candidate_p95_ms"] == 118
    assert receipt["cost_usd"] is None
    assert verify_receipt(receipt, book, evidence)
    assert "prompt" not in json.dumps(receipt)


def test_interactive_panel_rejects_obsolete_auth_route_even_with_matching_ballots():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    interactive = evidence["panels"][0]
    interactive["auth_route"] = "maka-subscription"
    for row in interactive["cases"]:
        for ballot in row["ballots"]:
            if ballot["judge"] == "maka":
                ballot["auth_route"] = "maka-subscription"

    receipt = evaluate_campaign(book, evidence)

    assert receipt["status"] == "red"
    assert receipt["panels"][0]["status"] == "red"


def test_two_maka_panels_must_judge_the_same_delivered_final_answers():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    changed = evidence["panels"][1]["cases"][0]
    changed["candidate"]["artifact_sha256"] = _hash("alternate final answer")
    changed["candidate"]["verified_final_sha256"] = _hash("alternate final answer")
    for ballot in changed["ballots"]:
        candidate_position = (
            "presented_a_sha256" if ballot["order"] == "candidate-first"
            else "presented_b_sha256"
        )
        ballot[candidate_position] = _hash("alternate final answer")

    receipt = evaluate_campaign(book, evidence)

    assert all(panel["status"] == "green" for panel in receipt["panels"])
    assert receipt["status"] == "red"
    assert receipt["cross_panel_consistent"] is False


def test_candidate_failed_delivery_is_loss_even_when_baseline_was_wrong():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    for panel in evidence["panels"]:
        first = panel["cases"][0]
        first["baseline"]["assertions"][0] = False
        first["candidate"]["delivered"] = False
        first["candidate"]["assertions"] = []
        first["ballots"] = []

    receipt = evaluate_campaign(book, evidence)

    assert receipt["status"] == "red"
    assert all(panel["candidate_failed_deliveries"] == 1 for panel in receipt["panels"])
    assert all(panel["missing_outcomes"] == 0 for panel in receipt["panels"])
    assert all(panel["paired_losses"] == 0 for panel in receipt["panels"])
    assert all(panel["baseline_wins"] >= 1 for panel in receipt["panels"])
    assert all(panel["decisive_cases"] == 19 for panel in receipt["panels"])


def test_missing_entire_outcome_is_delivery_loss_not_observed_paired_loss():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    for panel in evidence["panels"]:
        panel["cases"].pop(0)

    receipt = evaluate_campaign(book, evidence)

    assert receipt["status"] == "red"
    assert all(panel["distinct_cases"] == 59 for panel in receipt["panels"])
    assert all(panel["paired_cases"] == 59 for panel in receipt["panels"])
    assert all(panel["candidate_failed_deliveries"] == 1 for panel in receipt["panels"])
    assert all(panel["missing_outcomes"] == 1 for panel in receipt["panels"])
    assert all(panel["paired_losses"] == 0 for panel in receipt["panels"])
    assert all(panel["loss_upper_95"] == pytest.approx(exact_upper_loss_bound(0, 59))
               for panel in receipt["panels"])
    assert all(panel["baseline_wins"] == 1 for panel in receipt["panels"])


def test_no_delivered_council_baseline_cannot_certify_candidate_superiority():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    for panel in evidence["panels"]:
        for row in panel["cases"]:
            row["baseline"]["delivered"] = False
            row["baseline"]["assertions"] = []
            row["ballots"] = []

    receipt = evaluate_campaign(book, evidence)

    assert receipt["status"] == "red"
    assert all(panel["baseline_failed_deliveries"] == 60 for panel in receipt["panels"])
    assert all(panel["paired_cases"] == 0 for panel in receipt["panels"])
    assert all(panel["loss_upper_95"] == 1.0 for panel in receipt["panels"])
    assert all(panel["candidate_wins"] == 0 for panel in receipt["panels"])


def test_paired_assertion_regression_and_exact_loss_bound_fail_panel():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    first = evidence["panels"][0]["cases"][0]
    first["candidate"]["assertions"][0] = False

    receipt = evaluate_campaign(book, evidence)

    assert receipt["status"] == "red"
    assert receipt["panels"][0]["paired_losses"] == 1
    assert receipt["panels"][0]["loss_upper_95"] > 0.05
    assert receipt["panels"][1]["status"] == "green"


def test_pinned_maka_panel_effort_cannot_silently_downgrade():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    harbor = evidence["panels"][1]
    harbor["effort"] = "low"
    for row in harbor["cases"]:
        for ballot in row["ballots"]:
            if ballot["judge"] == "maka":
                ballot["effort"] = "low"
    receipt = evaluate_campaign(book, evidence)
    assert receipt["status"] == "red"
    assert receipt["panels"][1]["status"] == "red"


def test_non_maka_judge_model_substitution_is_not_a_valid_ballot():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    first = evidence["panels"][0]["cases"][0]
    for ballot in first["ballots"]:
        if ballot["judge"] == "codex":
            ballot["observed_model"] = "another-model"

    receipt = evaluate_campaign(book, evidence)

    assert receipt["status"] == "red"
    assert receipt["panels"][0]["provenance_failures"] == 2
    assert receipt["panels"][0]["decisive_cases"] == 19


def test_historical_harbor_maka_ballots_fail_the_current_campaign_contract():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    for ballot in evidence["panels"][1]["cases"][0]["ballots"]:
        if ballot["judge"] == "maka":
            ballot["requested_model"] = "gpt-5.6-sol"
            ballot["observed_model"] = "gpt-5.6-sol"
    receipt = evaluate_campaign(book, evidence)
    assert receipt["status"] == "red"
    assert receipt["panels"][0]["status"] == "green"
    assert receipt["panels"][1]["provenance_failures"] == 2


def test_claude_labeled_openai_ballot_cannot_supply_an_independent_family():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    for ballot in evidence["panels"][0]["cases"][0]["ballots"]:
        if ballot["judge"] == "claude":
            ballot["requested_model"] = "gpt-6.1-sol"
            ballot["observed_model"] = "gpt-6.1-sol"
            ballot["auth_route"] = "codex-auth"

    receipt = evaluate_campaign(book, evidence)

    assert receipt["status"] == "red"
    assert receipt["panels"][0]["provenance_failures"] == 2
    assert receipt["panels"][0]["decisive_cases"] == 19


def test_unverified_reconciled_answer_and_maka_boundary_mismatch_red_only_affected_panel():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    first = evidence["panels"][0]["cases"][0]
    first["candidate"]["verified_final_sha256"] = _hash("another final answer")
    first["ballots"][-1]["tool_boundary_sha256"] = _hash("different boundary")

    receipt = evaluate_campaign(book, evidence)

    assert receipt["status"] == "red"
    assert receipt["panels"][0]["status"] == "red"
    assert receipt["panels"][0]["provenance_failures"] >= 1
    assert receipt["panels"][1]["status"] == "green"


def test_position_inconsistency_abstains_despite_other_judges_agreeing():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    first = evidence["panels"][0]["cases"][0]
    for ballot in first["ballots"]:
        if ballot["judge"] == "claude" and ballot["order"] == "baseline-first":
            ballot["winner"] = "A"

    receipt = evaluate_campaign(book, evidence)

    assert receipt["panels"][0]["abstentions"] == 1
    assert receipt["panels"][0]["decisive_cases"] == 19
    assert receipt["panels"][0]["win_or_tie"] == pytest.approx(59 / 60)
    assert receipt["panels"][0]["superior"] is False


def test_codex_maka_family_disagreement_abstains_despite_claude_preference():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    first = evidence["panels"][0]["cases"][0]
    for ballot in first["ballots"]:
        if ballot["judge"] == "maka":
            ballot["winner"] = "A" if ballot["order"] == "baseline-first" else "B"

    receipt = evaluate_campaign(book, evidence)

    assert receipt["panels"][0]["abstentions"] == 1
    assert receipt["panels"][0]["decisive_cases"] == 19
    assert receipt["panels"][1]["decisive_cases"] == 20


def test_claimed_reverse_order_without_reversing_delivered_answers_is_red():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    first = evidence["panels"][0]["cases"][0]
    for ballot in first["ballots"]:
        ballot["order"] = (
            "baseline-first" if ballot["order"] == "candidate-first"
            else "candidate-first"
        )

    receipt = evaluate_campaign(book, evidence)

    assert receipt["status"] == "red"
    assert receipt["panels"][0]["provenance_failures"] == 6


@pytest.mark.parametrize("field,wrong", [
    ("case_id", "normal-02"), ("panel_id", "harbor"),
])
def test_ballot_from_another_case_or_panel_is_not_counted(field, wrong):
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    ballot = evidence["panels"][0]["cases"][0]["ballots"][0]
    ballot[field] = wrong

    receipt = evaluate_campaign(book, evidence)

    assert receipt["status"] == "red"
    assert receipt["panels"][0]["provenance_failures"] == 1
    assert receipt["panels"][0]["decisive_cases"] == 19


def test_repeated_same_family_ballots_never_increase_decisive_case_count():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    for panel in evidence["panels"]:
        panel["cases"][0]["ballots"].extend(copy.deepcopy(panel["cases"][0]["ballots"][2:4]))
    with pytest.raises(QualityError, match="duplicate.*ballot"):
        evaluate_campaign(book, evidence)


def test_new_blocker_regression_forces_red_even_when_all_judges_prefer_candidate():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    evidence["panels"][0]["cases"][4]["blocker_regressions"] = ["authorization"]
    receipt = evaluate_campaign(book, evidence)
    assert receipt["status"] == "red"
    assert receipt["panels"][0]["blocker_regressions"] == 1


@pytest.mark.parametrize("change", [
    lambda row: row.update(candidate_synthesized=False),
    lambda row: row.update(candidate_fresh_verifier=False),
    lambda row: row.update(source_contributions=[row["source_contributions"][0]]),
    lambda row: row.update(checks_no_weaker=False),
    lambda row: row.update(caller_mutated=True),
    lambda row: row.update(candidate_held_out_detected=False),
    lambda row: row.update(installed_consumer=False),
])
def test_forge_gate_requires_fusion_fresh_verification_defect_detection_and_isolation(change):
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    change(evidence["forge"][0])
    receipt = evaluate_campaign(book, evidence)
    assert receipt["status"] == "red"
    assert receipt["forge"]["status"] == "red"


@pytest.mark.parametrize("field", [
    "verified_candidate_sha256", "caller_after_sha256",
])
def test_forge_evidence_must_bind_delivered_bytes_verifier_and_unchanged_caller(field):
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    evidence["forge"][0][field] = _hash("unrelated bytes")
    receipt = evaluate_campaign(book, evidence)
    assert receipt["forge"]["status"] == "red"


def test_forge_verifier_receipt_identity_is_bound_to_campaign_receipt():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    receipt = evaluate_campaign(book, evidence)
    evidence["forge"][0]["candidate_verifier_receipt_sha256"] = _hash("other receipt")
    assert not verify_receipt(receipt, book, evidence)


def test_forge_held_out_defect_evidence_names_registered_case():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    evidence["forge"][0]["held_out_case_sha256"] = _hash("another case")
    receipt = evaluate_campaign(book, evidence)
    assert receipt["forge"]["status"] == "red"


def test_forge_missing_delivery_with_no_candidate_artifact_scores_red():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    row = evidence["forge"][0]
    row["candidate_delivered"] = False
    row["candidate_fresh_verifier"] = False
    row["candidate_final_artifact_sha256"] = None
    row["verified_candidate_sha256"] = None
    row["candidate_verifier_receipt_sha256"] = None
    receipt = evaluate_campaign(book, evidence)
    assert receipt["forge"]["status"] == "red"
    assert receipt["forge"]["failures"] == 1


def test_prose_only_forge_cases_cannot_certify_without_fixture_pin():
    provisional_book = load_casebook(CASEBOOK)
    fixture_book = copy.deepcopy(provisional_book)
    evidence = _valid_evidence(fixture_book)
    provisional_book["forge_fixture_manifest_sha256"] = None

    receipt = evaluate_campaign(provisional_book, evidence)

    assert receipt["status"] == "red"
    assert receipt["forge"]["fixture_binding_status"] == "unavailable"


@pytest.mark.parametrize("field,replacement", [
    ("fixture_manifest_sha256", _hash("different fixture")),
    ("source_tree_sha256", _hash("different source")),
    ("owned_paths", ["other/source.py"]),
    ("declared_checks", [{"id": "different", "spec_sha256": _hash("other check")}]),
])
def test_forge_fixture_source_paths_and_checks_must_match_prerun_pin(field, replacement):
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    evidence["forge"][0][field] = replacement

    receipt = evaluate_campaign(book, evidence)

    assert receipt["status"] == "red"
    assert receipt["forge"]["fixture_binding_status"] == "mismatch"


def test_forge_materialization_needs_an_execution_evidence_digest():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    evidence["forge"][0]["fixture_execution_sha256"] = None

    receipt = evaluate_campaign(book, evidence)

    assert receipt["forge"]["status"] == "red"
    assert receipt["forge"]["failures"] == 1


def test_operation_and_transport_gates_reject_replay_leak_and_missing_samples():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    evidence["transfers"][0]["blind_leak"] = True
    evidence["operations"][0]["settled_replays"] = 1
    evidence["operations"].pop()
    receipt = evaluate_campaign(book, evidence)
    assert receipt["status"] == "red"
    assert receipt["transfers"]["status"] == "red"
    assert receipt["operations"]["status"] == "red"


@pytest.mark.parametrize("standard,retries", [(False, 0), (True, 1)])
def test_operational_turn_claim_needs_thirty_no_retry_standard_samples(
    standard, retries,
):
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    for row in evidence["operations"]:
        row["standard_two_round"] = standard
        row["retries"] = retries
        row["turns"] = 0

    receipt = evaluate_campaign(book, evidence)

    assert receipt["status"] == "red"
    assert receipt["operations"]["standard_no_retry_samples"] == 0


def test_receipt_rejects_changed_casebook_or_outcome_without_raw_answers():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    receipt = evaluate_campaign(book, evidence)
    evidence["operations"][0]["candidate_ms"] += 1
    assert not verify_receipt(receipt, book, evidence)
    assert "cases" not in receipt


def test_receipt_verification_rejects_type_coercion_in_decision_counts():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    receipt = evaluate_campaign(book, evidence)
    forged = copy.deepcopy(receipt)
    forged["forge"]["failures"] = False  # Python equality otherwise treats False as zero.
    assert not verify_receipt(forged, book, evidence)


class _ToolFreeTransport:
    target_id = "maka"
    no_tools_characterization_sha256 = _hash("measured tool boundary")

    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.requests: list[evaluation.PresentedJudgeRequest] = []

    def judge(self, request: evaluation.PresentedJudgeRequest) -> dict[str, object]:
        self.requests.append(request)
        return self.result


def test_maka_judge_accepts_arbitrary_counterbalanced_answers_without_tools():
    transport = _ToolFreeTransport({
        "winner": "A", "auth_route": "api-key-relay",
        "requested_model": "gpt-6.1-sol", "observed_model": "gpt-6.1-sol",
        "effort": "xhigh", "tool_calls": 0,
        "tool_boundary_sha256": _ToolFreeTransport.no_tools_characterization_sha256,
    })
    adapter = MakaJudgeAdapter(transport)
    request = JudgeRequest(
        case_id="unseen-arbitrary-case", panel_id="interactive",
        criterion="Which answer catches the NULL trap?",
        candidate_answer="Use NOT EXISTS", baseline_answer="Use NOT IN",
        requested_model="gpt-6.1-sol", effort="xhigh",
        auth_route="api-key-relay",
    )

    ballot = adapter.judge(request)

    assert ballot["winner"] == "A"
    assert ballot["observed_model"] == "gpt-6.1-sol"
    assert len(transport.requests) == 1
    assert transport.requests[0].tools == ()
    assert transport.requests[0].target_id == "maka"


def test_maka_judge_request_pins_current_interactive_auth_route():
    request = JudgeRequest(
        case_id="case", panel_id="interactive", criterion="Which answer is safer?",
        candidate_answer="Candidate", baseline_answer="Baseline",
        requested_model="gpt-6.1-sol", effort="xhigh", auth_route="api-key-relay",
    )
    assert request.auth_route == "api-key-relay"
    with pytest.raises(JudgeTransportError, match="model/auth route"):
        JudgeRequest(
            case_id="case", panel_id="interactive", criterion="Which answer is safer?",
            candidate_answer="Candidate", baseline_answer="Baseline",
            requested_model="gpt-6.1-sol", effort="xhigh", auth_route="maka-subscription",
        )


def test_maka_adapter_derives_the_presented_order_from_named_answers():
    transport = _ToolFreeTransport({
        "winner": "A", "auth_route": "api-key-relay",
        "requested_model": "gpt-6.1-sol", "observed_model": "gpt-6.1-sol",
        "effort": "xhigh", "tool_calls": 0,
        "tool_boundary_sha256": _ToolFreeTransport.no_tools_characterization_sha256,
    })
    adapter = MakaJudgeAdapter(transport)
    base = dict(
        case_id="case", panel_id="interactive", criterion="Which answer is safer?",
        candidate_answer="Final candidate", baseline_answer="Final baseline",
        requested_model="gpt-6.1-sol", effort="xhigh", auth_route="api-key-relay",
    )
    first = adapter.judge(JudgeRequest(**base, order="candidate-first"))
    second = adapter.judge(JudgeRequest(**base, order="baseline-first"))

    assert [(item.answer_a, item.answer_b) for item in transport.requests] == [
        ("Final candidate", "Final baseline"),
        ("Final baseline", "Final candidate"),
    ]
    assert first["presented_a_sha256"] == _hash("Final candidate")
    assert first["presented_b_sha256"] == _hash("Final baseline")
    assert second["presented_a_sha256"] == _hash("Final baseline")
    assert second["presented_b_sha256"] == _hash("Final candidate")
    assert not hasattr(transport.requests[0], "order")


def test_maka_adapter_ballot_can_be_scored_for_its_bound_case_and_panel():
    book = load_casebook(CASEBOOK)
    evidence = _valid_evidence(book)
    row = evidence["panels"][0]["cases"][0]
    boundary = _hash("interactiveboundary")
    transport = _ToolFreeTransport({
        "winner": "A", "auth_route": "api-key-relay",
        "requested_model": "gpt-6.1-sol", "observed_model": "gpt-6.1-sol",
        "effort": "xhigh", "tool_calls": 0,
        "tool_boundary_sha256": boundary,
    })
    transport.no_tools_characterization_sha256 = boundary
    ballot = MakaJudgeAdapter(transport).judge(JudgeRequest(
        case_id=row["case_id"], panel_id="interactive",
        criterion="Compare these final answers.",
        candidate_answer=row["case_id"] + "candidate",
        baseline_answer=row["case_id"] + "baseline",
        requested_model="gpt-6.1-sol", effort="xhigh",
        auth_route="api-key-relay", order="candidate-first",
    ))
    row["ballots"] = [
        ballot if item["judge"] == "maka" and item["order"] == "candidate-first"
        else item for item in row["ballots"]
    ]

    assert evaluate_campaign(book, evidence)["status"] == "green"


@pytest.mark.parametrize("bad_result", [
    {"winner": "A", "auth_route": "api-key-relay", "requested_model": "gpt-6-sol",
     "observed_model": "gpt-6-sol", "effort": "xhigh", "tool_calls": 0},
    {"winner": "A", "auth_route": "api-key-relay", "requested_model": "gpt-6.1-sol",
     "observed_model": None, "effort": "xhigh", "tool_calls": 0},
    {"winner": "A", "auth_route": "api-key-relay", "requested_model": "gpt-6.1-sol",
     "observed_model": "gpt-6.1-sol", "effort": "xhigh", "tool_calls": 1},
    {"winner": "A", "auth_route": "wrong-route", "requested_model": "gpt-6.1-sol",
     "observed_model": "gpt-6.1-sol", "effort": "xhigh", "tool_calls": 0},
])
def test_maka_judge_fails_closed_on_unverified_model_tools_or_auth(bad_result):
    bad_result["tool_boundary_sha256"] = _ToolFreeTransport.no_tools_characterization_sha256
    adapter = MakaJudgeAdapter(_ToolFreeTransport(bad_result))
    request = JudgeRequest("case", "interactive", "criterion", "answer A", "answer B",
                           "gpt-6.1-sol", "xhigh", "api-key-relay")
    with pytest.raises(JudgeTransportError):
        adapter.judge(request)


def test_maka_judge_refuses_executor_transport_or_uncharacterized_tool_boundary():
    transport = _ToolFreeTransport({})
    transport.target_id = "codex"
    with pytest.raises(JudgeTransportError, match="Maka"):
        MakaJudgeAdapter(transport)
    transport.target_id = "maka"
    transport.no_tools_characterization_sha256 = ""
    with pytest.raises(JudgeTransportError, match="no-tools"):
        MakaJudgeAdapter(transport)


def test_maka_judge_pins_the_characterized_boundary_across_calls():
    transport = _ToolFreeTransport({
        "winner": "tie", "auth_route": "api-key-relay",
        "requested_model": "gpt-6.1-sol", "observed_model": "gpt-6.1-sol",
        "effort": "xhigh", "tool_calls": 0,
        "tool_boundary_sha256": _hash("drifted boundary"),
    })
    adapter = MakaJudgeAdapter(transport)
    transport.no_tools_characterization_sha256 = _hash("drifted boundary")
    request = JudgeRequest("case", "interactive", "criterion", "answer A", "answer B",
                           "gpt-6.1-sol", "xhigh", "api-key-relay")
    with pytest.raises(JudgeTransportError, match="boundary"):
        adapter.judge(request)


@pytest.mark.parametrize("mutation", [None, "observed_model", "auth_route", "effort",
                                     "tool_calls", "tool_boundary_sha256",
                                     "presented_a_sha256", "case_id", "order", "extra"])
def test_stored_maka_ballot_reuses_live_rules_without_sending(mutation):
    request = JudgeRequest("stored-case", "interactive", "criterion", "answer A", "answer B",
                           "gpt-6.1-sol", "xhigh", "api-key-relay")
    transport = _ToolFreeTransport({
        "winner": "A", "auth_route": "api-key-relay",
        "requested_model": "gpt-6.1-sol", "observed_model": "gpt-6.1-sol",
        "effort": "xhigh", "tool_calls": 0,
        "tool_boundary_sha256": _ToolFreeTransport.no_tools_characterization_sha256,
    })
    ballot = MakaJudgeAdapter(transport).judge(request)
    if mutation:
        ballot[mutation] = 1 if mutation == "tool_calls" else "changed"
    validate = getattr(evaluation, "validate_maka_ballot", None)
    assert callable(validate), "stored ballots need the existing live rulebook"
    if mutation:
        with pytest.raises(JudgeTransportError):
            validate(request, ballot, boundary_sha256=transport.no_tools_characterization_sha256)
    else:
        assert validate(request, ballot, boundary_sha256=transport.no_tools_characterization_sha256) == ballot
    assert len(transport.requests) == 1
