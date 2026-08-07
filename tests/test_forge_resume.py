"""Re-entering a run without re-spending it — and refusing when it cannot be proven."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402
from forge import (baseline, bundle, fleet, harvest,  # noqa: E402
                   inspect as finspect, journal, resume, runner as runnermod,
                   runstate, snapshot, storage)
from forge_fixtures import make_repo  # noqa: E402
from test_forge_runner import _open  # noqa: E402

IDENT = ("Forge Seat", "seat@forge.invalid")


def _seat_with_bundle(tmp_path, name="claude"):
    """A run directory holding one seat: its clone, its record, and its persisted bundle."""
    repo, run, b, _m = _open(tmp_path)
    clone = run / f"seat-{name}" / "attempt-1"
    clone.parent.mkdir(parents=True)
    st = fleet.clone_seat(repo, b, clone, name=name, identity=IDENT)
    (st.path / "a.txt").write_text("what the builder wrote\n")
    f0, _ = snapshot.take(st.path)
    fsetup = dict(f0)
    (st.path / "a.txt").write_text("what the builder wrote, revised\n")
    fwork, _ = snapshot.take(st.path)
    phases = harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=fwork)
    arts = harvest.artifact_set(phases, st.path, b.commit)
    cand = bundle.build(st.path, arts, b)
    storage.seat_bundle_path(run, name).write_bytes(bundle.dumps(cand))
    return run, repo, st.path, cand


def _seat_dir(run_dir, name, attempt):
    return Path(run_dir) / f"seat-{name}" / f"attempt-{attempt}"


def _journal_a_settled_seat(run, name="claude", attempt=1, **extra):
    log = journal.Journal(storage.journal_path(run))
    op = f"{name}-attempt-{attempt}"
    log.record(journal.intent("seat"), operation_id=op, seat=name, attempt=attempt)
    log.record(journal.done("seat"), operation_id=op, seat=name, attempt=attempt,
               forge="candidate", **extra)
    return log.read()


# ---- settled_seats ---------------------------------------------------------------------
def test_a_refused_attempt_is_not_a_settled_seat(tmp_path):
    """A seat whose every attempt was refused made no candidate, so a resume owes it a
    provider call — reading a refusal as a settlement would drop it silently."""
    run = tmp_path / "r"; run.mkdir()
    log = journal.Journal(storage.journal_path(run))
    log.record(journal.intent("seat"), operation_id="op", seat="claude", attempt=1)
    log.record(journal.done("seat"), operation_id="op", seat="claude", attempt=1,
               refused="the clone would not open")
    assert resume.settled_seats(log.read()) == {}


def test_a_refusal_after_a_settlement_does_not_erase_it(tmp_path):
    """THE EXTERNAL QUESTION: would this seat be sent back through the provider? §8.1 can
    refuse attempt 2 after attempt 1 settled, and a reader that let the last record win
    unconditionally would re-spend a call that was already paid for."""
    run = tmp_path / "r"; run.mkdir()
    log = journal.Journal(storage.journal_path(run))
    log.record(journal.intent("seat"), operation_id="a1", seat="claude", attempt=1)
    log.record(journal.done("seat"), operation_id="a1", seat="claude", attempt=1,
               forge="candidate")
    log.record(journal.intent("seat"), operation_id="a2", seat="claude", attempt=2)
    log.record(journal.done("seat"), operation_id="a2", seat="claude", attempt=2,
               refused="transient")
    settled = resume.settled_seats(log.read())
    assert "claude" in settled and settled["claude"]["attempt"] == 1


# ---- reconstruct_candidate -------------------------------------------------------------
def test_a_persisted_bundle_is_reloaded_and_proven_against_its_clone(tmp_path):
    """The ordinary resume case: the bytes come back identical AND are shown to describe the
    preserved clone, which is the claim that makes reloading them safe at all."""
    run, repo, seat, cand = _seat_with_bundle(tmp_path)
    got = resume.reconstruct_candidate(run, "claude", seat)
    assert got == cand


def test_a_clone_that_moved_since_the_harvest_is_refused_not_reloaded(tmp_path):
    """THE POINT OF THE BINDING. Without it a resume reloads bytes and calls them the seat's
    candidate on the strength of the filename. Here the clone is edited after the fact and the
    reload refuses, naming the path."""
    run, repo, seat, _ = _seat_with_bundle(tmp_path)
    (seat / "a.txt").write_text("edited long after the run died\n")
    with pytest.raises(resume.ResumeError, match="no longer holds the bytes"):
        resume.reconstruct_candidate(run, "claude", seat)


def test_an_unbound_bundle_is_refused_rather_than_taken_on_faith(tmp_path):
    """`fwork is None` is "nobody bound this". Accepting it would let the resume report a
    proof it never performed — a verdict reading cleaner than its evidence."""
    run, repo, seat, cand = _seat_with_bundle(tmp_path)
    from dataclasses import replace
    storage.seat_bundle_path(run, "claude").write_bytes(
        bundle.dumps(replace(cand, fwork=None)))
    with pytest.raises(resume.ResumeError, match="no Fwork binding"):
        resume.reconstruct_candidate(run, "claude", seat)


def test_a_missing_bundle_carries_the_reason_it_is_missing(tmp_path):
    """An absence that cannot say why is the "could not look" that reads as "nothing found".
    `runner._write` drops a `.bundle-error` beside where the bundle would have been."""
    run, repo, seat, _ = _seat_with_bundle(tmp_path)
    storage.seat_bundle_path(run, "claude").unlink()
    storage.seat_bundle_path(run, "claude").with_suffix(".bundle-error").write_text(
        "the disk was full\n")
    with pytest.raises(resume.ResumeError, match="the disk was full"):
        resume.reconstruct_candidate(run, "claude", seat)


def test_no_clone_to_check_against_is_a_refusal_not_a_pass(tmp_path):
    """A caller with nothing to compare against is asking this module to vouch for bytes by
    filename. Returning the bundle there would make the proof optional in practice."""
    run, repo, seat, _ = _seat_with_bundle(tmp_path)
    with pytest.raises(resume.ResumeError, match="does not vouch"):
        resume.reconstruct_candidate(run, "claude", None)


# ---- plan ------------------------------------------------------------------------------
def _make_seat_record(run, name="claude"):
    """The §14.2 seat file the journal's settled record implies."""
    from forge import seatrecord
    payload = {"name": name, "attempts": [{
        "attempt": 1, "forge": "candidate", "process": "ok", "artifacts": "present",
        "proven_read": True, "changed": True, "builder_setup": "ok",
    }]}
    try:
        seatrecord.decode(payload)
    except Exception:
        # The suite's job is the resume's classification, not seatrecord's schema; if the
        # minimal payload is not decodable, the real writer's shape is used instead.
        return None
    runstate.write_seat(run, name, payload)
    return payload


