"""The front end: `--start`, `--collect`, `--gc`.

WHAT THIS IS AND IS NOT. `--start` drives §5's gate, §7's fleet and §6's verification, and it
STOPS where `runner.run` stops — at `comparing`, with the candidates verified on disk and
nothing choosing between them. The choosing is §10 through §13, and §16 says who does it: "the
synthesis author is the trusted invoking orchestrator under its normal approval boundary — not
a fourth unattended bypass-permissions seat." That orchestrator is the agent running SKILL.md,
not this module, which is why there is no `--synthesize`.

`--collect` is the other half: it reads a run back off the disk, runs §13.1's cloud review
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
import json
import shutil
import sys
from pathlib import Path

from council import engine

from . import (brief as briefmod, fingerprint, gate, gitcmd, handover, journal, launch,
               preflight, review as reviewmod, runstate, storage, taskbundle, ultra, verify)
# `ledger` and NOTHING ELSE: this verb PERSISTS §10's rows and reads nothing off
# them, so `coverage` and `rubric` stay out of this import block until something
# ranks — a rank taken before §13's panel can be convened honestly would be a
# verdict with no adversary.
from . import ledger as ledgermod
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

    `ultrareview` IS REFUSED IN THE SHEET. It is decided by `--no-ultra`, which also re-prices
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
    if "ultrareview" in answers:
        raise CliError(
            "the answer sheet names `ultrareview`, which is decided by `--no-ultra` because "
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
    mk = make_launcher or launch.make_launcher
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
    timeout = _resolve_seat_timeout()
    answers = _answer_sheet(args.answers)
    quote_ = gate.quote(report, seats=args.seats, attempts=args.attempts,
                        review_rounds=args.review_rounds, ultrareview=not args.no_ultra)
    if "verify" not in answers:
        # `must_show` resolves §6.1's surface from the confirmed command, so the sheet has to
        # carry one before the operator can be shown anything. `gate.confirm` gives the same
        # refusal for the same key; this one exists because the line above it needs the value.
        raise CliError("the answer sheet names no `verify` command, and §6.1's gate surface — "
                       "the first thing §5 step 2 shows — is resolved from it")
    command = verify.Command(steps=_steps(answers["verify"]))
    for line in gate.must_show(report, quote_, command):
        print(f"  {line}", file=out)

    # ONE DECISION, WRITTEN ONCE. `--no-ultra` priced the quote above and answers §5 step 2
    # here; `gate.confirm` refuses the two if they ever disagree, and `--collect` reads the
    # recorded answer rather than re-deriving it from a flag it is not given.
    answers["ultrareview"] = not args.no_ultra
    confirmation = gate.confirm(report, quote_, answers)

    run_id = storage.new_run_id()
    run_dir = gate.open_run(report, confirmation, run_id)

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
    for line in _seat_table(run_dir):
        print(f"  {line}", file=out)
    print(f"Next: fuse in the synthesis worktree, then `--collect {run_id}`.", file=out)
    return 0


def _seat_lines(run_dir) -> tuple:
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
    lines = []
    for name in storage.seat_names(run_dir):
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


def _seat_table(run_dir) -> tuple:
    """What `--start` prints about the fleet it just ran, off the same rows §16.1 counts."""
    return tuple(
        f"{s.name}: {s.forge}, artifacts {s.artifacts}, verify "
        f"{s.verify_outcome or 'not run'}"
        for s in _seat_lines(run_dir))


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
    dimension, for every seat, until §13's review is wired. A rank here would therefore not be
    a rank taken over a quarter of the rubric; it would be one taken over a rubric this front
    end cannot fill in at all.

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


def _confirmed_ultrareview(run_dir) -> bool:
    """The operator's §13.1 answer, read back off the record `gate.open_run` wrote.

    `runner._confirmed_policy`'s shape and its argument, one answer over: it is journalled on
    the `confirm` operation's own done record and nowhere else, so this reads it there and
    FAILS CLOSED when it is missing or is not a bool. A default here would be this process
    deciding whether to spend $5-25 of the operator's usage credits on a run that cannot say
    what it agreed to.
    """
    manifest = runstate.read_manifest(run_dir)
    done = journal.done("confirm")
    events = journal.Journal(storage.journal_path(run_dir)).read()
    for e in reversed(events):
        if e.event == done and e.operation_id == manifest.run_id:
            value = e.data.get("ultrareview")
            if isinstance(value, bool):
                return value
            raise CliError(
                f"this run's {done} record carries ultrareview={value!r}, which is not the "
                "bool §5 step 2 records. §13.1's review is priced in money and this engine "
                "will not read a shape it cannot tell on from off.")
    raise CliError(
        f"this run's journal has no {done} record for {manifest.run_id!r}, so what the "
        "operator answered about §13.1's cloud review is not recorded anywhere. It is priced "
        "in usage credits and has no safe default, so this collect will not choose one.")


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
# validates BEFORE the spend therefore needs an `Ultra` to be a record at all; the only honest
# one for a review nobody has asked for yet is a `skipped` whose detail says so. It never
# reaches a header — `dataclasses.replace` puts the review's own result in its place — so the
# detail is written for the one reader who would ever see it: somebody looking at a handover
# where that replacement did not happen.
_UNREQUESTED = ultra.Ultra(
    ultra.SKIPPED, None, None, None, False,
    "this run's record was validated before §13.1 was asked for and the placeholder it was "
    "checked with was never replaced by the review's own result — so this line describes a "
    "defect in `--collect` and NOT a review the operator declined")


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
    # from disk, and all three used to run AFTER the cloud review, so a run with nothing to
    # hand over paid $5-25 to be told so. The reads below are what the header is built from as
    # well, so the same ordering makes the whole handover provably constructible before the
    # one call that cannot be taken back — including `Provenance`, whose own refusals are
    # argued where it is built.
    head = _rev(synth, "HEAD")
    tree = _rev(synth, "HEAD^{tree}")
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
    seats = _seat_lines(run_dir)
    terminal = _review_terminal(run_dir, rounds, events)
    unresolved = _unresolved(run_dir, rounds)
    strongest, agreement = _strongest(run_dir), _agreement(run_dir)

    # AND THE RECORD IS BUILT HERE, WHICH IS THE SAME RULE ONE VALIDATION FURTHER ON. Every
    # check `Provenance.__post_init__` makes is over a value this function already holds by
    # now — the two argv words, the seat rows, the confirmed verify command, §11's label, §13's
    # terminal and its counts — and the ONLY one that needs §13.1's result is the type check on
    # `ultra`. Built after the review, a mistyped `--strategy` or `--synthesis-outcome` bought
    # $5-25 of cloud review and was then refused for a word, and `--strategy` is the likelier
    # of the two to be mistyped because there are TWO strategy vocabularies: §5 step 2's answer
    # sheet takes `gate.STRATEGY_RULES` (`size-gated`, `fusion`, `base-and-port`) while this
    # flag reports what the fusion FOLLOWED, which is `strategy.STRATEGIES` (`from_scratch`,
    # `partition`, `base_and_port`) — the same three ideas, and not one shared spelling.
    #
    # THE WHOLE RECORD RATHER THAN THOSE TWO CHECKS, because hoisting the two would leave the
    # other dozen behind the call and put the next reader back where this one started.
    p = handover.Provenance(
        seats=seats, synthesis_outcome=args.synthesis_outcome,
        # `False`, AND IT IS A CONSTANT HERE RATHER THAN A FLAG. Nothing in this function runs
        # the confirmed verify command over the fusion: §16 makes the orchestrator the
        # synthesis author, and building a verifier clone here would be a fourth §6 pass that
        # §5.2 never quoted. So the outcome above arrives from argv, which is a REPORT, and
        # `handover.header` renders it in the words of one. There is no `--synthesis-measured`
        # flag, because a flag is exactly how an asserted verdict would come to be printed
        # under the "Verified here means" sentence.
        synthesis_measured=False,
        verify_command=" ".join(manifest.verify[0].argv) if manifest.verify else "",
        # `None`, for the same reason and by the same rule `Provenance` states: a duration
        # beside an unmeasured verdict would time a run this engine says it did not perform,
        # and that record is refused. There is no `--synthesis-seconds` for a verdict nobody
        # took here to be timed by.
        verify_seconds=None, strategy=args.strategy,
        strongest=strongest, agreement=agreement,
        review_terminal=terminal, review_rounds=rounds,
        unresolved_findings=unresolved, ultra=_UNREQUESTED)

    # THE SPEND, once, after everything above has agreed there is a handover to attach it to.
    enabled = _confirmed_ultrareview(run_dir)
    u = ultra.run_ultra(run_dir, checkout=synth, base=manifest.baseline_commit, head=head,
                        round_=max(1, rounds), enabled=enabled)

    # `replace` RE-RUNS `__post_init__`, so the record that renders is validated carrying the
    # review this run actually got and not on the strength of the placeholder above.
    body = handover.text(h, dataclasses.replace(p, ultra=u))

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
    # `_ultra_line` refuses a status added to `ultra.STATUSES` that grew no rendering here. An
    # unwritten record costs the operator a re-collect; a wrongly written one costs them the
    # run.
    handover.write_handover(run_dir, h)
    print(body, file=out)
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
    verb.add_argument("--collect", metavar="RUN_ID",
                      help="read a run back off disk, run §13.1, and print §16's handover")
    verb.add_argument("--ledger", metavar="RUN_ID",
                      help="persist the §10 claim ledger the ORCHESTRATOR authored, which "
                           "§13's review and §12.5's rank both read and neither can be "
                           "reached without")
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
    ap.add_argument("--review-rounds", type=int, default=2, dest="review_rounds")
    ap.add_argument("--no-ultra", action="store_true",
                    help="§13.1 is default on; this opts out and re-prices the run")
    ap.add_argument("--handover-target", dest="handover_target",
                    help="--collect: where the work went, so §15 can define 'unmerged'")
    ap.add_argument("--accept", action="store_true",
                    help="--collect: the user accepts delivery with no merge target")
    ap.add_argument("--ledger-file", dest="ledger_file",
                    help="--ledger: the JSON file holding §10's rows. Validated by "
                         "`ledger.write_ledger`'s own refusals and written only if it passes "
                         "every one of them")
    ap.add_argument("--synthesis-outcome", dest="synthesis_outcome",
                    help="--collect: the verify verdict the orchestrator REPORTS. This engine "
                         "does not run it, and the handover says so rather than calling it "
                         "verified")
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
        if args.collect:
            return collect(args, out=out)
        if args.ledger:
            return _ledger(args, out=out)
        return _gc(args, out=out)
    except (CliError, preflight.PreflightError, gate.GateError, taskbundle.TaskBundleError,
            # §15's refusals, which are the whole of what `--gc` says when it will not delete
            # something. Every one of them is this package declining to remove a path it
            # cannot describe, so printing the sentence is the right end.
            gcmod.GcError,
            handover.HandoverError, verify.VerifyError, ultra.UltraError,
            reviewmod.ReviewError, fingerprint.FingerprintError, baselinemod.BaselineError,
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
