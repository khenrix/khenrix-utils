"""§12.1's size gate and §12.3's failure classification."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import bundle, coverage, strategy, verify  # noqa: E402

from forge_fixtures import commit_all, git, make_repo, write  # noqa: E402


def _repo(tmp_path):
    r = make_repo(tmp_path)
    write(r, "keep.txt", "x\n")
    commit_all(r, "base")
    return r


def _patch(repo, edits) -> bytes:
    """A real tracked patch: apply `edits`, stage, ask git for the staged diff, reset.

    Through the fixtures' hermetic git, never a bare `subprocess`: `add` and `reset` are two
    of the verbs measured to fire `post-index-change`, so a bare call would run the
    DEVELOPER's own `core.hooksPath` hooks inside a throwaway fixture repo. `--output=`
    rather than reading stdout because the fixture helper is text-mode and a patch is bytes.
    """
    for rel, text in edits.items():
        (repo / rel).write_bytes(text if isinstance(text, bytes) else text.encode())
    git(repo, "add", "-A")
    out = Path(repo).parent / "candidate.patch"
    git(repo, "diff", "--cached", "--binary", f"--output={out}")
    git(repo, "reset", "--hard", "HEAD")
    return out.read_bytes()


def _cand(patch=b"", sidecars=(), omitted=()):
    return bundle.CandidateBundle(version=1, baseline_ref="refs/x", baseline_commit="0" * 40,
                                  tracked_patch=patch, sidecars=tuple(sidecars),
                                  omitted=tuple(omitted))


def test_a_text_patch_is_counted_in_lines_and_files(tmp_path):
    r = _repo(tmp_path)
    p = _patch(r, {"keep.txt": "a\nb\nc\n", "new.txt": "n\n"})
    s = strategy.measure(r, {"claude": _cand(p)})
    assert s.unmeasured == ()
    assert s.changed_files == 2
    # keep.txt: 3 added / 1 deleted; new.txt: 1 added / 0 deleted.
    assert s.changed_lines == 5


def test_a_binary_patch_makes_the_line_count_unknown_not_zero(tmp_path):
    """THE FAIL-OPEN. `git apply --numstat` prints `-` for a binary file (measured, git
    2.53.0). Counting it as 0 puts a whole-blob rewrite under the 400-line threshold."""
    r = _repo(tmp_path)
    p = _patch(r, {"blob.bin": b"\x00\x01\x02\x03\x04"})
    s = strategy.measure(r, {"claude": _cand(p)})
    assert s.changed_lines is None
    assert s.changed_files == 1          # the PATH is still measured; only the lines are not
    # The REASON has to name the binary delta, not merely the path: a generic "this cell was
    # not a number" refusal would satisfy a path-only assertion, and then removing the branch
    # that recognises `-` would cost nothing here.
    assert any("blob.bin" in u and "binary" in u for u in s.unmeasured)


def test_a_patch_git_cannot_read_voids_both_dimensions(tmp_path):
    """An unreadable patch is not a small one either. `git apply --numstat` exits 128 on
    input it cannot parse, and the engine then does not know which FILES the candidate
    touched — so a file count over the seats it could read is a total that is missing a
    term."""
    r = _repo(tmp_path)
    good = _patch(r, {"keep.txt": "a\n"})
    s = strategy.measure(r, {"claude": _cand(b"this is not a unified diff\n"),
                             "codex": _cand(good)})
    assert s.changed_lines is None and s.changed_files is None
    assert any("claude" in u and "--numstat" in u for u in s.unmeasured)


def test_a_refused_line_count_is_not_restored_by_a_later_record(tmp_path):
    """A patch really can carry two records for one path — a concatenation of a binary and a
    text diff for the same file yields both, measured — and the binary refusal has to win
    whichever order they arrive in. Letting the number win restores exactly the count git
    declined to give."""
    r = _repo(tmp_path)
    text = _patch(r, {"keep.txt": "a\nb\nc\n"})
    binary = _patch(r, {"keep.txt": b"\x00\x01\x02"})
    for label, joined in (("binary first", binary + text), ("text first", text + binary)):
        s = strategy.measure(r, {"claude": _cand(joined)})
        assert s.changed_lines is None, label
        assert s.changed_files == 1, label
        assert any("keep.txt" in u and "binary" in u for u in s.unmeasured), label


def test_the_record_parser_refuses_every_cell_it_cannot_read():
    """The parse's own closure. Only the binary cell is reachable through git today, so the
    other two shapes are checked here rather than through a fixture that cannot produce
    them — and the malformed-record shape is the one that has no path to blame, which is why
    it answers None and makes its caller void the whole patch."""
    assert strategy._numstat_record("2\t1\tt.txt") == ("t.txt", 3, None)
    # A path may itself hold a TAB and the two counts never can (measured, git 2.53.0).
    assert strategy._numstat_record("2\t0\thas\ttab.txt")[:2] == ("has\ttab.txt", 2)
    path, n, why = strategy._numstat_record("-\t-\tb.bin")
    assert (path, n) == ("b.bin", None) and "binary" in why
    path, n, why = strategy._numstat_record("2\tmany\tt.txt")
    assert (path, n) == ("t.txt", None) and "not two numbers" in why
    path, n, why = strategy._numstat_record("garbage-with-no-tabs")
    assert path is None and n is None and "three" in why


def test_a_candidate_set_that_is_not_a_mapping_of_bundles_is_refused(tmp_path):
    r = _repo(tmp_path)
    with pytest.raises(strategy.StrategyError):
        strategy.measure(r, ["claude"])
    with pytest.raises(strategy.StrategyError):
        strategy.measure(r, {"claude": "a patch, probably"})


def test_an_omitted_artifact_makes_the_whole_size_unknown(tmp_path):
    r = _repo(tmp_path)
    p = _patch(r, {"keep.txt": "a\n"})
    s = strategy.measure(r, {"claude": _cand(p, omitted=("secret.link",))})
    assert s.changed_lines is None and s.changed_files is None
    assert any("omitted" in u for u in s.unmeasured)


def test_a_text_sidecar_counts_and_a_binary_one_does_not(tmp_path):
    r = _repo(tmp_path)
    txt = bundle.SidecarEntry(path="doc.md", kind="file", mode=0o644, payload=b"a\nb\n")
    binr = bundle.SidecarEntry(path="img.png", kind="file", mode=0o644, payload=b"\xff\xfe\x00")
    ok = strategy.measure(r, {"claude": _cand(sidecars=[txt])})
    assert ok.changed_lines == 2 and ok.changed_files == 1 and ok.unmeasured == ()
    bad = strategy.measure(r, {"claude": _cand(sidecars=[txt, binr])})
    assert bad.changed_lines is None
    assert bad.changed_files == 2
    assert any("img.png" in u for u in bad.unmeasured)


def test_a_symlink_sidecar_is_one_line_and_an_empty_file_is_none(tmp_path):
    """`baseline`'s own reading of a link (D-1: "a symlink IS its target text"), so it counts
    as the one line that text is — not as 0, which would make a fleet that rewired a tree's
    links measure as having changed nothing."""
    r = _repo(tmp_path)
    link = bundle.SidecarEntry(path="ln", kind="symlink", mode=0, payload=b"../target")
    empty = bundle.SidecarEntry(path="e.txt", kind="file", mode=0o644, payload=b"")
    nonl = bundle.SidecarEntry(path="n.txt", kind="file", mode=0o644, payload=b"no trailing")
    s = strategy.measure(r, {"claude": _cand(sidecars=[link, empty, nonl])})
    assert s.unmeasured == ()
    assert s.changed_files == 3
    assert s.changed_lines == 2          # 1 for the link, 0 for the empty file, 1 for `nonl`


def test_the_union_is_over_paths_not_a_sum_over_seats(tmp_path):
    """§12.1 sizes the CHANGE, and three seats editing one file is one changed file."""
    r = _repo(tmp_path)
    p = _patch(r, {"keep.txt": "a\n"})
    s = strategy.measure(r, {"claude": _cand(p), "codex": _cand(p), "agy": _cand(p)})
    assert s.changed_files == 1


def test_an_empty_candidate_set_is_unknown_not_zero(tmp_path):
    r = _repo(tmp_path)
    s = strategy.measure(r, {})
    assert s.changed_lines is None and s.changed_files is None
    assert any("no candidate" in u for u in s.unmeasured)


def test_a_fleet_that_produced_nothing_is_unknown_not_zero(tmp_path):
    """The same argument as the empty set, arriving with seat names on it — and the one a
    caller reaches without ever passing `{}`. Three seats that each returned empty-handed is
    a populated mapping measuring 0 lines across 0 files, which `decide` reads as a small
    change and answers with from-scratch fusion over an artifact set that does not exist."""
    r = _repo(tmp_path)
    s = strategy.measure(r, {"claude": _cand(), "codex": _cand(), "agy": _cand()})
    assert s.changed_lines is None and s.changed_files is None
    assert any("not one of the 3 candidate(s)" in u for u in s.unmeasured)
    assert strategy.decide("size-gated", s).strategy is None


# --------------------------------------------------------------------------- #
# §12's strategy rule, applied to a measured size.
# --------------------------------------------------------------------------- #
def test_a_small_measured_change_selects_from_scratch_mechanically():
    d = strategy.decide("size-gated", strategy.Size(10, 2, ()))
    assert d.strategy == strategy.FROM_SCRATCH
    assert d.method == "mechanically_checked"


def test_an_unmeasured_size_never_selects_from_scratch():
    """THE FAIL-OPEN. A size nobody could take is not a small one."""
    d = strategy.decide("size-gated", strategy.Size(None, 2, ("a binary delta",)))
    assert d.strategy is None
    assert d.method == "unresolved"
    assert "binary" in d.detail


def test_above_the_threshold_no_strategy_is_produced():
    over = strategy.decide("size-gated", strategy.Size(401, 2, ()))
    assert over.strategy is None and over.method == "unresolved"
    assert strategy.decide("size-gated", strategy.Size(10, 16, ())).strategy is None


def test_the_thresholds_are_exclusive_at_the_boundary():
    assert strategy.decide("size-gated", strategy.Size(400, 15, ())).strategy is None
    assert strategy.decide("size-gated", strategy.Size(399, 14, ())).strategy \
        == strategy.FROM_SCRATCH


def test_a_gate_confirmed_rule_is_a_recorded_human_decision_not_a_measurement():
    for rule, expected in (("fusion", strategy.FROM_SCRATCH),
                           ("base-and-port", strategy.BASE_AND_PORT)):
        d = strategy.decide(rule, strategy.Size(None, None, ("nothing was measured",)))
        assert d.strategy == expected
        assert d.method == "manual_trace_confirmed"


def test_an_unknown_rule_is_refused():
    with pytest.raises(strategy.StrategyError):
        strategy.decide("whatever-seems-best", strategy.Size(1, 1, ()))


def test_a_seam_analysis_is_recorded_manually_and_never_mechanically():
    d = strategy.recorded_seam_analysis(strategy.PARTITION, "the API boundary is frozen")
    assert d.method == "manual_trace_confirmed" and d.strategy == strategy.PARTITION
    with pytest.raises(strategy.StrategyError):
        strategy.recorded_seam_analysis(strategy.PARTITION, "   ")
    with pytest.raises(strategy.StrategyError):
        strategy.recorded_seam_analysis(strategy.FROM_SCRATCH, "it looked small")


def test_a_decision_cannot_carry_a_method_its_strategy_does_not_support():
    with pytest.raises(strategy.StrategyError):
        strategy.Decision(strategy.PARTITION, "mechanically_checked", "no predicate says this")
    with pytest.raises(strategy.StrategyError):
        strategy.Decision(strategy.FROM_SCRATCH, "unresolved", "unresolved carries no strategy")
    with pytest.raises(strategy.StrategyError):
        strategy.Decision(None, "manual_trace_confirmed", "a confirmed nothing")


def test_a_decision_is_bounded_on_every_field_it_carries():
    with pytest.raises(strategy.StrategyError):
        strategy.Decision(strategy.PARTITION, "looked-about-right", "not one of §10.1's three")
    with pytest.raises(strategy.StrategyError):
        strategy.Decision("rewrite_it_all", "manual_trace_confirmed", "not one of §12.1's")
    with pytest.raises(strategy.StrategyError):
        strategy.Decision(strategy.PARTITION, "manual_trace_confirmed", "   ")


def test_a_size_with_an_unexplained_unknown_is_refused():
    """The invariant the whole module leans on, from the other side: `measure` is obliged to
    void the dimension every refusal it records takes away, and this is what collects that."""
    with pytest.raises(strategy.StrategyError):
        strategy.Size(None, 2, ())
    with pytest.raises(strategy.StrategyError):
        strategy.Size(10, 2, ("something went unmeasured",))


def test_the_two_readers_refuse_an_argument_of_the_wrong_shape():
    with pytest.raises(strategy.StrategyError):
        strategy.decide("size-gated", (10, 2))
    with pytest.raises(strategy.StrategyError):
        strategy.classify_failure(verify.FAIL, report={"unsatisfied": ()})


def test_every_value_these_functions_return_is_in_a_declared_vocabulary():
    """The exported tuples are the vocabulary a later phase validates a manifest against, so
    a value this module can produce and they do not name is a record nothing downstream can
    read back."""
    produced = {strategy.decide("size-gated", strategy.Size(10, 2, ())).strategy,
                strategy.decide("fusion", strategy.Size(10, 2, ())).strategy,
                strategy.decide("base-and-port", strategy.Size(10, 2, ())).strategy,
                strategy.recorded_seam_analysis(strategy.PARTITION, "seams").strategy}
    assert produced <= set(strategy.STRATEGIES) and produced == set(strategy.STRATEGIES)
    classes = {strategy.classify_failure(o, report=_report(_ok()))[0]
               for o in verify.OUTCOMES}
    classes |= {strategy.classify_failure(verify.FAIL, report=_report(_bad()))[0]}
    assert classes - {None} <= set(strategy.FAILURE_CLASSES)
    assert {strategy.fallback_disposition(c) for c in (*strategy.FAILURE_CLASSES, None)} \
        == set(strategy.DISPOSITIONS)


# --------------------------------------------------------------------------- #
# §12.3's three-way failure classification.
# --------------------------------------------------------------------------- #
def _report(*results):
    rs = tuple(results)
    return coverage.Report(rs,
                           (),
                           tuple(f"{r.row_id}[{r.criterion_index}]: {r.detail}"
                                 for r in rs if r.satisfied is False),
                           tuple(f"{r.row_id}[{r.criterion_index}]: {r.detail}"
                                 for r in rs if r.method == "unresolved"))


def _ok(row="a"):
    return coverage.Result(row, 0, "mechanically_checked", True, "the named check passed")


def _bad(row="a"):
    return coverage.Result(row, 0, "mechanically_checked", False, "the named check failed")


def _unknown(row="a"):
    return coverage.Result(row, 0, "unresolved", None, "no predicate exists for this row")


def _traced(row="a"):
    """§10.1's OTHER non-mechanical method, which this suite had no fixture for at all —
    which is why every test here passed while `classify_failure` returned
    `synthesis_introduced` over a report no predicate had touched."""
    return coverage.Result(row, 0, "manual_trace_confirmed", None, "a human traced this claim")


def test_infrastructure_outcomes_are_classified_and_never_permit_fallback():
    for outcome in (verify.BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE, verify.HARVEST_INCOMPLETE):
        cls, why = strategy.classify_failure(outcome, report=_report(_ok()))
        assert cls == strategy.INFRASTRUCTURE, outcome
        assert strategy.fallback_disposition(cls) == strategy.REFUSED
        assert why


def test_a_flaky_or_gate_changed_outcome_classifies_as_nothing():
    for outcome in (verify.FLAKY, verify.GATE_CHANGED):
        cls, why = strategy.classify_failure(outcome, report=_report(_ok()))
        assert cls is None, outcome
        assert strategy.fallback_disposition(cls) == strategy.UNDECIDABLE
        assert why


def test_a_fail_with_an_unsatisfied_claim_is_a_requirement_gap():
    cls, _ = strategy.classify_failure(verify.FAIL, report=_report(_ok(), _bad("b")))
    assert cls == strategy.REQUIREMENT_GAP
    assert strategy.fallback_disposition(cls) == strategy.PERMITTED


def test_a_fail_whose_claims_all_checked_clean_is_synthesis_introduced():
    cls, _ = strategy.classify_failure(verify.FAIL, report=_report(_ok(), _ok("b")))
    assert cls == strategy.SYNTHESIS_INTRODUCED
    assert strategy.fallback_disposition(cls) == strategy.PERMITTED


def test_a_fail_over_an_unresolved_report_classifies_as_nothing():
    """THE FAIL-OPEN. `unresolved` means nobody could check. Reading it as 'the claims are
    fine, so synthesis broke it' permits fallback on a measurement never taken."""
    cls, why = strategy.classify_failure(verify.FAIL, report=_report(_ok(), _unknown("b")))
    assert cls is None
    assert strategy.fallback_disposition(cls) == strategy.UNDECIDABLE
    assert "unresolved" in why


