"""§10.1: only criteria with a real predicate are mechanically checked."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import hashlib  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import pytest  # noqa: E402
from forge import coverage, ledger  # noqa: E402

# chmod 0 does not stop root, so the two unreadable-file tests would measure nothing under it
# and pass — the "green because it never ran" shape these suites are written against.
_NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="chmod 0 does not stop root")


def _crit(**kw):
    base = dict(kind="prose", text="it works", path=None, symbol=None,
                node_id=None, sha256=None, query=None, trace=None)
    base.update(kw)
    return ledger.Criterion(**base)


def _schema_crit(**kw):
    """A schema criterion IN THE SHAPE THE LEDGER RECORDS, which is the point.

    §10.1 lists a schema query among its four MECHANICAL kinds, so `ledger.Criterion` requires
    `path` and `query` on this kind — the inputs a predicate would need are on the record. A
    fixture that left them off would exercise `_schema` only in the shape no real ledger
    produces, and the interesting question is precisely whether a criterion carrying a runnable
    query still answers `unresolved`.
    """
    base = dict(kind="schema", text="the users table has a seq column",
                path="db/schema.sql", query="SELECT seq FROM users")
    base.update(kw)
    return _crit(**base)


def _fake_run(script):
    """A pytest stand-in. NO TEST HERE RUNS A REAL PROVIDER OR A REAL SUITE."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        rc, out = script(argv)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
    run.calls = calls
    return run


def test_a_named_test_that_passes_is_mechanically_checked_and_satisfied(tmp_path):
    def script(argv):
        if "--collect-only" in argv:
            return 0, "tests/t.py::test_x\n"
        return 0, "1 passed"
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_x"),
                          row_id="a1", index=0, tree=tmp_path,
                          pytest_argv=["pytest"], run=_fake_run(script))
    assert (r.method, r.satisfied) == ("mechanically_checked", True)
    assert "DOES NOT PROVE THE TEST TESTS THE CLAIM" in r.detail, \
        "mechanically_checked is a claim about mechanism, and the line must say so"


def test_a_named_test_that_fails_is_mechanically_checked_and_not_satisfied(tmp_path):
    def script(argv):
        if "--collect-only" in argv:
            return 0, "tests/t.py::test_x\n"
        return 1, "1 failed"
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_x"),
                          row_id="a1", index=0, tree=tmp_path,
                          pytest_argv=["pytest"], run=_fake_run(script))
    assert (r.method, r.satisfied) == ("mechanically_checked", False), \
        "'checked mechanically, and the answer is no' must not land in `unresolved`"


def test_a_vanished_test_is_unresolved_not_failed(tmp_path):
    """A claim whose test vanished is not a claim that was tested."""
    def script(argv):
        return 5, ""
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_gone"),
                          row_id="a1", index=0, tree=tmp_path,
                          pytest_argv=["pytest"], run=_fake_run(script))
    assert (r.method, r.satisfied) == ("unresolved", None)
    # The verdict alone does not separate this from any other collect failure, and the
    # separation is the actionable half: the report has to say the named test is ABSENT, or a
    # reader cannot tell a deleted test from a broken conftest.
    assert "collected no tests" in r.detail and "not the same as the claim being false" \
        in r.detail


