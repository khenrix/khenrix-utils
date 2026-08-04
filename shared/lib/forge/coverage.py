"""§10.1: coverage is only mechanical where a predicate exists.

A row reading "crash-safe atomic state update" is marked present because `os.replace`
appears, while `fsync` of the file and its directory is missing and the property is false. A
generic walk over natural-language rows is SYSTEMATIC REVIEW, NOT DETERMINISTIC COVERAGE —
calling it mechanical manufactures another false green. (That particular example is not live
in this tree: `storage.atomic_write` fsyncs the file and then calls `storage._fsync_dir` on
its parent. It is illustrative, and the shape it illustrates is the whole point of this file.)

TWO AXES, NOT ONE ENUM. §10.1's three values are a METHOD axis — how was this criterion
checked. §12.4 then uses coverage as a fallback trigger on a RESULT axis — a missing accepted
row. One enum cannot carry both: "checked mechanically, and the answer is no" has nowhere to
go, and it lands in `unresolved` beside "no evaluator exists for this kind of criterion" —
two OPPOSITE facts under one word, which is exactly a verdict reading cleaner than its
evidence. So every result is the pair `(method, satisfied)`, with `satisfied` non-None only
when `method == "mechanically_checked"`.

WHAT `mechanically_checked` DOES NOT PROVE, and every report line must say so: that the test
tests the claim. A test passing for the wrong reason is this project's first recurring defect
and no predicate here reaches it. This is a claim about MECHANISM, never about correctness.

WHERE THE PREDICATES RUN: the candidate tree handed in as `tree` — the verifier's clone.
Never the seat (the builder wrote it) and never the user's checkout.

NOTHING HERE SPENDS A PROVIDER CALL. `run` is injected and defaults to `subprocess.run`; the
suite passes fakes.
"""
import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import bundle as bundlemod
from . import ledger
from . import snapshot

METHODS = ("mechanically_checked", "manual_trace_confirmed", "unresolved")

# A predicate is bounded. A criterion whose test hangs must not hang the coverage check.
_TEST_TIMEOUT = 600

# `Result.criterion_index` when the result is about the ROW and no criterion exists to index.
# `check` emits one for an accepted row that declares no acceptance criteria; see its
# docstring for why that row must produce a line rather than nothing.
NO_CRITERION = -1


class CoverageError(RuntimeError):
    """This coverage check cannot be described honestly."""


def _lines(results, which: str) -> list:
    """One roll-up, as the ONE spelling both `check` and `Report.__post_init__` read.

    Two spellings of the same rule eventually disagree, and here the disagreement would be a
    report whose own consistency check passes because it was computed the same wrong way
    twice — so the producer and the audit share this function rather than each stating it.
    """
    keep = (lambda x: x.satisfied is False) if which == "unsatisfied" \
        else (lambda x: x.method == "unresolved")
    return [f"{x.row_id}[{x.criterion_index}]: {x.detail}" for x in results if keep(x)]


@dataclass(frozen=True)
class Result:
    """One criterion's outcome, on both axes.

    `satisfied` is None whenever `method != "mechanically_checked"`: a `manual_trace_confirmed`
    result carrying `satisfied=True` would be a human's word rendered in the shape of a
    measurement, and that is §10.1's manufactured green arriving through the type system.

    THE INVARIANT IS ENFORCED IN `__post_init__`, SO IT HOLDS FOR EVERY `Result` A CONSTRUCTOR
    PRODUCES — including the ones §12.4's consumer builds for itself. An earlier draft put the
    check in a private factory and documented that a directly-constructed `Result` bypassed it;
    a rule that holds only on the paths its author remembered is the shape this module refuses
    everywhere else, and `Report` is a public dataclass a later stage will populate.

    THE STATES THAT SENTENCE DOES NOT COVER, stated for the same reason `gate.Confirmation`
    states them: `__post_init__` is not a wall. `object.__new__` plus `object.__setattr__`
    reaches past it, so does an unpickle that restores `__dict__` — which is what DEFAULT
    dataclass pickling does, so a `Result` that made a round trip through `pickle` was never
    re-checked — and so does a subclass overriding `__post_init__`. Nothing here is pickled or
    subclassed today; the disclosure is here so the next reader learns the boundary from the
    docstring rather than from a `manual_trace_confirmed` row carrying `satisfied=True`. Two
    sibling docstrings disagreeing about how far their own enforcement reaches is the defect,
    whichever one is optimistic.
    """
    row_id: str
    criterion_index: int
    method: str
    satisfied: bool | None
    detail: str

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise CoverageError(f"method is one of {list(METHODS)}, not {self.method!r}")
        if self.method != "mechanically_checked" and self.satisfied is not None:
            raise CoverageError(
                f"a {self.method!r} result may not carry satisfied={self.satisfied!r}: only a "
                "mechanical check produces an answer, and a human's word in a measurement's "
                "shape is the manufactured green §10.1 exists to forbid")
        # THE OTHER HALF OF THE PAIR, and it was open. The sentence above forbids an answer
        # where no measurement was taken; this forbids a MEASUREMENT WITH NO ANSWER, which is
        # the state that reads as nothing at all: `(mechanically_checked, None)` is in neither
        # `Report.unsatisfied` (`satisfied is False`) nor `Report.unresolved`
        # (`method == "unresolved"`), so a criterion in it is checked, unanswered and invisible
        # in every roll-up §12.4 and §10 read. `bool | None` describes three states and only
        # three of the four combinations are ones this module can mean.
        if self.method == "mechanically_checked" and not isinstance(self.satisfied, bool):
            raise CoverageError(
                f"a mechanically_checked result carries the answer it measured, not "
                f"satisfied={self.satisfied!r}: a check with no answer appears in neither "
                "roll-up, so it would be a criterion nobody can see was left open")