def test_a_fail_with_no_report_at_all_classifies_as_nothing():
    cls, why = strategy.classify_failure(verify.FAIL, report=None)
    assert cls is None and "no coverage report" in why


def test_a_fail_over_a_report_with_no_results_classifies_as_nothing():
    """An empty report has no contradiction, no unsatisfied claim and no unresolved one, so
    every roll-up reads clean — and reading it clean is `synthesis_introduced`, the class
    that permits spending a fallback, concluded from a report about nothing at all."""
    cls, why = strategy.classify_failure(verify.FAIL, report=coverage.Report((), (), (), ()))
    assert cls is None
    assert strategy.fallback_disposition(cls) == strategy.UNDECIDABLE
    assert "no results at all" in why


def test_a_contradicted_ledger_is_a_requirement_gap_whatever_the_criteria_say():
    r = coverage.Report((_ok(),), ("row b contradicts a unanimous rejection",), (), ())
    cls, _ = strategy.classify_failure(verify.FAIL, report=r)
    assert cls == strategy.REQUIREMENT_GAP


def test_the_two_readers_of_one_report_never_disagree_about_it():
    """§12.3's classifier and §12.4's trigger read the SAME evidence, and read it in the same
    order or they answer differently about identical bytes. They did: measured,
    `Report((), ("…contradiction…",), (), ())` answered `triggered` in
    `rubric.fallback_trigger` and `None` here — this function checked "no results at all"
    before the contradiction, and reading a named contradiction as "this report says nothing"
    is the reading that is wrong.

    `Report.__post_init__` re-derives `unsatisfied` and `unresolved` from the results, so
    neither can appear beside an empty result list; `contradictions` is NOT derived, and
    `Report`'s own docstring says §12.4's consumer populates one by hand — so the shape is
    buildable rather than hypothetical. `coverage.check` cannot emit it, which is why nothing
    had noticed."""
    from forge import rubric
    contradiction = ("row b contradicts a unanimous rejection",)
    cases = [
        coverage.Report((), contradiction, (), ()),               # the divergence itself
        coverage.Report((_ok(),), contradiction, (), ()),
        coverage.Report((), (), (), ()),
        coverage.Report((_ok(),), (), (), ()),
        _report(_ok(), _bad("b")),
        _report(_ok(), _unknown("b")),
        None,
    ]
    for r in cases:
        cls, _ = strategy.classify_failure(verify.FAIL, report=r)
        answer, _ = rubric.fallback_trigger(r)
        assert (cls == strategy.REQUIREMENT_GAP) == (answer == rubric.TRIGGERED), (r, cls,
                                                                                   answer)
        assert (cls is None) == (answer == rubric.TRIGGER_UNDECIDABLE), (r, cls, answer)
        assert (cls == strategy.SYNTHESIS_INTRODUCED) == (answer == rubric.NOT_TRIGGERED), \
            (r, cls, answer)


