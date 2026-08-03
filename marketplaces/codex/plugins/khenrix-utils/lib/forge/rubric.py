"""§12.5's strongest-seat rubric and §12.4's coverage-as-fallback-trigger.

A RUBRIC THAT READS A LIVE MEASUREMENT IS A RUBRIC NOBODY CAN REPRODUCE. Every dimension
below is a value already on the ledger, the coverage report or the seat record, and `rank` is
a pure function of them — so `--collect` re-running this hours later, on a machine where the
gate would now answer differently, gets the same order. `dimensions_from` is the one place
that reads a record, and it takes the record rather than a path.

TOTAL BY CONSTRUCTION. §12.3's last sentence forbids "strongest seat" being an unrecorded
intuition, and a comparison that can return "tie" is one by another route: the caller then
picks, and nothing records how. The four declared dimensions are followed by the SEAT NAME,
which `storage.seat_state_path` already requires to be unique inside a run — so any two
distinct seats compare unequal, and `test_no_two_distinct_seats_ever_compare_equal` is what
stands where that stops being true.
"""
from dataclasses import dataclass

from . import coverage as coveragemod, strategy as strategymod, verify

TRIGGERED = "triggered"
NOT_TRIGGERED = "not_triggered"
TRIGGER_UNDECIDABLE = "undecidable"
TRIGGERS = (TRIGGERED, NOT_TRIGGERED, TRIGGER_UNDECIDABLE)

# §12.5's second dimension, as a declared total order over §6.2's outcomes. Spelled out
# rather than derived from `verify.OUTCOMES`' declaration order, because that order is a
# reading list and this is a preference — and a test asserts the two sets are equal, so an
# outcome added to §6.2 later fails here loudly instead of sorting as unknown.
GATE_RANK = {
    verify.PASS: 0,
    verify.BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE: 1,
    verify.GATE_CHANGED: 2,
    verify.FLAKY: 3,
    verify.FAIL: 4,
    verify.HARVEST_INCOMPLETE: 5,
}


class RubricError(RuntimeError):
    """A ranking question this module will not answer on the evidence it was given."""


@dataclass(frozen=True)
class Dimensions:
    """One seat's recorded values on §12.5's four dimensions.

    EVERY FIELD IS NULLABLE AND A NULL IS NOT A ZERO. An unmeasured `review_risk` is not a
    seat with no risk, and an unmeasured `diff_complexity` is not a seat with a small diff —
    both would sort that seat to the FRONT, which is the fail-open a rubric can least afford.
    A null makes the seat unrankable instead.

    `gate_outcome` IS TYPE-CHECKED HERE AND THE OTHERS ARE NOT ARBITRARILY. `_unmeasured` asks
    `gate_outcome not in GATE_RANK`, which raises `TypeError` — not `RubricError` — for a
    value that is not hashable, and an error outside this module's declared class is one no
    caller of it knows to catch. The numeric fields reach no such lookup; their own check is
    below for the separate reason that a `bool` is an `int` and `True` would sort as 1.
    """
    seat: str
    unsatisfied_criteria: int | None
    covered_criteria: int | None
    gate_outcome: str | None
    review_risk: int | None
    diff_complexity: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.seat, str) or not self.seat.strip():
            raise RubricError(f"a seat has a name, not {self.seat!r}")
        if self.gate_outcome is not None and not isinstance(self.gate_outcome, str):
            raise RubricError(
                f"a gate outcome is one of {list(verify.OUTCOMES)} or None, not "
                f"{self.gate_outcome!r}: an unhashable value reaches the rank table as a "
                "TypeError, which is not a refusal any caller of this module catches")
        for name in ("unsatisfied_criteria", "covered_criteria", "review_risk",
                     "diff_complexity"):
            v = getattr(self, name)
            if v is None:
                continue
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise RubricError(f"{name} is a whole number or None, not {v!r}")