@dataclass(frozen=True)
class Report:
    """Every result, plus the three roll-ups §12.4 and §10 read.

    `unsatisfied` and `unresolved` are SEPARATE and that separation is the file's reason for
    existing: "checked and false" is a fallback trigger, "nobody could check" is a report line
    that must not be read as either a pass or a failure.

    THE ROLL-UPS ARE RE-DERIVED IN `__post_init__`, for the reason `Result` enforces its own
    invariant there rather than in a private factory: a rule that holds only on the path its
    author remembered is what this module refuses everywhere else, and `Report` is a public
    dataclass §12.4's consumer will populate for itself. `check` is the only producer today,
    so nothing here changes what it emits — the check exists so a hand-built `Report` cannot
    report a clean run over results that say otherwise, which is a verdict reading cleaner
    than its evidence assembled out of honest parts.
    """
    results: tuple
    contradictions: tuple
    unsatisfied: tuple
    unresolved: tuple

    def __post_init__(self) -> None:
        wrong = sorted({type(r).__name__ for r in self.results
                        if not isinstance(r, Result)})
        if wrong:
            raise CoverageError(f"a report's results are Result records, not {wrong}")
        for name in ("unsatisfied", "unresolved"):
            expected = tuple(_lines(self.results, name))
            if tuple(getattr(self, name)) != expected:
                raise CoverageError(
                    f"this report's {name} is not what its own results say: it carries "
                    f"{tuple(getattr(self, name))!r} beside {expected!r}")


def _require_inputs(c) -> None:
    """The evaluator's inputs are present and are text, or this record is not a criterion.

    ONE TABLE, IMPORTED. `ledger._CRITERION_FIELDS` already says which structured fields each
    kind carries, and the predicates below read exactly those; re-listing them here is the
    second spelling this project's standing defect is made of — `ledger._check_criterion`
    imports `bundle._assert_contained` rather than re-inlining it for the same reason.

    A RAISE, NOT `unresolved`, and the distinction is WHO IS WRONG. An escaping path is a
    criterion this engine DECLINED to measure, so `unresolved` is the honest word for it. A
    `symbol` criterion carrying no `symbol` is not a criterion at all: `ledger._check_criterion`
    refuses it at write, so reaching here means a `Ledger` built in process — which §12.2's
    partitioned synthesis does by construction — while its producer is still present to fix it.
    Measured without this, `Path(tree) / None` raised `TypeError` out of `coverage.check`: an
    error escaping this module's declared class is one no caller of it knows to catch.
    """
    required = ledger._CRITERION_FIELDS[c.kind]
    for name in required:
        v = getattr(c, name)
        if not isinstance(v, str) or not v:
            raise CoverageError(
                f"a {c.kind!r} criterion carries {list(required)} as structured fields and "
                f"{name} is {v!r}, so no predicate can be run on it")


def _inside(tree, rel):
    """`rel`'s parent inside `tree` as an OPEN DESCRIPTOR, or None because it names something
    outside it. The caller closes it — `with _inside(...) as at:`.

    THE JOIN IS WHERE A CRITERION STOPS DESCRIBING THE CANDIDATE. `ledger.Criterion.path` is a
    bare `str` authored from three fallible seats' claims, and `Path(tree) / "/etc/passwd"` IS
    `/etc/passwd` while `../../` walks straight out — so an unchecked join reports a MECHANICAL
    check on a host file the ledger claims nothing about, which is a verdict reading cleaner
    than its evidence in the most literal available way. `taskbundle._check_rel` applies the
    same guard for the same reason and `ledger._check_criterion` refuses these at write; this
    is the second gate, because a `Ledger` value built in-process never passed through the
    first.

    WHAT THIS USED TO RETURN, AND WHY IT WAS THE SAME DEFECT ONE MODULE OVER. It answered a
    `Path`, having compared `realpath` OF THE PARENT against `realpath` of the tree — a check
    on a string, followed by a caller opening that string. `_symbol` then called `read_bytes()`
    on it, which follows a symlink leaf, and reported `(mechanically_checked, True)` about a
    file OUTSIDE the tree. Measured: `tree/mod.py -> ../host.py` answered "mod.py defines
    secret" about `host.py`. `bundle.contained` descends component by component with
    `O_NOFOLLOW` against the previous component's descriptor instead, and the caller opens the
    leaf with `dir_fd=` — so the check and the use are one syscall and there is no name left
    for a link to redirect. The `..` and absolute forms are still refused as STRINGS, inside
    `contained`, because a literal `..` needs no symlink to leave the tree.

    THE LEAF IS STILL NEVER FOLLOWED, and now that is a property of the open rather than of
    what this function declines to resolve: a symlink leaf is a legal entry whose TARGET TEXT
    is what `_hash` digests, so `_hash` reads it with `os.readlink(dir_fd=)` and `_symbol`
    refuses to parse through it at all.
    """
    try:
        return bundlemod.contained(tree, rel, "a coverage criterion path")
    except bundlemod.BundleError:
        return None


