"""§12.5's strongest-seat rubric and §12.4's coverage-as-fallback-trigger."""
import ast
import itertools
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import coverage, rubric, strategy, verify  # noqa: E402


def _d(seat, *, unsat=0, covered=5, outcome=verify.PASS, risk=0, complexity=10):
    return rubric.Dimensions(seat=seat, unsatisfied_criteria=unsat, covered_criteria=covered,
                             gate_outcome=outcome, review_risk=risk, diff_complexity=complexity)


def _report(*results, contradictions=()):
    rs = tuple(results)
    return coverage.Report(rs, tuple(contradictions),
                           tuple(f"{r.row_id}[{r.criterion_index}]: {r.detail}"
                                 for r in rs if r.satisfied is False),
                           tuple(f"{r.row_id}[{r.criterion_index}]: {r.detail}"
                                 for r in rs if r.method == "unresolved"))


def _ok(row="a"):
    return coverage.Result(row, 0, "mechanically_checked", True, "checked and satisfied")


def _bad(row="a"):
    return coverage.Result(row, 0, "mechanically_checked", False, "checked and not satisfied")


def _unknown(row="a"):
    return coverage.Result(row, 0, "unresolved", None, "no predicate exists")


def _traced(row="a"):
    return coverage.Result(row, 0, "manual_trace_confirmed", None, "a human traced this claim")


def _shapes():
    """Every report shape this module sees. A plain function, not a pytest fixture."""
    return (None, coverage.Report((), (), (), ()), _report(_ok()), _report(_ok(), _bad("b")),
            _report(_ok(), _unknown("b")), _report(_unknown("a")), _report(_ok(), _traced("b")),
            _report(_bad("a"), _unknown("b")), _report(_ok(), _traced("b"), _unknown("c")),
            _report(_ok(), contradictions=("row b contradicts a unanimous rejection",)),
            _report(_ok(), _unknown("b"),
                    contradictions=("row b contradicts a unanimous rejection",)))


# --------------------------------------------------------------------------- ordering
def test_requirement_coverage_outranks_every_later_dimension():
    """§12.5's order: coverage first, gate second, review risk third, diff complexity last."""
    strong = _d("agy", unsat=0, outcome=verify.FAIL, risk=9, complexity=999)
    weak = _d("claude", unsat=1, outcome=verify.PASS, risk=0, complexity=1)
    assert rubric.rank([weak, strong]).ordered == ("agy", "claude")


def test_the_gate_outcome_outranks_review_risk_and_complexity():
    a = _d("agy", outcome=verify.PASS, risk=9, complexity=999)
    b = _d("claude", outcome=verify.FAIL, risk=0, complexity=1)
    assert rubric.rank([b, a]).ordered == ("agy", "claude")


def test_review_risk_outranks_diff_complexity():
    a = _d("agy", risk=0, complexity=999)
    b = _d("claude", risk=1, complexity=1)
    assert rubric.rank([b, a]).ordered == ("agy", "claude")


def test_more_checked_criteria_break_a_tie_on_the_unsatisfied_count():
    """§12.5's first dimension is TWO numbers, and only the first of them had a test. The
    seat names are chosen so alphabetical order gives the opposite answer: a `_key` that
    dropped `covered_criteria`, or spelled it without the sign that puts MORE first, would
    fall through to the name and return `("agy", "codex")`."""
    thorough = _d("codex", unsat=0, covered=9)
    thin = _d("agy", unsat=0, covered=1)
    assert rubric.rank([thin, thorough]).ordered == ("codex", "agy")


def test_the_seat_name_is_the_final_tie_break_and_makes_the_order_total():
    seats = [_d(n) for n in ("codex", "agy", "claude")]
    for order in itertools.permutations(seats):
        assert rubric.rank(list(order)).ordered == ("agy", "claude", "codex")


def test_no_two_distinct_seats_ever_compare_equal():
    """§12.3's last sentence: 'strongest seat' is never an unrecorded intuition. A rubric that
    can return a tie has an unrecorded tie-break by another route."""
    seats = [_d(n) for n in ("agy", "claude", "codex")]
    keys = [rubric._key(d) for d in seats]
    assert len(set(keys)) == len(keys)


