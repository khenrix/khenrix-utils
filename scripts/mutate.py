#!/usr/bin/env python3
"""Run one mutation and report whether the suite catches it.

Checked in rather than re-authored per task. The guard below was present in five harnesses
written on one day and absent from twelve written the next, and its absence manufactures a
FALSE SURVIVED — the most expensive verdict a mutation table can carry, because a survivor is
what makes someone add a test or declare an equivalent mutant.

    scripts/mutate.py --file shared/lib/forge/verify.py \
        --old 'VERIFIER_NAME = "verify"' --new 'VERIFIER_NAME = "claude"' \
        -- uvx pytest tests/test_forge_seams.py -q

Exit 0 when the mutant is CAUGHT (the suite failed), 1 when it SURVIVED, 2 on a usage or
application error. The source file is restored from the bytes read at start, always.
"""
import argparse
import os
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

    path = ROOT / args.file
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

    purge = args.purge or [ROOT / "shared" / "lib", ROOT / "tests"]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

    mutant = text.replace(args.old, args.new).encode("utf-8")
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
            rc = subprocess.run(cmd, cwd=ROOT, env=env).returncode
        except OSError as e:
            # A command that cannot START is a usage error, and it has to say so HERE. The
            # uncaught traceback exits 1, and 1 is the machine-readable spelling of SURVIVED
            # — so a harness building a mutation table off exit codes records a survivor for
            # a typo'd test command, which is the false SURVIVED this script exists to
            # prevent, reached by a different route.
            print(f"mutate: cannot run the test command {cmd[0]!r}: {e}", file=sys.stderr)
            return 2
    finally:
        path.write_bytes(original)
        _purge_bytecode(purge)

    verdict = "CAUGHT" if rc != 0 else "SURVIVED"
    print(f"mutate: {verdict} (test command exit {rc})", file=sys.stderr)
    return 0 if rc != 0 else 1


if __name__ == "__main__":
    sys.exit(main())