def _escaped(c, *, row_id, index) -> Result:
    """The one answer an escaping path may have: nobody looked.

    NOT `(mechanically_checked, False)`. "The invariant is definitively unsatisfied" is a
    measurement, and no measurement was taken here — the path names a file this engine
    declined to open. `unresolved` is the honest word, and it keeps the row out of
    `Report.unsatisfied`, which §12.4 acts on.
    """
    return Result(row_id, index, "unresolved", None,
                  f"{c.path!r} does not name a path inside the candidate tree, so no "
                  "predicate was run: a criterion that leaves the tree describes content "
                  "the ledger does not claim to describe")


def _uncontained_node_file(node_id: str, tree):
    """The detail line for a node id whose FILE half does not name a regular file inside
    `tree`, or None because it does.

    A STRING THIS ENGINE MAY NOT PASS TO PYTEST AT ALL, so the answer is a refusal line rather
    than a run whose result is then discounted: pytest imports a `conftest.py` beside whatever
    it collects, and that import is code execution decided by the node id. `unresolved` is the
    verdict for the same reason `_escaped` gives — nobody looked — and it keeps the row out of
    `Report.unsatisfied`, which §12.4 acts on.
    """
    why = (f"{node_id!r} does not select a file inside the candidate tree, so no predicate "
           "was run: pytest imports the `conftest.py` beside whatever it collects, and a "
           "node id that leaves the tree is code execution the ledger claims nothing about")
    rel = node_id.split("::")[0]
    if not rel:
        return why
    try:
        at = bundlemod.contained(tree, rel, "a coverage criterion test id")
    except bundlemod.BundleError as e:
        return f"{why} ({e})"
    with at:
        # A LINK LEAF IS REFUSED ALONGSIDE, for the reason `_symbol` refuses one: the
        # components are descended, so `evil/x.py` through a symlinked `evil/` is already out,
        # and a link at the LEAF reaches the same file by the same means. Directories are let
        # through — a node id naming one selects many tests, which the count guard below
        # answers with the sentence that says why several is not one.
        try:
            st = os.stat(at.leaf, dir_fd=at.fd, follow_symlinks=False)
        except OSError as e:
            return f"{why} ({e.strerror})"
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            return f"{why} (st_mode {st.st_mode:#o})"
    return None


