"""§13's post-review fix, verified rather than authored."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from forge import fix, journal, storage, verify  # noqa: E402
from forge_fixtures import commit_all, make_repo, write  # noqa: E402


class _Manifest:
    setup = verify.Command.parse([["true"]]).steps
    verify = verify.Command.parse([["true"]]).steps
    generator_contract = ()


def _run(exit_code=0, stdout="", stderr=""):
    return verify.Run(exit_code=exit_code, stdout=stdout, stderr=stderr,
                      duration_sec=0.1, step_index=0)


def _synth(tmp_path):
    """A run directory with a synthesis worktree that has one commit."""
    run = tmp_path / "run"; run.mkdir()
    s = run / "synthesis"
    repo = make_repo(tmp_path, "synthesis-src")
    subprocess.run(["cp", "-r", str(repo), str(s)], check=True, capture_output=True)
    write(s, "a.py", "print('a')\n")
    commit_all(s, "base")
    head = subprocess.run(["git", "-C", str(s), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    return run, s, head


def _cal_event(tmp_path, *, stdout, stderr="", exit_code=1, clipped=False):
    run = tmp_path / "j"; run.mkdir(exist_ok=True)
    log = journal.Journal(storage.journal_path(run))
    log.record(journal.intent("calibrate"), operation_id="op")
    log.record(journal.done("calibrate"), operation_id="op", exit_code=exit_code,
               setup_exit_code=0, unexplained=[],
               stdout=stdout, stdout_chars=len(stdout) + (99 if clipped else 0),
               stderr=stderr, stderr_chars=len(stderr))
    return log.read()


# ---- baseline_run ---------------------------------------------------------------------
def test_the_baseline_run_is_recovered_from_the_journal(tmp_path):
    """§5 step 3 runs the baseline gate ONCE, hours before any review, in a clone `--gc`
    reclaims — so the calibration record is the only place its output survives to §13, and
    without it the baseline half of every §12.3 progress question is unanswerable."""
    ev = _cal_event(tmp_path, stdout="1 failed in 0.1s\nFAILED t.py::a - E\n")
    r = fix.baseline_run(ev)
    assert r is not None and r.exit_code == 1 and "FAILED t.py::a" in r.stdout


def test_a_clipped_calibration_record_is_refused_rather_than_parsed(tmp_path):
    """THE FAIL-OPEN THIS MUST NOT HAVE. `_clip` keeps the TAIL, which is the right half —
    pytest prints its summary last — but a `FAILED` line that scrolled past the cap is one
    nobody can see, so the fingerprint set would be a LOWER BOUND. `pytest_fingerprints`
    refuses a partial set; handing it one through a different door is the same fail-open."""
    ev = _cal_event(tmp_path, stdout="FAILED t.py::a - E\n", clipped=True)
    assert fix.baseline_run(ev) is None


def test_a_record_with_no_output_fields_is_none_not_an_empty_run(tmp_path):
    """An empty `Run` would parse as "the baseline failed nothing", which compares as
    progress against every candidate — the subset-of-everything shape one module over."""
    run = tmp_path / "j2"; run.mkdir()
    log = journal.Journal(storage.journal_path(run))
    log.record(journal.done("calibrate"), operation_id="op", exit_code=1)
    assert fix.baseline_run(log.read()) is None


# ---- apply ----------------------------------------------------------------------------
def test_a_head_that_never_moved_is_no_fix_and_costs_no_clone(tmp_path):
    """THE EXTERNAL QUESTION: was a clone built for a round in which nobody fixed anything?
    An orchestrator handed blockers who commits nothing is a legitimate outcome — §13's
    finding-reported-unresolved — and it must not cost a full §6 verifier pass to say so."""
    run, _s, head = _synth(tmp_path)
    built = []

    def _build(*a, **kw):
        built.append(a)
        raise AssertionError("a clone was built for a round with no fix in it")

    out = fix.apply(run, tmp_path, findings=(), checkpoint=head, manifest=_Manifest(),
                    identity=("F", "f@e.x"), build=_build)
    assert out == (None, False, None, None), out
    assert built == []


def test_the_fix_is_verified_in_a_clone_and_never_in_the_authors_tree(tmp_path):
    """§6, and it is the founding premise: the synthesis worktree is the tree the fix was
    WRITTEN in, so verifying there is the check its own author could have rigged."""
    run, s, head = _synth(tmp_path)
    write(s, "b.py", "print('b')\n")
    commit_all(s, "the fix")
    seen = {}

    class _V:
        path = run / "elsewhere"

    def _build(repo, baseline, candidate, dest, **kw):
        seen["dest"] = Path(dest)
        return _V()

    # `run_setup` and `fixed_point` are stubbed because THIS test's claim is about WHERE the
    # verification is built, not about what the gate said — running the real ones against a
    # stub verifier would fail for a reason this test is not asking about.
    import forge.verify as V
    monkey = pytest.MonkeyPatch()
    monkey.setattr(V, "run_setup", lambda v, c, **k: V.SetupResult(run=_run(0), overlap=()))
    monkey.setattr(V, "fixed_point", lambda *a, **k: V.FixedPoint(
        run=_run(0, stdout="3 passed in 0.1s\n"), admitted=(), unexplained=()))
    try:
        fix.apply(run, tmp_path, findings=(), checkpoint=head, manifest=_Manifest(),
                  identity=("F", "f@e.x"), build=_build,
                  events=_cal_event(tmp_path,
                                    stdout="1 failed in 0.1s\nFAILED t.py::a - E\n"))
    finally:
        monkey.undo()
    assert seen["dest"] != s, "the fix was verified in the tree that authored it"
    assert seen["dest"].parent == run


def test_a_fix_that_fails_the_gate_yields_no_checkpoint(tmp_path, monkeypatch):
    """§13: a fix that breaks verify is reverted and the finding reported unresolved. A
    checkpoint handed back for a red gate would let `loop` advance `current` onto it."""
    run, s, head = _synth(tmp_path)
    write(s, "b.py", "x\n"); commit_all(s, "the fix")

    class _V:
        path = run / "v"

    monkeypatch.setattr(verify, "run_setup", lambda v, c, **k: verify.SetupResult(run=_run(0), overlap=()))
    monkeypatch.setattr(verify, "fixed_point", lambda *a, **k: verify.FixedPoint(
        run=_run(exit_code=1, stdout="1 failed in 0.1s\nFAILED t.py::a - E\n"),
        admitted=(), unexplained=()))
    cp, verified, cand, base = fix.apply(
        run, tmp_path, findings=(), checkpoint=head, manifest=_Manifest(),
        identity=("F", "f@e.x"), build=lambda *a, **k: _V(),
        events=_cal_event(tmp_path, stdout="2 failed in 0.1s\nFAILED t.py::a - E\n"))
    assert cp is None and verified is False
    assert cand is not None and base is not None


def test_a_verifier_setup_that_failed_is_not_a_fix_that_failed(tmp_path, monkeypatch):
    """The engine's failure and the author's are two facts. Reporting a harness that could
    not be built as an unverified fix attributes one to the other — the collapse
    `runner.SETUP_REFUSED` exists to prevent one module over."""
    run, s, head = _synth(tmp_path)
    write(s, "b.py", "x\n"); commit_all(s, "the fix")

    class _V:
        path = run / "v"

    monkeypatch.setattr(verify, "run_setup", lambda v, c, **k: verify.SetupResult(run=_run(3, stderr="boom"), overlap=()))
    with pytest.raises(fix.FixError, match="engine's failure"):
        fix.apply(run, tmp_path, findings=(), checkpoint=head, manifest=_Manifest(),
                  identity=("F", "f@e.x"), build=lambda *a, **k: _V())


def test_both_runs_or_neither_is_handed_back(tmp_path, monkeypatch):
    """`from_runs` says a baseline whose output cannot be read makes "new" unanswerable and
    the whole tuple unknown — so a candidate beside a missing baseline would invite a
    half-measured pair, and `oscillation`'s rule is that such a sighting forms no pair."""
    run, s, head = _synth(tmp_path)
    write(s, "b.py", "x\n"); commit_all(s, "the fix")

    class _V:
        path = run / "v"

    monkeypatch.setattr(verify, "run_setup", lambda v, c, **k: verify.SetupResult(run=_run(0), overlap=()))
    monkeypatch.setattr(verify, "fixed_point", lambda *a, **k: verify.FixedPoint(
        run=_run(exit_code=0, stdout="3 passed in 0.1s\n"), admitted=(), unexplained=()))
    cp, verified, cand, base = fix.apply(
        run, tmp_path, findings=(), checkpoint=head, manifest=_Manifest(),
        identity=("F", "f@e.x"), build=lambda *a, **k: _V(), events=())
    assert verified is True and cp == _head_of(s)
    assert (cand, base) == (None, None), "a candidate was handed back with no baseline"


def _head_of(s):
    return subprocess.run(["git", "-C", str(s), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
