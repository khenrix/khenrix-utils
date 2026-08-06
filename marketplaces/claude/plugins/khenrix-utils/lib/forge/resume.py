"""Re-entering a run that stopped, without re-spending what it already paid for.

WHAT THIS RETIRES. `runner._refuse_a_second_pass` refused every second pass on a stated
premise: "the two values a second pass would need are `verify.Calibration` and
`bundle.CandidateBundle`, and nothing in this package serializes either." That was a true
statement about what had been WRITTEN and a false one about what was POSSIBLE. Every field of
a `CandidateBundle` is plain data, `bundle.dumps` now serializes it, and `runner._write`
persists one beside every seat record. The refusal was a missing function wearing the clothes
of an impossibility — the "a rule that holds only on the path its author remembered" shape
this package refuses everywhere else.

THE CALIBRATION IS RE-TAKEN, NOT RELOADED, AND THAT IS THE CHEAP HALF ON PURPOSE. §5 step 3
is one clone and the confirmed commands against a PINNED baseline commit; it spends no
provider call, which is the only thing §5.2 prices and the only thing this module exists to
protect. Reloading it would buy a few minutes and cost the guarantee that the calibration a
resumed run compares against was measured by the run that is doing the comparing.

WHAT IS RECONSTRUCTED AND WHAT IS RE-DRIVEN, which is the whole contract:

  - A seat with a settled record AND a persisted bundle whose Fwork binding still matches its
    preserved clone is RECONSTRUCTED. No provider call.
  - A seat with no settled record is RE-DRIVEN. It never produced anything, so there is
    nothing to re-spend.
  - A seat that settled but cannot be reconstructed exactly is REFUSED, and the whole resume
    refuses with it. Silently re-driving it would re-spend the provider call this module
    exists to protect, which is the one failure mode a resume must not have — and it would
    arrive looking like success.

THE BINDING IS WHAT MAKES THE RELOAD TRUSTWORTHY. Without `CandidateBundle.fwork` a
reconstructed seat would be "the bytes some earlier process wrote to this file", and a resume
built on that is a verifier taking its candidate on faith. With it, the reload is checked
against the snapshot the harvest measured, and a clone that moved since is named rather than
believed.
"""
from __future__ import annotations

from . import (bundle, harvest, journal as journalmod, runstate,
               seat as seatmod, seatrecord, storage, verify)


class ResumeError(RuntimeError):
    """A run this module will not continue on the evidence the run directory holds."""


_SEAT = "seat"


def settled_seats(events) -> dict:
    """Every seat whose journal shows an attempt that finished without a refusal.

    READ FROM THE JOURNAL AND NOT THE SEAT FILE, because the two answer different questions.
    The seat file is §14.2's deliverable and holds what the seat PRODUCED; the journal is the
    record of what this engine DID, and "was a provider call made and paid for" is a fact
    about the doing. A run killed between the provider returning and the record being written
    has a journal that says so and a seat file that does not exist.

    THE LAST ATTEMPT WINS. §8.1 retries, so one seat can hold several `done` records; the one
    that settled is the one whose outcome the run carried forward.
    """
    done = journalmod.done(_SEAT)
    out: dict[str, dict] = {}
    for e in events:
        if e.event != done:
            continue
        name = e.data.get("seat")
        if not isinstance(name, str):
            continue
        if e.data.get("refused") is not None:
            # A refused attempt is not a settled seat, and it must not ERASE one either: §8.1
            # can refuse attempt 2 after attempt 1 settled, and dropping the earlier record
            # here would send a paid seat back through the provider.
            continue
        out[name] = dict(e.data)
    return out


