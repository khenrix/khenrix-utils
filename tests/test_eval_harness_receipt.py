"""What a receipt is evidence OF.

`receipt_gate` compared two input hashes, and `_write_receipt` checked a subprocess exit
code. Neither asks whether anything was TESTED — so an all-skipped run, a command that runs
zero tests, and a certifier weakened between runs all left a fresh green receipt.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))

import checks  # noqa: E402
import eval_harness  # noqa: E402


def test_the_deterministic_gate_runs_the_whole_forge_suite_not_three_modules():
    """The receipt names `forge-handover-cli-gc-suites` and the Makefile names 31 suites.

    The omitted set includes `test_forge_packaging.py` — the module that checks rendered
    façade resolution and the quote prose — so breaking the façade left the gate green.
    """
    cmd = eval_harness.DETERMINISTIC_GATED["llm-forge"]
    named = {Path(a).name for a in cmd if a.endswith(".py")}
    mk = (ROOT / "Makefile").read_text()
    on_disk = {p.name for p in (ROOT / "tests").glob("test_forge_*.py")}
    assert named == on_disk, (
        f"the gate runs {len(named)} of {len(on_disk)} forge suites; missing "
        f"{sorted(on_disk - named)}")


def test_a_command_that_runs_no_tests_does_not_earn_a_receipt(tmp_path):
    """An all-skipped pytest run exits 0, and so does `true`. A receipt written on an exit
    code says a process finished, not that anything was checked."""
    counts = eval_harness._pytest_counts("no tests ran in 0.01s\n")
    assert counts["tests_run"] == 0
    assert not eval_harness._counts_are_evidence(counts), \
        "zero executed tests must not be evidence"


def test_an_all_skipped_run_does_not_earn_a_receipt():
    counts = eval_harness._pytest_counts("5 skipped in 0.10s\n")
    assert counts["skipped"] == 5 and counts["tests_run"] == 0
    assert not eval_harness._counts_are_evidence(counts)


def test_a_real_run_with_passes_is_evidence():
    """The guard against over-tightening: if nothing counted, no receipt could ever be
    written and the gate would be closed by making it impossible."""
    counts = eval_harness._pytest_counts("1074 passed, 2 deselected in 34.20s\n")
    assert counts["tests_run"] == 1074 and counts["skipped"] == 0
    assert eval_harness._counts_are_evidence(counts)


def test_a_run_with_any_skip_is_refused():
    """A skip in the CERTIFYING suite is a test that did not run, and the receipt would
    otherwise say the suite passed."""
    counts = eval_harness._pytest_counts("100 passed, 1 skipped in 2.00s\n")
    assert not eval_harness._counts_are_evidence(counts)


def test_the_certifier_and_the_test_manifest_are_in_the_source_closure():
    """Weakening DETERMINISTIC_GATED, or deleting a test it names, must stale the receipt.

    The closure held the skill's own directory and the shared engine. It did not hold the
    thing that decides what "certified" means, so a gate could be narrowed and every existing
    receipt stayed fresh.
    """
    paths = [rel for rel, _h in checks.source_manifest(ROOT, "llm-forge")]
    for rel in ("scripts/eval_harness.py", "scripts/lib/checks.py", "Makefile"):
        assert rel in paths, f"{rel} is not in llm-forge's source closure"
    assert len(paths) == len(set(paths)), \
        f"a file is hashed twice: {sorted({p for p in paths if paths.count(p) > 1})}"


def test_a_receipt_claiming_no_self_test_is_refused(tmp_path, monkeypatch):
    """receipt_gate compared input hashes only, so a receipt with matching hashes and
    `self_test: false` was accepted — "the certification failed" and "the certification
    passed" left the same verdict at the gate."""
    rp = ROOT / "evals" / "llm-forge" / "receipt.json"
    rec = json.loads(rp.read_text())
    rec["self_test"] = False
    bad = tmp_path / "receipt.json"
    bad.write_text(json.dumps(rec))
    assert checks._receipt_is_certified(rec) is False
    rec["self_test"] = True
    assert checks._receipt_is_certified(rec) is True


# ------------------------------------------------------------------ the blind A/B verdict
def test_an_unreadable_comparison_is_not_a_tie():
    """COMPARE_TMPL asks for "winner": "A" or "B" and never offers "tie" — so every tie this
    harness produced was a parse failure, an empty answer, or an off-slot response wearing a
    verdict's clothes. A judge that timed out yields raw="" -> {} -> "tie".

    This is `eval_trigger.parse_verdict`'s already-fixed bug one module over: that function's
    docstring names it exactly — "a judge that timed out, hit a quota wall or answered in
    prose was recorded as having said 'do not activate'".
    """
    key = {"A": "with_skill", "B": "without_skill"}
    for raw in ("", "the judge crashed", '{"winner": null}', '{"winner": "Q"}', "{"):
        c = eval_harness.parse_comparison(raw, key)
        assert c["winner_condition"] is None, \
            f"an unreadable comparison ({raw!r}) resolved to {c['winner_condition']!r}"


