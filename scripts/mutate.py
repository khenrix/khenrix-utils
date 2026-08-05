#!/usr/bin/env python3
"""Run one mutation and report whether the suite catches it.

Checked in rather than re-authored per task. The bytecode guard below was present in five
harnesses written on one day and absent from twelve written the next, and its absence
manufactures a FALSE SURVIVED: a verdict that costs the reader work, because a survivor is
what makes someone add a test or argue the mutant is equivalent.

The opposite verdict is the cheaper one to produce and the more expensive one to hold. A
FALSE CAUGHT makes someone NOT add a test, and it leaves nothing behind to re-examine — the
row reads covered and the hole stays. So `rc != 0` is not by itself evidence that the suite
noticed anything, and this script refuses to call it CAUGHT until it has watched the same
command pass against the UNMUTATED file.

A GREEN BASELINE IS ONLY HALF OF THAT ARGUMENT, AND THE MISSING HALF SHIPPED HERE FOR
LONGER THAN THE PARAGRAPH ABOVE DID. `rc != 0` was read as CAUGHT for every nonzero status,
so a `--new` that left a `{` unclosed made pytest exit 2 — one error during collection — and
the row was written CAUGHT for a mutant that never executed a line. Reproduced on this repo
against `tests/test_forge_coverage.py`. Only exit 1, `tests failed`, is evidence the suite
noticed the mutation; 0 is SURVIVED; 2, 3, 4, 5 and any status not in `_PYTEST_RC` are the
run failing to happen, and each is refused BY NAME rather than scored, because a verdict must
never read cleaner than its evidence.

REFUSING BY EXIT STATUS ONLY CLOSED THAT HOLE FOR PYTEST, AND THE ROW-KEEPING WORKAROUND IS
THE EVIDENCE. Against a BARE-SCRIPT suite — `python3 scripts/lib/checks.py --self-test`,
`scripts/eval_trigger.py --self-test`, anything whose failures are its own exit code — the
interpreter exits 1 for an uncaught exception, which is the identical status a real failure
reports. So the same unparseable mutant that pytest refused was still scored CAUGHT there,
and plans in docs/superpowers/ carry the instruction "Discard any row whose mutant broke the
syntax": a human filter standing in for a check, applied by whoever remembers. MEASURED
2026-08-04 while mutating this repo's own `scripts/lib/checks.py`: three separate mutants
exited 1 through a SyntaxError, a NameError and an IndexError, and all three printed CAUGHT.

THE PATH IS CLOSED AT TWO SEAMS, one per cause.

  1. THE MUTANT MUST PARSE, checked with `compile()` BEFORE it is written to disk. A `--new`
     that leaves the file unparseable is refused for every runner at once — pytest's exit 2
     and the bare script's exit 1 have the same origin, and this kills it upstream of both
     rather than teaching each runner's status table about it. Only the ORIGINAL-parses case
     is refused, so mutating a file that never parsed is still the operator's business, and
     only `.py` files are checked because only they make the claim.

  2. AN UNCAUGHT TRACEBACK ON STDERR IS THE BARE-SCRIPT SPELLING OF EXIT 2, and is refused
     the same way. A process that dies of an exception writes `Traceback (most recent call
     last):` and terminates on the exception line; a suite REPORTING failures does not. This
     is the discriminator the exit code cannot carry, so it is read from the channel that
     does carry it.

WHAT IS STILL NOT CLOSED, AND WHAT TO DO ABOUT IT. A bare-script suite that CATCHES its own
exception, prints no traceback and exits 1 anyway is indistinguishable from one reporting a
failure, and no check here can separate them — the runner has thrown the distinction away
before this process sees anything. For that, wrap the suite to pytest's conventions (exit 2
when the run did not happen) before handing its status here; the two seams above are what
make an unwrapped runner safe for the cases that actually occur, not a licence to skip that.

AND SEAM 2 ERRS TOWARDS REFUSING, WHICH IS THE DIRECTION TO ERR IN. A suite that prints a
caught traceback at column 0 and whose FINAL stderr line is itself exception-shaped trips
both halves of the signature and is refused, though it did reach a verdict. That costs a row
and a re-run; guessing the other way costs a covered row over a hole, which is the asymmetry
the top of this file is about. Print a summary line after the traceback and the run scores
normally.

    scripts/mutate.py --file shared/lib/forge/verify.py \
        --old 'VERIFIER_NAME = "verify"' --new 'VERIFIER_NAME = "claude"' \
        -- uvx pytest tests/test_forge_seams.py -q

Exit 0 when the mutant is CAUGHT (a green suite went red), 1 when it SURVIVED, 2 on a usage
or application error — including a baseline that was not green, and a mutated run whose exit
status is neither 0 nor 1. The source file is restored from the bytes read at start, always.

CHECK `git status` BEFORE YOU TRUST A RUN, AND AGAIN AFTER. "Always" above means the `finally`
below, and a `finally` only runs if this process reaches it: a SIGKILL, a session torn down
mid-run, or a machine that goes away leaves the mutant on disk. It has happened here —
`gate.py` was found still carrying mutant #9 (`user_config=True` stripped from
`propose_identity`) after an agent's teardown, with the next suite green because the mutation
was in a call site nothing had pinned yet. That is the FALSE CAUGHT this file is written
against, arriving by the one route it cannot close from inside. `git status` costs nothing and
is the only thing that sees it; a new mechanism here would be a second `finally` with the same
hole. `git checkout -- <file>` restores, and purge `__pycache__` after (see `_purge_bytecode`)
or the stale bytecode outlives the source.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _purge_bytecode(roots) -> int:
    """Delete every `__pycache__` under `roots`, and say how many.

    THE WHOLE POINT OF THIS SCRIPT. CPython's source-based `.pyc` header records the source's
    mtime as INTEGER SECONDS plus its size, and reuses the cache when both match. A
    length-preserving edit inside the same second — `"verify"` to `"claude"` — leaves both
    valid, so the interpreter runs the PRE-EDIT bytecode and the suite passes against code
    that is no longer on disk. Reproduced: header (1785594485, 16) stayed valid across the
    rewrite and the mutant never executed.

    Deleting the cache is necessary but not sufficient; the run also needs
    PYTHONDONTWRITEBYTECODE, or this process's own import writes a fresh `.pyc` that the test
    subprocess then reuses after the source is restored.
    """
    n = 0
    for root in roots:
        for d in sorted(Path(root).rglob("__pycache__")):
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


class _CannotRun(RuntimeError):
    """The test command never started, which is a fact about the command and not a verdict."""


# pytest's documented exit codes. 0 and 1 are the only two that are VERDICTS here; every other
# status is a fact about the run, and the table exists so a refusal can say which one the runner
# reported instead of printing a bare integer.
_PYTEST_RC = {1: "tests failed", 2: "interrupted, e.g. an error during collection",
              3: "internal error", 4: "usage error", 5: "no tests were collected"}
_TESTS_FAILED = 1

# The header CPython writes before an uncaught exception unwinds the process. Anchored to
# column 0 and to a whole line: a harness that FORMATS a caught exception into its own
# report indents the traceback it prints, and an indented one is a suite DESCRIBING a
# failure rather than a process dying of one.
_TRACEBACK_HEAD = re.compile(r"^Traceback \(most recent call last\):$", re.M)
# The LAST line of that unwind: `ExceptionName: message`, or a bare `ExceptionName`, at
# column 0. Required as well as the header, and it is the half that does most of the work:
# `traceback.print_exc()` also writes its header at column 0, so a suite that prints a
# caught traceback and then carries on reporting is told apart by what it says LAST. A dying
# process always ends on the exception line; a reporting one goes on to say something else.
_EXC_TAIL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(:|$)")


def _parses(text: str, path: Path) -> bool:
    try:
        compile(text, str(path), "exec")
    except SyntaxError:
        return False
    return True


def _died_of_an_exception(stderr: str) -> str | None:
    """The exception line, when `stderr` shows a process that was UNWOUND rather than a
    suite that reported. None otherwise.

    THE BARE-SCRIPT SPELLING OF PYTEST'S EXIT 2. A runner whose only failure signal is its
    own exit code cannot distinguish `assert` from `SyntaxError`; both are 1. The traceback
    is on a different channel and does distinguish them, so the evidence is read from there.
    """
    if not _TRACEBACK_HEAD.search(stderr or ""):
        return None
    lines = [ln for ln in (stderr or "").splitlines() if ln.strip()]
    if lines and _EXC_TAIL.match(lines[-1]):
        return lines[-1][:200]
    return None


def _run(cmd, env) -> tuple[int, str]:
    """(returncode, stderr). stdout streams through; stderr is captured AND re-emitted.

    Captured because `_died_of_an_exception` has to read it, re-emitted because the operator
    still has to see it. stdout is left alone so pytest's report stays live — an interpreter
    traceback never goes there, so nothing this needs is lost by not capturing it."""
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, stderr=subprocess.PIPE, text=True)
    except OSError as e:
        # A command that cannot START is a usage error, and it has to be told apart from a
        # verdict HERE. The uncaught traceback exits 1, and 1 is the machine-readable spelling
        # of SURVIVED — so a harness building a mutation table off exit codes records a
        # survivor for a typo'd test command.
        raise _CannotRun(f"mutate: cannot run the test command {cmd[0]!r}: {e}") from e
    if p.stderr:
        sys.stderr.write(p.stderr)
        sys.stderr.flush()
    return p.returncode, p.stderr or ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True, help="source file to mutate, relative to the repo")
    ap.add_argument("--old", required=True, help="exact text to replace")
    ap.add_argument("--new", required=True, help="text to replace it with")
    ap.add_argument("--count", type=int, default=1,
                    help="how many occurrences of --old to expect (default 1). Mutate ONE "
                         "site at a time: two together have masked an unpinned third here.")
    ap.add_argument("--purge", action="append", default=None,
                    help="directory to purge __pycache__ under (repeatable; "
                         "default shared/lib and tests)")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="-- followed by the test command to run")
    args = ap.parse_args()

    cmd = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not cmd:
        print("mutate: no test command given (put it after `--`)", file=sys.stderr)
        return 2

    # CONTAINED, BECAUSE THIS SCRIPT WRITES. `ROOT / args.file` happily resolves a `--file`
    # full of `..` to anywhere on the filesystem — reproduced: `--file ../../../../tmp/x.py`
    # was accepted, mutated and restored, with the whole cycle reported as a clean run. A
    # tool whose job is editing source in place must not be able to edit source it was never
    # pointed at, and `resolve()` before the containment test is the point: a check on the
    # unresolved string is defeated by the same `..` it is looking for.
    path = (ROOT / args.file).resolve()
    if path != ROOT and ROOT not in path.parents:
        print(f"mutate: {args.file} resolves to {path}, which is outside {ROOT} — this "
              "script rewrites the file it is given and will not do that to a path outside "
              "the repository it was invoked in", file=sys.stderr)
        return 2
    try:
        original = path.read_bytes()
    except OSError as e:
        print(f"mutate: cannot read {args.file}: {e}", file=sys.stderr)
        return 2

    text = original.decode("utf-8")
    found = text.count(args.old)
    if found != args.count:
        # Refused rather than best-effort: a mutation that did not apply where the author
        # thought reports the suite's verdict on UNMUTATED code, which is the same false
        # SURVIVED by a different route.
        print(f"mutate: --old occurs {found} time(s) in {args.file}, expected {args.count}",
              file=sys.stderr)
        return 2
    if args.old == args.new:
        print("mutate: --old and --new are identical, so nothing would be measured",
              file=sys.stderr)
        return 2

    mutant_text = text.replace(args.old, args.new)
    # SEAM 1: THE MUTANT MUST PARSE. Refused HERE — above the baseline run, above the write —
    # because this is a fact about the two strings the operator typed and needs no evidence
    # from a runner: a file that does not parse executes no line under any of them. Doing it
    # first also means the tree never carries the broken file and no baseline run is spent on
    # a mutation that was never going to produce a verdict.
    #
    # Refused only when the ORIGINAL parses — a file that never parsed is not this mutation's
    # doing — and only for `.py`, the one suffix `compile()` says anything about.
    if path.suffix == ".py" and _parses(text, path):
        try:
            compile(mutant_text, str(path), "exec")
        except SyntaxError as e:
            print(f"mutate: --new leaves {args.file} unparseable ({type(e).__name__}: "
                  f"{e.msg} at line {e.lineno}), so the mutant cannot execute a line — "
                  f"pytest would exit 2 and a bare script would exit 1, and neither is a "
                  f"verdict about the mutation", file=sys.stderr)
            return 2

    purge = args.purge or [ROOT / "shared" / "lib", ROOT / "tests"]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

    # The baseline run, and it is not optional. CAUGHT is a claim about a DIFFERENCE, and
    # `rc != 0` alone measures only one side of it: a suite already red for an unrelated
    # reason, a `-k` that selects nothing, a runner refusing its own arguments, all report
    # nonzero without executing the mutant at all — and then every CAUGHT in the table is
    # manufactured. Measured on this repo's suites the extra run costs 0.45s to 4.1s per
    # mutant, against a verdict nobody re-examines once it is written down.
    # Purged first because a stale __pycache__ from an earlier interrupted mutation would
    # otherwise decide what the baseline runs.
    _purge_bytecode(purge)
    try:
        base_rc, _ = _run(cmd, env)
    except _CannotRun as e:
        print(e, file=sys.stderr)
        return 2
    if base_rc != 0:
        why = _PYTEST_RC.get(base_rc, "unrecognized exit status")
        print(f"mutate: the UNMUTATED suite exits {base_rc} ({why}), so a nonzero exit under "
              f"the mutation would say nothing about the mutation", file=sys.stderr)
        return 2
    print("mutate: baseline green", file=sys.stderr)

    mutant = mutant_text.encode("utf-8")
    try:
        path.write_bytes(mutant)
        # Read back rather than trust the write: this is the one fact everything else rests
        # on. Compared as the WHOLE expected bytes, not as `--new in <text>`: that membership
        # test passes whenever `--new` already occurs anywhere else in the file, which is the
        # common case for a mutation that swaps one operator or one constant for another the
        # file already uses. Bytes, so the readback cannot disagree with the write about an
        # encoding either.
        if path.read_bytes() != mutant:
            print("mutate: the mutation did not survive the write", file=sys.stderr)
            return 2
        purged = _purge_bytecode(purge)
        print(f"mutate: applied to {args.file}, purged {purged} __pycache__ dir(s)",
              file=sys.stderr)
        try:
            rc, rc_stderr = _run(cmd, env)
        except _CannotRun as e:
            print(e, file=sys.stderr)
            return 2
    finally:
        path.write_bytes(original)
        _purge_bytecode(purge)

    if rc not in (0, _TESTS_FAILED):
        # THE ONLY NONZERO STATUS THAT MEANS THE SUITE NOTICED IS 1. The baseline was green, so
        # every other status is the mutation stopping the run rather than failing it: 2 is a
        # collection error, and a file the mutation left unparseable never executes a line; 5 is
        # a mutation that changed what gets COLLECTED — mutating a test file so that `-k` no
        # longer selects it, say — and the baseline collected tests, so reaching 5 is the
        # mutation's doing; 3 and 4 are the runner refusing its own arguments. Refused rather
        # than reported, because by here a nonzero exit is otherwise indistinguishable from a
        # failure, and that indistinguishability is exactly the FALSE CAUGHT above.
        why = _PYTEST_RC.get(rc, "unrecognized exit status")
        print(f"mutate: the mutated run exits {rc} ({why}), which is not a test failure — "
              f"nothing was measured, so this is not a verdict", file=sys.stderr)
        return 2
    # SEAM 2: exit 1 that arrived by the process being UNWOUND, not by the suite reporting.
    # Checked after the status table above rather than instead of it: pytest routes this to
    # exit 2 and is already refused there, and only a runner with no such status needs the
    # stderr read. Refused rather than reported for the reason the whole file exists — a
    # FALSE CAUGHT leaves nothing behind to re-examine.
    exc = _died_of_an_exception(rc_stderr) if rc == _TESTS_FAILED else None
    if exc:
        print(f"mutate: the mutated run exits {rc}, but its stderr shows the process was "
              f"unwound by an uncaught exception ({exc}) rather than the suite reporting a "
              f"failure — the mutant did not run to a verdict, so this is not one",
              file=sys.stderr)
        return 2
    verdict = "CAUGHT" if rc == _TESTS_FAILED else "SURVIVED"
    print(f"mutate: {verdict} (test command exit {rc})", file=sys.stderr)
    return 0 if rc == _TESTS_FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
