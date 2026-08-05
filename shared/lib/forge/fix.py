"""§13's post-review fix, as a VERIFICATION rather than an authorship.

WHAT THIS MODULE DOES NOT DO, AND WHY THAT IS THE WHOLE DESIGN. It does not edit the
synthesis tree. §16 makes the ORCHESTRATOR the synthesis author and `SKILL.md` says the
engine "deliberately has no `--synthesize`: it stops with the candidates verified on disk,
hands you a synthesis worktree, and waits. Picking a winner is the thing this skill exists
not to do." Applying a blocker's fix is authoring code, and an engine that authored it would
then be verifying its own work — which is the founding premise inverted: a check the builder
could have rigged is not a check.

So the orchestrator fixes the blockers and commits in the synthesis worktree, and this
module answers the only question §13 leaves for the engine: DID THAT FIX SURVIVE THE GATE,
measured in a clone the author never touched.

THE CONTRACT IS `review.loop`'s, exactly. `apply` returns
`(new_checkpoint | None, verified: bool, candidate_run | None, baseline_run | None)`.
`loop` refuses anything else — a 2-tuple padded with `None`s would make "this fix measured
no run" and "this fix predates the contract" the same record, and only the first is an
answer.

`None` FOR BOTH RUNS IS AN ANSWER AND IS NOT A FAILURE. §12.3's progress question needs the
candidate's gate output AND the baseline's; when the baseline's cannot be read this returns
`None` for both rather than half a pair, because `from_runs` says a baseline whose output
cannot be read makes "new" unanswerable and the whole tuple unknown.
"""
from __future__ import annotations

from pathlib import Path

from . import gitcmd, journal as journalmod, verify


class FixError(RuntimeError):
    """A post-review fix this module will not report on the evidence it was given."""


_CALIBRATION = "calibrate"


def baseline_run(events) -> verify.Run | None:
    """The calibration's gate run, off the journal, or `None` because it cannot be trusted.

    THE ONLY COPY. §5 step 3 runs the confirmed commands on the untouched baseline once,
    hours before any review, in a clone `--gc` reclaims — so the record `runner._calibrate`
    writes is the only place its output survives to §13.

    `None` HAS THREE CAUSES AND ALL THREE ARE THE SAME ANSWER HERE: no calibration record, a
    record predating the fields, or a record whose output was CLIPPED. The third is the one
    worth stating: `_clip` keeps the tail, which is the right half — pytest prints its summary
    last — but a `FAILED` line that scrolled past the cap is one nobody can see, so the
    fingerprint set would be a LOWER BOUND. `progress.pytest_fingerprints` refuses a partial
    set for exactly this reason, and handing it one anyway through a different door would be
    the same fail-open with an extra step.
    """
    done = journalmod.done(_CALIBRATION)
    for e in reversed(list(events)):
        if e.event != done:
            continue
        d = e.data
        out, err = d.get("stdout"), d.get("stderr")
        if not isinstance(out, str) or not isinstance(err, str):
            return None
        if d.get("stdout_chars") != len(out) or d.get("stderr_chars") != len(err):
            # Clipped: the recorded text is shorter than what the run really produced.
            return None
        code = d.get("exit_code")
        if not isinstance(code, int) or isinstance(code, bool):
            return None
        return verify.Run(exit_code=code, stdout=out, stderr=err,
                          duration_sec=0.0, step_index=0)
    return None


def apply(run_dir, repo, *, findings, checkpoint: str, manifest, identity,
          events=(), build=verify.build_verifier, baseline=None) -> tuple:
    """Verify the fix the orchestrator committed, and answer `loop`'s four-value contract.

    ORDER, AND WHAT EACH STEP MAKES TRUE. The tree is read before anything is built, so a
    round in which nobody fixed anything costs no clone: `checkpoint` is the commit the round
    was convened on, and a synthesis HEAD still sitting on it is an orchestrator who was
    handed blockers and committed nothing. That is `(None, False, …)` — §13's "a fix that
    breaks verify is reverted and the finding reported unresolved" reached by the shorter
    route, and `loop` records the blockers unresolved and stops.

    THE VERIFICATION IS IN A FRESH CLONE THE AUTHOR NEVER TOUCHED, which is §6 and is not
    negotiable here: the synthesis worktree is the tree the fix was written in, and verifying
    a fix in the tree that authored it is the check its own author could have rigged. `build`
    is injected so the suite can exercise this without cloning, and every test passes a fake —
    §6's real path costs a clone and a full setup, and a suite that spends them is one nobody
    runs.
    """
    if not isinstance(checkpoint, str) or not checkpoint:
        raise FixError(f"a fix is verified against the checkpoint its round was convened on, "
                       f"not {checkpoint!r}")
    synth = Path(run_dir) / "synthesis"
    if not synth.is_dir():
        raise FixError(
            f"there is no synthesis worktree at {synth}, so there is no fix to verify. §16 "
            "makes the orchestrator the author and this module only measures what they "
            "committed.")
    head = gitcmd.git(synth, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                      "rev-parse", "--verify", "HEAD", env_extra=gitcmd.READONLY).stdout.strip()
    if head == checkpoint:
        # NOTHING WAS FIXED, and this costs no clone to say. Reported as §13's unverified
        # branch rather than as an error: the orchestrator declining to fix a blocker is a
        # legitimate outcome that leaves the finding unresolved, not a malformed call.
        return None, False, None, None

    base = baseline_run(events)
    v = build(repo, baseline, None, Path(run_dir) / f"fix-{checkpoint[:12]}",
              identity=identity, contract=manifest.generator_contract)
    # `SetupResult` WRAPS A `Run`; it has no `exit_code` of its own. A first draft read
    # `setup_result.exit_code` and would have raised `AttributeError` out of a module whose
    # whole job is keeping the engine's failures apart from the author's.
    setup_result = verify.run_setup(v, verify.Command(steps=manifest.setup))
    if setup_result.run.exit_code != 0:
        # A VERIFIER SETUP THAT FAILED IS NOT A CANDIDATE THAT FAILED. §6 calls the
        # per-candidate setup essential, and a fix reported unverified because the harness
        # could not be built would blame the author for the engine — the same collapse
        # `runner.SETUP_REFUSED` exists to keep apart one module over.
        raise FixError(
            f"the verifier setup for this fix exited {setup_result.run.exit_code}, so no "
            "verdict "
            "about the fix was reached. This is the engine's failure and not the fix's, and "
            "reporting it as an unverified fix would attribute one to the other.")
    # `fixed_point`, WHICH IS WHAT §6.1 RUNS — there is no `run_gate`; a first draft of this
    # module guessed one behind a `hasattr` and would have silently taken the other branch
    # forever. It re-runs the gate until the tree stops moving, which is the pass §6.1 needs
    # before a verdict means anything.
    cand = verify.fixed_point(v.path, verify.Command(steps=manifest.verify),
                              manifest.generator_contract).run
    verified = cand.exit_code == 0
    # BOTH RUNS OR NEITHER. `from_runs` says a baseline whose output cannot be read makes
    # "new" unanswerable and the whole tuple unknown, so handing `loop` a candidate beside a
    # missing baseline would invite a half-measured pair — and `oscillation`'s rule is that a
    # sighting with unmeasured fingerprints forms no pair at all.
    return (head if verified else None), verified, (cand if base else None), base
