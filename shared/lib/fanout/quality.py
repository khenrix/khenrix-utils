"""Deterministic scoring for preregistered fanout replacement campaigns.

The evaluator consumes evidence about final delivered artifacts. It never runs a
provider, reads a prompt from a receipt, or treats repeated ballots as cases.
The live campaign driver must put each returned case_prompt in an isolated
executor workspace that cannot read this casebook's hidden assertions. This
module exposes the prompt alone; it cannot enforce the driver's workspace.
Outcome artifact digests must hash the exact UTF-8 final-answer bytes presented
to judges, not seat drafts or serialized transport envelopes.
This public tree contains synthetic fixture-contract tests only. Live quality
certification requires the separate, independently pinned private held-out
casebook and runnable Forge fixtures, plus validated execution evidence.
Synthetic fixtures do not establish replacement quality.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any, Mapping

from .artifacts import canonical_json


class QualityError(ValueError):
    """A campaign input is malformed or cannot be compared fairly."""


_DIGEST_CHARS = frozenset("0123456789abcdef")
_BLOCKERS = frozenset({"factual", "authorization", "security", "safety"})
_PANELS = {
    "interactive": ("api-key-relay", "gpt-6-sol", "xhigh"),
    "harbor": ("harbor-lab", "gpt-5.6-sol", "xhigh"),
}
_JUDGE_FAMILIES = {"claude": "claude", "codex": "openai", "maka": "openai"}
_JUDGE_MODELS = {"claude": "claude-opus-5-5", "codex": "gpt-6-sol"}
_ORDERS = frozenset({"candidate-first", "baseline-first"})
_WINNERS = frozenset({"A", "B", "tie", "abstain"})


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _DIGEST_CHARS


def _fields(value: object, required: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        raise QualityError(f"{label} must have exactly {sorted(required)}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise QualityError(f"{label} must be non-empty text")
    return value


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise QualityError(f"{label} must be boolean")
    return value


def _number(value: object, label: str, *, minimum: float = 0,
            maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QualityError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum or (maximum is not None and result > maximum):
        raise QualityError(f"{label} is out of range")
    return result


def _hex128(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 32 or set(value) - _DIGEST_CHARS:
        raise QualityError(f"{label} must be 128-bit lowercase hex")
    return value


def _validate_casebook(book: object) -> dict[str, Any]:
    value = _fields(book, {
        "schema_version", "cases", "cases_sha256", "forge_fixture_manifest_sha256",
    }, "casebook")
    if value["schema_version"] != 1 or not isinstance(value["cases"], list):
        raise QualityError("casebook schema is unsupported")
    ids: set[str] = set()
    prompts: set[str] = set()
    cases: list[dict[str, Any]] = []
    counts: dict[tuple[str, str], int] = {}
    for index, raw in enumerate(value["cases"]):
        case = _fields(raw, {"id", "kind", "mode", "prompt", "assertions"},
                       f"case {index}")
        case_id = _text(case["id"], "case ID")
        kind = case["kind"]
        mode = case["mode"]
        if (kind == "council" and mode not in {"normal", "deep"}) or (
            kind == "forge" and mode not in {"review", "deep-review", "ingress", "fusion"}
        ) or kind not in {"council", "forge"}:
            raise QualityError(f"case {case_id} has invalid kind/mode")
        prompt = _text(case["prompt"], f"case {case_id} prompt")
        assertions = case["assertions"]
        if (not isinstance(assertions, list) or not assertions
                or any(not isinstance(item, str) or not item.strip() for item in assertions)):
            raise QualityError(f"case {case_id} needs non-empty assertions")
        if case_id in ids:
            raise QualityError(f"duplicate case ID {case_id}")
        normalized_prompt = " ".join(prompt.casefold().split())
        if normalized_prompt in prompts:
            raise QualityError(f"duplicate case prompt {case_id}")
        ids.add(case_id)
        prompts.add(normalized_prompt)
        counts[(kind, mode)] = counts.get((kind, mode), 0) + 1
        cases.append(dict(case))
    if sum(count for (kind, _), count in counts.items() if kind == "council") < 60:
        raise QualityError("casebook needs at least 60 distinct Council cases")
    if not counts.get(("council", "normal")) or not counts.get(("council", "deep")):
        raise QualityError("casebook needs Council normal and deep strata")
    if sum(count for (kind, _), count in counts.items() if kind == "forge") < 12:
        raise QualityError("casebook needs at least 12 distinct Forge cases")
    if any(not counts.get(("forge", mode)) for mode in (
        "review", "deep-review", "ingress", "fusion",
    )):
        raise QualityError("casebook needs Forge review, deep-review, ingress, and fusion cases")
    if not _is_digest(value["cases_sha256"]) or value["cases_sha256"] != _digest(cases):
        raise QualityError("casebook seal does not match preregistered cases")
    fixture_lock = value["forge_fixture_manifest_sha256"]
    if fixture_lock is not None and not _is_digest(fixture_lock):
        raise QualityError("Forge fixture manifest pin is invalid")
    return {
        "schema_version": 1, "cases_sha256": value["cases_sha256"],
        "forge_fixture_manifest_sha256": fixture_lock, "cases": cases,
    }


def load_casebook(path: Path | str) -> dict[str, Any]:
    """Load a preregistered manifest; its canonical digest seals all prompts and oracles."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise QualityError(f"cannot load quality casebook: {error}") from error
    return _validate_casebook(data)