@dataclass(frozen=True)
class _Reading:
    """What one coverage report says, read once: §12.4's answer and §12.5's top dimension.

    THE TWO COVERAGE NUMBERS ARE FIELDS OF THE READING RATHER THAN A SECOND CALCULATION, and
    that is the whole reason this record exists. `dimensions_from` copies them out; it does
    not re-derive them, does not consult the report again, and has no branch of its own about
    what the report meant.

    `answer` AND THE NUMBERS ARE NOT THE SAME QUESTION, AND THE DIFFERENCE IS DELIBERATE. A
    trigger is an ACTION — §12.4: "a missing accepted row is a fallback trigger *and* a report
    line, regardless of verify" — so a measured miss fires it even when other criteria went
    unchecked. A dimension is a NUMBER that seats are ordered by, and a count taken over a
    report with unchecked criteria is a LOWER BOUND wearing a measurement's shape. So the
    numbers are `None` whenever anything about the report went unread, including when the
    answer is `triggered`; the one traversal below produces both.
    """
    answer: str
    why: str
    unsatisfied_criteria: int | None
    covered_criteria: int | None


def _read_report(report) -> _Reading:
    """§12.4's reading of one coverage report — THE ONLY READING OF A REPORT IN THIS MODULE.

    BOTH CONSUMERS COME THROUGH HERE, AND THAT IS THE ENTIRE POINT OF THE FUNCTION.
    `fallback_trigger` answers §12.4's question and `dimensions_from` fills §12.5's TOP
    dimension, and both are the same question about the same evidence: is there an accepted
    claim this run knows to be missing, and did this run manage to look at all of them? When
    those were two separate readings they disagreed, silently and in the worst possible
    direction: an all-`unresolved` report has an empty `unsatisfied` and an empty
    `contradictions`, so `len(report.unsatisfied) + len(report.contradictions)` scored it
    **zero** — the STRONGEST value on §12.5's first dimension — while `fallback_trigger`, in
    this same module, called that identical report `undecidable`. The seat nobody could check
    outranked the seat with one measured miss. Two spellings of one judgement cannot be kept
    in step by both being remembered; one function cannot disagree with itself.

    `undecidable` IS THE THIRD ANSWER AND IT IS THE ONE THIS FUNCTION EXISTS FOR. `unresolved`
    means nobody could check. Folding it into `not_triggered` is §10.1's own example failure
    — "marked present because os.replace appears" — arriving as §12's fallback decision.

    A TRIGGER OUTRANKS A GAP AND A GAP STILL VOIDS THE NUMBERS. §12.4 makes the trigger fire
    "regardless of verify", so an unresolved criterion beside an unsatisfied one does not
    soften it and the two `TRIGGERED` branches are read first. It does not follow that the
    COUNT is then a measurement: a report with one checked miss and ninety unchecked criteria
    yields `unsatisfied_criteria=1`, which would sort that seat ahead of a fully checked seat
    holding two — the all-`unresolved` fail-open above, arriving through the branch that
    answers `triggered` rather than the one that answers `undecidable`. So `gap` is computed
    before any answer is chosen and voids both numbers wherever it is set, and a seat whose
    coverage nobody finished reading is unrankable however loudly the trigger fired.

    A CONSEQUENCE WORTH STATING OUTRIGHT, because it decides how often `strongest` names
    anybody: `coverage._schema` is `unresolved` by construction and `coverage._prose` is
    `unresolved` for any criterion with no recorded trace, so a real ledger carrying either
    kind produces a report this function calls incomplete. That is the intended reading —
    those criteria genuinely were not checked — and a rubric that named a strongest seat over
    them would be reporting an order it did not measure.
    """
    if report is None:
        return _Reading(TRIGGER_UNDECIDABLE, (
            "no coverage report was produced, so whether an accepted claim is missing is a "
            "question nobody asked; §12.4 calls this check the only thing that catches a "
            "false green"), None, None)
    if not isinstance(report, coveragemod.Report):
        raise RubricError(f"a coverage.Report or None is required, "
                          f"not {type(report).__name__}")
    # WHETHER THIS REPORT'S NUMBERS ARE A MEASUREMENT, decided before and independently of
    # which of the three answers it gets. Both spellings of "nobody finished looking" live
    # here, and both are checked on every path.
    gap = None
    if not report.results:
        gap = ("this report holds no results at all, so it says nothing about any claim; an "
               "all-empty report reading as a covered run is §10.1's own failure shape")
    if report.unresolved:
        gap = (f"{len(report.unresolved)} criterion/criteria are unresolved — nobody could "
               f"check them ({report.unresolved[0]}) — so 'no accepted row is missing' is not "
               "something this run measured")
    if gap is None:
        unsat = len(report.unsatisfied) + len(report.contradictions)
        # A `manual_trace_confirmed` result is a HUMAN'S WORD and is deliberately not counted
        # here: §10.1 keeps it off the mechanical axis, and counting it would put human
        # diligence into a number §12.5 orders seats by. It is not a miss either — nobody
        # said the claim was unmet — so it lowers `covered` without raising `unsat`, which is
        # the fail-closed direction.
        covered = sum(1 for r in report.results
                      if r.method == "mechanically_checked" and r.satisfied is True)
    else:
        unsat = covered = None

    if report.contradictions:
        return _Reading(TRIGGERED, (f"{len(report.contradictions)} ledger contradiction(s): "
                                    f"{report.contradictions[0]}"), unsat, covered)
    if report.unsatisfied:
        return _Reading(TRIGGERED, (f"{len(report.unsatisfied)} accepted claim(s) were checked "
                                    f"and are not satisfied: {report.unsatisfied[0]}"),
                        unsat, covered)
    if gap is not None:
        # `unsat` and `covered` are already None here, and they are passed rather than
        # respelled as literals so that the block above stays the ONE place that decides
        # whether this report has numbers at all.
        return _Reading(TRIGGER_UNDECIDABLE, gap, unsat, covered)
    return _Reading(NOT_TRIGGERED, (
        f"no accepted criterion is checked-and-unsatisfied and none is unresolved: {covered} "
        f"of this report's {len(report.results)} result(s) were mechanically checked and "
        f"satisfied and {len(report.results) - covered} carry a recorded human trace; no row "
        "contradicts a unanimous rejection"), unsat, covered)


