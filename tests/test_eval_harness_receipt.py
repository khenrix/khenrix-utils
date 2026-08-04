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