def test_two_records_for_one_seat_name_are_refused():
    """A duplicate name makes the final tie-break non-total, silently."""
    with pytest.raises(rubric.RubricError):
        rubric.rank([_d("agy"), _d("agy", complexity=3)])


def test_a_ranking_over_a_generator_sees_every_seat():
    """`rank(dimensions_from(...) for seat in fleet)` is the caller shape this module is
    written for, and an iterator that is not materialised first is exhausted by the type
    check before the ranking loop ever reads it — leaving a three-seat fleet ranked as
    `Ranking((), ())` and `strongest` reporting that no seat was supplied."""
    assert rubric.rank(_d(n) for n in ("codex", "agy", "claude")).ordered \
        == ("agy", "claude", "codex")


def test_the_refused_seats_are_ordered_the_same_way_whatever_order_they_arrive_in():
    """§12.5's answer is reproducible or it is an intuition, and the seats it REFUSED are
    part of the answer: `strongest`'s refusal prints them in this order, and a run whose
    refusal reads differently on a replay is one nobody can diff against the first."""
    dims = [_d("codex", risk=None), _d("agy", complexity=None), _d("claude", covered=None)]
    expected = (("agy", "diff_complexity"), ("claude", "requirement_coverage"),
                ("codex", "review_risk"))
    for order in itertools.permutations(dims):
        r = rubric.rank(list(order))
        assert r.unrankable == expected
        assert rubric.strongest(list(order)) == rubric.strongest(dims)


def test_a_record_that_is_not_a_dimensions_is_refused_rather_than_skipped():
    with pytest.raises(rubric.RubricError):
        rubric.rank([_d("agy"), "claude"])


def test_every_verify_outcome_has_a_declared_rank():
    """A §6.2 outcome added later must fail loudly rather than sort as unknown."""
    assert set(rubric.GATE_RANK) == set(verify.OUTCOMES)


def test_the_gate_ranks_are_distinct_and_a_pass_is_the_strongest():
    """Set equality with `verify.OUTCOMES` says every outcome is PRESENT and nothing about
    the order it declares. Two outcomes sharing a rank silently hands their comparison to
    review risk, and only the PASS/FAIL pair had a test pinning a direction."""
    assert len(set(rubric.GATE_RANK.values())) == len(rubric.GATE_RANK)
    assert min(rubric.GATE_RANK, key=rubric.GATE_RANK.get) == verify.PASS
    assert max(rubric.GATE_RANK, key=rubric.GATE_RANK.get) == verify.HARVEST_INCOMPLETE


def test_an_outcome_outside_the_declared_set_makes_the_seat_unrankable():
    r = rubric.rank([_d("agy"), _d("claude", outcome="MOSTLY_FINE")])
    assert r.ordered == ("agy",)
    assert [s for s, _ in r.unrankable] == ["claude"]


# --------------------------------------------------------------------------- fail closed
def test_a_seat_with_an_unmeasured_dimension_is_unrankable_not_worst():
    r = rubric.rank([_d("agy"), _d("claude", risk=None)])
    assert r.ordered == ("agy",)
    assert r.unrankable == (("claude", "review_risk"),)


def test_strongest_refuses_to_name_a_seat_while_any_seat_is_unrankable():
    """THE FAIL-OPEN. Ranking only the measurable seats prints 'the strongest seat we could
    measure' as 'the strongest seat'."""
    name, why = rubric.strongest([_d("agy"), _d("claude", risk=None)])
    assert name is None
    assert "claude" in why and "review_risk" in why


def test_strongest_names_the_seat_when_every_dimension_was_measured():
    name, why = rubric.strongest([_d("agy", unsat=1), _d("claude", unsat=0)])
    assert name == "claude" and why


def test_strongest_over_no_seats_at_all_is_a_refusal():
    name, why = rubric.strongest([])
    assert name is None and "no seat" in why


def test_a_dimensions_record_refuses_a_value_that_is_not_a_count_or_a_name():
    """`__post_init__` is the only thing standing between a hand-built record and a rank key.
    `True` is an `int` and would sort as 1; a negative count is not a count; a `gate_outcome`
    that is not a string reaches `GATE_RANK` as an unhashable key, and `TypeError` is not a
    class any caller of this module catches."""
    for bad in ({"seat": ""}, {"seat": None}, {"risk": -1}, {"risk": True},
                {"unsat": 1.5}, {"covered": "5"}, {"outcome": ["PASS"]}):
        seat = bad.pop("seat", "agy")
        with pytest.raises(rubric.RubricError):
            _d(seat, **bad)


