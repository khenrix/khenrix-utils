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


def test_an_unparseable_mutant_is_refused_before_it_is_ever_written(tmp_path):
    """SEAM 1. The status table above only ever saw this AFTER a runner had reported on
    it, which meant each runner needed its own rule; a mutant that does not parse cannot
    execute a line under ANY of them, so it is refused at the source. Checked with
    `compile()` before the write, so the tree never carries the broken file at all."""
    cmd, target = _harness(tmp_path, 1)          # the runner would say CAUGHT
    cmd[cmd.index("--new") + 1] = "MUTATED'"     # ...but this leaves a quote unclosed
    r = _run(cmd)
    assert r.returncode == 2, r.stderr
    assert "CAUGHT" not in r.stderr
    assert "unparseable" in r.stderr and "SyntaxError" in r.stderr
    assert target.read_text() == "VALUE = 'ORIGINAL'\n"
    assert "baseline green" not in r.stderr, "it should refuse before spending a baseline run"


def test_a_syntax_break_in_a_non_python_file_is_not_second_guessed(tmp_path):
    """`compile()` speaks for `.py` and nothing else. A mutation to a .toml/.md must
    still run, or the guard would refuse work it cannot actually judge."""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(MUTATE, tmp_path / "scripts" / "mutate.py")
    target = tmp_path / "conf.toml"
    target.write_text('name = "ORIGINAL"\n')
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import pathlib, sys\n"
        "text = pathlib.Path(sys.argv[1]).read_text()\n"
        "sys.exit(1 if sys.argv[2] in text else 0)\n")
    r = _run([sys.executable, str(tmp_path / "scripts" / "mutate.py"),
              "--file", "conf.toml", "--old", 'name = "ORIGINAL"', "--new", 'name = "MUTATED',
              "--purge", str(tmp_path),
              "--", sys.executable, str(runner), str(target), "MUTATED"])
    assert r.returncode == 0, r.stderr
    assert "CAUGHT" in r.stderr


def test_a_bare_script_suite_that_crashes_is_refused_not_scored_CAUGHT(tmp_path):
    """SEAM 2, AND THE RESIDUAL THIS COMMIT EXISTS FOR. A runner whose only failure
    signal is its own exit code reports 1 for an uncaught exception and 1 for a real
    failure, so refusing statuses other than 1 did nothing here -- the same unparseable
    mutant pytest refused was still scored CAUGHT. Measured on this repo three times.

    The mutation here parses (so seam 1 lets it through) and makes the suite die of a
    NameError rather than report: the run did not reach a verdict, so neither does this."""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(MUTATE, tmp_path / "scripts" / "mutate.py")
    (tmp_path / "mod.py").write_text("def value():\n    return 'ORIGINAL'\n")
    suite = tmp_path / "suite.py"
    suite.write_text(
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import mod\n"
        "sys.exit(0 if mod.value() == 'ORIGINAL' else 1)\n" % str(tmp_path))
    r = _run([sys.executable, str(tmp_path / "scripts" / "mutate.py"),
              "--file", "mod.py", "--old", "return 'ORIGINAL'", "--new", "return NOPE",
              "--purge", str(tmp_path),
              "--", sys.executable, str(suite)])
    assert r.returncode == 2, r.stderr
    assert "CAUGHT" not in r.stderr
    assert "uncaught exception" in r.stderr
    assert "NameError" in r.stderr, "must name what unwound the process"
    assert (tmp_path / "mod.py").read_text() == "def value():\n    return 'ORIGINAL'\n"