def case_prompt(casebook: Mapping[str, Any], case_id: str) -> str:
    """Return only one prompt; the caller must isolate executors from the casebook."""
    book = _validate_casebook(dict(casebook))
    for case in book["cases"]:
        if case["id"] == case_id:
            return case["prompt"]
    raise QualityError(f"unregistered quality case {case_id!r}")


def exact_upper_loss_bound(losses: int, cases: int, confidence: float = 0.95) -> float:
    """Clopper-Pearson one-sided upper bound, inverting the exact binomial CDF."""
    if (isinstance(losses, bool) or isinstance(cases, bool) or not isinstance(losses, int)
            or not isinstance(cases, int) or cases < 1 or losses < 0 or losses > cases
            or not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
            or not 0 < confidence < 1):
        raise QualityError("invalid exact-binomial inputs")
    if losses == cases:
        return 1.0
    alpha = 1 - confidence
    if losses == 0:
        return -math.expm1(math.log(alpha) / cases)

    def cdf(probability: float) -> float:
        if probability >= 1:
            return 0.0
        log_p = math.log(probability)
        log_q = math.log1p(-probability)
        terms = [
            math.lgamma(cases + 1) - math.lgamma(index + 1)
            - math.lgamma(cases - index + 1)
            + index * log_p + (cases - index) * log_q
            for index in range(losses + 1)
        ]
        largest = max(terms)
        return math.exp(largest) * math.fsum(math.exp(term - largest) for term in terms)

    low, high = losses / cases, 1.0
    for _ in range(80):
        middle = (low + high) / 2
        if cdf(middle) > alpha:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def wilson_lower_bound(wins: int, decisive: int, confidence: float = 0.95) -> float:
    """Two-sided Wilson 95% lower bound for candidate wins among decisive cases."""
    if (isinstance(wins, bool) or isinstance(decisive, bool) or not isinstance(wins, int)
            or not isinstance(decisive, int) or decisive < 1 or wins < 0 or wins > decisive
            or confidence != 0.95):
        raise QualityError("invalid Wilson inputs")
    z = 1.959963984540054
    p = wins / decisive
    z2 = z * z
    return (p + z2 / (2 * decisive) - z * math.sqrt(
        p * (1 - p) / decisive + z2 / (4 * decisive * decisive)
    )) / (1 + z2 / decisive)


