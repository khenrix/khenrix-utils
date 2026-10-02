"""A pytest plugin that records what the named test's `call` phase actually did.

WHY A RECEIPT AND NOT AN EXIT CODE. `coverage._test` read `returncode == 0` as "the named
test passed", and a zero exit is what pytest returns for a test that never evaluated the
claim: `@pytest.mark.skip` and `pytest.skip()` exit 0, an expected `xfail` whose body
genuinely failed exits 0, and a configuration that collects without executing exits 0. So
"never executed" reached the cleanest state §10.1's method axis has — checked, and true —
which is the fail-open in the one axis the whole design rests on.

A process-level number cannot answer a per-test question. This writes the per-test answer.

ONE LINE PER (nodeid, when) AS JSON, appended: a plugin that returned a value would have to
be imported by the parent, and the point is that the parent is a DIFFERENT process from the
one pytest runs in. The path comes from the environment rather than an argument because
pytest's own argv is the seat's to influence and this file's contract is not.

STDLIB ONLY. `pytest` is not importable by this repository's python3 (the Makefile falls back
to `uvx`), so this module must never import it — the hook is resolved by name at runtime, in
the interpreter that actually has pytest.
"""
import json
import os

_ENV = "FORGE_PYTEST_RECEIPT"


def pytest_runtest_logreport(report):
    """Every phase transition of every test, as the runner sees it.

    `setup`, `call` and `teardown` all arrive here. `coverage._test` reads only `call`,
    because that is the phase in which a claim is or is not evaluated — a test skipped at
    `setup` has no `call` report at all, which is exactly how "it never ran" becomes visible
    rather than being inferred from a number that cannot express it.

    `report.outcome` is pytest's own word ("passed" / "failed" / "skipped"), and
    `report.wasxfail` distinguishes an expected failure from a real pass: an xfailing test
    reports `outcome == "skipped"` with `wasxfail` set, and an XPASS reports `passed` with
    it set. Both are recorded as themselves rather than flattened, so the reader decides.
    """
    path = os.environ.get(_ENV)
    if not path:
        return
    row = {"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome,
           "wasxfail": hasattr(report, "wasxfail")}
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        # A receipt that cannot be written must not take the test run down with it: the
        # reader treats a missing receipt as `unresolved`, which is the honest answer and the
        # one this failure should produce.
        pass