def reconstruct_candidate(run_dir, name: str, seat_path=None):
    """A seat's persisted `CandidateBundle`, proven against the clone it was harvested from.

    `seat_path=None` SKIPS THE PROOF AND SAYS SO BY RAISING. A caller with no clone to check
    against is asking this module to vouch for bytes on the strength of the filename they
    arrived under, and the answer is that it cannot. §8.1 preserves the clones precisely so
    this check has something to run against.
    """
    path = storage.seat_bundle_path(run_dir, name)
    if not path.is_file():
        why = path.with_suffix(".bundle-error")
        cause = why.read_text().strip() if why.is_file() else "no reason was recorded"
        raise ResumeError(
            f"seat {name!r} has no persisted candidate bundle at {path}, so continuing it "
            f"would mean re-spending its provider call: {cause}")
    try:
        cand = bundle.loads(path.read_bytes())
    except (OSError, bundle.BundleError) as e:
        raise ResumeError(f"seat {name!r}: its persisted bundle could not be read: {e}") from e
    if bundle.unbound(cand):
        # `fwork is None` is "nobody bound this", never "bound and clean". A resume that
        # accepted it would be reporting a proof it did not perform.
        raise ResumeError(
            f"seat {name!r}: its persisted bundle carries no Fwork binding, so nothing "
            "records what it should reproduce and its contents cannot be checked against the "
            "clone they were harvested from.")
    if seat_path is None:
        raise ResumeError(
            f"seat {name!r}: no clone was given to check its bundle against, and this module "
            "does not vouch for bytes on the strength of the path they were found under.")
    moved = bundle.verify_materialized(cand, seat_path)
    if moved:
        raise ResumeError(
            f"seat {name!r}: the preserved clone at {seat_path} no longer holds the bytes its "
            f"harvest measured, so its persisted bundle cannot be shown to describe it: "
            + "; ".join(moved))
    return cand


def plan(run_dir, repo, names, *, seat_dir) -> tuple[dict, tuple[str, ...]]:
    """`(reconstructed, to_drive)` — what this resume will reload and what it must re-drive.

    COMPUTED WHOLE, BEFORE ANYTHING IS SPENT. Every seat is classified first and the refusals
    are collected together, so an operator learns about all of them at once rather than after
    a clone and two provider calls have gone into a run that was going to refuse anyway. That
    is `gate.quote`'s own discipline — price the run before taking it — applied to the resume.

    `seat_dir` IS INJECTED RATHER THAN IMPORTED. `runner.seat_dir` is the one place that
    knows where a seat's attempt clone lives, and importing it here would make `runner` and
    `resume` mutually dependent — `runner` is what calls this. Passing it keeps the
    single spelling of that path without the cycle.
    """
    recon = runstate.reconstruct(run_dir, repo)
    if recon.diverged:
        # §9's rule reaches a resume UNCHANGED, and this branch deliberately does not loosen
        # it: a repository that moved makes every question below moot, and a candidate
        # harvested from the old one is not a candidate for this tree.
        #
        # THE MESSAGE IS SPLIT BECAUSE THE TWO CAUSES ARE NOT THE SAME PROBLEM. A run that
        # already transported its seats and cut a synthesis branch has REFS UNDER ITS OWN RUN
        # ID that the manifest could not have declared — `drift`'s docstring is explicit that
        # a ref this manifest does not name "was never forge's" and lands in the new-ref case.
        # That is a completed handover, not the user's tree moving underneath the run, and an
        # operator told the latter would go looking for a change nobody made.
        own = [d for d in recon.diverged if recon.manifest.run_id in d]
        if len(own) == len(recon.diverged):
            raise ResumeError(
                f"run {recon.manifest.run_id} has already transported its seats and cut its "
                f"synthesis branch ({', '.join(own)}), so its fleet is finished and there is "
                "nothing left to resume. Fuse in the synthesis worktree and run `--collect` "
                "instead.")
        raise ResumeError(
            f"the repository this run was opened against has moved: {recon.diverged}. "
            "Nothing that was harvested from it describes the tree you have now.")
    events = journalmod.Journal(storage.journal_path(run_dir)).read()
    settled = settled_seats(events)
    reconstructed, to_drive, refusals = {}, [], []
    for name in names:
        record = settled.get(name)
        if record is None:
            # NEVER SETTLED, SO THERE IS NOTHING TO RE-SPEND. This is the ordinary resume
            # case — the run died with seats still owed — and it is the only branch that
            # reaches a provider at all.
            to_drive.append(name)
            continue
        attempt = record.get("attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            refusals.append(
                f"seat {name!r}: its settled record carries attempt={attempt!r}, so which "
                "clone it was harvested from is not recorded and its bundle cannot be "
                "checked against one")
            continue
        seat_record = runstate.read_seat(run_dir, name)
        if seat_record is None:
            refusals.append(
                f"seat {name!r}: the journal records a settled attempt but there is no seat "
                "file, so what it produced is not on disk anywhere")
            continue
        try:
            seatrecord.decode(seat_record)
        except seatrecord.SeatRecordError as e:
            refusals.append(f"seat {name!r}: its record does not decode: {e}")
            continue
        try:
            reconstructed[name] = reconstruct_candidate(
                run_dir, name, seat_dir(run_dir, name, attempt))
        except ResumeError as e:
            refusals.append(str(e))
    if refusals:
        raise ResumeError(
            "this run cannot be resumed without re-spending provider calls it already made, "
            "and re-driving those seats silently is the one thing a resume must not do:\n  - "
            + "\n  - ".join(refusals))
    return reconstructed, tuple(to_drive)