def test_a_node_id_selecting_more_than_one_test_is_unresolved(tmp_path):
    """The predicate must SELECT the named test and observe that one result — not read a
    green suite and infer it."""
    def script(argv):
        if "--collect-only" in argv:
            return 0, "tests/t.py::test_x\ntests/t.py::test_y\n"
        return 0, "2 passed"
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py"),
                          row_id="a1", index=0, tree=tmp_path,
                          pytest_argv=["pytest"], run=_fake_run(script))
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_one_collected_node_that_is_not_the_named_one_is_unresolved(tmp_path):
    """The count is not the whole guard. `tests/t.py::test_x` collects `test_x[a]` when the
    test is parametrized — ONE node, a DIFFERENT node from the one the claim wrote down."""
    def script(argv):
        if "--collect-only" in argv:
            return 0, "tests/t.py::test_x[a]\n"
        return 0, "1 passed"
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_x"),
                          row_id="a1", index=0, tree=tmp_path,
                          pytest_argv=["pytest"], run=_fake_run(script))
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_a_usage_error_is_unresolved(tmp_path):
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_x"),
                          row_id="a1", index=0, tree=tmp_path,
                          pytest_argv=["pytest"],
                          run=_fake_run(lambda argv: (4, "usage error")))
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_a_pytest_that_cannot_be_launched_is_unresolved(tmp_path):
    """A runner that is not there measured nothing. Every fake above RETURNS; this is the
    branch where the call itself raises, and it is the one a missing interpreter takes."""
    def boom(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", "pytest")
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_x"),
                          row_id="a1", index=0, tree=tmp_path,
                          pytest_argv=["pytest"], run=boom)
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_a_test_that_outruns_its_timeout_is_unresolved(tmp_path):
    """A criterion whose test hangs must not hang the coverage check, and the timeout that
    stops it is not a failure of the named test."""
    def script(argv):
        if "--collect-only" in argv:
            return 0, "tests/t.py::test_x\n"
        raise subprocess.TimeoutExpired(argv, coverage._TEST_TIMEOUT)

    def run(argv, **kw):
        rc, out = script(argv)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_x"),
                          row_id="a1", index=0, tree=tmp_path,
                          pytest_argv=["pytest"], run=run)
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_no_pytest_runner_is_unresolved_never_manual_trace(tmp_path):
    r = coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_x"),
                          row_id="a1", index=0, tree=tmp_path, pytest_argv=None)
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_the_test_predicate_runs_in_the_candidate_tree(tmp_path):
    """Never the seat, never the user's checkout."""
    seen = {}

    def run(argv, **kw):
        seen["cwd"] = kw.get("cwd")
        return subprocess.CompletedProcess(argv, 0, stdout="tests/t.py::test_x\n", stderr="")
    coverage.evaluate(_crit(kind="test", node_id="tests/t.py::test_x"),
                      row_id="a1", index=0, tree=tmp_path, pytest_argv=["pytest"], run=run)
    assert Path(seen["cwd"]) == tmp_path


def test_a_symbol_that_exists_is_found_by_ast_not_by_grep(tmp_path):
    """grep matches a docstring, a comment and a string literal. This repository's modules
    are majority comment by line count."""
    (tmp_path / "m.py").write_text(
        '"""This module mentions def ghost_function in prose."""\n'
        "from os import replace\n"
        "GHOST = 1\n"
        "ANNOTATED: int = 2\n\n\nclass C:\n    def method(self):\n        pass\n\n\n"
        "def real_function():\n    return 1\n")
    for sym, want in (("real_function", True), ("GHOST", True), ("ANNOTATED", True),
                      ("C", True), ("ghost_function", False),
                      # An IMPORTED name is bound, not defined. False is the fail-closed
                      # direction: it costs a reader work, where the opposite reading turns
                      # `from os import replace` into evidence of a crash-safe write.
                      ("replace", False)):
        r = coverage.evaluate(_crit(kind="symbol", text="m.py defines it",
                                    path="m.py", symbol=sym),
                              row_id="a1", index=0, tree=tmp_path)
        assert (r.method, r.satisfied) == ("mechanically_checked", want), sym


