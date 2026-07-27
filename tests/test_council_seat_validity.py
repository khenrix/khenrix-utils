"""Seat-validity tests for the llm-council engine.

Regression cover for the defect that produced these tests: during the review
rounds of the bootstrap-hardening effort the agy seat soft-denied its own
ReadFile (headless mode cannot prompt for tool permission), returned a single
sentence without ever opening the document under review, and the engine scored
it `ok` because the output was non-empty. A 3-seat verdict silently became a
2-seat verdict and the synthesis presented it as three.

Two claims are under test:
  1. non-empty != valid — a seat is `ok` only if it produced substantive output
     AND demonstrably read its input (it must quote the per-run sentinel);
  2. the seat count is never silently degraded — the header the synthesizer is
     required to emit states responded/attempted and names every failed seat.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "skills" / "llm-council" / "scripts"))

from fanout import (  # noqa: E402
    MIN_SUBSTANTIVE_CHARS,
    apply_sentinel,
    council_header,
    make_sentinel,
    score_seat,
)

TOKEN = "SENTINEL-1234abcd"


def _substantive(token: str = TOKEN) -> str:
    """A long answer that quotes the sentinel — the only shape that scores ok."""
    return ("The tradeoff here is real. " * 40) + f"\n\n{token}\n\n" + ("Details follow. " * 40)


# --------------------------------------------------------------------------- #
# score_seat — what makes a seat valid.
# --------------------------------------------------------------------------- #
def test_empty_output_is_not_ok():
    r = score_seat("", TOKEN)
    assert r["status"] == "failed"
    assert r["cause"] == "empty"


def test_whitespace_only_output_is_empty_not_ok():
    r = score_seat("   \n\t  ", TOKEN)
    assert r["status"] == "failed"
    assert r["cause"] == "empty"


def test_one_sentence_response_is_not_ok():
    r = score_seat("I was unable to read the document.", TOKEN)
    assert r["status"] != "ok", "a one-sentence non-answer must not score ok"
    assert r["cause"] == "non_substantive"
    assert str(MIN_SUBSTANTIVE_CHARS) in r.get("detail", "")


def test_response_that_never_read_the_input_is_not_ok():
    r = score_seat("x" * 5000, TOKEN)
    assert r["status"] != "ok", "long output that never cites the sentinel must not score ok"
    assert r["cause"] == "did_not_read_input"


def test_substantive_response_citing_sentinel_is_ok():
    r = score_seat(_substantive(), TOKEN)
    assert r["status"] == "ok", r
    assert r["cause"] == "ok"


def test_sentinel_match_tolerates_markdown_and_case():
    """Models wrap tokens in backticks and reflow case; the proof-of-read must survive."""
    body = ("A careful answer. " * 40) + "\n\nToken: `" + TOKEN.lower() + "`\n" + ("More. " * 40)
    assert score_seat(body, TOKEN)["status"] == "ok"


def test_no_sentinel_configured_falls_back_to_length_only():
    """Callers that cannot inject a sentinel (eval harness, smoke) still get the floor."""
    assert score_seat("x" * 5000, None)["status"] == "ok"
    assert score_seat("too short", None)["cause"] == "non_substantive"


# --------------------------------------------------------------------------- #
# Failure causes — distinguished, not collapsed into one bucket.
# --------------------------------------------------------------------------- #
def test_failure_cause_is_distinguished():
    cases = [
        ("", "empty"),
        ("Error: quota exceeded", "auth_or_quota"),
        ("tool_confirmation_manager.go:183 denied", "tool_permission"),
        ("I could not answer.", "non_substantive"),
        ("x" * 5000, "did_not_read_input"),
    ]
    for out, expected in cases:
        r = score_seat(out, TOKEN)
        assert r["status"] != "ok", f"{expected}: must not score ok"
        assert r["cause"] == expected, f"expected {expected}, got {r['cause']} for {out[:40]!r}"


def test_tool_permission_is_distinct_from_auth_or_quota():
    """A soft-denied tool call is a fixable invocation defect, not an outage."""
    r = score_seat("ReadFile: permission denied (no approval available in headless mode)", TOKEN)
    assert r["cause"] == "tool_permission"
    assert r["cause"] != "auth_or_quota"
    assert r.get("hint"), "tool_permission must carry an actionable hint"
    assert "headless" in r["hint"].lower()


def test_tool_permission_and_auth_are_not_retried_but_transient_is():
    from fanout import NONRETRYABLE_REASONS

    assert "tool_permission" in NONRETRYABLE_REASONS
    assert "auth_or_quota" in NONRETRYABLE_REASONS
    assert "not_installed" in NONRETRYABLE_REASONS
    # A momentary outage is legitimate and recurs — retry is the whole point.
    assert "error_sentinel" not in NONRETRYABLE_REASONS
    # A seat that rambled without reading may do better on a second roll of the dice.
    assert "did_not_read_input" not in NONRETRYABLE_REASONS
    assert "non_substantive" not in NONRETRYABLE_REASONS


def test_a_real_answer_discussing_quota_is_not_vetoed():
    """Sentinels refine an already-failing attempt; they never veto a real answer.

    Guards the hard-won S12 invariant: an answer that legitimately discusses rate
    limits, quotas or permission denials must survive the keyword scan.
    """
    body = ("Your 429 handling should treat quota exceeded and permission denied "
            "as distinct: unauthorized means auth, not a rate limit. " * 12)
    r = score_seat(body + f"\n{TOKEN}\n", TOKEN)
    assert r["status"] == "ok", r


# --------------------------------------------------------------------------- #
# Sentinel plumbing.
# --------------------------------------------------------------------------- #
def test_make_sentinel_is_unique_per_run_and_well_formed():
    a, b = make_sentinel(), make_sentinel()
    assert a != b, "a per-run sentinel must not repeat across runs"
    assert a.startswith("SENTINEL-") and len(a) > len("SENTINEL-")


def test_apply_sentinel_instructs_the_seat_and_preserves_the_prompt():
    out = apply_sentinel("original question", TOKEN)
    assert TOKEN in out
    assert out.endswith("original question"), "the user's prompt must survive verbatim"
    assert "verbatim" in out.lower(), "the seat must be told to quote it verbatim"


# --------------------------------------------------------------------------- #
# The seat count is never silently degraded.
# --------------------------------------------------------------------------- #
def _manifest(providers):
    valid = sum(1 for p in providers if p["valid"])
    return {"summary": {"requested": len(providers), "valid": valid,
                        "failed": len(providers) - valid,
                        "degraded": valid < len(providers)},
            "providers": providers}


def test_header_reports_all_seats_when_none_failed():
    h = council_header(_manifest([
        {"name": "claude", "valid": True, "reason": "ok"},
        {"name": "codex", "valid": True, "reason": "ok"},
        {"name": "agy", "valid": True, "reason": "ok"},
    ]))
    assert "3 of 3" in h
    assert "failed" not in h.lower()


def test_header_states_true_seat_count_and_names_failed_seats():
    h = council_header(_manifest([
        {"name": "claude", "valid": True, "reason": "ok"},
        {"name": "codex", "valid": True, "reason": "ok"},
        {"name": "agy", "valid": False, "reason": "tool_permission",
         "hint": "headless mode cannot prompt"},
    ]))
    assert "2 of 3" in h, f"a 2-seat verdict must not read as 3 seats: {h!r}"
    assert "3 of 3" not in h
    assert "agy" in h
    assert "tool_permission" in h
    assert "DEGRADED" in h.upper()


def test_header_distinguishes_two_seats_failing_for_different_reasons():
    """The exact shape of the incident: agy failed twice, for two different causes."""
    h = council_header(_manifest([
        {"name": "claude", "valid": True, "reason": "ok"},
        {"name": "codex", "valid": False, "reason": "auth_or_quota"},
        {"name": "agy", "valid": False, "reason": "did_not_read_input"},
    ]))
    assert "1 of 3" in h
    assert "codex" in h and "auth_or_quota" in h
    assert "agy" in h and "did_not_read_input" in h