def _delivered(value: object, assertion_count: int, label: str,
               *, candidate: bool) -> tuple[bool, int, bool]:
    fields = {"delivered", "assertions", "artifact_sha256", "installed_consumer"}
    if candidate:
        fields.update({
            "synthesis_sources", "verification_receipt_sha256", "verified_final_sha256",
        })
    item = _fields(value, fields, label)
    delivered = _bool(item["delivered"], f"{label} delivered")
    assertions = item["assertions"]
    if not isinstance(assertions, list) or any(not isinstance(bit, bool) for bit in assertions):
        raise QualityError(f"{label} assertions must be boolean")
    if delivered:
        if len(assertions) != assertion_count or not _is_digest(item["artifact_sha256"]):
            raise QualityError(f"{label} delivered artifact/assurances are incomplete")
        if item["installed_consumer"] is not True:
            raise QualityError(f"{label} did not use the installed consumer path")
        valid_delivery = True
        if candidate:
            sources = item["synthesis_sources"]
            if not isinstance(sources, list) or any(not _is_digest(source) for source in sources):
                raise QualityError(f"{label} has invalid synthesis source digests")
            valid_delivery = all((
                len(set(sources)) >= 2,
                item["artifact_sha256"] not in sources,
                _is_digest(item["verification_receipt_sha256"]),
                item["verified_final_sha256"] == item["artifact_sha256"],
            ))
    elif assertions:
        raise QualityError(f"{label} cannot score assertions on a failed delivery")
    else:
        valid_delivery = False
    return valid_delivery and all(assertions), sum(assertions) if valid_delivery else 0, valid_delivery


def _judge_verdict(
    ballots: object, panel: Mapping[str, Any], case_id: str,
    candidate_digest: object, baseline_digest: object,
) -> tuple[str | None, int]:
    if not isinstance(ballots, list):
        raise QualityError("ballots must be a list")
    by_judge: dict[str, dict[str, str | None]] = {
        judge: {} for judge in _JUDGE_FAMILIES
    }
    provenance_failures = 0
    for raw in ballots:
        ballot = _fields(raw, {
            "case_id", "panel_id",
            "judge", "family", "order", "winner", "auth_route", "requested_model",
            "observed_model", "effort", "tool_boundary_sha256", "tool_calls",
            "presented_a_sha256", "presented_b_sha256",
        }, "ballot")
        judge = ballot["judge"]
        order = ballot["order"]
        if judge not in _JUDGE_FAMILIES or order not in _ORDERS or ballot["winner"] not in _WINNERS:
            raise QualityError("ballot has unknown judge, order, or winner")
        if ballot["family"] != _JUDGE_FAMILIES[judge]:
            raise QualityError("ballot model family is inconsistent with judge")
        if order in by_judge[judge]:
            raise QualityError(f"duplicate ballot for {judge} {order}")
        if not isinstance(ballot["tool_calls"], int) or isinstance(ballot["tool_calls"], bool):
            raise QualityError("ballot tool_calls must be an integer")
        expected_a, expected_b = (
            (candidate_digest, baseline_digest) if order == "candidate-first"
            else (baseline_digest, candidate_digest)
        )
        if (ballot["tool_calls"] != 0 or not _is_digest(ballot["tool_boundary_sha256"])
                or ballot["case_id"] != case_id
                or ballot["panel_id"] != panel["panel_id"]
                or not _is_digest(ballot["presented_a_sha256"])
                or not _is_digest(ballot["presented_b_sha256"])
                or ballot["presented_a_sha256"] != expected_a
                or ballot["presented_b_sha256"] != expected_b
                or not isinstance(ballot["observed_model"], str)
                or not ballot["observed_model"].strip()
                or not isinstance(ballot["requested_model"], str)
                or not ballot["requested_model"].strip()
                or ballot["observed_model"] != ballot["requested_model"]
                or not isinstance(ballot["effort"], str) or not ballot["effort"].strip()
                or not isinstance(ballot["auth_route"], str) or not ballot["auth_route"].strip()):
            provenance_failures += 1
            by_judge[judge][order] = None
            continue
        if judge == "maka" and (
            ballot["requested_model"] != panel["requested_model"]
            or ballot["observed_model"] != panel["observed_model"]
            or ballot["auth_route"] != panel["auth_route"]
            or ballot["effort"] != panel["effort"]
            or ballot["tool_boundary_sha256"] != panel["no_tools_characterization_sha256"]
        ):
            provenance_failures += 1
            by_judge[judge][order] = None
            continue
        if judge != "maka" and (
            ballot["requested_model"] != _JUDGE_MODELS[judge]
            or not ballot["auth_route"].startswith(f"{judge}-")
        ):
            provenance_failures += 1
            by_judge[judge][order] = None
            continue
        winner = ballot["winner"]
        by_judge[judge][order] = (
            winner if winner in {"tie", "abstain"} else
            "candidate" if ((winner == "A") == (order == "candidate-first")) else "baseline"
        )

    positions: dict[str, str | None] = {}
    for judge, orders in by_judge.items():
        first = orders.get("candidate-first")
        second = orders.get("baseline-first")
        positions[judge] = first if first == second and first not in {None, "abstain"} else None
    # Two OpenAI routes are one family, never two independent votes.
    openai = positions["codex"] if positions["codex"] == positions["maka"] else None
    claude = positions["claude"]
    return (claude if claude == openai else None), provenance_failures