def test_a_real_verdict_still_resolves():
    """The guard against over-tightening: if nothing resolved, the blind A/B would report
    nothing and the collapse would be closed by making the signal useless."""
    key = {"A": "with_skill", "B": "without_skill"}
    assert eval_harness.parse_comparison('{"winner": "A"}', key)["winner_condition"] == "with_skill"
    assert eval_harness.parse_comparison('{"winner": "B"}', key)["winner_condition"] == "without_skill"


def test_unreadable_comparisons_are_excluded_from_the_tally_not_counted_as_ties():
    """A dead judge inflated the tie column, which is the column that decides the winner."""
    cs = [{"winner_condition": "with_skill", "winner_slot": "A"},
          {"winner_condition": None, "winner_slot": "?"},
          {"winner_condition": None, "winner_slot": "?"}]
    t = eval_harness._blind_tally(cs)
    assert t["with_skill"] == 1 and t["tie"] == 0
    assert t["unreadable"] == 2, "two silent judges must be reported, not absorbed"
    assert eval_harness.blind_winner(cs) == "with_skill"


def test_a_constant_slot_preference_is_not_a_tie():
    """The live witness: all six comparison.json files in this skill's own artifacts recorded
    winner_slot "A". `blind_pair` alternates which condition sits in slot A by eval-id parity,
    so a judge with a fixed slot preference maps to with, without, with, without... — a clean
    3-3 that is indistinguishable from six genuinely matched pairs. Nothing read winner_slot.

    n=6, one judge, one session: enough to show the collapse is real and unguarded, not
    enough to claim this judge is generally position-biased.
    """
    cs = [{"winner_condition": c, "winner_slot": "A"}
          for c in ("with_skill", "without_skill") * 3]
    assert eval_harness.blind_winner(cs) == "slot_degenerate", \
        "a judge that always answered the same slot is not a tie"


def test_an_absent_slot_is_not_a_repeated_slot():
    """The degeneracy check's own version of "nothing leaves the same record as nobody".

    A comparison built without `winner_slot` — the self-test's own cases, or any caller
    constructing one by hand — records no slot at all. Reading that as "every judgement chose
    the same slot" made a missing field indistinguishable from a position-biased judge, and
    turned three green self-test cases red.
    """
    no_slot = [{"winner_condition": "with_skill"}, {"winner_condition": "with_skill"},
               {"winner_condition": "without_skill"}]
    assert eval_harness.blind_winner(no_slot) == "with_skill"
    assert eval_harness.blind_winner([{"winner_condition": "with_skill", "winner_slot": "?"},
                                      {"winner_condition": "without_skill",
                                       "winner_slot": "?"}]) == "tie"


def test_the_gate_name_is_not_narrower_than_the_gate():
    """A receipt exists to say what ran, so a provenance string naming three suites while
    thirty-one execute is the same defect the receipt is supposed to prevent, one field over.

    Pinned loosely — the name is prose and may be reworded — but it may not name a SUBSET it
    no longer describes.
    """
    name = eval_harness.DETERMINISTIC_GATE_NAMES["llm-forge"]
    cmd = eval_harness.DETERMINISTIC_GATED["llm-forge"]
    suites = [a for a in cmd if a.endswith(".py")]
    for narrow in ("handover", "cli", "gc"):
        assert narrow not in name or len(suites) <= 3, (
            f"the gate name {name!r} names a subset while {len(suites)} suites run")


def test_the_counts_parser_reads_unittest_as_well_as_pytest():
    """Two of the three DETERMINISTIC_GATED skills run `unittest discover`, whose summary is a
    different shape. Reading only pytest's reported `tests_run: 0` for a run of 83 real tests,
    so the counts check refused a receipt it should have written — fail-closed, and wrong about
    which runner it was looking at.

    `Ran N tests` counts skips; pytest's `N passed` does not. The skips come back out so
    `tests_run` means the same thing for both: tests that actually executed.
    """
    assert eval_harness._pytest_counts("Ran 83 tests in 0.139s\n\nOK") == \
        {"tests_run": 83, "skipped": 0, "failed": 0}
    assert eval_harness._pytest_counts("Ran 83 tests in 0.1s\n\nOK (skipped=2)") == \
        {"tests_run": 81, "skipped": 2, "failed": 0}
    assert not eval_harness._counts_are_evidence(
        eval_harness._pytest_counts("Ran 83 tests in 0.1s\n\nFAILED (failures=1)"))
    assert not eval_harness._counts_are_evidence(
        eval_harness._pytest_counts("Ran 0 tests in 0.0s\n\nOK"))