def seat_result(run_dir, name: str, candidate, *, make, seat_dir):
    """A `runner.SeatResult` for a seat this resume did NOT re-drive.

    EVERYTHING IT CARRIES COMES OFF THE RECORD, and that is the point rather than an economy.
    `runner._write` rewrites the seat file from whatever `SeatResult` reaches it, so a
    reconstruction that left `status`, `setup_run` or the launch fields empty would REPLACE a
    complete record with a thinner one — the resume would destroy the very evidence it exists
    to preserve, and the run would end holding less than it did before it was resumed.

    `make` IS `runner.SeatResult` INJECTED, for `plan`'s reason: `runner` calls this module,
    so importing it here would close a cycle.

    THE ATTEMPT IS THE ONE THAT SETTLED, not the highest one recorded. §8.1 can refuse an
    attempt after an earlier one settled, and rebuilding from the last entry would describe a
    clone that produced nothing.
    """
    record = runstate.read_seat(run_dir, name)
    if record is None:
        raise ResumeError(f"seat {name!r}: no seat file to reconstruct from")
    attempts = record.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ResumeError(f"seat {name!r}: its record carries no attempts")
    settled = [a for a in attempts
               if isinstance(a, dict) and isinstance(a.get("status"), dict)
               and a["status"].get("forge") != "failed"]
    if not settled:
        raise ResumeError(
            f"seat {name!r}: no attempt in its record settled on a candidate, so there is "
            "nothing here to carry forward")
    a = settled[-1]
    st = a["status"]
    sr = a.get("setup_run")
    run = None if not isinstance(sr, dict) else verify.Run(
        exit_code=sr.get("exit_code"), stdout=sr.get("stdout") or "",
        stderr=sr.get("stderr") or "", duration_sec=sr.get("duration_sec") or 0.0,
        step_index=sr.get("step_index") or 0)
    arts = a.get("artifacts") or {}
    return make(
        name=name,
        attempt=a.get("attempt"),
        # `seat` IS None DELIBERATELY: a `fleet.Seat` is the handle on a clone this resume did
        # not create, and fabricating one would let a later caller act on it. `_record` reads
        # only `.branch` from it and takes None, so the branch is carried below instead.
        seat=None,
        status=seatmod.Status(
            process=st.get("process"), artifacts=st.get("artifacts"),
            proven_read=st.get("proven_read"), forge=st.get("forge"),
            builder_setup=st.get("builder_setup"), verify=st.get("verify")),
        artifacts=harvest.ArtifactSet(
            paths=tuple(arts.get("paths") or ()),
            origin=dict(arts.get("origin") or {}),
            setup_overlap=tuple(arts.get("setup_overlap") or ()),
            # THE PATCH IS THE BUNDLE'S, NOT THE RECORD'S. `_record` stores the tracked diff
            # by LENGTH only, and `bundle.build` derives the patch from this string — so the
            # bundle's bytes are the only surviving copy, and this is their exact inverse
            # (`build` encodes with surrogateescape; nothing else round-trips a git patch).
            tracked_diff=candidate.tracked_patch.decode("utf-8", "surrogateescape"),
            verify_overlap=tuple(arts.get("verify_overlap") or ()),
            fwork=candidate.fwork),
        candidate=candidate,
        run=run,
        path=seat_dir(run_dir, name, a.get("attempt")),
        token=a.get("sentinel") or "",
        launch_result=a.get("launch"))