def _score_panel(cases: list[dict[str, Any]], raw: object) -> dict[str, Any]:
    panel = _fields(raw, {
        "panel_id", "auth_route", "requested_model", "observed_model", "effort",
        "no_tools_characterization_sha256", "cases",
    }, "Maka panel")
    panel_id = panel["panel_id"]
    if panel_id not in _PANELS:
        raise QualityError("unknown Maka panel")
    profile_ok = (
        panel["auth_route"] == _PANELS[panel_id][0]
        and panel["requested_model"] == _PANELS[panel_id][1]
        and panel["observed_model"] == panel["requested_model"]
        and panel["effort"] == _PANELS[panel_id][2]
        and _is_digest(panel["no_tools_characterization_sha256"])
    )
    rows = panel["cases"]
    if not isinstance(rows, list):
        raise QualityError("panel cases must be a list")
    by_id: dict[str, Mapping[str, Any]] = {}
    expected = {case["id"]: case for case in cases}
    for raw_row in rows:
        row = _fields(raw_row, {
            "case_id", "baseline", "candidate", "blocker_regressions", "ballots",
        }, "Council outcome")
        case_id = row["case_id"]
        if case_id not in expected or case_id in by_id:
            raise QualityError(f"duplicate or unregistered Council case {case_id}")
        by_id[case_id] = row

    baseline_assertions = candidate_assertions = paired_losses = 0
    candidate_failed_deliveries = baseline_failed_deliveries = 0
    missing_outcomes = blocker_regressions = provenance_failures = paired_cases = 0
    candidate_wins = baseline_wins = ties = abstentions = 0
    judged_decisive = judged_candidate_wins = 0
    strata: dict[str, dict[str, int]] = {
        mode: {"distinct_cases": 0, "paired_losses": 0, "candidate_wins": 0,
               "baseline_wins": 0, "ties": 0, "abstentions": 0}
        for mode in ("normal", "deep")
    }
    for case_id, case in expected.items():
        row = by_id.get(case_id)
        if row is None:
            candidate_failed_deliveries += 1
            baseline_failed_deliveries += 1
            missing_outcomes += 1
            baseline_wins += 1
            strata[case["mode"]]["baseline_wins"] += 1
            continue
        strata[case["mode"]]["distinct_cases"] += 1
        baseline_ok, baseline_count, baseline_delivery = _delivered(
            row["baseline"], len(case["assertions"]), f"{case_id} baseline", candidate=False
        )
        candidate_ok, candidate_count, candidate_delivery = _delivered(
            row["candidate"], len(case["assertions"]), f"{case_id} candidate", candidate=True
        )
        baseline_assertions += baseline_count
        candidate_assertions += candidate_count
        paired_cases += int(baseline_delivery)
        baseline_failed_deliveries += int(not baseline_delivery)
        paired_loss = int(baseline_ok and not candidate_ok)
        paired_losses += paired_loss
        strata[case["mode"]]["paired_losses"] += paired_loss
        if not candidate_delivery:
            candidate_failed_deliveries += 1
        blockers = row["blocker_regressions"]
        if (not isinstance(blockers, list) or len(set(blockers)) != len(blockers)
                or any(value not in _BLOCKERS for value in blockers)):
            raise QualityError(f"{case_id} has invalid blocker regressions")
        blocker_regressions += len(blockers)
        verdict, failures = _judge_verdict(
            row["ballots"], panel, case_id,
            row["candidate"]["artifact_sha256"], row["baseline"]["artifact_sha256"],
        )
        provenance_failures += failures
        if candidate_delivery and baseline_delivery and verdict in {"candidate", "baseline"}:
            judged_decisive += 1
            judged_candidate_wins += int(verdict == "candidate")
        if not candidate_delivery:
            verdict = "baseline"
        elif not baseline_delivery:
            verdict = None
        if verdict == "candidate":
            candidate_wins += 1
            strata[case["mode"]]["candidate_wins"] += 1
        elif verdict == "baseline":
            baseline_wins += 1
            strata[case["mode"]]["baseline_wins"] += 1
        elif verdict == "tie":
            ties += 1
            strata[case["mode"]]["ties"] += 1
        else:
            abstentions += 1
            strata[case["mode"]]["abstentions"] += 1

    n = len(cases)
    decisive = judged_decisive
    lower = wilson_lower_bound(judged_candidate_wins, decisive) if decisive else 0.0
    loss_upper = exact_upper_loss_bound(paired_losses, paired_cases) if paired_cases else 1.0
    win_or_tie = (candidate_wins + ties) / n
    superior = decisive >= 20 and lower > 0.5
    green = all((
        profile_ok, len(by_id) >= 60, paired_cases >= 60,
        candidate_failed_deliveries == baseline_failed_deliveries == 0,
        candidate_assertions >= baseline_assertions, loss_upper < 0.05,
        win_or_tie >= 0.8, candidate_wins >= baseline_wins,
        blocker_regressions == 0, provenance_failures == 0,
    ))
    return {
        "panel_id": panel_id, "auth_route": panel["auth_route"],
        "requested_model": panel["requested_model"], "observed_model": panel["observed_model"],
        "effort": panel["effort"], "no_tools_characterization_sha256":
            panel["no_tools_characterization_sha256"],
        "status": "green" if green else "red", "distinct_cases": len(by_id),
        "paired_cases": paired_cases,
        "baseline_assertions": baseline_assertions, "candidate_assertions": candidate_assertions,
        "paired_losses": paired_losses, "loss_upper_95": loss_upper,
        "candidate_failed_deliveries": candidate_failed_deliveries,
        "baseline_failed_deliveries": baseline_failed_deliveries,
        "missing_outcomes": missing_outcomes,
        "candidate_wins": candidate_wins, "baseline_wins": baseline_wins,
        "ties": ties, "abstentions": abstentions, "win_or_tie": win_or_tie,
        "decisive_cases": decisive, "wilson_lower_95": lower,
        "superior": superior, "blocker_regressions": blocker_regressions,
        "provenance_failures": provenance_failures, "strata": strata,
    }