def test_a_dotted_symbol_resolves_through_a_class(tmp_path):
    (tmp_path / "m.py").write_text("class C:\n    def method(self):\n        pass\n\n\n"
                                   "def f():\n    def inner():\n        pass\n")
    r = coverage.evaluate(_crit(kind="symbol", text="m.py defines C.method",
                                path="m.py", symbol="C.method"),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("mechanically_checked", True)
    r2 = coverage.evaluate(_crit(kind="symbol", text="m.py defines C.absent",
                                 path="m.py", symbol="C.absent"),
                           row_id="a1", index=0, tree=tmp_path)
    assert (r2.method, r2.satisfied) == ("mechanically_checked", False)
    # Only a ClassDef is descended into: a closure is not a module's surface.
    r3 = coverage.evaluate(_crit(kind="symbol", text="m.py defines f.inner",
                                 path="m.py", symbol="f.inner"),
                           row_id="a1", index=0, tree=tmp_path)
    assert (r3.method, r3.satisfied) == ("mechanically_checked", False)


def test_a_syntax_error_is_unresolved_never_symbol_absent(tmp_path):
    (tmp_path / "m.py").write_text("def broken(:\n")
    r = coverage.evaluate(_crit(kind="symbol", text="m.py defines x", path="m.py", symbol="x"),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_a_missing_file_definitively_does_not_define_the_symbol(tmp_path):
    r = coverage.evaluate(_crit(kind="symbol", text="gone.py defines x",
                                path="gone.py", symbol="x"),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("mechanically_checked", False)


@_NOT_ROOT
def test_a_python_file_nobody_can_read_is_unresolved(tmp_path):
    """The gap between "does not exist" and "does not parse": a file that is there and
    unreadable. Absent is a measurement; unreadable is the absence of one."""
    p = tmp_path / "m.py"
    p.write_text("def x():\n    return 1\n")
    p.chmod(0o000)
    try:
        r = coverage.evaluate(_crit(kind="symbol", text="m.py defines x",
                                    path="m.py", symbol="x"),
                              row_id="a1", index=0, tree=tmp_path)
    finally:
        p.chmod(0o644)
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_a_non_python_path_has_no_symbol_evaluator(tmp_path):
    (tmp_path / "m.txt").write_text("x = 1\n")
    r = coverage.evaluate(_crit(kind="symbol", text="m.txt defines x",
                                path="m.txt", symbol="x"),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("unresolved", None)


def test_a_hash_invariant_is_recomputed(tmp_path):
    (tmp_path / "f.bin").write_bytes(b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    r = coverage.evaluate(_crit(kind="hash", text="f.bin is unchanged",
                                path="f.bin", sha256=digest),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("mechanically_checked", True)
    r2 = coverage.evaluate(_crit(kind="hash", text="f.bin is unchanged",
                                 path="f.bin", sha256="0" * 64),
                           row_id="a1", index=0, tree=tmp_path)
    assert (r2.method, r2.satisfied) == ("mechanically_checked", False)


def test_a_symlink_is_its_target_text_and_is_never_hashed_through(tmp_path):
    """`open()` follows a link, so the invariant would describe content from OUTSIDE the
    tree the ledger claims to describe."""
    (tmp_path / "real.txt").write_bytes(b"payload")
    os.symlink("real.txt", tmp_path / "alias.txt")
    through = hashlib.sha256(b"payload").hexdigest()
    target = hashlib.sha256(b"real.txt").hexdigest()
    r = coverage.evaluate(_crit(kind="hash", text="alias.txt", path="alias.txt",
                                sha256=through),
                          row_id="a1", index=0, tree=tmp_path)
    assert r.satisfied is False, "hashing through the link is the fail-open"
    r2 = coverage.evaluate(_crit(kind="hash", text="alias.txt", path="alias.txt",
                                 sha256=target),
                           row_id="a1", index=0, tree=tmp_path)
    assert (r2.method, r2.satisfied) == ("mechanically_checked", True)


def test_a_missing_path_definitively_fails_a_hash_invariant(tmp_path):
    r = coverage.evaluate(_crit(kind="hash", text="gone", path="gone.bin", sha256="0" * 64),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("mechanically_checked", False)


def test_a_special_file_fails_a_hash_invariant_without_being_opened(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    r = coverage.evaluate(_crit(kind="hash", text="pipe", path="pipe", sha256="0" * 64),
                          row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("mechanically_checked", False)


@_NOT_ROOT
def test_a_regular_file_nobody_can_read_leaves_the_hash_unresolved(tmp_path):
    """`lstat` succeeds and the read does not. Answering False here would report an invariant
    as violated on the strength of a measurement nobody took."""
    p = tmp_path / "f.bin"
    p.write_bytes(b"payload")
    p.chmod(0o000)
    try:
        r = coverage.evaluate(_crit(kind="hash", text="f.bin", path="f.bin",
                                    sha256=hashlib.sha256(b"payload").hexdigest()),
                              row_id="a1", index=0, tree=tmp_path)
    finally:
        p.chmod(0o644)
    assert (r.method, r.satisfied) == ("unresolved", None)


@pytest.mark.parametrize("escaping", ["../outside.py", "/etc/passwd", "a/../../outside.py",
                                      ".."])
def test_a_path_that_leaves_the_tree_is_never_reported_as_checked(tmp_path, escaping):
    """`Path(tree) / "/abs"` IS "/abs", and `../` walks straight out. Reporting either as a
    MECHANICAL check makes the coverage line describe a host file the ledger claims nothing
    about — and `unresolved` rather than `False`, because "the invariant is unsatisfied" is
    itself a measurement, and none was taken.

    The bare `".."` is here because it is the one form the RESOLVED-PARENT half cannot see:
    the parent of `tree/..` is `tree` itself, which is inside the tree by construction. Only
    the string guard refuses it, and without that guard a hash criterion returns a mechanical
    verdict about the candidate tree's own parent directory.
    """
    tree = tmp_path / "cand"
    (tree / "a").mkdir(parents=True)
    (tmp_path / "outside.py").write_text("def x():\n    return 1\n")
    for c in (_crit(kind="symbol", text="defines x", path=escaping, symbol="x"),
              _crit(kind="hash", text="unchanged", path=escaping, sha256="0" * 64)):
        r = coverage.evaluate(c, row_id="a1", index=0, tree=tree)
        assert (r.method, r.satisfied) == ("unresolved", None), f"{c.kind} {escaping}"


def test_an_intermediate_symlink_component_cannot_leave_the_tree(tmp_path):
    """The escape no string check can see: `lstat` declines to follow the FINAL component and
    says nothing about the ones before it."""
    tree = tmp_path / "cand"
    tree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "m.py").write_text("def x():\n    return 1\n")
    os.symlink(outside, tree / "link")
    r = coverage.evaluate(_crit(kind="symbol", text="link/m.py defines x",
                                path="link/m.py", symbol="x"),
                          row_id="a1", index=0, tree=tree)
    assert (r.method, r.satisfied) == ("unresolved", None)
    r2 = coverage.evaluate(_crit(kind="hash", text="link/m.py", path="link/m.py",
                                 sha256="0" * 64),
                           row_id="a1", index=0, tree=tree)
    assert (r2.method, r2.satisfied) == ("unresolved", None)


def test_there_is_no_schema_evaluator_and_it_says_so(tmp_path):
    r = coverage.evaluate(_schema_crit(), row_id="a1", index=0, tree=tmp_path)
    assert (r.method, r.satisfied) == ("unresolved", None)
    assert "no schema evaluator" in r.detail


def test_a_schema_criterion_carrying_a_runnable_query_is_still_unresolved(tmp_path):
    """§10.1 calls a schema query mechanical and `ledger.Criterion` records one, so the
    predicate's INPUTS are present while the predicate is not. The tempting fallthrough is
    `manual_trace_confirmed`, which would assert a human traced it — and the tempting shortcut
    is an evaluator that reports back what the record already said."""
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "schema.sql").write_text("CREATE TABLE users (seq INTEGER);\n")
    r = coverage.evaluate(_schema_crit(), row_id="a1", index=0, tree=tmp_path)
    assert r.method == "unresolved", \
        "a schema file that exists and satisfies the query still had no predicate run on it"


def test_prose_with_a_trace_is_manual_and_without_one_is_unresolved(tmp_path):
    traced = coverage.evaluate(_crit(kind="prose", text="stable seams exist",
                                     trace="walked both partitions by hand"),
                               row_id="a1", index=0, tree=tmp_path)
    assert (traced.method, traced.satisfied) == ("manual_trace_confirmed", None)
    bare = coverage.evaluate(_crit(kind="prose", text="stable seams exist"),
                             row_id="a1", index=0, tree=tmp_path)
    assert (bare.method, bare.satisfied) == ("unresolved", None)


def test_satisfied_is_none_whenever_the_method_is_not_mechanical(tmp_path):
    for c in (_crit(kind="prose", text="x", trace="t"), _schema_crit(), _crit(kind="prose",
                                                                              text="x")):
        r = coverage.evaluate(c, row_id="a1", index=0, tree=tmp_path)
        assert r.satisfied is None


def test_a_result_refuses_to_carry_an_answer_it_did_not_measure():
    """The invariant on the TYPE, not on the paths this module happens to take. §12.4's
    consumer builds `Result`s too, and `("manual_trace_confirmed", True)` there is a human's
    word wearing a measurement's shape."""
    with pytest.raises(coverage.CoverageError):
        coverage.Result("a1", 0, "manual_trace_confirmed", True, "a human said so")
    with pytest.raises(coverage.CoverageError):
        coverage.Result("a1", 0, "unresolved", False, "nobody looked, and the answer is no")
    with pytest.raises(coverage.CoverageError):
        coverage.Result("a1", 0, "checked", None, "not one of the three")
    assert coverage.Result("a1", 0, "mechanically_checked", False, "d").satisfied is False


def test_every_ledger_criterion_kind_has_a_declared_evaluator():
    """The allow-list, closed at both ends. A kind added to `ledger.CRITERION_KINDS` with no
    entry here would reach `evaluate`'s raise at RUN time, inside a fused run; this turns it
    into a suite failure at the moment the kind is added. An entry here for a kind the ledger
    does not admit is dead dispatch."""
    assert set(coverage._EVALUATORS) == set(ledger.CRITERION_KINDS)


def test_an_unknown_criterion_kind_raises_rather_than_acquiring_a_default(tmp_path):
    with pytest.raises(coverage.CoverageError):
        coverage.evaluate(_crit(kind="vibes", text="feels right"),
                          row_id="a1", index=0, tree=tmp_path)


@pytest.mark.parametrize("kind,missing", [("symbol", "symbol"), ("symbol", "path"),
                                          ("hash", "path"), ("test", "node_id")])
def test_a_criterion_missing_its_evaluator_inputs_is_refused_in_this_modules_class(
        tmp_path, kind, missing):
    """`ledger._check_criterion` refuses these at write, so one reaching here came from a
    `Ledger` built in process — which §12.2's partitioned synthesis does by construction.
    Without the guard, `Path(tree) / None` raised `TypeError` out of a public function: an
    error class no caller of this module knows to catch."""
    fields = dict(symbol=dict(path="m.py", symbol="x"), hash=dict(path="m.py",
                  sha256="0" * 64), test=dict(node_id="t.py::x"))[kind]
    fields[missing] = None
    with pytest.raises(coverage.CoverageError):
        coverage.evaluate(_crit(kind=kind, text="t", **fields),
                          row_id="a1", index=0, tree=tmp_path, pytest_argv=["pytest"])


def _row(rid, claim, status="accepted", **kw):
    base = dict(id=ledger.row_id(rid, claim), requirement_id=rid,
                requirement_span="s:1", requirement_sha256="0" * 64, kind="behavior",
                component="c", semantic_claim=claim, status=status, dependencies=(),
                seat_evidence=(), counterevidence="", acceptance_criteria=(_crit(),),
                synthesis_evidence=None, verification_receipt=None, risk="low", rationale="")
    base.update(kw)
    return ledger.Row(**base)


def _led(rows):
    return ledger.Ledger(ledger.VERSION, tuple(rows), 10,
                         ledger.DEGRADE_UNION_DIFF_BYTES, False)


def test_two_conflicting_rows_both_accepted_is_a_contradiction(tmp_path):
    a = _row("R1", "alpha")
    b = _row("R2", "beta")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "conflicts"),)})
    rep = coverage.check(_led([a2, b]), tree=tmp_path)
    assert any("conflict" in c for c in rep.contradictions)


def test_a_conflict_resolved_by_rejecting_one_side_is_not_a_contradiction(tmp_path):
    """The finding is that BOTH are accepted. A conflict with a rejected side is a run that
    made a decision, and reporting it would train a reader to skip the section."""
    a = _row("R1", "alpha")
    b = _row("R2", "beta", status="rejected")
    a2 = ledger.Row(**{**a.__dict__, "dependencies": (ledger.Dependency(b.id, "conflicts"),)})
    rep = coverage.check(_led([a2, b]), tree=tmp_path)
    assert not rep.contradictions


def test_an_accepted_row_over_a_unanimous_rejection_is_a_contradiction(tmp_path):
    """§10: if all three seats considered and rejected a cache layer, that is the most
    valuable signal in the run — and from-scratch synthesis would otherwise add it back."""
    ev = tuple(ledger.SeatEvidence(s, "contradicts", "no", None)
               for s in ("claude", "codex", "agy"))
    rep = coverage.check(_led([_row("R1", "add a cache layer", seat_evidence=ev)]),
                         tree=tmp_path)
    assert any("unanimous" in c for c in rep.contradictions)


def test_a_silent_seat_is_not_a_rejection(tmp_path):
    """Non-vacuity: a seat that said nothing did not reject."""
    ev = (ledger.SeatEvidence("claude", "contradicts", "no", None),
          ledger.SeatEvidence("codex", "contradicts", "no", None),
          ledger.SeatEvidence("agy", "silent", "", None))
    rep = coverage.check(_led([_row("R1", "add a cache layer", seat_evidence=ev)]),
                         tree=tmp_path)
    assert not any("unanimous" in c for c in rep.contradictions)


def test_one_seat_is_not_unanimity_and_no_seats_are_not_either(tmp_path):
    """`all(...)` over an empty sequence is True, so a row nobody weighed in on would be
    reported as accepted over a unanimous rejection — the vacuous truth the `< 2` guard
    exists for, and the same shape as the silent seat one branch over."""
    one = (ledger.SeatEvidence("claude", "contradicts", "no", None),)
    for ev in (one, ()):
        rep = coverage.check(_led([_row("R1", "add a cache layer", seat_evidence=ev)]),
                             tree=tmp_path)
        assert not any("unanimous" in c for c in rep.contradictions), ev


def test_the_report_separates_unsatisfied_from_unresolved(tmp_path):
    """The two axes, kept apart: 'checked and false' and 'nobody could check' are opposite
    facts, and §12.4 acts on only one of them."""
    (tmp_path / "m.py").write_text("x = 1\n")
    row = _row("R1", "alpha", acceptance_criteria=(
        _crit(kind="symbol", text="m.py defines gone", path="m.py", symbol="gone"),
        _schema_crit()))
    rep = coverage.check(_led([row]), tree=tmp_path)
    assert len(rep.results) == 2
    assert len(rep.unsatisfied) == 1 and len(rep.unresolved) == 1


def test_an_accepted_row_with_no_criteria_is_unresolved_not_silently_covered(tmp_path):
    """`ledger._decode` refuses an ABSENT `acceptance_criteria` key for exactly this reason,
    and cannot refuse a PRESENT-and-empty one: a rejected or deferred row has no acceptance
    criteria to write. So the other end of the hole is here — an accepted claim nobody wrote a
    criterion for produced zero results, and a row with zero results reads as a covered row."""
    rep = coverage.check(_led([_row("R1", "alpha", acceptance_criteria=())]), tree=tmp_path)
    assert len(rep.unresolved) == 1 and not rep.unsatisfied
    assert rep.results[0].criterion_index == coverage.NO_CRITERION


def test_a_rejected_row_with_no_criteria_is_not_reported(tmp_path):
    """Non-vacuity for the line above: it must not fire on every row that legitimately has no
    acceptance criteria, or the report grows a line per rejected claim and stops being read."""
    rep = coverage.check(_led([_row("R1", "alpha", status="rejected",
                                    acceptance_criteria=())]), tree=tmp_path)
    assert rep.results == () and rep.unresolved == ()


def test_check_hands_the_pytest_runner_to_the_predicate(tmp_path):
    """A `check` that dropped `pytest_argv` or `run` would report every test criterion
    `unresolved` — a whole run's mechanical coverage silently gone, under a green suite."""
    def script(argv):
        if "--collect-only" in argv:
            return 0, "tests/t.py::test_x\n"
        return 0, "1 passed"
    row = _row("R1", "alpha", acceptance_criteria=(
        _crit(kind="test", text="tests/t.py::test_x passes", node_id="tests/t.py::test_x"),))
    rep = coverage.check(_led([row]), tree=tmp_path, pytest_argv=["pytest"],
                         run=_fake_run(script))
    assert [(x.method, x.satisfied) for x in rep.results] == [("mechanically_checked", True)]


def test_check_refuses_a_value_that_is_not_a_ledger_in_this_modules_class(tmp_path):
    """`_contradictions` reads `d.relation` and `e.stance` off the nested lists, and a Ledger
    built in process can hold dicts there. `AttributeError` out of a public function is an
    error class no caller of this module knows to catch."""
    with pytest.raises(coverage.CoverageError):
        coverage.check({"rows": []}, tree=tmp_path)
    bad = _led([_row("R1", "alpha", dependencies=({"id": "x", "relation": "requires"},))])
    with pytest.raises(coverage.CoverageError):
        coverage.check(bad, tree=tmp_path)