def test_a_passing_outcome_has_no_failure_to_classify():
    cls, why = strategy.classify_failure(verify.PASS, report=_report(_ok()))
    assert cls is None and "not a failure" in why


def test_an_outcome_outside_the_declared_set_is_refused():
    with pytest.raises(strategy.StrategyError):
        strategy.classify_failure("MOSTLY_FINE", report=None)


def test_every_verify_outcome_has_a_declared_reading():
    """Every value §6.2 declares today is answered rather than raised on."""
    for outcome in verify.OUTCOMES:
        strategy.classify_failure(outcome, report=_report(_ok()))


def test_an_outcome_added_to_verify_without_a_reading_here_fails_loudly(monkeypatch):
    """The other half of the sentence above, and the half that was doing no work: iterating
    today's outcomes cannot see what happens to tomorrow's. Without a raise, a new §6.2 row
    falls through every named branch into the coverage read and is classified
    `synthesis_introduced` — the one class of the three that PERMITS spending a fallback —
    off a report that was never about it."""
    monkeypatch.setattr(verify, "OUTCOMES", (*verify.OUTCOMES, "MOSTLY_GREEN"))
    with pytest.raises(strategy.StrategyError):
        strategy.classify_failure("MOSTLY_GREEN", report=_report(_ok()))