def _score_forge(
    cases: list[dict[str, Any]], rows: object, fixture_lock: str | None,
) -> dict[str, Any]:
    if not isinstance(rows, list):
        raise QualityError("Forge results must be a list")
    expected = {case["id"]: case for case in cases}
    seen: set[str] = set()
    fixture_descriptors: list[dict[str, Any]] = []
    failures = baseline_verified = candidate_verified = 0
    baseline_defects = candidate_defects = 0
    for raw in rows:
        row = _fields(raw, {
            "case_id", "baseline_fresh_verifier", "candidate_fresh_verifier",
            "baseline_held_out_detected", "candidate_held_out_detected",
            "candidate_delivered", "candidate_synthesized", "source_contributions",
            "checks_no_weaker", "caller_mutated", "installed_consumer",
            "baseline_verifier_receipt_sha256", "candidate_verifier_receipt_sha256",
            "candidate_final_artifact_sha256", "verified_candidate_sha256",
            "caller_before_sha256", "caller_after_sha256",
            "held_out_case_sha256", "baseline_held_out_evidence_sha256",
            "candidate_held_out_evidence_sha256",
            "fixture_manifest_sha256", "source_tree_sha256", "owned_paths",
            "declared_checks", "fixture_execution_sha256",
        }, "Forge outcome")
        case_id = row["case_id"]
        if case_id not in expected or case_id in seen:
            raise QualityError(f"duplicate or unregistered Forge case {case_id}")
        seen.add(case_id)
        for key in (
            "baseline_fresh_verifier", "candidate_fresh_verifier",
            "baseline_held_out_detected", "candidate_held_out_detected",
            "candidate_delivered", "candidate_synthesized", "checks_no_weaker",
            "caller_mutated", "installed_consumer",
        ):
            _bool(row[key], f"{case_id} {key}")
        contributions = row["source_contributions"]
        if not isinstance(contributions, list) or any(not _is_digest(value) for value in contributions):
            raise QualityError(f"{case_id} has invalid source contributions")
        owned_paths = row["owned_paths"]
        if (not isinstance(owned_paths, list) or not owned_paths
                or any(
                    not isinstance(path, str) or path.startswith("/") or "\\" in path
                    or "\x00" in path or any(part in {"", ".", ".."} for part in path.split("/"))
                    for path in owned_paths
                ) or len(set(owned_paths)) != len(owned_paths)):
            raise QualityError(f"{case_id} has invalid owned fixture paths")
        checks = row["declared_checks"]
        if not isinstance(checks, list) or not checks:
            raise QualityError(f"{case_id} needs declared fixture checks")
        check_ids: set[str] = set()
        for check in checks:
            check = _fields(check, {"id", "spec_sha256"}, f"{case_id} fixture check")
            check_id = _text(check["id"], f"{case_id} fixture check ID")
            if check_id in check_ids or not _is_digest(check["spec_sha256"]):
                raise QualityError(f"{case_id} has duplicate or unbound fixture checks")
            check_ids.add(check_id)
        fixture_descriptors.append({key: row[key] for key in (
            "case_id", "fixture_manifest_sha256", "source_tree_sha256",
            "owned_paths", "declared_checks",
        )})
        complete_evidence = all(_is_digest(row[key]) for key in (
            "baseline_verifier_receipt_sha256", "candidate_verifier_receipt_sha256",
            "candidate_final_artifact_sha256", "verified_candidate_sha256",
            "caller_before_sha256", "caller_after_sha256",
            "held_out_case_sha256", "baseline_held_out_evidence_sha256",
            "candidate_held_out_evidence_sha256",
            "fixture_manifest_sha256", "source_tree_sha256", "fixture_execution_sha256",
        ))
        baseline_verified += int(row["baseline_fresh_verifier"])
        candidate_verified += int(row["candidate_fresh_verifier"])
        baseline_defects += int(row["baseline_held_out_detected"])
        candidate_defects += int(row["candidate_held_out_detected"])
        if not all((
            row["candidate_delivered"], row["candidate_synthesized"],
            row["candidate_fresh_verifier"], len(set(contributions)) >= 2,
            row["checks_no_weaker"], not row["caller_mutated"],
            row["installed_consumer"],
            complete_evidence,
            row["candidate_final_artifact_sha256"] == row["verified_candidate_sha256"],
            row["candidate_final_artifact_sha256"] not in contributions,
            row["caller_before_sha256"] == row["caller_after_sha256"],
            row["held_out_case_sha256"] == _digest(expected[case_id]),
            row["candidate_fresh_verifier"] >= row["baseline_fresh_verifier"],
            row["candidate_held_out_detected"] >= row["baseline_held_out_detected"],
        )):
            failures += 1
    failures += len(expected) - len(seen)
    fixture_manifest_sha256 = _digest(sorted(
        fixture_descriptors, key=lambda item: item["case_id"]
    ))
    fixture_binding_status = (
        "unavailable" if fixture_lock is None else
        "bound" if fixture_lock == fixture_manifest_sha256 else "mismatch"
    )
    return {
        "status": "green" if (
            not failures and len(seen) >= 12 and fixture_binding_status == "bound"
        ) else "red",
        "distinct_cases": len(seen), "failures": failures,
        "fixture_binding_status": fixture_binding_status,
        "fixture_manifest_sha256": fixture_manifest_sha256,
        "baseline_fresh_verifier_successes": baseline_verified,
        "candidate_fresh_verifier_successes": candidate_verified,
        "baseline_held_out_defects": baseline_defects,
        "candidate_held_out_defects": candidate_defects,
    }