# --------------------------------------------------------------------------- §12.4
def test_an_unsatisfied_accepted_row_triggers_fallback():
    answer, why = rubric.fallback_trigger(_report(_ok(), _bad("b")))
    assert answer == rubric.TRIGGERED and "b" in why


def test_a_contradiction_triggers_fallback():
    answer, _ = rubric.fallback_trigger(
        _report(_ok(), contradictions=("row b contradicts a unanimous rejection",)))
    assert answer == rubric.TRIGGERED


def test_an_unresolved_criterion_is_undecidable_never_a_clean_report():
    """THE FAIL-OPEN. 'Nobody could check' read as 'nothing is wrong' is §10.1's own
    failure, arriving as §12.4's fallback decision."""
    answer, why = rubric.fallback_trigger(_report(_ok(), _unknown("b")))
    assert answer == rubric.TRIGGER_UNDECIDABLE and "unresolved" in why


def test_a_fully_checked_clean_report_does_not_trigger():
    answer, _ = rubric.fallback_trigger(_report(_ok(), _ok("b")))
    assert answer == rubric.NOT_TRIGGERED


def test_an_empty_report_is_undecidable_not_clean():
    """C4's shape one container out again: `coverage.check` refuses a zero-ROW ledger, and
    `Report` is a public dataclass whose __post_init__ only re-derives roll-ups."""
    answer, why = rubric.fallback_trigger(coverage.Report((), (), (), ()))
    assert answer == rubric.TRIGGER_UNDECIDABLE and "no results" in why


def test_a_trigger_outranks_an_undecidable_because_it_is_a_measurement():
    answer, _ = rubric.fallback_trigger(_report(_bad("a"), _unknown("b")))
    assert answer == rubric.TRIGGERED


def test_a_missing_report_is_undecidable():
    answer, why = rubric.fallback_trigger(None)
    assert answer == rubric.TRIGGER_UNDECIDABLE and "no coverage report" in why


def test_every_report_shape_answers_with_a_declared_trigger_and_a_reason():
    """`TRIGGERS` is exported for a consumer validating one of these read back out of a
    report, and nothing asserted the answers stay inside it. A reason that is empty is the
    same defect: a caller printing the verdict prints nothing beside it."""
    for report in _shapes():
        answer, why = rubric.fallback_trigger(report)
        assert answer in rubric.TRIGGERS, report
        assert isinstance(why, str) and why.strip(), report


def test_a_report_this_module_cannot_read_is_refused_rather_than_guessed():
    for report in ({"unsatisfied": ()}, (), "clean"):
        with pytest.raises(rubric.RubricError):
            rubric.fallback_trigger(report)


# --------------------------------------------------------------------------- extraction
def test_dimensions_are_extracted_from_the_records_and_never_re_measured():
    d = rubric.dimensions_from("agy", report=_report(_ok(), _bad("b")),
                               gate_outcome=verify.FAIL, review_risk=2,
                               size=strategy.Size(120, 4, ()))
    assert (d.seat, d.unsatisfied_criteria, d.covered_criteria) == ("agy", 1, 1)
    assert d.gate_outcome == verify.FAIL and d.review_risk == 2
    assert d.diff_complexity == 124


def test_a_ledger_contradiction_counts_against_the_coverage_dimension():
    """§12.5's top dimension is §12.4's trigger as a number, and §12.4's trigger has two
    sources. A `unsatisfied`-only count reads a contradicted ledger as fully covered."""
    d = rubric.dimensions_from(
        "agy", report=_report(_ok(), contradictions=("b contradicts a unanimous rejection",)),
        gate_outcome=verify.PASS, review_risk=0, size=strategy.Size(1, 1, ()))
    assert d.unsatisfied_criteria == 1 and d.covered_criteria == 1


