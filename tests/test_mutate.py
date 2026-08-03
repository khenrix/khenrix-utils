"""Behavioural tests for scripts/mutate.py — what it will and will not call CAUGHT.

HERMETIC BY RELOCATION. `mutate.py` resolves its ROOT from its own `__file__` and mutates
files on disk beneath it, so these tests copy the script into a `scripts/` under `tmp_path`
and let it mutate throwaway files there. Nothing in this repository is written to, which
matters more here than elsewhere: the thing under test is a thing that edits source files.

The one assertion these are all really making is that a nonzero exit is not a verdict. A
mutation that leaves the file unparseable exits pytest 2 and executes no line of the mutant;
scoring that CAUGHT writes a covered row over a hole nobody will look at again.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MUTATE = ROOT / "scripts" / "mutate.py"

MARKER = "MUTATED"


def _harness(tmp_path, rc_when_mutated: int):
    """A relocated `mutate.py`, a file to mutate, and a test command with a chosen verdict.

    The stub runner exits 0 while the target is unmutated — so the baseline is green by
    construction — and `rc_when_mutated` once `MARKER` appears in it. That is the whole
    dependency: the status the runner reports, decoupled from any real suite.
    """
    (tmp_path / "scripts").mkdir()
    shutil.copy2(MUTATE, tmp_path / "scripts" / "mutate.py")
    target = tmp_path / "target.py"
    target.write_text("VALUE = 'ORIGINAL'\n")
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import pathlib, sys\n"
        "text = pathlib.Path(sys.argv[1]).read_text()\n"
        "sys.exit(int(sys.argv[3]) if sys.argv[2] in text else 0)\n")
    cmd = [sys.executable, str(tmp_path / "scripts" / "mutate.py"),
           "--file", "target.py", "--old", "ORIGINAL", "--new", MARKER,
           "--purge", str(tmp_path),
           "--", sys.executable, str(runner), str(target), MARKER, str(rc_when_mutated)]
    return cmd, target


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


@pytest.mark.parametrize("rc, phrase", [
    (2, "interrupted"),          # the measured one: a collection error / SyntaxError
    (3, "internal error"),
    (4, "usage error"),
    (5, "no tests were collected"),
    (99, "unrecognized exit status"),
])
def test_a_status_that_is_not_a_test_failure_is_refused_not_scored(tmp_path, rc, phrase):
    """The FALSE CAUGHT this script exists to prevent, arriving through the exit code.

    Measured before the fix: a `--new` that left a bracket unclosed made pytest exit 2 and
    `mutate.py` printed `CAUGHT (test command exit 2)` and returned 0, for a mutant that never
    ran. Every status here must instead leave the process nonzero and NAME what the runner
    reported, so the row that gets written says nothing was measured.
    """
    cmd, _ = _harness(tmp_path, rc)
    r = _run(cmd)
    assert r.returncode == 2, r.stderr
    assert "CAUGHT" not in r.stderr
    assert f"exits {rc}" in r.stderr and phrase in r.stderr


def test_only_a_test_failure_is_caught(tmp_path):
    """Exit 1 is the one nonzero status that says the suite noticed."""
    cmd, _ = _harness(tmp_path, 1)
    r = _run(cmd)
    assert r.returncode == 0, r.stderr
    assert "CAUGHT (test command exit 1)" in r.stderr


def test_a_green_mutated_run_is_a_survivor(tmp_path):
    cmd, _ = _harness(tmp_path, 0)
    r = _run(cmd)
    assert r.returncode == 1, r.stderr
    assert "SURVIVED (test command exit 0)" in r.stderr


def test_the_file_is_restored_whatever_the_verdict(tmp_path):
    """Every branch above returns through the same `finally`, including the refusals."""
    for rc in (0, 1, 2, 5):
        d = tmp_path / f"rc{rc}"
        d.mkdir()
        cmd, target = _harness(d, rc)
        _run(cmd)
        assert target.read_text() == "VALUE = 'ORIGINAL'\n"


def test_a_real_unparseable_mutant_under_real_pytest_is_not_a_verdict(tmp_path):
    """End-to-end against pytest itself, because the stub above could agree with a wrong table.

    This is the exact shape that was measured: the mutation deletes a closing bracket, the
    module fails to import, pytest reports one error during collection and exits 2.
    """
    (tmp_path / "scripts").mkdir()
    shutil.copy2(MUTATE, tmp_path / "scripts" / "mutate.py")
    (tmp_path / "mod.py").write_text("METHODS = ('a', 'b')\n")
    (tmp_path / "test_mod.py").write_text(
        "import mod\n\n\ndef test_it():\n    assert mod.METHODS == ('a', 'b')\n")
    r = _run([sys.executable, str(tmp_path / "scripts" / "mutate.py"),
              "--file", "mod.py", "--old", "('a', 'b')", "--new", "('a', 'b'",
              "--purge", str(tmp_path),
              "--", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
              str(tmp_path / "test_mod.py")])
    assert r.returncode == 2, r.stderr
    assert "CAUGHT" not in r.stderr
    assert "exits 2" in r.stderr
    assert (tmp_path / "mod.py").read_text() == "METHODS = ('a', 'b')\n"