def dimensions_from(seat, *, report, gate_outcome, review_risk, size) -> Dimensions:
    """Extract §12.5's dimensions from the records that already hold them.

    A REPORT THIS RUN COULD NOT FINISH READING YIELDS TWO NULLS, not two zeros — and which
    reports those are is `_read_report`'s answer, not a second opinion formed here. "No
    accepted criterion is unsatisfied" and "nobody could check whether one is" are the two
    states §10.1 exists to keep apart, and zero would spell them the same way, with the
    unmeasured seat sorting FIRST.

    `diff_complexity` IS THE SAME SHAPE ONE DIMENSION OVER, and `Size`'s two numbers are
    independently nullable, so both are checked. `strategy.measure` voids BOTH of them in four
    states — no candidate at all, an artifact omitted from the harvest, a patch git could not
    read (not knowing the patch is not knowing which FILES it touched either), and a fleet
    whose seats all came back empty-handed — and voids `changed_lines` ALONE for a binary
    delta or a sidecar that is not UTF-8, where the path count is still a real measurement.
    `Size` additionally permits the mirror state, a line count with no file count, which
    `measure` does not produce today; treating it as measured would rest this dimension on
    which spellings of unknown one producer happens to emit. Every one of them scores 0 if the
    two numbers are summed without being checked, and 0 is the SMALLEST diff and therefore the
    STRONGEST value here. The reasons are on the `Size` itself (`Size.unmeasured`, which
    `Size.__post_init__` guarantees is non-empty whenever either dimension is None), and a
    reader printing `rank`'s refusal prints those lines beside it.
    """
    reading = _read_report(report)
    if not isinstance(size, strategymod.Size):
        raise RubricError(f"a strategy.Size is required, not {type(size).__name__}")
    complexity = (None if size.changed_lines is None or size.changed_files is None
                  else size.changed_lines + size.changed_files)
    return Dimensions(seat=seat, unsatisfied_criteria=reading.unsatisfied_criteria,
                      covered_criteria=reading.covered_criteria,
                      gate_outcome=gate_outcome, review_risk=review_risk,
                      diff_complexity=complexity)