def _score_ablation(cases: list[dict[str, Any]], rows: object) -> dict[str, Any]:
    if not isinstance(rows, list):
        raise QualityError("ablation must be a list")
    allowed = {case["id"] for case in cases}
    seen: set[str] = set()
    nonces: set[str] = set()
    orders: set[str] = set()
    deltas: list[float] = []
    for raw in rows:
        row = _fields(raw, {
            "case_id", "assignment_nonce", "order",
            "sharing_enabled_score", "sharing_disabled_score",
        }, "ablation result")
        case_id = row["case_id"]
        nonce = _hex128(row["assignment_nonce"], "ablation assignment nonce")
        if case_id not in allowed or case_id in seen or nonce in nonces:
            raise QualityError("ablation reuses or invents a case/assignment")
        if row["order"] not in {"enabled-first", "disabled-first"}:
            raise QualityError("ablation order must be counterbalanced")
        seen.add(case_id)
        nonces.add(nonce)
        orders.add(row["order"])
        deltas.append(
            _number(row["sharing_enabled_score"], "enabled score", maximum=1)
            - _number(row["sharing_disabled_score"], "disabled score", maximum=1)
        )
    return {
        "status": "green" if len(seen) >= 12 and len(orders) == 2 else "red",
        "distinct_cases": len(seen), "mean_delta": mean(deltas) if deltas else None,
    }