def test_a_recorded_human_trace_is_not_a_mechanically_checked_criterion():
    """§10.1's method axis, carried into the number seats are ordered by. A trace is a
    human's word: it is not a miss, so it does not raise `unsatisfied_criteria`, and it is
    not a mechanical check, so it must not raise `covered_criteria` either — and the reason
    the trigger prints may not describe it as one."""
    report = _report(_ok(), _traced("b"))
    answer, why = rubric.fallback_trigger(report)
    assert answer == rubric.NOT_TRIGGERED
    assert "1 of this report's 2 result(s) were mechanically checked" in why
    d = rubric.dimensions_from("agy", report=report, gate_outcome=verify.PASS,
                               review_risk=0, size=strategy.Size(1, 1, ()))
    assert d.unsatisfied_criteria == 0 and d.covered_criteria == 1


def test_an_unmeasured_size_yields_an_unmeasured_complexity_not_zero():
    d = rubric.dimensions_from("agy", report=_report(_ok()), gate_outcome=verify.PASS,
                               review_risk=0, size=strategy.Size(None, 4, ("a binary delta",)))
    assert d.diff_complexity is None


def test_a_missing_report_yields_unmeasured_coverage_not_a_clean_one():
    d = rubric.dimensions_from("agy", report=None, gate_outcome=verify.PASS,
                               review_risk=0, size=strategy.Size(1, 1, ()))
    assert d.unsatisfied_criteria is None and d.covered_criteria is None


def test_a_size_this_module_cannot_read_is_refused_rather_than_guessed():
    with pytest.raises(rubric.RubricError):
        rubric.dimensions_from("agy", report=None, gate_outcome=verify.PASS,
                               review_risk=0, size=(1, 1))


def test_an_all_unresolved_report_is_unrankable_and_never_the_strongest_seat():
    """THE FAIL-OPEN THIS MODULE IS MOST EXPOSED TO. `unsatisfied` and `contradictions` are
    BOTH empty for a report nobody could check, so summing their lengths scores it 0 — the
    strongest value on §12.5's first dimension — and the seat nobody could check then beats
    the seat with one measured miss. `fallback_trigger` calls that same report `undecidable`,
    so the two answers have to come from one reading."""
    nobody = _report(_unknown("a"), _unknown("b"))
    size = strategy.Size(10, 2, ())
    blind = rubric.dimensions_from("agy", report=nobody, gate_outcome=verify.PASS,
                                   review_risk=0, size=size)
    assert blind.unsatisfied_criteria is None and blind.covered_criteria is None
    assert rubric.fallback_trigger(nobody)[0] == rubric.TRIGGER_UNDECIDABLE
    one_miss = rubric.dimensions_from("claude", report=_report(_ok(), _bad("b")),
                                      gate_outcome=verify.PASS, review_risk=0, size=size)
    assert one_miss.unsatisfied_criteria == 1
    name, why = rubric.strongest([blind, one_miss])
    assert name is None and "requirement_coverage" in why


def test_a_partly_unresolved_report_yields_no_coverage_number_even_when_it_triggers():
    """THE SAME FAIL-OPEN ONE BRANCH IN, and the branch that answers `triggered` reaches it.
    A report with one checked miss beside ninety unchecked criteria counts 1, which sorts
    that seat AHEAD of a fully checked seat holding 2 — a lower bound wearing a measurement's
    shape. §12.4's trigger still fires, because a trigger is an action and a measured miss is
    grounds for one; the NUMBER is refused, because nobody finished reading the report."""
    partial = _report(_bad("a"), _unknown("b"))
    size = strategy.Size(10, 2, ())
    assert rubric.fallback_trigger(partial)[0] == rubric.TRIGGERED
    blind = rubric.dimensions_from("agy", report=partial, gate_outcome=verify.PASS,
                                   review_risk=0, size=size)
    assert blind.unsatisfied_criteria is None and blind.covered_criteria is None
    two_misses = rubric.dimensions_from("claude", report=_report(_bad("a"), _bad("b")),
                                        gate_outcome=verify.PASS, review_risk=0, size=size)
    assert two_misses.unsatisfied_criteria == 2
    name, why = rubric.strongest([blind, two_misses])
    assert name is None and "requirement_coverage" in why