def _unmeasured(d: Dimensions):
    """The first dimension this seat cannot be ranked on, or None. FIRST, in §12.5's order,
    so the reason a seat was dropped names the highest-priority thing that was missing."""
    if d.unsatisfied_criteria is None or d.covered_criteria is None:
        return "requirement_coverage"
    if d.gate_outcome not in GATE_RANK:
        return "gate_outcome"
    if d.review_risk is None:
        return "review_risk"
    if d.diff_complexity is None:
        return "diff_complexity"
    return None


def _key(d: Dimensions) -> tuple:
    """§12.5's order as one sort key, ascending: smaller is stronger.

    Requirement coverage is TWO numbers because §12.5 names one dimension that has two: fewer
    unsatisfied accepted claims first — that is the thing §12.4 calls a fallback trigger — and
    among seats tied there, more mechanically-checked-and-satisfied criteria. `-covered` puts
    "more" at the front of an ascending sort.
    """
    why = _unmeasured(d)
    if why is not None:
        raise RubricError(f"{d.seat} cannot be ranked: {why} was not measured")
    return (d.unsatisfied_criteria, -d.covered_criteria, GATE_RANK[d.gate_outcome],
            d.review_risk, d.diff_complexity, d.seat)


@dataclass(frozen=True)
class Ranking:
    """The rankable seats in §12.5's order, and every seat that was left out with its reason.

    `unrankable` is not a footnote. A ranking that dropped seats silently would let
    `strongest` describe a two-seat comparison as a fleet-wide verdict.
    """
    ordered: tuple
    unrankable: tuple


def rank(dims) -> Ranking:
    """Order the seats §12.5 can compare, and name the ones it cannot."""
    dims = list(dims)
    wrong = sorted({type(d).__name__ for d in dims if not isinstance(d, Dimensions)})
    if wrong:
        raise RubricError(f"a ranking is over Dimensions records, not {wrong}")
    names = [d.seat for d in dims]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise RubricError(
            f"two records name the seat(s) {dupes}. The seat name is §12.5's final "
            "tie-break, so a duplicate makes the order non-total exactly where the rubric "
            "stops being reproducible — and it would do it silently")
    good, bad = [], []
    for d in dims:
        why = _unmeasured(d)
        (bad if why else good).append((d.seat, why) if why else d)
    return Ranking(tuple(d.seat for d in sorted(good, key=_key)), tuple(sorted(bad)))


def strongest(dims) -> tuple:
    """§12.5's strongest seat, or `None` with the reason no seat can be named.

    A SEAT IS NAMED ONLY WHEN EVERY SEAT WAS RANKABLE. Ranking the measurable ones and
    reporting their winner turns "the strongest seat we were able to measure" into "the
    strongest seat" — and the seat that was dropped may have been better on the very
    dimension nobody read. §12.4's coverage check is a fallback trigger for the same reason:
    the thing that was not measured is exactly the thing that decides.
    """
    r = rank(dims)
    if r.unrankable:
        return None, (
            "no strongest seat can be named while "
            + "; ".join(f"{s} has no measured {why}" for s, why in r.unrankable)
            + " — a rubric run over the seats it could read would report the winner of a "
              "smaller comparison than the one the fleet ran")
    if not r.ordered:
        return None, ("no seat was supplied, so there is nothing to compare; an empty fleet "
                      "has no strongest member")
    return r.ordered[0], (f"§12.5's order over {len(r.ordered)} fully measured seat(s): "
                          f"{', '.join(r.ordered)}")


def fallback_trigger(report) -> tuple:
    """§12.4's check, as three answers. One line, because the reading is `_read_report`'s.

    THIS FUNCTION IS DELIBERATELY A PROJECTION OF THAT ONE READING AND MUST STAY ONE. The
    moment it grows a branch of its own, §12.4's answer and §12.5's top dimension are two
    readings again, and the version of this module that had two disagreed about the report
    that matters most: the one nobody could check.
    """
    reading = _read_report(report)
    return reading.answer, reading.why