def test_a_seat_that_never_settled_is_re_driven_and_costs_a_provider_call(tmp_path):
    """THE ORDINARY RESUME. The run died with a seat still owed; it produced nothing, so
    there is nothing to re-spend and it is the only branch that reaches a provider."""
    run, repo, seat, _ = _seat_with_bundle(tmp_path)
    runstate.reconstruct(run, repo)   # the run directory is readable as a run
    got, to_drive = resume.plan(run, repo, ("gemini",), seat_dir=_seat_dir)
    assert got == {} and to_drive == ("gemini",)


def test_a_settled_seat_with_no_seat_file_refuses_the_whole_resume(tmp_path):
    """FAIL CLOSED, AND LOUDLY. The journal says a provider call was made and paid for while
    nothing on disk says what it produced. Re-driving it would silently spend that call a
    second time — the one failure mode a resume must not have."""
    run, repo, seat, _ = _seat_with_bundle(tmp_path)
    _journal_a_settled_seat(run)
    with pytest.raises(resume.ResumeError, match="no seat file"):
        resume.plan(run, repo, ("claude",), seat_dir=_seat_dir)


def test_a_settled_record_with_no_attempt_number_cannot_name_its_clone(tmp_path):
    """The attempt is what says WHICH clone the bundle was harvested from. Without it there is
    nothing to check the bundle against, and an unchecked reload is the faith this module
    refuses everywhere else."""
    run, repo, seat, _ = _seat_with_bundle(tmp_path)
    log = journal.Journal(storage.journal_path(run))
    log.record(journal.intent("seat"), operation_id="op", seat="claude")
    log.record(journal.done("seat"), operation_id="op", seat="claude", forge="candidate")
    with pytest.raises(resume.ResumeError, match="attempt="):
        resume.plan(run, repo, ("claude",), seat_dir=_seat_dir)


def test_every_refusal_is_reported_at_once_rather_than_one_clone_in(tmp_path):
    """`gate.quote`'s discipline applied to the resume: price it before taking it. An operator
    with two broken seats learns about both now, not after the first one aborted the run."""
    run, repo, seat, _ = _seat_with_bundle(tmp_path)
    log = journal.Journal(storage.journal_path(run))
    for n in ("claude", "codex"):
        log.record(journal.intent("seat"), operation_id=n, seat=n, attempt=1)
        log.record(journal.done("seat"), operation_id=n, seat=n, attempt=1, forge="candidate")
    with pytest.raises(resume.ResumeError) as ei:
        resume.plan(run, repo, ("claude", "codex"), seat_dir=_seat_dir)
    text = str(ei.value)
    assert "claude" in text and "codex" in text, text


def test_a_resumed_run_drives_its_owed_seats_at_the_width_it_was_quoted(tmp_path,
                                                                        monkeypatch):
    """THE EXTERNAL QUESTION: does a resume honour the fleet width the operator PAID for?

    `--resume` re-reads the run off disk, and `concurrency` is on the manifest for the same
    reason `seats` is — §5.2 priced the wall clock BY it. A resume that silently fell back to
    serial would take up to `concurrency`x the quoted ceiling; one that widened past the
    recorded value would take a contention risk nobody agreed to.
    """
    import threading
    from test_forge_runner import _open, _per_seat, IDENT
    repo, run, b, m = _open(tmp_path, seats=3, attempts=1, concurrency=3)
    assert runstate.read_manifest(run).concurrency == 3, "the fixture did not record a width"

    barrier = threading.Barrier(3, timeout=60)
    inner = _per_seat(lambda name, n, path: True)
    peak, live, lock = [0], [0], threading.Lock()

    def _launch(**kw):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        barrier.wait()
        with lock:
            live[0] -= 1
        return inner(**kw)

    # No seat has settled, so a resume owes all three — and must drive them at width 3.
    out = runnermod.run(run, repo, identity=IDENT, launch=_launch, resume=True)
    assert peak[0] == 3, f"the resume drove {peak[0]} builder(s) at once, not 3"
    assert len(out) == 3