def test_an_undeclared_failure_class_is_refused_by_the_disposition():
    with pytest.raises(strategy.StrategyError):
        strategy.fallback_disposition("probably_fine")


def test_a_traced_report_does_not_buy_a_fallback(tmp_path):
    """`synthesis_introduced` is the one class `fallback_disposition` PERMITS spending on, and
    the sentence it printed claimed every criterion "was mechanically checked and satisfied"
    when none had been. That is a verdict naming evidence it does not have.
    """
    cls, why = strategy.classify_failure(verify.FAIL, report=_report(_traced("a"), _traced("b")))
    assert cls is None, "a report nobody checked cannot conclude the synthesis is at fault"
    assert "no predicate run" in why and "mechanically checked" not in why


def test_a_MIXED_report_does_not_buy_a_fallback_either(tmp_path):
    """The realistic ledger. `all(...)` where `any(...)` belongs passes every all-or-nothing
    case and fails only here."""
    cls, _ = strategy.classify_failure(
        verify.FAIL, report=_report(_ok("a"), _ok("b"), _ok("c"), _traced("d")))
    assert cls is None


def test_a_fully_mechanical_failing_report_still_reaches_synthesis_introduced(tmp_path):
    """The guard against over-tightening: if nothing could reach it, §12.3 would never permit
    a fallback and the fix would be indistinguishable from deleting the class."""
    cls, why = strategy.classify_failure(verify.FAIL, report=_report(_ok("a"), _ok("b")))
    assert cls == strategy.SYNTHESIS_INTRODUCED
    assert strategy.fallback_disposition(cls) == strategy.PERMITTED


def test_the_two_readers_never_disagree_over_any_shape_INCLUDING_the_traced_one(tmp_path):
    """An agreement test between two COPIES cannot find a defect they share — measured, both
    modules agreed on the traced shape and both were wrong. It is worth keeping only now that
    the predicate is one function they both import, and only if the shape that broke them is
    in the list.
    """
    from forge import rubric
    shapes = (_report(_ok("a")), _report(_bad("a")), _report(_unknown("a")),
              _report(_traced("a")), _report(_ok("a"), _traced("b")),
              _report(_ok("a"), _unknown("b")), _report())
    for rep in shapes:
        gap_r = rubric.dimensions_from("agy", report=rep, gate_outcome=verify.FAIL,
                                       review_risk=0,
                                       size=strategy.Size(10, 1, ())).unsatisfied_criteria is None
        gap_s = strategy.classify_failure(verify.FAIL, report=rep)[0] is None
        assert gap_r == gap_s, f"the two readers disagree about {rep}"