def _score_transfers(cases: list[dict[str, Any]], rows: object) -> dict[str, Any]:
    if not isinstance(rows, list):
        raise QualityError("transfers must be a list")
    allowed = {case["id"] for case in cases}
    seen: set[str] = set()
    facts: set[str] = set()
    successful = leaks = poisoned = 0
    for raw in rows:
        row = _fields(raw, {
            "case_id", "fact_hex", "peer_received", "blind_leak", "poisoned_peer_adoption",
        }, "transfer result")
        case_id = row["case_id"]
        fact = _hex128(row["fact_hex"], "peer fact")
        if case_id not in allowed or case_id in seen or fact in facts:
            raise QualityError("transfer reuses or invents a case/fact")
        seen.add(case_id)
        facts.add(fact)
        successful += int(_bool(row["peer_received"], "peer_received"))
        leaks += int(_bool(row["blind_leak"], "blind_leak"))
        poisoned += int(_bool(row["poisoned_peer_adoption"], "poisoned_peer_adoption"))
    return {
        "status": "green" if len(seen) == successful == 6 and leaks == poisoned == 0 else "red",
        "distinct_cases": len(seen), "peer_transfers": successful,
        "blind_leaks": leaks, "poisoned_peer_adoptions": poisoned,
    }


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _score_operations(rows: object) -> dict[str, Any]:
    if not isinstance(rows, list):
        raise QualityError("operations must be a list")
    seen: set[str] = set()
    baseline: list[float] = []
    candidate: list[float] = []
    bad_turns = settled_replays = standard_no_retry_samples = 0
    for raw in rows:
        row = _fields(raw, {
            "sample_id", "baseline_ms", "candidate_ms", "standard_two_round",
            "turns", "retries", "settled_replays",
        }, "operation sample")
        sample_id = _text(row["sample_id"], "operation sample ID")
        if sample_id in seen:
            raise QualityError("duplicate operation sample")
        seen.add(sample_id)
        baseline.append(_number(row["baseline_ms"], "baseline duration"))
        candidate.append(_number(row["candidate_ms"], "candidate duration"))
        standard = _bool(row["standard_two_round"], "standard_two_round")
        for key in ("turns", "retries", "settled_replays"):
            if isinstance(row[key], bool) or not isinstance(row[key], int) or row[key] < 0:
                raise QualityError(f"operation {key} must be nonnegative integer")
        qualifies = standard and row["retries"] == 0
        standard_no_retry_samples += int(qualifies)
        bad_turns += int(qualifies and row["turns"] != 6)
        settled_replays += row["settled_replays"]
    return {
        "status": "green" if (
            len(seen) >= 30 and standard_no_retry_samples >= 30
            and bad_turns == settled_replays == 0
        ) else "red",
        "samples": len(seen), "baseline_p95_ms": _p95(baseline),
        "candidate_p95_ms": _p95(candidate), "wrong_turn_samples": bad_turns,
        "standard_no_retry_samples": standard_no_retry_samples,
        "settled_replays": settled_replays,
    }