def test_a_seat_cannot_become_rankable_by_acquiring_a_measured_failure():
    """The property the case above is an instance of, and it reads as absurd on purpose: if
    an unresolved criterion voids the count only while nothing else went wrong, then adding a
    FAILING criterion to a report restores the seat's rank. Same report, one result changed
    from satisfied to unsatisfied; both readings must refuse the number."""
    size = strategy.Size(10, 2, ())
    for first in (_ok("a"), _bad("a")):
        d = rubric.dimensions_from("agy", report=_report(first, _unknown("b")),
                                   gate_outcome=verify.PASS, review_risk=0, size=size)
        assert d.unsatisfied_criteria is None and d.covered_criteria is None, first
        assert rubric.rank([d]).unrankable == (("agy", "requirement_coverage"),), first


def test_the_trigger_and_the_top_dimension_agree_on_every_report_shape():
    """Not "both were edited to match" — the same call, over every shape this module sees.

    THE RULE IS NOT "unmeasured IFF undecidable", and an earlier version of this test said it
    was, over a shape list holding no report that triggers AND has an unresolved criterion —
    the one input where the two answers legitimately differ. What one reading guarantees is
    the implication that matters: an undecidable report NEVER yields a number, the two
    numbers are always absent together, and a number appears only when every result was
    resolved and there was at least one.
    """
    size = strategy.Size(10, 2, ())
    for report in _shapes():
        answer, _ = rubric.fallback_trigger(report)
        d = rubric.dimensions_from("agy", report=report, gate_outcome=verify.PASS,
                                   review_risk=0, size=size)
        unmeasured = d.unsatisfied_criteria is None
        assert unmeasured == (d.covered_criteria is None), (report, answer)
        if answer == rubric.TRIGGER_UNDECIDABLE:
            assert unmeasured, (report, answer)
        complete = report is not None and bool(report.results) and not report.unresolved
        assert unmeasured == (not complete), (report, answer)


def test_the_module_reads_a_coverage_report_in_exactly_one_place():
    """By construction rather than by both being remembered: a second `report.unresolved` is
    a second chance for §12.4's answer and §12.5's top dimension to drift apart, and the
    drift is silent and decides which seat the run calls strongest."""
    src = (ROOT / "shared" / "lib" / "forge" / "rubric.py").read_text(encoding="utf-8")
    for branch in ("if report.contradictions:", "if report.unsatisfied:",
                   "if not report.results:", "if report.unresolved:"):
        assert src.count(branch) == 1, branch


def test_no_function_but_the_one_reading_touches_a_report_at_all():
    """The counted branches above are four spellings, and a second reading only has to pick
    a fifth — `len(report.unresolved) > 0` passes that test unchanged. This asks the
    structural question instead: which functions reach for an attribute of `report`."""
    src = (ROOT / "shared" / "lib" / "forge" / "rubric.py").read_text(encoding="utf-8")
    readers = {fn.name for fn in ast.walk(ast.parse(src))
               if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
               for node in ast.walk(fn)
               if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
               and node.value.id == "report"}
    assert readers == {"_read_report"}, readers


def test_every_unknown_size_yields_an_unmeasured_complexity_and_never_a_zero():
    """`Size`'s two numbers are independently nullable and BOTH must be checked. Zero is the
    SMALLEST diff and therefore the strongest value on this dimension, so a coerced 0 here is
    C5's shape one field over.

    The three shapes below are the three ways a `Size` can be short a number, not a listing
    of `strategy.measure`'s refusals: `measure` voids BOTH dimensions in four states — no
    candidate at all, an omitted artifact, a patch git could not read, and a fleet whose
    seats all came back empty-handed — and voids `changed_lines` alone for a binary delta,
    where the PATH count is still a real measurement. The third shape, a line count with no
    file count, is one `measure` does not currently produce and `Size` permits; a rubric that
    accepted it would rest on which spellings of unknown one producer happens to emit.
    """
    for size in (strategy.Size(None, None, ("nothing was measured",)),
                 strategy.Size(None, 4, ("a binary delta",)),
                 strategy.Size(120, None, ("a path this run could not name",))):
        d = rubric.dimensions_from("agy", report=_report(_ok()), gate_outcome=verify.PASS,
                                   review_risk=0, size=size)
        assert d.diff_complexity is None, size
        name, why = rubric.strongest([d, _d("claude")])
        assert name is None and "diff_complexity" in why