def test_a_bare_script_suite_that_really_fails_is_still_CAUGHT(tmp_path):
    """THE DISCRIMINATION CHECK, and the reason seam 2 reads the traceback rather than
    just distrusting every non-pytest runner. A bare script that exits 1 by REPORTING is
    a verdict and must stay one, or the fix would delete the workflow it was protecting."""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(MUTATE, tmp_path / "scripts" / "mutate.py")
    (tmp_path / "mod.py").write_text("VALUE = 'ORIGINAL'\n")
    suite = tmp_path / "suite.py"
    suite.write_text(
        "import pathlib, sys\n"
        "text = pathlib.Path(%r).read_text()\n"
        "ok = \"VALUE = 'ORIGINAL'\" in text\n"
        "print('PASS' if ok else 'FAIL  value changed')\n"
        "sys.exit(0 if ok else 1)\n" % str(tmp_path / "mod.py"))
    r = _run([sys.executable, str(tmp_path / "scripts" / "mutate.py"),
              "--file", "mod.py", "--old", "'ORIGINAL'", "--new", "'MUTATED'",
              "--purge", str(tmp_path),
              "--", sys.executable, str(suite)])
    assert r.returncode == 0, r.stderr
    assert "CAUGHT (test command exit 1)" in r.stderr


def test_a_printed_traceback_is_not_a_process_that_died_of_one(tmp_path):
    """The false-refusal seam 2 must not walk into. A suite that CATCHES an exception,
    prints the traceback and then reports a failure has reached a verdict; refusing it
    would manufacture 'nothing was measured' out of a measurement."""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(MUTATE, tmp_path / "scripts" / "mutate.py")
    (tmp_path / "mod.py").write_text("VALUE = 'ORIGINAL'\n")
    suite = tmp_path / "suite.py"
    suite.write_text(
        "import pathlib, sys, traceback\n"
        "text = pathlib.Path(%r).read_text()\n"
        "ok = \"VALUE = 'ORIGINAL'\" in text\n"
        "if not ok:\n"
        "    try:\n"
        "        raise AssertionError('value changed')\n"
        "    except AssertionError:\n"
        "        traceback.print_exc()\n"
        "    print('1 failed', file=sys.stderr)\n"
        "sys.exit(0 if ok else 1)\n" % str(tmp_path / "mod.py"))
    r = _run([sys.executable, str(tmp_path / "scripts" / "mutate.py"),
              "--file", "mod.py", "--old", "'ORIGINAL'", "--new", "'MUTATED'",
              "--purge", str(tmp_path),
              "--", sys.executable, str(suite)])
    assert r.returncode == 0, r.stderr
    assert "CAUGHT (test command exit 1)" in r.stderr


def test_an_indented_traceback_in_a_report_is_not_a_process_that_died(tmp_path):
    """Pins the ANCHORING in the traceback header, which an earlier draft of this suite
    left unmeasured -- the mutant `r"Traceback"` SURVIVED 16 tests.

    A harness that formats a caught exception into its own report indents the traceback
    and can sign off with an exception-shaped summary line, tripping both halves of the
    crash signature at once. Refusing that would manufacture 'nothing was measured' out of
    a real failure, so the header must start at column 0 to count."""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(MUTATE, tmp_path / "scripts" / "mutate.py")
    (tmp_path / "mod.py").write_text("VALUE = 'ORIGINAL'\n")
    suite = tmp_path / "suite.py"
    suite.write_text(
        "import pathlib, sys\n"
        "ok = \"'ORIGINAL'\" in pathlib.Path(%r).read_text()\n"
        "if not ok:\n"
        "    sys.stderr.write('    Traceback (most recent call last):\\n')\n"
        "    sys.stderr.write('      File \"suite.py\", line 1, in check\\n')\n"
        "    sys.stderr.write('    AssertionError: value changed\\n')\n"
        "    sys.stderr.write('AssertionError: 1 check failed\\n')\n"
        "sys.exit(0 if ok else 1)\n" % str(tmp_path / "mod.py"))
    r = _run([sys.executable, str(tmp_path / "scripts" / "mutate.py"),
              "--file", "mod.py", "--old", "'ORIGINAL'", "--new", "'MUTATED'",
              "--purge", str(tmp_path),
              "--", sys.executable, str(suite)])
    assert r.returncode == 0, r.stderr
    assert "CAUGHT (test command exit 1)" in r.stderr