def _test(c, *, row_id, index, tree, pytest_argv, run, **_) -> Result:
    """Run the named pytest node id and observe THAT ONE RESULT.

    THE FAIL-OPEN THIS FORECLOSES, and it is the obvious one: treating "the run's verify gate
    exited 0" as satisfying every test-ID criterion. `verify.Run` holds only `exit_code`,
    `stdout` and `stderr`, and the one reader forge has for that text —
    `progress.pytest_fingerprints` — answers with FAILING ids or `None`, so it can say a named
    test is among the failures and can never say one ran and passed. "The suite passed, so the
    named test passed" is one line of code and is a manufactured green. The predicate must
    SELECT the named test and watch it.

    `--collect-only` first, and exactly one collected node required, MATCHING THE NODE ID
    EXACTLY. A node id naming a FILE selects many tests, and a green run over many says nothing
    about the one the claim names; a node id naming a parametrized function selects
    `…::f[a]`, which is a different node from the `…::f` the claim wrote down. (Spelled with
    a placeholder rather than a plausible node id: the packaging suite reads any `test_*`-shaped
    token in shipped forge prose as a CITATION to a real test, and an illustration is not a
    citation anything can resolve.)

    Pytest's exits carry the distinction: 0 passed, 1 failed, 4 usage error, 5 nothing
    collected. 5 is `unresolved`, never "checked and failed" — a claim whose test vanished is
    not a claim that was tested.

    `-p no:cacheprovider` because the candidate tree is the artefact under measurement: a
    `.pytest_cache` written into it is this engine modifying what it is describing.

    THE NODE ID'S FILE HALF IS CONTAINED, AND IT WAS CONTAINED NOWHERE. `node_id` was the one
    `Criterion` field no guard stood in front of — `ledger._check_criterion` guards `path` and
    this predicate joins nothing, it hands the string to pytest with `cwd=tree`. Both halves of
    that were measured on real pytest. EXECUTION: an id of the form `../outside/mod.py::f` made
    pytest walk up out of the tree and IMPORT `../outside/conftest.py`, which ran and wrote its
    marker file — arbitrary code from outside the candidate, executed by a coverage check.
    VERDICT: the `..` spelling then answered `unresolved`, because pytest prints collected ids
    relative to the rootdir it picked and `startswith(...)` matched none of them — but the same
    escape spelled as a SYMLINK (`tree/outside -> ../outside`, id `outside/mod.py::f`) printed
    the id back unchanged, matched, ran, and answered `(mechanically_checked, True)` about a
    test file entirely outside the tree. The escape is a fact about the filesystem, not about
    the string, which is why the containment below is `bundle.contained`'s descent and not a
    fourth spelling of "does this name look safe". (Both spelled with placeholders: the
    packaging suite reads any `test_*`-shaped token in shipped forge prose as a CITATION to a
    real test, and an illustration is not a citation anything can resolve.)

    WHAT THAT DOES NOT CLOSE, and it must be said rather than implied by the guard's presence:
    pytest resolves the id ITSELF, in another process, from the name — there is no way to hand
    a subprocess a descriptor for it. So this establishes that the file half named a regular
    file inside the tree AT THE MOMENT IT WAS CHECKED, and a tree mutated during the run can
    still move it. That residual is bounded by who can write the candidate tree; the previous
    state was unbounded by anything.
    """
    if not pytest_argv:
        return Result(row_id, index, "unresolved", None,
                      "no pytest runner is wired for this run, so no test-ID predicate exists")
    if (bad := _uncontained_node_file(c.node_id, tree)) is not None:
        return Result(row_id, index, "unresolved", None, bad)
    common = ["-p", "no:cacheprovider", "--no-header", "-q"]
    try:
        collected = run([*pytest_argv, "--collect-only", *common, c.node_id],
                        cwd=str(tree), capture_output=True, text=True, timeout=_TEST_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return Result(row_id, index, "unresolved", None, f"pytest could not be run: {e}")
    if collected.returncode == 5:
        return Result(row_id, index, "unresolved", None,
                      f"{c.node_id!r} collected no tests in this tree: the claim's test is "
                      "absent, which is not the same as the claim being false")
    if collected.returncode != 0:
        return Result(row_id, index, "unresolved", None,
                      f"pytest --collect-only exited {collected.returncode} for {c.node_id!r}")
    names = [ln.strip() for ln in (collected.stdout or "").splitlines()
             if ln.strip().startswith(c.node_id.split("::")[0])]
    if len(names) != 1 or names[0] != c.node_id:
        return Result(row_id, index, "unresolved", None,
                      f"{c.node_id!r} selected {len(names)} tests {names}; a predicate must "
                      "observe the named test's own result, not a green run over several")
    # THE RECEIPT, BECAUSE A PROCESS EXIT CODE CANNOT ANSWER A PER-TEST QUESTION. This read
    # `returncode == 0` as "the named test passed", and a zero exit is what pytest returns for
    # a test that never evaluated the claim: `@pytest.mark.skip` exits 0, an expected `xfail`
    # whose body genuinely failed exits 0, and a run that collects without executing exits 0.
    # "Never executed" therefore reached the cleanest state §10.1's method axis has — checked,
    # and true — in the one axis the whole design rests on.
    with tempfile.TemporaryDirectory() as td:
        receipt = os.path.join(td, "receipt.jsonl")
        # THE PLUGIN IS COPIED, NOT REACHED IN PLACE, AND THAT IS NOT TIDINESS. Putting this
        # package's own directory on PYTHONPATH puts `forge/inspect.py` ahead of the STDLIB
        # `inspect` for the child — which pytest imports — so the runner died at collection
        # and every criterion came back `unresolved`. Measured. A directory holding exactly
        # one module shadows nothing.
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_pytest_receipt.py")
        plugin_dir = os.path.join(td, "plugin")
        os.mkdir(plugin_dir)
        shutil.copyfile(src, os.path.join(plugin_dir, "_pytest_receipt.py"))
        env = dict(os.environ)
        env["FORGE_PYTEST_RECEIPT"] = receipt
        # The plugin is imported by the interpreter that HAS pytest, which is not necessarily
        # this one — this repository's `python3` cannot import pytest at all, which is why the
        # Makefile falls back to `uvx`. So it is reached by path rather than by package name.
        env["PYTHONPATH"] = os.pathsep.join(
            [plugin_dir] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        try:
            r = run([*pytest_argv, *common, "-p", "_pytest_receipt", c.node_id],
                    cwd=str(tree), capture_output=True, text=True, timeout=_TEST_TIMEOUT,
                    env=env)
        except (OSError, subprocess.SubprocessError) as e:
            return Result(row_id, index, "unresolved", None, f"pytest could not be run: {e}")
        except TypeError:
            # A caller's injected `run` that takes no `env=`. Refused rather than retried
            # without it: a run with no receipt path set writes no receipt, and the branch
            # below would then read "the test never ran" from a harness detail.
            return Result(row_id, index, "unresolved", None,
                          "this run's pytest runner cannot carry the receipt environment, so "
                          "the named test's own outcome could not be observed")
        calls = _receipt_calls(receipt, c.node_id)

    if not calls:
        return Result(row_id, index, "unresolved", None,
                      f"pytest exited {r.returncode} for {c.node_id!r} and recorded no `call` "
                      "phase for it — the test did not run, which is not the same as the "
                      "claim being false. A skip, a collection-only run and a setup error all "
                      "look like this, and all of them exit 0.")
    if len(calls) != 1:
        return Result(row_id, index, "unresolved", None,
                      f"{c.node_id!r} recorded {len(calls)} `call` phases; a predicate must "
                      "observe one named test's own result")
    call = calls[0]
    if call.get("wasxfail"):
        return Result(row_id, index, "unresolved", None,
                      f"{c.node_id} is an expected failure (xfail/xpass), so its own result "
                      "does not say whether the claim holds")
    if call["outcome"] == "passed":
        return Result(row_id, index, "mechanically_checked", True,
                      f"{c.node_id} passed. THIS DOES NOT PROVE THE TEST TESTS THE CLAIM: "
                      "mechanically_checked is a claim about mechanism, not correctness.")
    if call["outcome"] == "failed":
        return Result(row_id, index, "mechanically_checked", False, f"{c.node_id} failed")
    return Result(row_id, index, "unresolved", None,
                  f"{c.node_id} recorded outcome {call['outcome']!r}, which is neither a pass "
                  "nor a failure of the named test")


def _receipt_calls(path: str, node_id: str) -> list:
    """The `call`-phase rows the plugin wrote for `node_id`, or `[]` if it wrote none.

    `[]` FOR AN ABSENT OR UNREADABLE RECEIPT IS THE FAIL-CLOSED DIRECTION HERE, and it is the
    one place in this module where absence and emptiness may agree: both mean this run cannot
    show the named test's own outcome, and the caller turns that into `unresolved`. A missing
    receipt must never read as a pass, which is exactly what the exit code did.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except (OSError, ValueError):
        return []
    return [r for r in rows if r.get("when") == "call" and r.get("nodeid") == node_id]


def _defines(tree_node, dotted: str) -> bool:
    """Resolve a dotted name through the module's OWN definitions. AST, never grep.

    `grep -n "def atomic_write"` matches a docstring, a comment, a string literal and a
    reference in prose — and this repository's modules are MAJORITY COMMENT by line count, so
    a grep-based symbol check here is more wrong than average, not less.

    TWO DELIBERATE NARROWINGS, BOTH FAIL-CLOSED, both of which answer False for a name that is
    in some sense present. An IMPORTED name is not a definition, so a pure re-export reports
    False. Only the MODULE's top-level body and a `ClassDef`'s own body are walked, so a name
    bound inside `if TYPE_CHECKING:`, a `try:` block or a version guard reports False. Each
    sends the criterion to `Report.unsatisfied`, which costs a reader work; the opposite
    reading — treating a mention as a definition — is the manufactured green.

    THE LOOP RETURNS ON ITS LAST ITERATION, ALWAYS: `"x".split(".")` is `["x"]`, so `parts` is
    never empty, and its final element takes either the `i == len(parts) - 1` return or one of
    the two `False` returns beside it. A trailing `return False` was there and was unreachable
    — dead code under a docstring arguing two deliberate narrowings, which is the shape a
    reader mistakes for a third.
    """
    parts = dotted.split(".")
    body = tree_node.body
    for i, part in enumerate(parts):
        found = None
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == part:
                found = node
                break
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == part for t in node.targets):
                found = node
                break
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and node.target.id == part:
                found = node
                break
        if found is None:
            return False
        if i == len(parts) - 1:
            return True
        if not isinstance(found, ast.ClassDef):
            return False
        body = found.body


def _symbol(c, *, row_id, index, tree, **_) -> Result:
    """`path` defines `symbol`, by parse.

    THE LARGER FAIL-OPEN, AND THE MOST IMPORTANT RULE IN THIS FILE: an exact-symbol predicate
    proves a SYMBOL EXISTS. §10.1's own worked example is a symbol-presence check standing in
    for a behavioural claim while the property is false. So this may only ever be attached to
    a criterion PHRASED as "P defines S" — which is why `ledger.Criterion` refuses a `symbol`
    criterion that does not carry `path` and `symbol` as structured fields. The rule lives in
    the decoder because a rule stated only here is one the next author will not meet.

    A `SyntaxError` is `unresolved`, NEVER "symbol absent": an unparseable file is one nobody
    could look in, and reporting absence would be a measurement nobody took.

    A SYMLINK LEAF IS `unresolved`, AND IT IS `_hash`'S RULE RATHER THAN A NEW ONE. `_hash`
    already refuses to hash through a link because "the invariant would describe content from
    OUTSIDE the tree the ledger claims to describe"; parsing through one says the same thing
    about the same bytes. Before this, `read_bytes()` followed it and answered
    `(mechanically_checked, True)` about whatever it named — measured. The narrowing costs a
    reader work on an in-tree link; the opposite reading is a mechanical truth about a file
    this predicate was never pointed at.
    """
    at = _inside(tree, c.path)
    if at is None:
        return _escaped(c, row_id=row_id, index=index)
    with at:
        if Path(c.path).suffix != ".py":
            return Result(row_id, index, "unresolved", None,
                          f"{c.path!r} is not Python; no symbol evaluator is wired for it")
        try:
            fd = bundlemod.open_leaf(at, os.O_RDONLY, "a coverage criterion path")
        except FileNotFoundError:
            return Result(row_id, index, "mechanically_checked", False,
                          f"{c.path!r} does not exist, so it does not define {c.symbol!r}")
        except OSError as e:
            return Result(row_id, index, "unresolved", None,
                          f"{c.path!r} could not be opened without following a link "
                          f"({e.strerror}), so nobody looked inside the file the criterion "
                          "names; that is not the same as the symbol being absent")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return Result(row_id, index, "unresolved", None,
                              f"{c.path!r} is not a regular file (st_mode {st.st_mode:#o}); "
                              "no symbol evaluator is wired for one")
            src = bundlemod.read_fd(fd)
        finally:
            os.close(fd)
    try:
        parsed = ast.parse(src, filename=str(c.path))
    except SyntaxError as e:
        return Result(row_id, index, "unresolved", None,
                      f"{c.path!r} does not parse ({e}); nobody could look inside it, which "
                      "is not the same as the symbol being absent")
    ok = _defines(parsed, c.symbol)
    return Result(row_id, index, "mechanically_checked", ok,
                  f"{c.path} {'defines' if ok else 'does not define'} {c.symbol}")


def _hash(c, *, row_id, index, tree, **_) -> Result:
    """Recompute sha256 over the path and compare.

    `snapshot`'s kind rules, reused rather than re-spelled: a SYMLINK is the sha256 of its
    TARGET TEXT with surrogateescape, never hashed through. `open()` follows a link, so hashing
    through one makes the invariant describe content from OUTSIDE the tree the ledger claims to
    describe — `fleet.clone_seat` already learned this; do not re-learn it.

    A missing path and a non-regular file where a regular one was expected are both
    `(mechanically_checked, False)`: the invariant is definitively unsatisfied. An `OSError`
    reading a regular file is `unresolved` — nobody managed to look.

    A path that LEAVES the tree is neither: see `_inside`. `lstat` alone does not close it —
    it declines to follow the FINAL component and says nothing about an intermediate one, which
    is why the components are now DESCENDED with `O_NOFOLLOW` and every operation below takes
    `dir_fd=` rather than a name.
    """
    at = _inside(tree, c.path)
    if at is None:
        return _escaped(c, row_id=row_id, index=index)
    with at:
        try:
            st = os.stat(at.leaf, dir_fd=at.fd, follow_symlinks=False)
        except FileNotFoundError:
            return Result(row_id, index, "mechanically_checked", False,
                          f"{c.path!r} does not exist, so the invariant is unsatisfied")
        except OSError as e:
            return Result(row_id, index, "unresolved", None,
                          f"{c.path!r} could not be stat'd: {e}")
        if stat.S_ISLNK(st.st_mode):
            try:
                target = os.readlink(at.leaf, dir_fd=at.fd)
            except OSError as e:
                return Result(row_id, index, "unresolved", None,
                              f"{c.path!r} could not be read as a link: {e}")
            got = hashlib.sha256(target.encode("utf-8", "surrogateescape")).hexdigest()
        elif not stat.S_ISREG(st.st_mode):
            return Result(row_id, index, "mechanically_checked", False,
                          f"{c.path!r} is not the regular file the invariant describes "
                          f"(st_mode {st.st_mode:#o}). It was NOT opened: a read-open on a "
                          "FIFO blocks until a writer appears, and there is no timeout in "
                          "this call path.")
        else:
            # THE `fstat` IS NOT REDUNDANT WITH THE `stat` ABOVE. That one answered about a
            # NAME; this one answers about the descriptor whose bytes are actually digested,
            # and between the two the path can have become a link (refused by `O_NOFOLLOW`) or
            # a FIFO (which `O_NONBLOCK` keeps from blocking and this refuses).
            try:
                fd = bundlemod.open_leaf(at, os.O_RDONLY, "a coverage criterion path")
            except OSError as e:
                return Result(row_id, index, "unresolved", None,
                              f"{c.path!r} could not be read: {e}")
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    return Result(row_id, index, "mechanically_checked", False,
                                  f"{c.path!r} is not the regular file the invariant "
                                  "describes")
                got = snapshot.digest_fd(fd)
            except OSError as e:
                return Result(row_id, index, "unresolved", None,
                              f"{c.path!r} could not be read: {e}")
            finally:
                os.close(fd)
    ok = got == c.sha256
    return Result(row_id, index, "mechanically_checked", ok,
                  f"{c.path}: {got} {'==' if ok else '!='} {c.sha256}")


def _schema(c, *, row_id, index, **_) -> Result:
    """Nothing is evaluated, and the detail says so.

    There is no database and no schema in this repository and none should be invented for one.
    The kind exists because §10.1 names a schema query among its four mechanical checks, and
    `ledger.Criterion` therefore records the query and the schema it runs against — so the
    INPUTS to a predicate are present here while the predicate is not. That gap is exactly the
    tempting one: an evaluator that reads the recorded query and reports what the record
    already said would be a check in name only.

    It must NOT fall through to `manual_trace_confirmed`, which asserts a human traced it — an
    unwritten evaluator would then produce a value that reads as human diligence. `unresolved`
    is the honest word for "nobody looked".
    """
    return Result(row_id, index, "unresolved", None,
                  "no schema evaluator is wired in this repository, so nobody looked: the "
                  f"criterion records the query {c.query!r} against {c.path!r} and no "
                  "predicate ran it")


def _prose(c, *, row_id, index, **_) -> Result:
    """A natural-language criterion is NEVER `mechanically_checked`.

    A recorded human trace is `manual_trace_confirmed` — a human's word, carried as a human's
    word — and its absence is `unresolved`. §10.1: a generic walk over natural-language rows is
    systematic review, not deterministic coverage.
    """
    if c.trace:
        return Result(row_id, index, "manual_trace_confirmed", None, c.trace)
    return Result(row_id, index, "unresolved", None,
                  "a natural-language criterion with no recorded trace: a generic walk "
                  "over it is systematic review, not deterministic coverage")


# THE ALLOW-LIST, AND IT IS WHY A NEW CRITERION KIND FAILS CLOSED. A kind added to
# `ledger.CRITERION_KINDS` with no entry here reaches `evaluate`'s raise rather than a silent
# default, and `test_every_ledger_criterion_kind_has_a_declared_evaluator` turns that runtime
# raise into a suite failure at the moment the kind is added.
_EVALUATORS = {
    "test": _test,
    "symbol": _symbol,
    "hash": _hash,
    "schema": _schema,
    "prose": _prose,
}


def evaluate(criterion, *, row_id, index, tree, pytest_argv=None, run=subprocess.run) -> Result:
    """One criterion's `(method, satisfied)` pair.

    Raises rather than answering for a record that is not a criterion this engine can dispatch:
    an unknown kind, or a kind whose declared inputs are absent. Both are producer defects that
    `ledger` refuses at write, and neither is a measurement that came out any particular way —
    `unresolved` would file a broken record beside "we looked and could not tell".
    """
    if not isinstance(criterion, ledger.Criterion):
        raise CoverageError(f"a Criterion is required, not {type(criterion).__name__}")
    fn = _EVALUATORS.get(criterion.kind)
    if fn is None:
        raise CoverageError(
            f"no evaluator is declared for criterion kind {criterion.kind!r}; a kind with no "
            "predicate must fail closed here rather than acquire a default one")
    _require_inputs(criterion)
    return fn(criterion, row_id=row_id, index=index, tree=tree,
              pytest_argv=pytest_argv, run=run)


def _contradictions(l) -> tuple:
    """The two contradictions a machine CAN find, and a note about the one it cannot.

    (a) TWO ROWS THAT CONFLICT MAY NOT BOTH BE ACCEPTED. This is the mechanical reading of
    §10's "coverage asserts no accepted row contradicts a unanimous rejection", and it is a
    REPORT FINDING rather than a write refusal: §12.4 makes the coverage check "a fallback
    trigger AND a report line", and a write refusal can be neither.

    (b) AN ACCEPTED ROW WHOSE EVERY SEAT REJECTED IT. "Unanimous" requires at least two seats
    and NO `silent` one — a seat that said nothing did not reject. §10's example is the whole
    reason: if all three seats considered and rejected a cache layer, that is the most valuable
    signal in the run, and from-scratch synthesis (which reads only the ledger) would otherwise
    add it straight back.

    WHAT IS NOT REACHABLE HERE, stated so nobody reads this as complete: whether an accepted
    row's CONTENT contradicts a different, unanimously-rejected row is a semantic comparison no
    predicate can make. §10.1's own rule applies to this file too.
    """
    out = []
    status = {r.id: r.status for r in l.rows}
    claim = {r.id: r.semantic_claim for r in l.rows}
    pairs = set()
    for r in l.rows:
        for d in r.dependencies:
            if d.relation == "conflicts":
                pairs.add(tuple(sorted((r.id, d.id))))
    for a, b in sorted(pairs):
        if status.get(a) == "accepted" and status.get(b) == "accepted":
            out.append(f"{a} and {b} conflict and are both accepted: "
                       f"{claim.get(a, '')!r} vs {claim.get(b, '')!r}")
    for r in l.rows:
        if r.status != "accepted" or len(r.seat_evidence) < 2:
            continue
        if all(e.stance == "contradicts" for e in r.seat_evidence):
            out.append(f"{r.id} is accepted over a unanimous rejection by "
                       f"{[e.seat for e in r.seat_evidence]}: {r.semantic_claim!r}")
    return tuple(out)


def unmeasured(results) -> tuple:
    """Every result no predicate ran on — §10.1's `manual_trace_confirmed` AND `unresolved`.

    ONE FUNCTION, BECAUSE TWO SPELLINGS OF ONE JUDGEMENT CANNOT BE KEPT IN STEP BY BOTH BEING
    REMEMBERED. `rubric._read_report` and `strategy.classify_failure` each carried a copy that
    knew `unresolved` and not `manual_trace_confirmed` — §10.1 names both in one sentence
    ("Everything else is marked `manual_trace_confirmed` or `unresolved`") and each copy
    caught one. The agreement test between the two modules passed throughout, because they
    AGREED: an agreement test between two copies cannot find a defect they share.

    WHAT IT COST, measured through `check` from a real ledger: a report of traced prose scored
    `unsatisfied_criteria=0` — the best possible value on §12.5's top dimension — and came
    back fully rankable, so a seat that failed its gate with no predicate run on any claim
    outranked one that passed with a real miss. Since a criterion's `kind` is the author's
    choice, that made the cheapest criterion to write also the highest-scoring one.

    NOT A COMPLETE GAP PREDICATE ON ITS OWN, and callers must not treat it as one: an EMPTY
    result list has no unmeasured member either, so `unmeasured(()) == ()`. "Nothing was
    measured because there was nothing to measure" is a different sentence from "everything
    was measured", and the empty case stays a separate branch in both callers.
    """
    return tuple(r for r in results if r.method != "mechanically_checked")


def check(l, *, tree, pytest_argv=None, run=subprocess.run) -> Report:
    """Every criterion on every row, plus the contradictions and the two roll-ups.

    AN ACCEPTED ROW WITH NO ACCEPTANCE CRITERIA PRODUCES A LINE, and it must. `ledger._decode`
    refuses a row whose `acceptance_criteria` key is ABSENT for exactly this reason — "the row
    reports as fully covered with nothing checked" — but a key that is PRESENT and empty
    decodes cleanly, and it has to: a `rejected` or `deferred` row has no acceptance criteria to
    write. So the refusal cannot live at write, and this is the other end of the same hole. An
    accepted claim nobody wrote a criterion for is `unresolved`, indexed `NO_CRITERION`.

    A LEDGER WITH NO ROWS IS REFUSED, not reported as an empty run — see the comment below.

    `_check_rows` is imported rather than restated. `_contradictions` reads `d.relation` and
    `e.stance` off the nested lists, and a `Ledger` built in process — which §12.2 does by
    construction — can hold dicts there; `AttributeError` out of a public function is an error
    class no caller of this module knows to catch.
    """
    if not isinstance(l, ledger.Ledger):
        raise CoverageError(f"a Ledger is required, not {type(l).__name__}")
    try:
        ledger._check_rows(l.rows)
    except ledger.LedgerError as e:
        raise CoverageError(str(e)) from e
    # A LEDGER WITH NO ROWS IS THE EMPTY-CRITERIA FAIL-OPEN ONE CONTAINER OUT. The paragraph
    # above closes the row that declares no criteria; this closes the LEDGER that declares no
    # rows, which reached the same place by a shorter route — zero results, zero `unsatisfied`,
    # zero `unresolved`, zero contradictions, a run reported as fully covered having checked
    # nothing. `ledger._decode` refuses it on the way in and says so in the same words, but
    # §12.2's partitioned synthesis builds a `Ledger` in process and never meets a decoder,
    # which is exactly the caller this function was written for.
    if not l.rows:
        raise CoverageError(
            "this ledger has no rows, so there is nothing to check and an all-empty report "
            "would read as a fully covered run. A ledger with no claims is a run with no "
            "claims, which §10.1's own failure shape is made of.")
    results = []
    for r in l.rows:
        if r.status == "accepted" and not r.acceptance_criteria:
            results.append(Result(
                r.id, NO_CRITERION, "unresolved", None,
                "this row is accepted and declares no acceptance criteria, so nothing about "
                "it was checked; a row with no criteria must not read as a covered one"))
        for i, c in enumerate(r.acceptance_criteria):
            results.append(evaluate(c, row_id=r.id, index=i, tree=tree,
                                    pytest_argv=pytest_argv, run=run))
    return Report(tuple(results), _contradictions(l),
                  tuple(_lines(results, "unsatisfied")), tuple(_lines(results, "unresolved")))