def _same_final_answers(panels: list[Mapping[str, Any]]) -> bool:
    first, second = (
        {row["case_id"]: row for row in panel["cases"]} for panel in panels
    )
    if set(first) != set(second):
        return False
    return all(
        all(
            first[case_id][role][field] == second[case_id][role][field]
            for role in ("baseline", "candidate")
            for field in ("delivered", "artifact_sha256", "assertions")
        )
        for case_id in first
    )


def evaluate_campaign(casebook: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Score a sealed campaign; the receipt contains hashes and counts, not case text."""
    book = _validate_casebook(dict(casebook))
    data = _fields(evidence, {
        "schema_version", "panels", "forge", "ablation", "transfers",
        "operations", "cost_usd", "provenance",
    }, "campaign evidence")
    if data["schema_version"] != 1 or not isinstance(data["panels"], list):
        raise QualityError("campaign schema is unsupported")
    provenance = _fields(
        data["provenance"], {"baseline_lock_sha256", "run_inputs_sha256"}, "campaign provenance"
    )
    if any(not _is_digest(value) for value in provenance.values()):
        raise QualityError("campaign provenance requires baseline and run-input digests")
    council = [case for case in book["cases"] if case["kind"] == "council"]
    forge = [case for case in book["cases"] if case["kind"] == "forge"]
    panels = [_score_panel(council, panel) for panel in data["panels"]]
    if len(panels) != 2 or {panel["panel_id"] for panel in panels} != set(_PANELS):
        raise QualityError("both distinct Maka panels are mandatory")
    cross_panel_consistent = _same_final_answers(data["panels"])
    forge_score = _score_forge(
        forge, data["forge"], book["forge_fixture_manifest_sha256"]
    )
    ablation_score = _score_ablation(council, data["ablation"])
    transfer_score = _score_transfers(council, data["transfers"])
    operation_score = _score_operations(data["operations"])
    cost = data["cost_usd"]
    if cost is not None:
        cost = _number(cost, "cost_usd")
    green = cross_panel_consistent and all(part["status"] == "green" for part in (
        *panels, forge_score, ablation_score, transfer_score, operation_score
    ))
    return {
        "schema_version": 1, "status": "green" if green else "red",
        "manifest_sha256": _digest(book), "evidence_sha256": _digest(data),
        "baseline_lock_sha256": provenance["baseline_lock_sha256"],
        "run_inputs_sha256": provenance["run_inputs_sha256"],
        "panels": panels, "cross_panel_consistent": cross_panel_consistent,
        "forge": forge_score, "ablation": ablation_score,
        "transfers": transfer_score, "operations": operation_score,
        "cost_usd": cost,
    }


def verify_receipt(receipt: object, casebook: Mapping[str, Any],
                   evidence: Mapping[str, Any]) -> bool:
    """Recompute every score and source digest instead of trusting receipt status."""
    try:
        return canonical_json(receipt) == canonical_json(evaluate_campaign(casebook, evidence))
    except (QualityError, TypeError, ValueError, OverflowError):
        return False