def test_the_child_runners_stderr_reaches_the_operator_though_it_is_captured(tmp_path):
    """Seam 2 needs the CHILD's stderr in a variable; the operator still needs it on the
    terminal. Capturing without re-emitting would trade a false CAUGHT for a blind run.

    The marker is written by the RUNNER, not by mutate.py. An earlier draft asserted on
    'baseline green' -- one of mutate.py's OWN prints -- and so never touched the
    passthrough at all: deleting the re-emit SURVIVED 16 tests. Both runs are counted,
    because the baseline's diagnostics matter as much as the mutant's."""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(MUTATE, tmp_path / "scripts" / "mutate.py")
    (tmp_path / "mod.py").write_text("VALUE = 'ORIGINAL'\n")
    suite = tmp_path / "suite.py"
    suite.write_text(
        "import pathlib, sys\n"
        "print('RUNNER-DIAGNOSTIC-MARKER', file=sys.stderr)\n"
        "ok = \"'ORIGINAL'\" in pathlib.Path(%r).read_text()\n"
        "sys.exit(0 if ok else 1)\n" % str(tmp_path / "mod.py"))
    r = _run([sys.executable, str(tmp_path / "scripts" / "mutate.py"),
              "--file", "mod.py", "--old", "'ORIGINAL'", "--new", "'MUTATED'",
              "--purge", str(tmp_path),
              "--", sys.executable, str(suite)])
    assert r.returncode == 0, r.stderr
    assert r.stderr.count("RUNNER-DIAGNOSTIC-MARKER") == 2, (
        "the runner's own stderr must reach the operator on BOTH the baseline and the "
        "mutated run: " + r.stderr)


def test_a_real_unparseable_mutant_under_real_pytest_is_not_a_verdict(tmp_path):
    """End-to-end against pytest itself, because the stub above could agree with a wrong table.

    This is the exact shape that was measured: the mutation deletes a closing bracket, the
    module fails to import, pytest reports one error during collection and exits 2.

    SEAM 1 NOW CATCHES IT ONE STEP EARLIER, and the assertion follows the code rather than
    the other way round: the refusal message is the parse refusal, not the exit-2 one, and
    pytest is never launched. What this still pins end to end is the property the exit-2
    rule was written for -- returncode 2, no CAUGHT, file restored -- reached by whichever
    seam gets there first. The exit-2 path itself is pinned by the test below, on a mutant
    that parses.
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
    assert "unparseable" in r.stderr
    assert (tmp_path / "mod.py").read_text() == "METHODS = ('a', 'b')\n"


def test_a_parseable_mutant_that_breaks_collection_still_exits_2_under_real_pytest(tmp_path):
    """The exit-2 rule, kept pinned end to end now that seam 1 intercepts the syntax case.

    This mutant PARSES -- it swaps an import for a module that does not exist -- so it
    reaches pytest, which reports one error during collection and exits 2. The mutant
    executed no assertion, so it is refused by name exactly as before."""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(MUTATE, tmp_path / "scripts" / "mutate.py")
    (tmp_path / "mod.py").write_text("import json\n\nMETHODS = ('a', 'b')\n")
    (tmp_path / "test_mod.py").write_text(
        "import mod\n\n\ndef test_it():\n    assert mod.METHODS == ('a', 'b')\n")
    r = _run([sys.executable, str(tmp_path / "scripts" / "mutate.py"),
              "--file", "mod.py", "--old", "import json", "--new", "import no_such_module_xyz",
              "--purge", str(tmp_path),
              "--", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
              str(tmp_path / "test_mod.py")])
    assert r.returncode == 2, r.stderr
    assert "CAUGHT" not in r.stderr
    assert "exits 2" in r.stderr and "collection" in r.stderr
    assert (tmp_path / "mod.py").read_text() == "import json\n\nMETHODS = ('a', 'b')\n"
