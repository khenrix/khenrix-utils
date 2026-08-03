"""§12.3's progress tuple, its three-outcome comparison, and oscillation over the journal."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import journal, progress, storage, verify  # noqa: E402


def _run(exit_code=1, stdout="", stderr=""):
    return verify.Run(exit_code=exit_code, stdout=stdout, stderr=stderr,
                      duration_sec=0.1, step_index=0)


PYTEST_TAIL = """\
=================================== FAILURES ===================================
_______________________________ test_alpha _____________________________________
=========================== short test summary info ============================
FAILED tests/test_a.py::test_alpha - AssertionError
FAILED tests/test_b.py::test_beta - ValueError
=========================== 2 failed, 8 passed in 1.2s =========================
"""

PYTEST_GREEN = "=========================== 10 passed in 1.1s ==================\n"

COLLECTION_ERROR = """\
==================================== ERRORS ====================================
ImportError while importing test module 'tests/test_a.py'.
=========================== short test summary info ============================
ERROR tests/test_a.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
"""


def test_a_pytest_failure_tail_yields_the_named_ids():
    ids = progress.pytest_fingerprints(PYTEST_TAIL, "", 1)
    assert ids == frozenset({"tests/test_a.py::test_alpha", "tests/test_b.py::test_beta"})


def test_a_green_pytest_run_yields_the_empty_set_and_that_is_honest():
    assert progress.pytest_fingerprints(PYTEST_GREEN, "", 0) == frozenset()


def test_output_that_is_not_pytest_yields_unknown_never_the_empty_set():
    """THE FAIL-OPEN. An empty set is a subset of everything, so it compares as progress
    against every prior attempt and the loop runs to its cap reporting improvement."""
    assert progress.pytest_fingerprints("make: *** [verify] Error 2\n", "", 2) is None


def test_a_zero_exit_from_something_that_is_not_pytest_is_unknown_not_a_green_run():
    """THE BANNER GUARD'S OWN TEST, and it has to be a ZERO exit. A nonzero non-pytest run
    already answers `None` through `return ids or None`, so it never reaches the guard —
    measured, the `_PYTEST_BANNERS` mutation SURVIVES on it. A zero exit does reach it, and
    without the guard the empty set comes back as an honest green run: `compare` then reads
    every later attempt as a strict improvement and the loop runs to its cap."""
    assert progress.pytest_fingerprints("BUILD SUCCESSFUL in 3s\n", "", 0) is None
    assert progress.pytest_fingerprints("", "", 0) is None


def test_a_collection_error_yields_unknown_not_one_failing_id():
    """pytest exited nonzero and named no failing TEST. 'ERROR <module>' is not a test id,
    and reading it as one manufactures a fingerprint that can never shrink."""
    assert progress.pytest_fingerprints(COLLECTION_ERROR, "", 2) is None


def test_a_nonzero_pytest_run_with_a_banner_and_no_failed_line_is_unknown():
    banner = "=========================== short test summary info ============\n"
    assert progress.pytest_fingerprints(banner, "", 1) is None


def test_the_parser_reads_stderr_too_because_a_gate_may_redirect():
    assert progress.pytest_fingerprints("", PYTEST_TAIL, 1) == \
        frozenset({"tests/test_a.py::test_alpha", "tests/test_b.py::test_beta"})


def test_an_exit_code_that_is_not_one_is_refused_rather_than_read_as_zero():
    """`0` and `False` are equal, so a bool reaching here would take the green-run branch and
    turn an unmeasured gate into an honest empty set."""
    for bad in (False, True, "0", 1.0, None):
        with pytest.raises(progress.ProgressError):
            progress.pytest_fingerprints(PYTEST_GREEN, "", bad)


def test_from_runs_counts_only_failures_the_baseline_did_not_have():
    base = _run(1, "=== short test summary info ===\nFAILED t.py::old - X\n")
    cand = _run(1, "=== short test summary info ===\nFAILED t.py::old - X\n"
                   "FAILED t.py::new - Y\n")
    p = progress.from_runs(cand, base)
    assert p.new_failure_count == 1
    assert p.failing_test_fingerprints == frozenset({"t.py::old", "t.py::new"})


def test_an_unparseable_baseline_makes_the_whole_tuple_unknown():
    cand = _run(1, PYTEST_TAIL)
    p = progress.from_runs(cand, _run(2, "make: *** Error 2\n"))
    assert p.new_failure_count is None and p.failing_test_fingerprints is None


def test_an_unparseable_candidate_makes_the_whole_tuple_unknown():
    p = progress.from_runs(_run(2, "make: *** Error 2\n"), _run(0, PYTEST_GREEN))
    assert p.new_failure_count is None and p.failing_test_fingerprints is None


# --------------------------------------------------------------------------- comparison
def _p(n, ids):
    return progress.Progress(n, None if ids is None else frozenset(ids))


def test_a_shrinking_count_is_progress():
    assert progress.compare(_p(3, {"a", "b", "c"}), _p(1, {"a"})) == progress.BETTER


def test_a_strictly_shrinking_set_is_progress():
    assert progress.compare(_p(2, {"a", "b"}), _p(2, {"a"})) == progress.BETTER


def test_an_identical_tuple_is_not_progress():
    assert progress.compare(_p(2, {"a", "b"}), _p(2, {"a", "b"})) == progress.NOT_BETTER


def test_a_traded_failure_is_not_progress_and_is_not_unknown_either():
    """Neither set contains the other. That IS a measurement, and its answer is 'no'.
    `not_comparable` is reserved for 'we could not tell', which is what makes it useful."""
    assert progress.compare(_p(1, {"a"}), _p(1, {"b"})) == progress.NOT_BETTER


def test_a_growing_set_is_not_progress():
    assert progress.compare(_p(1, {"a"}), _p(2, {"a", "b"})) == progress.NOT_BETTER


def test_an_unknown_on_either_side_is_not_comparable():
    assert progress.compare(_p(1, {"a"}), _p(None, None)) == progress.NOT_COMPARABLE
    assert progress.compare(_p(None, None), _p(1, {"a"})) == progress.NOT_COMPARABLE


def test_something_that_is_not_a_progress_is_refused_by_the_comparison():
    """`not_comparable` is what an UNMEASURED side answers, and a caller who handed in the
    wrong type has measured nothing — so returning it here would report a type error as a
    gate this module could not read."""
    for pair in ((_p(1, {"a"}), None), (None, _p(1, {"a"})), (_p(1, {"a"}), (1, {"a"}))):
        with pytest.raises(progress.ProgressError):
            progress.compare(*pair)


def test_a_half_measured_tuple_cannot_be_built():
    with pytest.raises(progress.ProgressError):
        progress.Progress(3, None)
    with pytest.raises(progress.ProgressError):
        progress.Progress(None, frozenset())


def test_a_measured_tuple_that_is_not_a_measurement_cannot_be_built():
    """The other half of the constructor, and the mutable set is the one that matters: a
    `Progress` carrying a plain `set` builds, compares, and then raises `unhashable type`
    from inside `oscillation`'s `seen.add` — a crash in the detector, from a value the
    tuple accepted three steps earlier."""
    with pytest.raises(progress.ProgressError):
        progress.Progress(1, {"a"})
    for bad in (-1, True, 1.5, "1"):
        with pytest.raises(progress.ProgressError):
            progress.Progress(bad, frozenset())


# --------------------------------------------------------------------------- journal
def _log(tmp_path):
    return journal.Journal(storage.journal_path(tmp_path))


def _fix(log, op, tree, ids, count=0):
    progress.record_fix_start(log, operation_id=op, tree_oid=tree)
    progress.record_fix_done(log, operation_id=op, tree_oid=tree,
                             prog=progress.Progress(count, frozenset(ids)))


def test_two_fixes_at_different_trees_are_not_oscillating(tmp_path):
    log = _log(tmp_path)
    _fix(log, "op1", "a" * 40, {"t::x"})
    _fix(log, "op2", "b" * 40, {"t::y"})
    answer, why = progress.oscillation(log.read())
    assert answer == progress.NOT_OSCILLATING and why


def test_the_second_sighting_of_one_tree_and_failure_pair_is_the_stop_signal(tmp_path):
    log = _log(tmp_path)
    _fix(log, "op1", "a" * 40, {"t::x"})
    _fix(log, "op2", "b" * 40, {"t::y"})
    _fix(log, "op3", "a" * 40, {"t::x"})
    answer, why = progress.oscillation(log.read())
    assert answer == progress.OSCILLATING
    assert "a" * 40 in why


def test_the_same_tree_with_a_different_failure_set_is_not_a_repeat(tmp_path):
    log = _log(tmp_path)
    _fix(log, "op1", "a" * 40, {"t::x"})
    _fix(log, "op2", "a" * 40, {"t::y"})
    assert progress.oscillation(log.read())[0] == progress.NOT_OSCILLATING


def test_an_unmeasured_failure_set_never_matches_another_one(tmp_path):
    """THE FAIL-OPEN. Two unreadable attempts at one tree are `(oid, None)` twice. Treating
    that as a repeat manufactures a stop signal and reports it as a measured recurrence."""
    log = _log(tmp_path)
    for op in ("op1", "op2"):
        progress.record_fix_start(log, operation_id=op, tree_oid="a" * 40)
        progress.record_fix_done(log, operation_id=op, tree_oid="a" * 40,
                                 prog=progress.Progress(None, None))
    answer, why = progress.oscillation(log.read())
    assert answer == progress.OSCILLATION_UNKNOWN
    assert "could not" in why or "unmeasured" in why


def test_a_fix_with_no_checkpoint_is_recorded_absent_and_never_as_empty_string(tmp_path):
    log = _log(tmp_path)
    progress.record_fix_start(log, operation_id="op1", tree_oid=None)
    progress.record_fix_done(log, operation_id="op1", tree_oid=None,
                             prog=progress.Progress(0, frozenset()))
    rows = [e for e in log.read() if e.event == journal.done(progress.FIX_KIND)]
    assert rows[0].data["tree_oid"] is None
    assert progress.sightings(log.read()) == ()
    assert progress.oscillation(log.read())[0] == progress.OSCILLATION_UNKNOWN


def test_an_empty_tree_oid_is_refused_at_the_writer(tmp_path):
    log = _log(tmp_path)
    with pytest.raises(progress.ProgressError):
        progress.record_fix_start(log, operation_id="op1", tree_oid="")


def test_a_done_record_that_carries_no_progress_is_refused_at_the_writer(tmp_path):
    """The pair the detector reads back is written from this argument, so a caller handing in
    a bare set or a 2-tuple would record a half-measurement the reader has no way to doubt."""
    log = _log(tmp_path)
    for bad in (None, frozenset({"t::x"}), (0, frozenset())):
        with pytest.raises(progress.ProgressError):
            progress.record_fix_done(log, operation_id="op1", tree_oid="a" * 40, prog=bad)
    assert progress.sightings(log.read()) == (), "a refused write left a record behind"


def test_a_journal_written_by_something_other_than_this_module_is_refused(tmp_path):
    """`record_fix_done` writes a non-empty string or null, and a list of ids or null. Anything
    else came from another writer, and interpreting it is how an empty-string tree id becomes
    the key every unrecorded attempt shares."""
    for i, payload in enumerate(({"tree_oid": "", "failure_fingerprints": None},
                                 {"tree_oid": "a" * 40, "failure_fingerprints": "t::x"},
                                 {"tree_oid": "a" * 40,
                                  "failure_fingerprints": ["t::x", 7]})):
        d = tmp_path / f"log{i}"
        d.mkdir()
        log = _log(d)
        log.record(journal.done(progress.FIX_KIND), operation_id="op1", **payload)
        with pytest.raises(progress.ProgressError):
            progress.sightings(log.read())


def test_an_orphaned_fix_makes_the_answer_unknown(tmp_path):
    log = _log(tmp_path)
    _fix(log, "op1", "a" * 40, {"t::x"})
    progress.record_fix_start(log, operation_id="op2", tree_oid="b" * 40)
    answer, why = progress.oscillation(log.read())
    assert answer == progress.OSCILLATION_UNKNOWN and "op2" in why


def test_a_proven_repeat_outranks_a_gap_in_the_evidence(tmp_path):
    """A positive finding is not weakened by missing evidence elsewhere — `agreement_label`'s
    rule ("a measured difference outranks an unmeasured field"), applied here."""
    log = _log(tmp_path)
    _fix(log, "op1", "a" * 40, {"t::x"})
    _fix(log, "op2", "a" * 40, {"t::x"})
    progress.record_fix_start(log, operation_id="op3", tree_oid="c" * 40)
    assert progress.oscillation(log.read())[0] == progress.OSCILLATING


def test_a_run_that_has_fixed_nothing_is_not_oscillating():
    assert progress.oscillation(())[0] == progress.NOT_OSCILLATING


# --------------------------------------------------------------------------- the cap
class _Cap:
    def __init__(self, cap):
        self.synthesis_fix_cap = cap


def test_the_cap_counts_starts_so_a_crashed_fix_is_still_spent(tmp_path):
    """THE FAIL-OPEN. Counting `_done` records lets a fix that crashed after spending a
    provider call read as unspent budget, and the loop buys it a second time."""
    log = _log(tmp_path)
    _fix(log, "op1", "a" * 40, {"t::x"})
    progress.record_fix_start(log, operation_id="op2", tree_oid="b" * 40)
    assert progress.cap_remaining(_Cap(3), log.read()) == 1


def test_the_cap_never_reports_negative_budget(tmp_path):
    log = _log(tmp_path)
    for i, op in enumerate(("op1", "op2", "op3")):
        _fix(log, op, chr(ord("a") + i) * 40, {f"t::{i}"})
    assert progress.cap_remaining(_Cap(2), log.read()) == 0


def test_a_manifest_with_no_cap_is_refused_rather_than_defaulted():
    with pytest.raises(progress.ProgressError):
        progress.cap_remaining(object(), ())
    for bad in (None, True, -1, "3", 3.0):
        with pytest.raises(progress.ProgressError):
            progress.cap_remaining(_Cap(bad), ())


def test_the_other_operations_in_a_real_journal_are_not_read_as_fixes(tmp_path):
    """EVERY OTHER CASE HERE RUNS OVER A JOURNAL HOLDING NOTHING BUT FIXES, and a real run's
    journal holds `confirm`, `calibration`, `seat` and `verification` pairs around them.
    Measured: with a fix-only fixture, widening all three filters from `synthesis_fix` to
    "anything ending `_start`/`_done`" leaves the suite green — so `cap_remaining` would spend
    the §12.3 budget on a calibration and `sightings` would read a verification's payload as a
    fix's tree.
    """
    log = _log(tmp_path)
    for kind, op in (("confirm", "r1"), ("calibration", "c1"), ("seat", "s1")):
        log.record(journal.intent(kind), operation_id=op)
        log.record(journal.done(kind), operation_id=op, seat="claude")
    # Carrying the two payload keys a fix's own record uses. Nothing writes them on another
    # operation today, and the filter must not be relying on that: a verification recording
    # the tree it verified is one field away, and a filter keyed on the SUFFIX would then
    # read it as a synthesis fix that never happened.
    log.record(journal.intent("verification"), operation_id="v1")
    log.record(journal.done("verification"), operation_id="v1",
               tree_oid="d" * 40, failure_fingerprints=["t::z"])
    _fix(log, "op1", "a" * 40, {"t::x"})
    events = log.read()

    assert len(progress.done_records(events)) == 1
    assert progress.sightings(events) == (progress.Sighting("a" * 40, frozenset({"t::x"})),)
    assert progress.oscillation(events)[0] == progress.NOT_OSCILLATING
    assert progress.cap_remaining(_Cap(3), events) == 2
