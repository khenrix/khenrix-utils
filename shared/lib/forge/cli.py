"""The front end: `--start`, `--collect`, `--gc`.

WHAT THIS IS AND IS NOT. `--start` drives §5's gate, §7's fleet and §6's verification, and it
STOPS where `runner.run` stops — at `comparing`, with the candidates verified on disk and
nothing choosing between them. The choosing is §10 through §13, and §16 says who does it: "the
synthesis author is the trusted invoking orchestrator under its normal approval boundary — not
a fourth unattended bypass-permissions seat." That orchestrator is the agent running SKILL.md,
not this module, which is why there is no `--synthesize`.

`--collect` is the other half: it reads a run back off the disk, runs §13.1's deep review
once, decides §16's mergeability and prints the handover. §14 makes "always from disk and
never from conversation state" a requirement rather than a style, so `--collect` takes a run id
and a repository and nothing else.

IT DOES NOT MEASURE THE SYNTHESIS, and the header it prints says so in words. Verifying the
fusion would be a fourth §6 pass — a verifier clone, a setup and a gate run — that §5.2 never
quoted, so a `--collect` that took it would spend money the operator did not approve. The
verdict therefore arrives from argv as a REPORT, `Provenance.synthesis_measured` is the
constant `False`, and `handover.text` prints "who said so" rather than §16.1's "Verified here
means" paragraph. There is deliberately no flag that flips that constant.

NOTHING HERE INVENTS A TIMEOUT. §19 forbids a second timeout mechanism; the seat window is
`council.engine.MODE_TIMEOUT["forge"]` and there is no `--timeout`.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import shutil
import sys
from pathlib import Path

from council import engine

from . import (brief as briefmod, fingerprint, gate, gitcmd, handover, journal, launch,
               preflight, review as reviewmod, runstate, snapshot, storage, taskbundle,
               deepreview, verify)
# `ledger` and NOTHING ELSE: this verb PERSISTS §10's rows and reads nothing off
# them, so `coverage` and `rubric` stay out of this import block until something
# ranks — a rank taken before §13's panel can be convened honestly would be a
# verdict with no adversary.
from . import journal as journalmod
from . import runner as runnermod
from . import resume as resumemod
from . import ledger as ledgermod
from . import fix as fixmod
from . import progress
from . import gc as gcmod
from . import baseline as baselinemod
from . import runner as runnermod

# The three CLIs a closure can be resolved for, read off the module that owns the paths rather
# than spelled again here: `taskbundle.installed_closure` is what answers per CLI, so its own
# table is the only honest list of which CLIs this can ask about.
_CLIS = tuple(taskbundle.INSTALL_GLOBS)

# `review`'s reading of a resolution that leaves a finding standing. Referenced rather than
# respelled, on `handover`'s precedent for `seat._FORGE`: the module that decides a value owns
# the strings that spell it, and a copy here is a second place to be right about which
# resolutions close a finding — with the copy silently counting a blocker as resolved.
_FIXED = reviewmod._FIXED


class CliError(RuntimeError):
    """A run this front end will not start or collect."""


def _fail(out, lines) -> int:
    for line in lines:
        print(f"  ✗ {line}", file=out)
    return 1


def _resolve_seat_timeout() -> int:
    """§19's window, from the engine's own table and from nowhere else.

    A NUMBER CHOSEN HERE WOULD BE THE SECOND TIMEOUT MECHANISM §19 FORBIDS, and §19 exists
    because the first one silently degraded a three-seat panel to two. So a missing entry is a
    refusal rather than a fallback: `deep`'s 1200s is a review window, not a build window, and
    reaching for it here would put a forty-minute seat under a twenty-minute cap.

    The entry now exists — `council.engine` carries it, with the measurements and the one
    unmeasured dependency it rests on written beside it there. This refusal is therefore the
    guard for a build where it is missing or unusable (an older engine, a rolled-back one, a
    value edited to something that is not a positive int), not the ordinary path. Changing the
    window is still `council.engine`'s edit to make and not this module's.
    """
    t = engine.MODE_TIMEOUT.get("forge")
    if not isinstance(t, int) or isinstance(t, bool) or t < 1:
        raise CliError(
            "council.engine.MODE_TIMEOUT has no usable `forge` entry, so there is no window "
            "this run may build under. §19 forbids a second timeout mechanism, so this does "
            "not pick one — add the entry (§19 asks for >= 3600) and re-run.")
    return t


def _closures() -> dict:
    """The three live installed plugin closures, resolved once. `None` for a CLI that is not
    installed, which `taskbundle.ambient_verdict` reads as False — three absences do not hash
    identically."""
    return {c: taskbundle.installed_closure(c) for c in _CLIS}


def _read(path, what: str) -> str:
    """A file this front end was pointed at, or a refusal naming it.

    An unreadable argument is a CALLER's mistake and this gate's whole contract is that it
    answers those in refusal lines. Left to raise it would reach `main` as an `OSError`, which
    is not in the narrow tuple below and would leave a traceback where a sentence belongs.
    """
    try:
        return Path(path).read_text(encoding="utf-8", errors="surrogateescape")
    except OSError as e:
        raise CliError(f"{what} could not be read: {e}") from e


def _answer_sheet(path) -> dict:
    """§5 step 2's answers, off the JSON file the operator wrote.

    `author` IS THE ONE FIELD JSON CANNOT ROUND-TRIP. `gate._confirmed_author` unpacks a pair,
    and a two-element LIST unpacks the same way — so this conversion is not what makes the
    answer usable. It is here so the record holds the tuple every other route to a
    `Confirmation` holds, rather than a shape that depends on how the answer arrived.

    `deep_review` IS REFUSED IN THE SHEET. It is decided by `--skip-deep-review`, which also re-prices
    the quote; a sheet carrying its own copy is the second spelling of one decision that
    `gate.confirm`'s agreement check exists to stop, arriving one layer earlier and silently,
    because this function would then have to choose which of the two to keep.
    """
    text = _read(path, f"the answer sheet {path}")
    try:
        answers = json.loads(text)
    except ValueError as e:
        raise CliError(f"the answer sheet {path} is not readable as JSON: {e}") from e
    if not isinstance(answers, dict):
        raise CliError(f"the answer sheet {path} holds §5 step 2's answers as an object, not "
                       f"a {type(answers).__name__}")
    if "deep_review" in answers:
        raise CliError(
            "the answer sheet names `deep_review`, which is decided by `--skip-deep-review` because "
            "that flag also re-prices the run. Two spellings of one decision are free to "
            "disagree, and the disagreement is money — drop the key and pass the flag.")
    if isinstance(answers.get("author"), list):
        answers["author"] = tuple(answers["author"])
    return answers


def _steps(rows) -> tuple:
    """§5.1's steps out of the answer sheet's argv lists.

    `verify.Command.parse` is the parser, called rather than restated, so the refusals a
    malformed sheet gets here are the ones `gate.confirm` would give the same rows one call
    later — and they still name the index of the step at fault.
    """
    return verify.Command.parse(rows).steps


def _abandoned_runs(repo) -> list:
    """Runs for `repo` that stopped somewhere short of a terminal phase.

    READ FROM DISK, NEVER FROM MEMORY. §14.1's whole point is that a run's position survives
    the process that wrote it, and `runstate.PHASES`/`TERMINAL` are the vocabulary — a phase
    outside `TERMINAL` is a run that neither finished nor failed, which is exactly what an
    interruption leaves.

    IT REPORTS AND NEVER RAISES. A run directory this engine cannot read is itself worth
    saying — but it must not stop a NEW run from starting, because the operator's next action
    may be the only way to make progress at all.
    """
    out = []
    try:
        dirs = storage.run_dirs(repo)
    except (OSError, storage.StorageError):
        return []
    for rd in dirs:
        try:
            state = runstate.read_state(rd)
            manifest = runstate.read_manifest(rd)
        except Exception:                                    # noqa: BLE001
            out.append(f"note: {rd} holds a run this engine could not read; `--gc` reports "
                       "what it is holding")
            continue
        if state is None or state.phase in runstate.TERMINAL:
            continue
        out.append(
            f"note: run {manifest.run_id} stopped at `{state.phase}` and never reached a "
            f"terminal phase. Its clones are still on disk. There is no `--resume` verb, so "
            f"this run cannot be continued — but `--gc {manifest.run_id}` reclaims it, and "
            "starting a new run below spends the full quote again.")
    return out


def start(args, *, out, make_launcher=None) -> int:
    """§5's gate, §7's fleet, §6's verification — and it stops at `comparing`.

    `make_launcher` IS A SEAM AND `None` MEANS "ASK THE MODULE", NOT "USE A DEFAULT". Resolved
    at call time rather than bound as a default at `def` time, because the two callers that
    need it want different things: a test passing an explicit fake through `main`, and a test
    that has monkeypatched `cli.launch.make_launcher` to spy on the real one. A `def`-time
    default would silently ignore the second, and without the seam at all there is no way to
    exercise this function without paying three providers — which no test in this project may
    do.

    WHAT THIS CAN LEAVE BEHIND, stated because a front end that stops mid-run is where debris
    comes from. Every refusal ABOVE `gate.open_run` leaves nothing at all — that is the gate's
    own contract. After it, the run directory and B1's ref exist by design and `--gc` is what
    reclaims them. The one that is neither is `handover.create_synthesis_worktree`: it
    registers a worktree on a new branch and then materializes §20's bundle into it, so a
    bundle that will not materialize leaves a REGISTERED worktree and its branch standing.
    That call's own refusal names the tree it left, and reclaiming it is `--gc <run-id>`'s
    verb — which refuses a run that was never handed over, so an orphan of exactly this shape
    needs `--gc <run-id> --force`.
    """
    mk = _launcher_factory(make_launcher)
    repo = Path(args.repo).resolve()
    task_root = Path(args.task).resolve()
    entrypoint = args.entrypoint
    instruction = _read(task_root / entrypoint,
                        f"the task entrypoint {task_root / entrypoint}")

    try:
        report = preflight.inspect_repo(repo, args.select or ())
    except gitcmd.GitError as e:
        # Caught HERE and not in `main`'s tuple: `GitError` is what every git call in this
        # package raises, so admitting it there would turn a mid-fleet git failure into the
        # same one-line message as a bad `--repo`. This one call is the argument check —
        # `repo_facts` asks git for the worktree root first — and a path that is not a
        # repository is the caller's mistake rather than the engine's.
        raise CliError(f"{repo} is not a git repository this engine can read: {e}") from e
    blocked = preflight.refusals(report)
    if blocked:
        return _fail(out, ["preflight refuses this repository, so nothing is spent:", *blocked])

    b = taskbundle.scan(task_root, entrypoint=entrypoint)
    # §20's refusal. The bundle is passed for the record, and it CLEARS NOTHING: it does not
    # make a provider-specific referent portable and it does not make an ambient skill
    # identical across three CLIs. The ambient bar is the three-closure hash and nothing else —
    # a `bundle is None` guard on it would be dead here, because this line always has one.
    closures = _closures()
    not_portable = preflight.task_refusals(instruction, bundle=b, closures=closures)
    if not_portable:
        return _fail(out, ["§20 refuses this task:", *not_portable])
    # §20's other half, and `taskbundle.ambient_note`'s only caller: a named skill that DID
    # clear the three-way hash is declared provider-neutral in the prompt the seats receive.
    # Clearing it and then saying nothing would use the skill on all three CLIs on the strength
    # of a check whose result never left this function.
    notes = preflight.ambient_notes(instruction, closures=closures)
    if notes:
        instruction = instruction + "\n\n" + "\n".join(notes)

    # BEFORE the quote and before anything is shown, because a run with no window to build in
    # is a run that cannot happen and §5 step 2 must not be asked about one.
    # AN ABANDONED RUN IS NAMED BEFORE A SECOND ONE IS PAID FOR. §14.1 makes a run's position
    # durable precisely so an interrupted one can be picked up, and there is no `--resume`
    # verb yet — so the failure this closes is the expensive one: an operator whose run died
    # at `building` re-runs `--start`, spends ~19 more provider calls and ~63 GB, and the
    # first run's clones stay on disk with nothing naming them.
    #
    # A LINE, NOT A REFUSAL. The operator may genuinely want a fresh run, and refusing would
    # make a dead run block the tool. What they must not do is spend twice without being told
    # the first one is there — and `--gc` is what reclaims it.
    for line in _abandoned_runs(Path(args.repo).resolve()):
        print(f"  {line}", file=out)
    timeout = _resolve_seat_timeout()
    answers = _answer_sheet(args.answers)
    quote_ = gate.quote(report, seats=args.seats, attempts=args.attempts,
                        review_rounds=args.review_rounds, deep_review=not args.skip_deep_review,
                        concurrency=args.concurrency, seat_timeout_sec=timeout)
    if "verify" not in answers:
        # `must_show` resolves §6.1's surface from the confirmed command, so the sheet has to
        # carry one before the operator can be shown anything. `gate.confirm` gives the same
        # refusal for the same key; this one exists because the line above it needs the value.
        raise CliError("the answer sheet names no `verify` command, and §6.1's gate surface — "
                       "the first thing §5 step 2 shows — is resolved from it")
    if "setup" not in answers:
        # §5.2's provider-invocation rule covers setup and setup runs more often than verify.
        # A sheet with no setup command is not a run with no setup step — it is a run whose
        # setup nobody screened.
        raise CliError("the answer sheet names no `setup` command, and §5.2's "
                       "provider-invocation screen covers it — it runs once per builder clone "
                       "and once per verifier clone, more often than verify does")
    command = verify.Command(steps=_steps(answers["verify"]))
    for line in gate.must_show(report, quote_, command,
                               setup=verify.Command(steps=_steps(answers["setup"]))):
        print(f"  {line}", file=out)

    # ONE DECISION, WRITTEN ONCE. `--skip-deep-review` priced the quote above and answers §5 step 2
    # here; `gate.confirm` refuses the two if they ever disagree, and `--collect` reads the
    # recorded answer rather than re-deriving it from a flag it is not given.
    answers["deep_review"] = not args.skip_deep_review
    confirmation = gate.confirm(report, quote_, answers)

    run_id = storage.new_run_id()
    run_dir = gate.open_run(report, confirmation, run_id, quote_=quote_)

    # §20: "Persist the fully resolved instruction plus resource hashes so `--collect` never
    # depends on vanished conversation context." The BYTES go beside the manifest, because a
    # resume must not depend on the operator's own directory still existing.
    shutil.copytree(task_root, storage.task_source_path(run_dir))
    taskbundle.write_task_bundle(run_dir, b)

    # HASHED OFF THE RECORD, NOT OFF THE VALUE IN HAND. `read_task_bundle` is what a resume and
    # `--collect` read, so hashing the same bytes they will is what makes §11's `bundle_sha256`
    # a statement about the run rather than about this process's memory.
    launcher = mk(prompt=instruction, timeout=timeout,
                  bundle_sha256=taskbundle.bundle_hash(taskbundle.read_task_bundle(run_dir)))
    results = runnermod.run(run_dir, repo, identity=confirmation.author, launch=launcher)

    return _finish_the_fleet(run_dir, repo, run_id, results, out=out)


def _launcher_factory(make_launcher):
    """THE ONE DOOR TO THE REAL LAUNCHER, and it is deliberately the only one.

    Every test in this front end drives a verb without paying a provider, and that is a
    property of this module only while there is a single reference to `launch.make_launcher`
    to hold shut — `test_the_cli_reaches_the_launcher_only_through_the_injected_seam` counts
    them in the AST for exactly that reason. `--start` and `--resume` both need one, so the
    resolution lives here rather than being spelled twice.

    RESOLVED AT CALL TIME. `None` means "ask the module", not "use a default": a `def`-time
    default would bind the attribute before a test could monkeypatch `cli.launch.make_launcher`
    to spy on the real one, and would silently ignore it.
    """
    return make_launcher or launch.make_launcher


def _resume(args, *, out, make_launcher=None) -> int:
    """§14.1's other half: re-enter a run that stopped, without re-buying what it paid for.

    WHY THIS IS NOT `--start` AGAIN. `--start` opens a run — preflight, §20's task refusals,
    §5's gate and the quote — and every one of those has already happened for a run that
    exists. Re-taking them would ask the operator to re-confirm a bargain they already struck,
    and `gate.open_run` would build a second run directory rather than continue this one.

    THE PROVIDER CALLS ARE THE WHOLE POINT. `resume.plan` classifies each seat against the
    journal and the preserved clones BEFORE anything is spent: a seat that settled is reloaded
    from its persisted bundle, a seat that never settled is re-driven, and a seat that settled
    but cannot be proven refuses the resume outright. That last branch is the one that matters
    — re-driving an unprovable seat would silently spend its call twice, which is the single
    failure a resume must not have.

    THE PROMPT COMES OFF THE RECORD. §20 copied the task source beside the manifest precisely
    so a resume does not depend on the operator's own directory still existing, and the
    launcher is built from those bytes rather than from `--task`, which this verb does not take.
    """
    repo = Path(args.repo).resolve()
    run_id = args.resume
    # `must_be_new=False`, WHICH IS THE WHOLE POINT OF THE VERB. `run_root` CREATES, so the
    # default would refuse the only kind of run this can act on; `--collect` and `--review`
    # resolve an existing run the same way, and the empty-directory cleanup below is theirs.
    run_dir = storage.run_root(repo, run_id, must_be_new=False)
    if not storage.manifest_path(run_dir).exists():
        try:
            run_dir.rmdir()
        except OSError:
            pass
        raise CliError(f"{repo} has no run {run_id!r} to resume")
    manifest = runstate.read_manifest(run_dir)
    # THE IDENTITY IS READ OFF B1 ITSELF, which is the only place it survives. §5 step 2's
    # answer sheet is the operator's input and is gone; the manifest does not carry it. B1 is
    # the one commit forge wrote into the user's repository, and it was written WITH this
    # identity — so the commit is the run's own record of it. Re-deriving it from the ambient
    # git config instead would let a resume sign seat commits as a different author than the
    # one the run's baseline says made it, on a machine whose config moved in between.
    # `-s` ALREADY SUPPRESSES THE PATCH, and the driver flags go in anyway: the seam test
    # classifies by SUBCOMMAND, and an exemption arguing "this particular `show` prints no
    # diff" is a rule that holds only while nobody adds a format. They cost nothing here.
    ident = gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                       "show", *gitcmd.NO_DIFF_DRIVERS, "-s",
                       "--format=%an%x00%ae", manifest.baseline_commit,
                       env_extra=gitcmd.READONLY).stdout.strip("\n")
    name, _, email = ident.partition("\x00")
    if not name or not email:
        raise CliError(
            f"the identity {manifest.baseline_commit[:12]} was recorded under cannot be read "
            f"back from it, so this resume cannot sign seat commits as the run did.")
    identity = (name, email)
    b = taskbundle.read_task_bundle(run_dir)
    instruction = _read(storage.task_source_path(run_dir) / b.entrypoint,
                        "the task entrypoint this run recorded")
    mk = _launcher_factory(make_launcher)
    launcher = mk(prompt=instruction, timeout=_resolve_seat_timeout(),
                  bundle_sha256=taskbundle.bundle_hash(b))
    results = runnermod.run(run_dir, repo, identity=identity, launch=launcher, resume=True)
    return _finish_the_fleet(run_dir, repo, run_id, results, out=out)


def _finish_the_fleet(run_dir, repo, run_id, results, *, out) -> int:
    """Everything after the fleet has been driven: transport, the synthesis worktree, the report.

    SHARED BY `--start` AND `--resume` BECAUSE IT IS ONE PROCEDURE. A resumed run has to leave
    the operator exactly what a straight-through run leaves — the seat branches transported out
    of the remote-less clones, a synthesis worktree, a brief and §16.1's table. Two copies of
    this would be two ideas of what finishing means, and the one on the resume path would be
    the one nobody looked at.
    """
    # §16: seat work is transported out of each remote-less clone by the ENGINE, from the
    # user's side, with an explicit refspec — before anything else can fail and leave it in a
    # directory `--gc` is allowed to delete.
    for r in results:
        if r.seat is not None:
            handover.transport_seat(repo, r.seat.path, run_id=run_id, seat=r.name)

    synth = run_dir / handover.SYNTHESIS
    handover.create_synthesis_worktree(
        repo, synth, run_id=run_id,
        at=runstate.read_manifest(run_dir).baseline_commit, run_dir=run_dir)
    print(f"run: {run_id}", file=out)
    print(f"synthesis worktree: {synth}", file=out)
    # THE FLEET IS ALREADY PAID FOR, SO THIS REPORTS RATHER THAN REFUSES — and "fail closed"
    # is satisfied by never writing a brief that describes a run it could not read, not by
    # destroying a run whose providers have already answered. The refusal is printed where the
    # operator is looking, and the "Next:" line below never claims a brief that is not there.
    #
    # THE TUPLE IS THE WHOLE OF WHAT THE READ CAN RAISE, and a narrower one would make the
    # comment above false: `runstate.read_seat` raises `StateError` for a damaged record and
    # `brief_path` raises `GitError` through git — both are bare `RuntimeError` subclasses that
    # no `except` further out catches, so either would kill a paid-for run at the last print.
    try:
        print(f"fusion brief: {briefmod.write(run_dir, synth)}", file=out)
    except (briefmod.BriefError, runstate.StateError, gitcmd.GitError, OSError,
            storage.StorageError) as e:
        print(f"  ✗ no fusion brief was written: {e}", file=out)
    for line in _seat_table(run_dir, runstate.read_manifest(run_dir)):
        print(f"  {line}", file=out)
    print(f"Next: fuse in the synthesis worktree, then `--collect {run_id}`.", file=out)
    return 0


def _seat_lines(run_dir, manifest=None) -> tuple:
    """§16.1's first line, built from the RECORDS ON DISK.

    NEVER FROM A RESULTS TUPLE. `runner.run` returns one `SeatResult` per seat that produced
    one, "which is not always `manifest.seats` of them: a seat every one of whose attempts was
    REFUSED has no verdict to return, and the loop reports that by absence rather than by
    inventing one." Counting the tuple therefore reports "2 of 2 seats completed" for a
    three-seat run in which one seat was refused outright — a denominator that shrank to fit
    the numerator.

    THE KEYS ARE `runner._record`'s AND WERE READ OFF IT. `status` is a mapping or `None`, and
    `verification` is `{"outcome", "reason"}` or `None` — not a pair, which is the shape it has
    in a `SeatResult` and not the shape it is written in. A reader that guesses either gets
    `None` and renders a `failed` seat for a run that succeeded, which is this function's own
    fail-open arriving through the dictionary rather than through the tuple.

    A SEAT WITH NO VERDICT RENDERS AS `failed`/`unusable`, and that is a rendering forced by
    `handover.SeatLine`'s vocabulary rather than a verdict §8 reached: `run_seat` writes
    `status: None` for an attempt it could not classify and then raises, and `SeatLine` has no
    seventh value for "nobody classified this". The substitution is the fail-closed direction —
    it adds to §16.1's denominator and to none of its numerators — but it is a substitution,
    and the alternative (dropping the row) is the denominator shrinking again.
    """
    # THE DENOMINATOR IS THE FLEET THE OPERATOR PAID FOR, NOT THE RECORDS ON DISK, and this
    # is the second shrink — the paragraph above closes the first. `storage.seat_names` globs
    # the seats that left a RECORD FILE, so a seat whose launcher died before `runner._record`
    # ran is absent from the glob entirely and §16.1 reports "1 of 1 seats completed" for a
    # three-seat run. Reproduced: one recorded seat in a run directory answers a denominator
    # of 1. `runner._fleet` is the ONE place a confirmed count resolves to names — reused
    # rather than restated, because a second copy of that rule is how the two spellings of
    # this denominator came to disagree in the first place.
    #
    # A seat with no record renders `failed`/`unusable`, which is what the paragraph above
    # already argues for a seat with no verdict: it adds to the denominator and to none of the
    # numerators, and the alternative is the shrink this comment is about.
    try:
        expected = tuple(runnermod._fleet(manifest)) if manifest is not None else ()
    except runnermod.RunnerError:
        # A manifest whose count exceeds the providers that exist is `run`'s refusal to make,
        # not this reader's — reporting it here would raise out of a handover.
        expected = ()
    names = list(dict.fromkeys([*expected, *storage.seat_names(run_dir)]))
    lines = []
    for name in names:
        rec = runstate.read_seat(run_dir, name) or {}
        attempts = rec.get("attempts") or []
        last = attempts[-1] if attempts else {}
        status = last.get("status") or {}
        verification = last.get("verification")
        lines.append(handover.SeatLine(
            name=name,
            forge=status.get("forge", "failed"),
            artifacts=status.get("artifacts", "unusable"),
            verify_outcome=(verification or {}).get("outcome")))
    return tuple(lines)


def _seat_table(run_dir, manifest=None) -> tuple:
    """What `--start` prints about the fleet it just ran, off the same rows §16.1 counts."""
    return tuple(
        f"{s.name}: {s.forge}, artifacts {s.artifacts}, verify "
        f"{s.verify_outcome or 'not run'}"
        for s in _seat_lines(run_dir, manifest))


def _rev(checkout, rev: str) -> str:
    """One oid out of `checkout`, or a refusal — never the empty string `_measured` refuses.

    `--verify` so a name this repository does not hold exits non-zero here rather than coming
    back blank and being compared against a tree oid two calls later. The two presets are on
    it because this is the USER's own worktree: `handover`'s module rule, and the closure in
    `tests/test_forge_seams.py` is what keeps it on the next call added here.
    """
    try:
        oid = gitcmd.git(checkout, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                         "rev-parse", "--verify", rev,
                         env_extra=gitcmd.READONLY).stdout.strip()
    except gitcmd.GitError as e:
        raise CliError(f"{checkout} could not resolve {rev!r}: {e}") from e
    if not oid:
        raise CliError(f"git resolved {rev!r} in {checkout} to nothing at exit 0, so §16's "
                       "conditions would be read off a tree nobody measured")
    return oid


# What `--collect` is willing to read out of the synthesis tree's out-of-band set. The
# SYNTHESIS tree is one an orchestrator fused in and may have run a toolchain in, so
# `Quota.default()` — the pre-launch screen's question about the user's own selected baseline —
# is the wrong bar: its own docstring records this repository's worktree failing it on the file
# cap alone. `for_harvest` is the question this is, one tree over: what an inventory of a tree
# that has had setup run in it may weigh.
_OOB_QUOTA = storage.Quota.for_harvest()


def _sidecars_of(synth) -> tuple:
    """§16's out-of-band set for the SYNTHESIS tree, ENUMERATED rather than assumed.

    `mergeability` and `out_of_band` both refuse `None` and both accept `()`, and the two mean
    different things — "nobody looked" against "this tree holds none". So this measures: git's
    own porcelain names every path the synthesis BRANCH does not carry, which is exactly §16's
    "merging the branch alone does not install these". `??` is untracked and `!!` is ignored;
    both are files a user has to copy out by hand.

    A DIRECTORY ENTRY IS A REFUSAL, NOT AN EMPTY SET. git reports an untracked or ignored
    directory as one `dir/` record when it does not need to descend, and reading that as one
    path with no payload — or skipping it — would report a handover with nothing to copy over a
    tree holding a hundred files.

    `--ignored=traditional` IS WHAT MAKES THE IGNORED HALF PER-FILE, and the name reads
    backwards. Measured on git 2.53.0 against a tree with `ignoredir/` in `.gitignore` holding
    two files in two directories: with `--untracked-files=all --ignored=matching` git answered
    the single record `!! ignoredir/`, and with `--ignored=traditional` it answered
    `!! ignoredir/b.txt` and `!! ignoredir/sub/a.txt`. git's own documentation is where the
    coupling is stated — `traditional` shows directories "unless --untracked-files=all is
    specified, in which case individual files in ignored directories are displayed" — so the
    two flags are one measurement and neither may be changed alone. The trailing-`/` refusal
    below is the backstop for the day they are, and it is a refusal rather than a skip because
    a directory this reader did not descend into is not a directory holding nothing.

    THE PAYLOADS GO THROUGH `verify._materialized_sidecar`, which is the read half
    `bundle.materialize` wrote: it produces `bundle.SidecarEntry` in the bundle's own shape,
    containment-checked, with the kind and mode a sidecar carries. Its `None` is "nothing a
    sidecar could describe" — missing, unreadable, a FIFO — and that is RAISED here rather than
    dropped, because a path git listed and this could not read is not a path that is not there.
    """
    r = gitcmd.git(synth, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                   "status", "--porcelain", "-z", "--untracked-files=all",
                   "--ignored=traditional", env_extra=gitcmd.READONLY, binary=True)
    paths, unreadable = [], []
    for record in r.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if len(record) < 4 or record[:2] not in ("??", "!!"):
            continue
        paths.append(record[3:])
    directories = sorted(p for p in paths if p.endswith("/"))
    if directories:
        raise CliError(
            f"git named {len(directories)} out-of-band DIRECTORY in {synth} "
            f"({', '.join(directories[:5])}{' …' if len(directories) > 5 else ''}) rather "
            "than the files inside it, so §16's copy commands would name a path with no "
            "payload. This reader asked git to descend and it did not, which is a shape it "
            "cannot enumerate.")
    out = []
    total = 0
    for rel in sorted(paths):
        entry = verify._materialized_sidecar(Path(synth), rel)
        if entry is None:
            unreadable.append(rel)
            continue
        total += len(entry.payload)
        breach = _OOB_QUOTA.breach(files=len(out) + 1, file_bytes=len(entry.payload),
                                   total_bytes=total)
        if breach:
            raise CliError(
                f"the synthesis tree's out-of-band set is over quota ({breach}), so this run's "
                "handover cannot list what merging the branch would leave behind. Nothing is "
                "truncated here — a partial list reads as a complete one.")
        out.append(entry)
    if unreadable:
        raise CliError(
            f"git named {len(unreadable)} out-of-band path(s) in {synth} that could not be "
            f"read as a file or a link ({', '.join(unreadable[:5])}"
            f"{' …' if len(unreadable) > 5 else ''}). A path this engine cannot read is not a "
            "path the tree does not hold, and §16's second mergeability condition turns on the "
            "difference.")
    return tuple(out)


def _baseline_owned(manifest) -> tuple:
    """§16's "unchanged selected untracked/ignored files are the BASELINE's".

    THE WHOLE SELECTION, AND NOT A SUBSET FILTERED BY THE OUT-OF-BAND SET. A filter reading
    "drop the selected paths the fusion rewrote" is what this took first, and it can never
    fire: `baseline.materialize` puts every selected untracked/ignored path INTO B1's tracked
    tree, so a selected path is tracked in the synthesis worktree and `_sidecars_of` — whose
    whole subject is what the branch does NOT carry — can never name one. A filter that cannot
    fire is a claim the code does not make.

    Nothing is lost, because the qualifier is about OWNERSHIP rather than about a diff:
    §16's sentence, which `handover.text` prints beside this list, is "these are yours, not
    forge's; only their B→S changes are forge-authored". The B→S changes are the tracked patch,
    which the branch carries. What this list says is whose the FILES are, and the answer is the
    same whether or not the fusion edited them.
    """
    return tuple(manifest.selected_paths)


def _b1_files(run_dir) -> tuple:
    """§16's B1 file list, off the record `baseline.materialize` wrote.

    NOT `manifest.selected_paths`, which is the SELECTION and is `_baseline_owned`'s answer one
    field over. B1 is the tracked tree PLUS the selection, and `handover.text` prints the two
    lists under different headings — so taking both from the same tuple would render the
    selection twice and never say what the baseline was, which for an ordinary clean repository
    is the line "B1 — the baseline this run started from: (no files)".

    `read_filesystem_manifest` is the only record of B1's content that survives the run: a
    path-to-digest mapping written where `fleet.clone_seat` reads it to check a seat's
    checkout. It RAISES for a missing or empty one rather than answering `()`, which is the
    refusal this needs — an empty B1 list in a handover is a claim about a baseline nobody
    recorded.
    """
    return tuple(sorted(baselinemod.read_filesystem_manifest(run_dir)))


def _strongest(run_dir) -> tuple:
    """§12.5's `(seat | None, why)` pair.

    IT NAMES NOBODY IN THIS BUILD, and after `--ledger` the reason is no longer that nothing
    writes a ledger. `rubric.dimensions_from` needs FOUR things — a coverage report over §10's
    claim ledger, §6.2's outcome, §13's review risk and §12.1's measured size — and this front
    end wires the outcome and, now, the ledger. It reaches no coverage runner, convenes no
    review and measures no size.

    THE BINDING ONE IS THE REVIEW RISK, NOT THE COVERAGE, and it is worth naming exactly
    because the coverage is the plausible-looking answer. `rubric._unmeasured` walks §12.5's
    order and returns the FIRST dimension a seat cannot be ranked on — coverage, then gate
    outcome, then review risk, then diff complexity. So even a run whose coverage report were
    complete and whose gate outcome were `PASS` would still be unrankable at the third
    dimension, for every seat.

    AND THAT IS PERMANENT, NOT PENDING. An earlier version of this paragraph said "until
    §13's review is wired"; §13 is wired now — `--review` convenes a real panel — and no seat
    gained a review risk, because the panel reviews the SYNTHESIS, after fusion, and §12.5
    ranks the SEATS, before it. Nothing in the spec reviews a candidate seat: the quote's
    review term is `review_rounds * seats` where `seats` is the PANEL size over one tree, and
    a per-candidate panel would be nine calls nobody priced. So a four-dimension rank is
    unreachable BY CONSTRUCTION under this design, and this function naming nobody is the
    rubric's honest output, not a stage that has not shipped yet. The spec would have to
    change — a per-seat review, or a rubric whose third dimension something produces — before
    a name could ever be printed here.

    NAMING THE BEST-DESCRIBED SEAT INSTEAD WOULD BE THE FAIL-OPEN `rubric.strongest` ITSELF
    REFUSES: "ranking the measurable ones and reporting their winner turns 'the strongest seat
    we were able to measure' into 'the strongest seat'." With nothing measured at all, that
    would be a winner of a comparison nobody ran. §16.1's header renders this reason as its
    Fusion line, which is why the pair carries one.
    """
    if not storage.ledger_path(run_dir).exists():
        return None, ("no strongest seat can be named: §12.5 ranks seats on §10's claim "
                      "ledger and this run recorded none, so there is nothing to compare and "
                      "no dimension any seat could be ranked on")
    return None, ("no strongest seat can be named: this run recorded a claim ledger and no "
                  "coverage report over it, and §12.5's order is taken over the coverage, the "
                  "gate outcome, the review risk and the measured size together — a rank over "
                  "the one input that exists would order the fleet on a quarter of the rubric")


def _rounds_run(run_dir) -> int:
    """How many §13 rounds this run RECORDED, counted from 1 until one is missing.

    THE MANIFEST'S `review_rounds` IS THE BUDGET AND NOT THIS. It is what the operator agreed
    to pay for; this is what happened. `handover.Provenance` refuses a record whose terminal is
    `None` beside a non-zero round count — "a review nobody convened" carrying rounds — so
    handing it the budget would fail every run whose review has not been implemented, and
    handing it the budget beside a terminal that WAS reached would print a round count nobody
    ran.
    """
    n = 0
    while reviewmod.findings_path(run_dir, n + 1).exists():
        n += 1
    return n


def _review_terminal(run_dir, rounds_run: int, events):
    """§13's terminal off the record, or `None` because no round was written.

    `None` AND NEVER `ready`. §13's whole named failure is `--collect` unable to tell two
    opposite terminal states apart; a run with no round at all is neither of them, and `ready`
    would be a review's verdict over a review that was never convened —
    `review._check_rounds_run` refuses the same thing on its own side.
    """
    if not rounds_run:
        return None
    return reviewmod.terminal_from_record(run_dir, rounds_run=rounds_run, events=events)[0]


def _unresolved(run_dir, rounds_run: int) -> int:
    """How many recorded findings a fix pass did not close.

    A ROUND WITH NO RESOLUTIONS RECORD CONTRIBUTES ITS FINDINGS RATHER THAN ZERO.
    `read_resolutions` answers `None` for a round whose fix pass never ran, and reading that as
    "nothing was left open" is the direction that prints a clean header over an unresolved
    blocker. It RAISES for a record it cannot read, and that raise is not caught here for the
    same reason.

    COUNTED OVER EVERY SEVERITY, where `review.terminal_from_record` decides its terminal on
    blockers alone. That makes this a superset of the blockers still open, which keeps
    `Provenance`'s rule — `review_blocked` beside zero unresolved findings is refused — true by
    construction rather than by two enumerations agreeing.
    """
    total = 0
    for n in range(1, rounds_run + 1):
        rows = reviewmod.read_resolutions(run_dir, n)
        by_id = {r.finding_id: r for r in (rows or ())}
        for f in reviewmod.read_round(run_dir, n).findings:
            r = by_id.get(f.id)
            if r is None or r.resolution != _FIXED:
                total += 1
    return total


def _confirmed_deep_review(run_dir) -> bool:
    """The operator's §13.1 answer, read back off the record `gate.open_run` wrote.

    `runner._confirmed_policy`'s shape and its argument, one answer over: it is journalled on
    the `confirm` operation's own done record and nowhere else, so this reads it there and
    FAILS CLOSED when it is missing or is not a bool. A default here would be this process
    deciding whether to spend three of the operator's provider calls on a run that cannot say
    what it agreed to.
    """
    manifest = runstate.read_manifest(run_dir)
    done = journal.done("confirm")
    events = journal.Journal(storage.journal_path(run_dir)).read()
    for e in reversed(events):
        if e.event == done and e.operation_id == manifest.run_id:
            value = e.data.get("deep_review")
            if isinstance(value, bool):
                return value
            raise CliError(
                f"this run's {done} record carries deep_review={value!r}, which is not the "
                "bool §5 step 2 records. §13.1's review is priced in money and this engine "
                "will not read a shape it cannot tell on from off.")
    raise CliError(
        f"this run's journal has no {done} record for {manifest.run_id!r}, so what the "
        "operator answered about §13.1's deep review is not recorded anywhere. It is priced "
        "in provider calls and has no safe default, so this collect will not choose one.")


def _agreement(run_dir) -> str:
    """§11's label over the seats' recorded fingerprints.

    A SEAT WITH NO RECORDED FINGERPRINT IS NOT A SEAT THAT MATCHED. `agreement_label` compares
    recorded values; dropping an unrecorded seat would compute a label over a smaller fleet
    than the one that ran, and report it as the fleet's. So a missing row makes the label
    `not-comparable` — which is one of §11's three and is what a comparison nobody could make
    is called. A fleet of ONE gets the same answer for the same reason: `agreement_label`
    refuses fewer than two seats, because one seat agreeing with itself is not a measurement.
    """
    ids = []
    for name in storage.seat_names(run_dir):
        rec = runstate.read_seat(run_dir, name) or {}
        rows = [a.get("prompt_identity") for a in (rec.get("attempts") or [])]
        rows = [r for r in rows if r]
        if not rows:
            return "not-comparable"
        ids.append(fingerprint.from_row(rows[-1]))
    if len(ids) < 2:
        return "not-comparable"
    return fingerprint.agreement_label(ids)


# §13.1's result before there is one. `Provenance` types this field, and the record `collect`
# validates BEFORE the spend therefore needs a `DeepReview` to be a record at all; the only honest
# one for a review nobody has asked for yet is a `skipped` whose detail says so. It never
# reaches a header — `dataclasses.replace` puts the review's own result in its place — so the
# detail is written for the one reader who would ever see it: somebody looking at a handover
# where that replacement did not happen.
# A KEY THAT IS ABSENT AND A KEY THAT IS `null` MEAN DIFFERENT THINGS in the deep-review
# record, and `dict.get` collapses them. Absent = written before `reporting` existed, so
# nothing can be said about which seats reported; `null` = written since, by a status that
# records no reporters. `get(k, _MISSING)` is what keeps the two apart.
_MISSING = object()

_UNREQUESTED = deepreview.DeepReview(
    deepreview.SKIPPED, None, None, (), False,
    "this run's record was validated before §13.1 was asked for and the placeholder it was "
    "checked with was never replaced by the review's own result — so this line describes a "
    "defect in `--collect` and NOT a review the operator declined")


def _reported_outcome(args, *, head: str) -> tuple:
    """`(outcome | None, evidence line | None)` out of §16's evidence flags.

    THE THREE FLAGS ARE ALL-OR-NOTHING BECAUSE A PARTIAL SET IS A CALLER'S MISTAKE AND NOT AN
    ABSENCE. Dropping a lone `--verified-at` would lose a verdict the operator meant to report
    while the header rendered "no verify verdict was reported for the fusion" — the missing
    argument and the deliberate silence spelling the same. `--verify-log` is the one optional
    member: it adds a citation and it decides nothing.

    THE MAP FROM AN EXIT CODE IS TWO-VALUED ON PURPOSE. §6.2 names four outcomes and only two
    of them can be read off one exit status; `FLAKY` is a claim about two runs and this engine
    has one number, so a flag that could assert it would be a verdict cleaner than its
    evidence. The EXACT code goes in the line, because `FAIL` at 127 (no such command) and
    `FAIL` at 1 (a failing test) are one word for two events.

    THE LOG IS READ UNDER A QUOTA AND THE CAP IS A REFUSAL. `--verify-log` is a caller-named
    path and the citation is a digest over its whole contents, so an uncapped `read_bytes` is
    this verb loading an arbitrary file into memory on the caller's word — the one such read in
    the package, every other going through `storage.Quota`. `for_harvest` is the right bar: it
    is what this module already applies to a tree that has had setup run in it. Over the cap is
    REFUSED rather than truncated, because a digest over a prefix is a citation that does not
    re-derive.
    """
    at, code, log = args.verified_at, args.verify_exit, args.verify_log
    if at is None and code is None and log is None:
        return None, None
    if at is None or code is None:
        raise CliError(
            "§16's fusion evidence is `--verified-at <oid>` AND `--verify-exit <n>` together. "
            f"Got verified_at={at!r}, verify_exit={code!r}. A partial set is a verdict lost "
            "under a header that would then say none was offered, so it is refused rather "
            "than dropped.")
    if at != head:
        raise CliError(
            f"--verified-at {at} is not this run's synthesis HEAD ({head}). A verify that ran "
            "at another OID measured another tree, and §16.1's header would attribute its "
            "result to the one being handed over.")
    outcome = verify.PASS if code == 0 else verify.FAIL
    line = f"reported by the orchestrator at {head}, exit {code}"
    if log is not None:
        p = Path(log)
        cap = storage.Quota.for_harvest().max_file_bytes
        try:
            size = p.stat().st_size
            if size > cap:
                raise CliError(
                    f"--verify-log {log} is {size} bytes and too large to cite (the cap is "
                    f"{cap}). A digest over part of it is a citation that does not re-derive, "
                    "so this is refused rather than truncated.")
            blob = p.read_bytes()
        except OSError as e:
            raise CliError(f"--verify-log {log} is not a file this engine can read ({e}); a "
                           "citation that does not resolve is worse than no citation") from e
        line += (f", log {len(blob)} byte(s) sha256:"
                 f"{hashlib.sha256(blob).hexdigest()[:12]}")
    return outcome, line


def _refuse_a_seats_candidate(repo, run_dir, *, run_id: str, tree: str,
                              base_tree: str) -> None:
    """§16: the deliverable is a FUSION. A synthesis tree identical to one seat's candidate is
    that seat promoted, which is the one thing this skill exists not to do.

    TREE OIDS, NOT DIFF BYTES. Both trees are built over B1, so "byte-identical diff" and
    "identical tree" are the same statement, and git already computed the second.

    A SEAT WHOSE BRANCH CANNOT BE RESOLVED IS REFUSED WHEN IT PRODUCED A CANDIDATE AND SKIPPED
    WHEN IT DID NOT — never skipped on both. `handover.transport_seat` is called only for a
    seat that produced one, so an absent branch is legitimate for a seat that produced
    nothing; for a seat whose record says `usable`, an absent branch is a candidate this check
    cannot compare against, and passing the check by not making the comparison is how the
    identical fusion ships.
    """
    for name in storage.seat_names(run_dir):
        # `refs/khenrix-forge/<run>/<seat>`, WHICH IS WHERE `transport_seat` ACTUALLY PUTS IT.
        # `handover.branch` spells `forge/<run>/<seat>`, which git resolves under `refs/heads/`
        # — and that head exists only for the SYNTHESIS worktree. Measured after a real
        # `--start`: the repository held `refs/heads/forge/<run>/synthesis` plus
        # `refs/khenrix-forge/<run>/{base,claude,codex,agy}`, so asking for the head form made
        # every seat unresolvable and turned this check into a refusal of every run.
        b = f"refs/khenrix-forge/{run_id}/{name}"
        try:
            seat_tree = _rev(repo, f"{b}^{{tree}}")
        except CliError:
            rec = runstate.read_seat(run_dir, name) or {}
            attempts = rec.get("attempts") or []
            status = (attempts[-1].get("status") or {}) if attempts else {}
            if status.get("artifacts") == "usable":
                raise CliError(
                    f"seat {name} recorded a usable artifact set and its branch {b} does not "
                    "resolve, so this collect cannot check whether the fusion is that seat's "
                    "candidate. §16's deliverable is a fusion and an unmade comparison is how "
                    "a promoted candidate ships.") from None
            continue
        if seat_tree == base_tree:
            # A SEAT THAT COMMITTED NOTHING HAS B1'S OWN TREE, and so does an unfused
            # synthesis — so comparing the two would refuse "this is seat X's candidate" over a
            # run in which nobody fused anything. That is `mergeability`'s refusal, it is more
            # specific, and it tells the operator what to do next. Skipping here is not a hole:
            # a synthesis identical to B1 never reaches the handover at all.
            continue
        if seat_tree == tree:
            raise CliError(
                f"the synthesis tree is byte-identical to seat {name}'s candidate: this is "
                f"seat {name}'s candidate, not a fusion. §16's deliverable is a new answer "
                "assembled from the best of all seats — if that IS the answer, say so out of "
                "band; this engine will not hand it over under a fusion's header.")


def _refuse_a_head_that_is_not_the_branch(synth, manifest) -> None:
    """The synthesis worktree's HEAD is the branch this handover is about to deliver.

    MEASURED, AND THE TWO CAN DIFFER. Everything below is built from `HEAD` — the tree oid
    `mergeability` compares, the evidence line, the out-of-band set — while the command handed
    to the operator is `git merge --no-ff <branch>`, whose name comes from the RUN ID
    (`handover.branch`). Nothing between read `symbolic-ref`, so a worktree whose HEAD had
    moved off the branch produced a report describing one commit and a command delivering
    another. Reproduced: fuse and commit on the branch, then `git checkout --detach <base>` in
    the worktree — HEAD is the base and the branch is the fusion.

    BOTH DIRECTIONS ARE WRONG AND THE SECOND IS WORSE. HEAD behind the branch reports "nothing
    fused" and merges the fusion. HEAD ahead of it, or detached, describes work the merge does
    not carry — and `--gc` then deletes the branch by name, reclaiming the difference with
    nothing left naming it.

    THE INVARIANT IS ALREADY INTENDED. `handover.create_synthesis_worktree` uses `-b` and
    never the detach flag, and its docstring gives the reason in one sentence: "a detached HEAD
    leaves the commits unreachable and the next `git gc` deletes the deliverable". It was
    enforced at creation and nowhere at collection, which is a rule that holds only on the path
    its author remembered — the shape this package refuses everywhere else.
    """
    want = "refs/heads/" + handover.branch(manifest.run_id, handover.SYNTHESIS)
    r = gitcmd.git(synth, *gitcmd.NO_DAEMON_CACHE, *gitcmd.NO_HOOKS,
                   "symbolic-ref", "--quiet", "HEAD", env_extra=gitcmd.READONLY, check=False)
    have = r.stdout.strip()
    if not have:
        # A DETACHED HEAD IS ITS OWN SENTENCE. `symbolic-ref --quiet` exits 1 with no output
        # there, and folding it in with "on the wrong branch" would tell an operator to switch
        # branches when what they have is a HEAD pointing at no branch at all.
        raise CliError(
            f"the synthesis worktree at {synth} has a DETACHED HEAD, so the commits you fused "
            f"are not on {want} — the branch this handover would tell you to merge, and the "
            "one `--gc` deletes. Re-attach with `git -C "
            f"{synth} checkout {handover.branch(manifest.run_id, handover.SYNTHESIS)}` "
            "before collecting.")
    if have != want:
        raise CliError(
            f"the synthesis worktree at {synth} is on {have} and this handover is about "
            f"{want}. Everything reported below is measured from HEAD while the merge command "
            "names the branch, so the report and the command would describe different commits."
        )


_FINDING_FIELDS = ("id", "round", "seat", "severity", "claim", "evidence", "resolution")


def _finding_row(f) -> dict:
    """One `review.Finding` as JSON, by its OWN field list.

    `review` ships no public serializer, and spelling the seven names here would be a second
    copy that drifts the day an eighth is added — so the tuple above is the one place, and
    `_finding_of` reads it back through the same tuple.
    """
    return {k: getattr(f, k) for k in _FINDING_FIELDS}


def _finding_of(row) -> reviewmod.Finding:
    """The inverse, refusing a row that is missing a field rather than defaulting one.

    A defaulted `severity` would turn a blocker into whatever the default is, on a record
    §13.1's findings are read out of — so a partial row is an error, not a finding.
    """
    if not isinstance(row, dict) or any(k not in row for k in _FINDING_FIELDS):
        raise CliError(f"a recorded §13.1 finding carries {_FINDING_FIELDS}, not {row!r}")
    return reviewmod.Finding(**{k: row[k] for k in _FINDING_FIELDS})


_DEEP_KIND = "deep_review"


def _deep_review_already_spent(events):
    """§13.1's recorded result, or `None` because this run has not paid for one yet.

    THE DURABLE RECEIPT §13.1 NEVER HAD. `deepreview.run_deep_review` spends three provider calls and this package
    journalled nothing about it, so a crash between the invocation and the report lost the
    fact that money had been spent — and the refusal an operator then sees tells them to
    re-run `--collect`, which spent it again. That made the double charge the documented
    path rather than an accident.

    A COMPLETED RECORD IS REPLAYED, AN ORPHANED ONE IS NOT. An `intent` with no `done` is a
    request whose outcome nobody knows: it may have reached the remote and it may not, so
    this returns `None` and the caller requests again. That is the direction §14.1 chooses
    everywhere else — an unknown outcome is retried and a KNOWN one is never repeated — and
    it is the one that cannot silently drop a finding.
    """
    done = journalmod.done(_DEEP_KIND)
    for e in reversed(list(events)):
        if e.event != done:
            continue
        d = e.data
        status = d.get("status")
        if status not in deepreview.STATUSES:
            # A record this engine cannot read is not a receipt. Requesting again costs money
            # and is the safe direction: reporting a status nobody can interpret would put an
            # unreadable value into §16.1's header.
            return None
        rows = d.get("findings")
        findings = None if rows is None else tuple(_finding_of(r) for r in rows)
        rep = d.get("reporting", _MISSING)
        return deepreview.DeepReview(status, d.get("reason"), findings,
                                     tuple(d.get("seats") or ()),
                                     bool(d.get("diff_measured")), d.get("detail"),
                                     reporting=None if rep is _MISSING or rep is None
                                     else tuple(rep))
    return None


def collect(args, *, out) -> int:
    """§14's read-from-disk half: §13.1 once, §16's mergeability, and the handover text."""
    repo = Path(args.repo).resolve()
    run_dir = storage.run_root(repo, args.collect, must_be_new=False)
    if not storage.manifest_path(run_dir).exists():
        # `run_root` CREATES, so a `--collect` on an id nothing opened would otherwise leave a
        # directory behind for `--gc` to find and report as a run. `rmdir` removes it only
        # while it is empty, so this can never take a real run's evidence with it.
        try:
            run_dir.rmdir()
        except OSError:
            pass
        return _fail(out, [f"{repo} has no run {args.collect!r}: nothing under "
                           "XDG_STATE_HOME records one, so there is nothing to collect."])
    # RAISES rather than reporting nothing. `reconstruct`'s own docstring lists what it refuses
    # a run directory for, and every one of them is a run whose state is unknown — which §14.1
    # names `outcome_unknown` and says is never silently retried. A `except: return 1` here
    # would print "nothing to collect" for a run that spent three provider calls.
    recon = runstate.reconstruct(run_dir, repo)
    manifest = recon.manifest
    synth = run_dir / handover.SYNTHESIS
    if not synth.exists():
        return _fail(out, [f"{synth} does not exist: this run has no synthesis worktree, so "
                           "there is nothing to hand over. Re-run `--start`, or create it and "
                           "fuse there."])

    # EVERY REFUSAL THIS FUNCTION CAN MAKE WITHOUT SPENDING COMES FIRST, and §13.1's review is
    # the only thing here that costs money. `mergeability` refuses a synthesis worktree nobody
    # fused into, `_sidecars_of` refuses a tree whose out-of-band set it cannot enumerate, and
    # `Handover` refuses a delivery that names neither a target nor an acceptance — all three
    # from disk, and all three used to run AFTER the deep review, so a run with nothing to
    # hand over paid $5-25 to be told so. The reads below are what the header is built from as
    # well, so the same ordering makes the whole handover provably constructible before the
    # one call that cannot be taken back — including `Provenance`, whose own refusals are
    # argued where it is built.
    _refuse_a_head_that_is_not_the_branch(synth, manifest)
    head = _rev(synth, "HEAD")
    tree = _rev(synth, "HEAD^{tree}")

    # BOTH BEFORE THE SPEND, on this function's own standing rule: every refusal it can make
    # from disk comes above §13.1's deep review, so a run with nothing to hand over never
    # spends the fan-out's three provider calls to be told so.
    _refuse_a_seats_candidate(repo, run_dir, run_id=manifest.run_id, tree=tree,
                              base_tree=manifest.tracked_tree_oid)
    reported, evidence = _reported_outcome(args, head=head)
    sidecars = _sidecars_of(synth)
    merge = handover.mergeability(manifest, synthesis_tree_oid=tree, sidecars=sidecars)
    oob = handover.out_of_band(sidecars, synthesis_path=synth)
    h = handover.Handover(
        run_id=manifest.run_id, branch=handover.branch(manifest.run_id, handover.SYNTHESIS),
        kind=merge.kind, handover_target=args.handover_target,
        accepted=bool(args.accept), out_of_band=oob,
        baseline_owned=_baseline_owned(manifest),
        b1_files=_b1_files(run_dir), why=merge.why)

    rounds = _rounds_run(run_dir)
    events = journal.Journal(storage.journal_path(run_dir)).read()
    seats = _seat_lines(run_dir, manifest)
    terminal = _review_terminal(run_dir, rounds, events)
    unresolved = _unresolved(run_dir, rounds)
    strongest, agreement = _strongest(run_dir), _agreement(run_dir)

    # AND THE RECORD IS BUILT HERE, WHICH IS THE SAME RULE ONE VALIDATION FURTHER ON. Every
    # check `Provenance.__post_init__` makes is over a value this function already holds by
    # now — the two argv words, the seat rows, the confirmed verify command, §11's label, §13's
    # terminal and its counts — and the ONLY one that needs §13.1's result is the type check on
    # `deep_review`. Built after the review, a mistyped `--strategy` bought
    # three provider calls of deep review and was then refused for a word. `--strategy` is easy to mistype
    # because there are TWO strategy vocabularies: §5 step 2's answer
    # sheet takes `gate.STRATEGY_RULES` (`size-gated`, `fusion`, `base-and-port`) while this
    # flag reports what the fusion FOLLOWED, which is `strategy.STRATEGIES` (`from_scratch`,
    # `partition`, `base_and_port`) — the same three ideas, and not one shared spelling.
    #
    # THE WHOLE RECORD RATHER THAN THOSE TWO CHECKS, because hoisting the two would leave the
    # other dozen behind the call and put the next reader back where this one started.
    p = handover.Provenance(
        seats=seats, synthesis_outcome=reported,
        # `False`, AND IT IS A CONSTANT HERE RATHER THAN A FLAG. Nothing in this function runs
        # the confirmed verify command over the fusion: §16 makes the orchestrator the
        # synthesis author, and building a verifier clone here would be a fourth §6 pass that
        # §5.2 never quoted. So the outcome above arrives from argv, which is a REPORT, and
        # `handover.header` renders it in the words of one. There is no `--synthesis-measured`
        # flag, because a flag is exactly how an asserted verdict would come to be printed
        # under the "Verified here means" sentence.
        synthesis_measured=False,
        # EVERY STEP, NOT `verify[0]`. §5.1's verify command is a LIST of argv lists — its
        # own motivating example is `cd frontend && npm ci` as two steps in two directories —
        # and rendering only the first told the operator the gate was narrower than the one
        # that actually ran. Reproduced: a confirmed `[["make","verify"],["make","lint"]]`
        # reported as `make verify`, with the lint step nowhere in the handover.
        #
        # ` && ` IS THE ACCURATE SHELL EQUIVALENT AND NOT A COSMETIC JOIN: `verify._run`
        # iterates the steps and `break`s on the first non-zero exit, so the gate passes only
        # if every step does — which is what `&&` means. A space-join would render two
        # commands as one unrunnable line.
        verify_command=" && ".join(" ".join(st.argv) for st in manifest.verify)
        if manifest.verify else "",
        # `None`, for the same reason and by the same rule `Provenance` states: a duration
        # beside an unmeasured verdict would time a run this engine says it did not perform,
        # and that record is refused. There is no `--synthesis-seconds` for a verdict nobody
        # took here to be timed by.
        verify_seconds=None, strategy=args.strategy,
        strongest=strongest, agreement=agreement,
        review_terminal=terminal, review_rounds=rounds,
        unresolved_findings=unresolved, deep_review=_UNREQUESTED)

    # THE SPEND, once, after everything above has agreed there is a handover to attach it to.
    #
    # WRITE-AHEAD, AND IDEMPOTENT, because this is the only thing `--collect` does that costs
    # money and it had NEITHER. §13.1 spends three provider calls and the old module carried no journal reference at
    # all (measured: zero), so a crash between the invocation and the report left nothing
    # saying money had been spent — and the next `--collect` spent it again. The engine's own
    # refusal text tells an operator to re-run this verb, which made the second charge the
    # documented path.
    #
    # The intent/done pair is `journal`'s, exactly as every other spending operation in this
    # package uses it, and the DONE record is the durable receipt: a completed deep review is
    # replayed from it rather than re-requested.
    enabled = _confirmed_deep_review(run_dir)
    log = journal.Journal(storage.journal_path(run_dir))
    u = _deep_review_already_spent(log.read())
    if u is None:
        op = f"deep-review-{manifest.run_id}"
        log.record(journal.intent(_DEEP_KIND), operation_id=op, round=max(1, rounds))
        u = deepreview.run_deep_review(run_dir, checkout=synth, base=manifest.baseline_commit,
                                       head=head, round_=max(1, rounds), enabled=enabled)
        # `findings` IS A TUPLE OF `review.Finding`, NOT A COUNT — a first draft wrote `int(u.bugs)`
        # and `handover` then called `len()` on it. `None` and `()` are DIFFERENT here and the
        # round-trip must keep them apart: `deepreview.DeepReview` refuses `findings=None` on a `ran` status
        # precisely because "the review ran and found nothing" and "no review ran" are the
        # false green this module exists to prevent.
        log.record(journal.done(_DEEP_KIND), operation_id=op,
                   status=u.status, reason=u.reason,
                   findings=None if u.findings is None else [_finding_row(b) for b in u.findings],
                   seats=list(u.seats), diff_measured=bool(u.diff_measured),
                   detail=u.detail,
                   # WRITTEN EVEN WHEN None, so a replayed record can tell "no seat reported"
                   # from "this run predates the field". `_deep_review_of` reads the ABSENCE
                   # of the key as the second, and the header then declines to name reporters.
                   reporting=None if u.reporting is None else list(u.reporting))
    else:
        print(f"§13.1 was already run for this run ({u.status}); replaying its recorded "
              "result rather than paying for it twice.", file=out)

    # `replace` RE-RUNS `__post_init__`, so the record that renders is validated carrying the
    # review this run actually got and not on the strength of the placeholder above.
    body = handover.text(h, dataclasses.replace(p, deep_review=u))
    if evidence:
        # PRINTED BESIDE THE HEADER RATHER THAN INSIDE IT. `handover.text` renders §16.1, whose
        # vocabulary is fixed; this is the citation for the sentence it already prints, and it
        # names the OID and the exact exit status so a reader can re-derive both.
        body = f"{body}\n\nSynthesis verify evidence: {evidence}."

    # §9's DRIFT AND §14.1's ORPHANS, WHICH THIS FUNCTION COMPUTED AND THREW AWAY.
    # `runstate.reconstruct` measures both — it is the first statement of this verb — and
    # every line below used `recon.manifest` and nothing else. So a delivery described
    # against a repository that had MOVED under the run, or a run holding an operation that
    # started and never finished, read exactly like one where neither was true.
    #
    # STATED, NOT REFUSED. The handover is still constructible and the operator still needs
    # it; what they must not do is read it as describing a repository it no longer describes.
    # That is the same disposition §5's acceptable gaps get — told once, in the operator's own
    # words, beside the artifact rather than inside §16.1's fixed vocabulary.
    if recon.diverged:
        body = (f"{body}\n\n§9 DRIFT: {', '.join(recon.diverged)} moved in {repo} since this "
                "run recorded it. The handover above describes the run's own snapshot, so "
                "re-read anything below that depends on the repository's current state.")
    if recon.orphans:
        ops = ", ".join(sorted({e.operation_id for e in recon.orphans}))
        body = (f"{body}\n\n§14.1 ORPHANS: {len(recon.orphans)} operation(s) started and "
                f"recorded no result ({ops}). Their outcome is UNKNOWN — not failed — so "
                "anything they would have written may be missing from the record above.")

    # AND THE RECORD IS PERSISTED LAST, BECAUSE §15 READS IT AS THE LICENCE TO DELETE THIS RUN.
    # `handover.json` is not a note about the collect: `gc.collect` refuses a run that has none
    # and reclaims one that has it — the synthesis worktree, both of §9's ref namespaces and the
    # whole run directory — with no `--force`. A `--collect` that goes on to REFUSE has handed
    # nothing over, so the file it leaves behind is a false claim of exactly the kind §15's
    # refusal exists to catch. Measured where this write used to sit, above the record that
    # checks the two argv words: `--collect --strategy fusion` was refused at exit 1 and
    # `read_handover` then answered non-None, `gc.usage` reported the run handed over, and
    # `gc.collect` removed SEVEN paths unforced. Every refusal this function can make is now
    # above this line, the rendering included — `handover.text` is the last of them, since its
    # `_deep_review_line` refuses a status added to `deepreview.STATUSES` that grew no rendering here. An
    # unwritten record costs the operator a re-collect; a wrongly written one costs them the
    # run.
    handover.write_handover(run_dir, h)
    print(body, file=out)
    return 0


def _other_clone_roots(run_dir) -> tuple:
    """Every clone root a reviewer must not find the ledger under, derived rather than listed.

    `assert_ledger_is_out_of_reach` REQUIRES this and never defaults it, for its own stated
    reason: `()` is a caller claiming no other clone root exists, which can be wrong out loud,
    while an omitted argument is a claim nobody made and is wrong silently. So this enumerates
    what is actually on disk — every seat attempt under `seats/` and every verifier clone —
    rather than reciting the shapes `runner` builds, which would be a second spelling of a
    layout that module owns.
    """
    root = Path(run_dir)
    out = []
    seats = root / "seats"
    if seats.is_dir():
        for seat in sorted(seats.iterdir()):
            if seat.is_dir():
                out.extend(sorted(a for a in seat.iterdir() if a.is_dir()))
    for child in sorted(root.iterdir()):
        # `runner.verifier_dir` is a SIBLING of `seats/` and its own docstring says why: a
        # verifier nested under the seat directory would be one `rm -rf seats/<name>` away
        # from taking the evidence with it. Matching on the prefix keeps that layout in
        # `runner` rather than restating the join here.
        if child.is_dir() and child.name.startswith("verify-"):
            out.append(child)
    return tuple(out)


def _confirmed_author(events, manifest) -> tuple:
    """The identity the operator agreed forge could work under, off the journal.

    IT IS IN THE JOURNAL AND NOT THE MANIFEST, and `gate.open_run` says why: §14.2's list of
    what the manifest holds does not include it, so the `confirm` operation's done record is
    the only copy. `runner._confirmed_policy` reads its neighbour the same way for the same
    reason.

    FAILS CLOSED. A clone made under an invented identity is a clone whose commits are
    attributed to somebody the operator never approved, and there is no safe default for a
    name — so a missing or malformed record refuses rather than substituting one.
    """
    done = journal.done("confirm")
    for e in reversed(events):
        if e.event == done and e.operation_id == manifest.run_id:
            author = e.data.get("author")
            if (isinstance(author, (list, tuple)) and len(author) == 2
                    and all(isinstance(x, str) and x.strip() for x in author)):
                return (author[0], author[1])
            raise CliError(
                f"run {manifest.run_id}'s confirm record carries author={author!r}, which is "
                "not the (name, email) pair §16 needs to attribute a clone's commits. There "
                "is no safe default for whose name forge works under.")
    raise CliError(
        f"run {manifest.run_id} has no `confirm` record on its journal, so the identity the "
        "operator agreed forge could work under cannot be read — and it is the only copy.")


def _review(args, *, out) -> int:
    """§13's review round — THE ONE VERB IN THIS FRONT END THAT CONVENES A REAL PANEL.

    IT SPENDS. Three provider calls per round at the wired `--retries 0`, which §5.2 priced
    and `--start` already quoted. Nothing below is reachable without them, so every refusal
    this function can make from disk comes first — the run must exist, carry a manifest, carry
    a synthesis worktree somebody fused in, and carry a ledger. That ordering is `collect`'s
    own rule one verb over, where it exists because a run with nothing to hand over used to
    pay $5-25 to be told so.

    THE PANEL READS A FRESH CLONE AND NEVER THE SYNTHESIS WORKTREE. `clone_review_tree` is
    what K4 built and this is its first production caller: the worktree is a LINKED worktree
    of the user's own repository, where `.git` is a file pointing at the parent's git dir, and
    three unattended bypass-permissions reviewers in it reach `hooks/`, `config` and the object
    store by ordinary relative path. A failure to get that tree is a round that cannot be
    convened, so there is no fallback here and deliberately no `except`.

    WHAT §13'S BLINDNESS IS. `assert_ledger_is_out_of_reach` runs first and refuses a run whose
    ledger it cannot read — but it asserts PATHS, and reviewers run as the operator's own UID
    with a shell. The panel is blind BY CONSTRUCTION (not given the path, bytes not in its
    tree, prompt does not name them) and not by containment; that function's docstring argues
    it in full and `SKILL.md` tells the operator. This verb does not claim more.

    ONE ROUND, NOT THE LOOP. `review.loop` needs a `fix` implementation to buy a second round,
    and this package has none — so a verb that called it would refuse at the first blocker
    having spent three calls. One round is what can honestly be driven today, and the terminal
    is left to `--collect`, which reads the record rather than this function's return value.
    """
    repo = Path(args.repo).resolve()
    run_dir = storage.run_root(repo, args.review, must_be_new=False)
    if not storage.manifest_path(run_dir).exists():
        try:
            run_dir.rmdir()
        except OSError:
            pass
        return _fail(out, [f"{repo} has no run {args.review!r}: nothing under XDG_STATE_HOME "
                           "records one, so there is no run to review."])
    manifest = runstate.read_manifest(run_dir)
    synth = run_dir / handover.SYNTHESIS
    if not synth.is_dir():
        return _fail(out, [f"run {manifest.run_id} has no synthesis worktree at {synth}, so "
                           "there is no candidate to review. Re-run `--start`, or create it "
                           "and fuse there."])
    if not storage.ledger_path(run_dir).exists():
        return _fail(out, [
            f"run {manifest.run_id} has no ledger. §13's blindness assertion refuses a check "
            "whose evidence is missing, so the round cannot be convened — write the ledger "
            f"and hand it over with `--ledger {manifest.run_id} --ledger-file <path>` first."])

    head = _rev(synth, "HEAD")
    tree = _rev(synth, "HEAD^{tree}")
    if tree == manifest.tracked_tree_oid:
        return _fail(out, [
            f"run {manifest.run_id}'s synthesis tree is still B1's ({tree}), so nobody fused "
            "into it and there is no candidate for a panel to read. Three provider calls "
            "would be spent describing an empty delivery."])

    round_ = _rounds_run(run_dir) + 1
    if round_ > manifest.review_rounds:
        return _fail(out, [
            f"run {manifest.run_id} has recorded {round_ - 1} round(s) and the operator "
            f"confirmed {manifest.review_rounds}. §5 step 2 asks once and §14.2 forbids "
            "rewriting the manifest, so this round is not one this run was quoted for."])

    log = journal.Journal(storage.journal_path(run_dir))
    quota = storage.Quota.for_harvest()
    others = _other_clone_roots(run_dir)
    tree_dir = reviewmod.clone_review_tree(
        repo, run_dir, run_id=manifest.run_id, round_=round_, checkpoint=head,
        identity=_confirmed_author(log.read(), manifest))

    before_digest, before = reviewmod.worktree_identity(tree_dir, quota)
    reviewmod.record_worktree_before(log, round_=round_, digest=before_digest,
                                     entries=len(before))
    r = reviewmod.run_round(
        run_dir, round_=round_, checkout=tree_dir, checkpoint=head,
        baseline_commit=manifest.base_commit, baseline_tree=manifest.tracked_tree_oid,
        artifact_manifest=None, log=log,
        other_clones=tuple(others) + (Path(synth),))
    after_digest, after = reviewmod.worktree_identity(tree_dir, quota)
    reviewmod.record_worktree_after(log, round_=round_, digest=after_digest,
                                    entries=len(after),
                                    changed=snapshot.diff(before, after))

    print(f"round {round_}: {len(r.seats_responded)} of "
          f"{len(r.seats_responded) + len(r.seats_silent)} reviewers answered", file=out)
    for seat, why in r.seats_silent:
        print(f"  silent: {seat} ({why})", file=out)
    for f in r.findings:
        print(f"  [{f.severity}] {f.seat}: {f.claim} — {f.evidence}", file=out)
    if after_digest != before_digest:
        # SAID, NOT SWALLOWED. The bracket moving means the findings describe a tree that did
        # not stand still, and `terminal_from_record` is what refuses over it — this line is so
        # the operator learns it here rather than from a terminal two verbs later.
        print("  the review tree MOVED during the round, so these findings describe a state "
              "this run cannot vouch for", file=out)
    print(f"Next: `--collect {manifest.run_id}` reads the record and classifies it.", file=out)
    return 0


def _verify_fix(args, *, out) -> int:
    """§13's post-review fix, measured rather than asserted.

    WHY THIS IS A VERB AND NOT PART OF `--review`. §16 makes the ORCHESTRATOR the synthesis
    author and `SKILL.md` says the engine "deliberately has no `--synthesize`". Applying a
    blocker's fix is authoring code; an engine that applied it would then be verifying its
    own work, which is the founding premise inverted. So the loop is: `--review` reports the
    blockers, YOU fix them and commit in the synthesis worktree, and this verifies that the
    fix survived the gate — in a clone you never touched.

    IT SPENDS NO PROVIDER CALL. One setup and one verify, which §5.2 priced as part of the
    post-review pass; the money in §13 is the panel, and `--review` is where that goes.

    THE CHECKPOINT IS THE LAST ROUND'S, read off the record rather than taken from argv — a
    caller who passed the wrong one would have this engine verify a fix against a round that
    did not raise the blockers it is answering.
    """
    repo = Path(args.repo).resolve()
    run_dir = storage.run_root(repo, args.verify_fix, must_be_new=False)
    if not storage.manifest_path(run_dir).exists():
        try:
            run_dir.rmdir()
        except OSError:
            pass
        return _fail(out, [f"{repo} has no run {args.verify_fix!r}."])
    manifest = runstate.read_manifest(run_dir)
    rounds = _rounds_run(run_dir)
    if rounds < 1:
        return _fail(out, [
            f"run {manifest.run_id} has recorded no review round, so there are no blockers a "
            f"fix could be answering. Run `--review {manifest.run_id}` first."])
    last = reviewmod.read_round(run_dir, rounds)
    log = journal.Journal(storage.journal_path(run_dir))
    blockers = tuple(f for f in last.findings if f.severity == "blocker")
    checkpoint, verified, cand, base = fixmod.apply(
        run_dir, repo, findings=blockers, checkpoint=last.checkpoint, manifest=manifest,
        identity=_confirmed_author(log.read(), manifest), events=log.read())
    if checkpoint is None and not verified:
        if cand is None and base is None:
            print("no fix to verify: the synthesis worktree is still on the checkpoint round "
                  f"{rounds} was convened at, so nothing was committed.", file=out)
        else:
            print(f"the fix did NOT pass verify, so round {rounds}'s {len(blockers)} "
                  "blocker(s) stay unresolved (§13).", file=out)
        return 1
    print(f"the fix passed verify at checkpoint {checkpoint}.", file=out)
    if cand is not None and base is not None:
        prog = progress.from_runs(cand, base)
        print(f"  §12.3 progress: new failures {prog.new_failure_count}", file=out)
    else:
        # SAID, NOT SILENT. `Progress(None, None)` is what an unmeasured pair produces, and
        # an operator reading a clean line would take it for a measurement.
        print("  §12.3 progress: NOT MEASURED — the calibration's output is not on this "
              "run's journal, or was clipped, so which failures are new cannot be said",
              file=out)
    print(f"Next: `--review {manifest.run_id}` for another round, or "
          f"`--collect {manifest.run_id}`.", file=out)
    return 0


def _ledger(args, *, out) -> int:
    """§10's claim ledger, authored by the ORCHESTRATOR and persisted by this engine.

    WHY THIS IS A VERB AND NOT A GENERATOR. §10 says writing the ledger "requires reading all
    three artifact sets"; §13 says the orchestrator consults it AFTER independent findings; §16
    makes the orchestrator the synthesis author. So there is nothing here for this engine to
    author, and everything for it to CHECK — which is `ledger.write_ledger`, whose refusals are
    the validation. This function adds no check of its own and catches none of its.

    WHY IT EXISTS AT ALL. `review.assert_ledger_is_out_of_reach` refuses a run whose ledger it
    cannot read, and it is `run_round`'s first statement — so without a written ledger §13's
    review is unreachable rather than merely unrun.

    NOTHING IN THIS FRONT END READS THE LEDGER BACK. §12.5's rank needs four things together —
    a coverage report over these rows, §6.2's gate outcome, §13's review risk and §12.1's
    measured size — and this build reaches only the gate outcome. So `_strongest` still names
    nobody and says which of the four is missing. A rank taken off a ledger the ORCHESTRATOR
    authored, with the blind panel that could contradict it not yet convened, would be a
    verdict with no adversary. Storing it is not trusting it.

    IT REFUSES A RUN THAT DOES NOT EXIST rather than opening one. `storage.run_root` creates,
    which is why `collect` removes the directory it made on that path; this does the same.
    """
    if not args.ledger_file:
        raise CliError("--ledger needs --ledger-file: the rows are the orchestrator's and "
                       "there is nothing for this engine to write without them")
    repo = Path(args.repo).resolve()
    run_dir = storage.run_root(repo, args.ledger, must_be_new=False)
    if not storage.manifest_path(run_dir).exists():
        try:
            run_dir.rmdir()
        except OSError:
            pass
        return _fail(out, [f"{repo} has no run {args.ledger!r}: nothing under XDG_STATE_HOME "
                           "records one, so there is no run to give a ledger to."])
    text = _read(args.ledger_file, f"the ledger {args.ledger_file}")
    try:
        payload = json.loads(text)
    except ValueError as e:
        raise CliError(f"the ledger {args.ledger_file} is not readable as JSON: {e}") from e
    if not isinstance(payload, dict):
        raise CliError(f"§10's ledger is an object, not a {type(payload).__name__}")
    try:
        l = ledgermod.decode_payload(payload)
        ledgermod.write_ledger(run_dir, l)
    except ledgermod.LedgerError as e:
        # NOT CAUGHT AND REPORTED AS WRITTEN. Every refusal in that module is a row §12.5 would
        # otherwise be ranked on and §13 would otherwise be asserted blind against.
        raise CliError(f"this ledger was not written: {e}") from e
    print(f"ledger: {storage.ledger_path(run_dir)} "
          f"({len(l.rows)} row(s), hash {ledgermod.ledger_hash(l)})", file=out)
    return 0


def _gc(args, *, out) -> int:
    """§15's cleanup, or its disk report when the run id is `all`.

    `all` IS NOT A RUN ID ANY RUN CAN HAVE — `storage.new_run_id` draws six hex characters —
    so the two readings of this argument cannot collide.
    """
    repo = Path(args.repo).resolve()
    if args.gc == "all":
        rows = gcmod.usage(repo)
        if not rows:
            print("no forge runs are on disk for this repository", file=out)
            return 0
        total = sum(r.bytes_ for r in rows if r.bytes_ is not None)
        unknown = [r for r in rows if r.bytes_ is None]
        for r in rows:
            size = "unknown" if r.bytes_ is None else f"{r.bytes_ / 1e9:.2f} GB"
            files = "unknown" if r.files is None else f"{r.files} file(s)"
            # THREE MARKS, BECAUSE THERE ARE THREE STATES. `handed_over is None` is a record
            # this engine could not read, and printing it as "NOT handed over" would tell an
            # operator that a delivery they made is unfinished work — the one sentence that
            # gets a deliverable deleted.
            mark = ("handed over" if r.handed_over is True else
                    "NOT handed over" if r.handed_over is False else
                    "handover record UNREADABLE")
            print(f"  {r.run_id}  {size:>12}  {files:>14}  {mark}", file=out)
            if r.why:
                print(f"      {r.why}", file=out)
        print(f"  total: {total / 1e9:.2f} GB over {len(rows) - len(unknown)} run(s)", file=out)
        if unknown:
            # THE TOTAL IS NOT THE WHOLE ANSWER AND SAYS SO. A sum that silently omitted an
            # unwalkable run would be the one number an operator acts on, quietly short.
            print(f"  {len(unknown)} run(s) could not be measured and are NOT in that total",
                  file=out)
        return 0
    for line in gcmod.collect(repo, args.gc, force=args.force):
        print(f"  removed: {line}", file=out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="llm-forge", description=__doc__.splitlines()[0])
    verb = ap.add_mutually_exclusive_group(required=True)
    verb.add_argument("--start", action="store_true",
                      help="open a run: preflight, §5's gate, the fleet, §6's verification")
    verb.add_argument("--resume", metavar="RUN_ID",
                      help="continue a run that stopped, without re-spending its "
                           "provider calls")
    verb.add_argument("--collect", metavar="RUN_ID",
                      help="read a run back off disk, run §13.1, and print §16's handover")
    verb.add_argument("--ledger", metavar="RUN_ID",
                      help="persist the §10 claim ledger the ORCHESTRATOR authored, which "
                           "§13's review and §12.5's rank both read and neither can be "
                           "reached without")
    verb.add_argument("--review", metavar="RUN_ID",
                      help="convene §13's review panel over a run's synthesis candidate — "
                           "SPENDS three provider calls per round")
    verb.add_argument("--verify-fix", dest="verify_fix", metavar="RUN_ID",
                      help="§13: verify, in a FRESH clone, the fix you committed in the "
                           "synthesis worktree after a review round. Spends no provider "
                           "call; runs the confirmed setup and verify commands once")
    verb.add_argument("--gc", metavar="RUN_ID",
                      help="§15's cleanup for one run, or `--gc all` for the disk report")
    ap.add_argument("--repo", default=".", help="the repository the run is about")
    ap.add_argument("--task", help="--start: the directory holding the task bundle")
    ap.add_argument("--entrypoint", default="TASK.md",
                    help="--start: the bundle entry the seats are told to read")
    ap.add_argument("--answers", help="--start: the JSON answer sheet for §5 step 2")
    ap.add_argument("--select", action="append", default=[],
                    help="--start: an untracked path to carry into the baseline (repeatable)")
    ap.add_argument("--seats", type=int, default=3)
    ap.add_argument("--attempts", type=int, default=3)
    # DEFAULT 1 = SERIAL, which is what every run did before this flag existed. §19's window
    # is a timeout rather than a budget, so builders sharing one machine can push a seat past
    # its cap; the quote states that trade and the operator opts in, rather than this front
    # end taking it for them.
    ap.add_argument("--concurrency", type=int, default=1,
                    help="--start: how many builders run at once (default 1 = serial). "
                         "Lowers the quoted wall-clock ceiling; raises the risk of a seat "
                         "timing out under contention")
    ap.add_argument("--review-rounds", type=int, default=2, dest="review_rounds")
    ap.add_argument("--skip-deep-review", action="store_true",
                    help="§13.1 is default on; this opts out and re-prices the run")
    ap.add_argument("--handover-target", dest="handover_target",
                    help="--collect: where the work went, so §15 can define 'unmerged'")
    ap.add_argument("--accept", action="store_true",
                    help="--collect: the user accepts delivery with no merge target")
    ap.add_argument("--ledger-file", dest="ledger_file",
                    help="--ledger: the JSON file holding §10's rows. Validated by "
                         "`ledger.write_ledger`'s own refusals and written only if it passes "
                         "every one of them")
    ap.add_argument("--verified-at", dest="verified_at", metavar="OID",
                    help="--collect: the OID the orchestrator's verify ran at. Refused unless "
                         "it is this run's synthesis HEAD — a verdict about another tree is a "
                         "verdict about another artifact")
    ap.add_argument("--verify-exit", dest="verify_exit", type=int, metavar="N",
                    help="--collect: the exit status that verify returned. This engine did "
                         "not run it and the handover says so; the status is EVIDENCE, and "
                         "the exact number is printed so a 127 is not read as a 1")
    ap.add_argument("--verify-log", dest="verify_log", metavar="PATH",
                    help="--collect: the captured stdout of that run, recorded by size and "
                         "digest so the handover cites something a reader can re-derive")
    ap.add_argument("--strategy",
                    help="--collect: the §12 strategy the fusion FOLLOWED, as the orchestrator "
                         "reports it. Not checked against the rule §5 step 2 confirmed: that "
                         "check is `strategy.decide`, which needs §12.1's measured size over "
                         "the candidate bundles, and nothing serializes one")
    ap.add_argument("--force", action="store_true", help="--gc: see §15")
    return ap


def main(argv=None, *, out=None, make_launcher=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        if args.start:
            for required in ("task", "answers"):
                if not getattr(args, required):
                    return _fail(out, [f"--start needs --{required}"])
            return start(args, out=out, make_launcher=make_launcher)
        if args.resume:
            return _resume(args, out=out, make_launcher=make_launcher)
        if args.collect:
            return collect(args, out=out)
        if args.ledger:
            return _ledger(args, out=out)
        if args.review:
            return _review(args, out=out)
        if args.verify_fix:
            return _verify_fix(args, out=out)
        return _gc(args, out=out)
    except (CliError, preflight.PreflightError, gate.GateError, taskbundle.TaskBundleError,
            # §15's refusals, which are the whole of what `--gc` says when it will not delete
            # something. Every one of them is this package declining to remove a path it
            # cannot describe, so printing the sentence is the right end.
            gcmod.GcError,
            handover.HandoverError, verify.VerifyError, deepreview.DeepReviewError,
            reviewmod.ReviewError, fingerprint.FingerprintError, baselinemod.BaselineError,
            # `--resume`'s refusals, and every one of them is this package declining to
            # continue a run it cannot prove — the same shape as the GcError entry above.
            resumemod.ResumeError,
            # MEASURED, and the first draft of this tuple was missing all four.
            # `runstate.reconstruct` — the only thing `--collect` does before it can say
            # anything — raises exactly these for a run directory it cannot read whole, and
            # every one is a direct RuntimeError subclass that would otherwise leave the CLI
            # with a traceback instead of a refusal. They belong here for the SAME reason as
            # the others and not as a widening: each is this package saying "this run's
            # state is unknown", which §14.1 names `outcome_unknown` — the one thing a
            # `--collect` must be able to tell an operator.
            runstate.ManifestError, runstate.StateError,
            journal.JournalError, storage.StorageError) as e:
        # NARROW ON PURPOSE. Every class here is one this package raises to say "I will not
        # do that", and printing it is the right end. A bare `except Exception` would turn a
        # crash mid-fleet — three paid seats, a half-written run directory — into the same
        # one-line message as a rejected answer sheet, which is the state §14.1 calls
        # `outcome_unknown` and says must be distinguishable.
        #
        # WHAT IS DELIBERATELY ABSENT. `gitcmd.GitError` is raised by every git call this
        # package makes, so admitting it here would give a failure mid-fleet the same
        # rendering as a bad `--repo`; the one place it is an argument error is caught at that
        # call. `ValueError` is absent for `reconstruct`'s own reason — a manifest path
        # carrying a NUL reaches `os.lstat` and raises one, and that is not this package
        # refusing anything.
        return _fail(out, [str(e)])
