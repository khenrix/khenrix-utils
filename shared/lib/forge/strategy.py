"""§12's strategy decision and §12.3's failure classification.

WHAT THIS MODULE REFUSES TO DO. §12.1 says the size gate "triggers analysis, it does not
force partitioning", and admits a partition only "where stable seams exist" — which §10.1
names as the kind of natural-language criterion that must never be presented as a checked
predicate. So above the threshold this module returns NO strategy and method `unresolved`.
The door to a strategy up there is `recorded_seam_analysis`, which takes a rationale and
records `manual_trace_confirmed`, exactly as §12.1 requires.

THE SIZE IS A UNION OVER PATHS, NOT A SUM OVER SEATS. Three seats editing one file is one
changed file; summing the fleet would treat agreement as bulk and push every three-seat run
over the threshold. Lines are summed per path across the seats' patches, because two seats
touching the same file really did produce two different amounts of change and the larger is
not a safe stand-in for either.

TWO AXES, AND THEY ARE NOT THE SAME AXIS. `verify.classify` answers WHAT HAPPENED — PASS,
FAIL, BASELINE_RED_…; §12.3's classification below answers WHOSE FAULT — infrastructure,
synthesis-introduced, requirement-gap. A run has both, they are read off different evidence,
and collapsing either into the other loses the question the other one was asked.
"""
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import bundle as bundlemod, coverage, gitcmd, verify

# §12.1's thresholds, verbatim: "Below ~400 changed lines / ~15 files, from-scratch fusion."
SIZE_LINE_THRESHOLD = 400
SIZE_FILE_THRESHOLD = 15

FROM_SCRATCH = "from_scratch"
PARTITION = "partition"
BASE_AND_PORT = "base_and_port"
STRATEGIES = (FROM_SCRATCH, PARTITION, BASE_AND_PORT)

INFRASTRUCTURE = "infrastructure"
SYNTHESIS_INTRODUCED = "synthesis_introduced"
REQUIREMENT_GAP = "requirement_gap"
FAILURE_CLASSES = (INFRASTRUCTURE, SYNTHESIS_INTRODUCED, REQUIREMENT_GAP)

PERMITTED = "permitted"
REFUSED = "refused"
UNDECIDABLE = "undecidable"
DISPOSITIONS = (PERMITTED, REFUSED, UNDECIDABLE)


class StrategyError(RuntimeError):
    """A strategy question this module will not answer on the evidence it was given."""


@dataclass(frozen=True)
class Size:
    """§12.1's measured artifact size, with the reasons it is partly or wholly unknown.

    `changed_lines` and `changed_files` are INDEPENDENTLY nullable, and that is not a
    convenience: a binary file has a countable PATH and an uncountable line delta, and
    collapsing both to None would throw away a measurement that was taken. `unmeasured` is
    never empty when either is None, and a reader that prints the size prints these lines
    beside it.

    THE SECOND CHECK BELOW IS WHY `measure` CANNOT LEAVE A REFUSAL UNAPPLIED. A size naming
    something it could not measure while still reporting both dimensions is a verdict reading
    cleaner than its evidence, so it is refused here — which makes every path in `measure`
    that records a reason obliged to void the dimension that reason takes away. That
    obligation is the invariant; the raise is only how it is collected.
    """
    changed_lines: int | None
    changed_files: int | None
    unmeasured: tuple[str, ...]

    def __post_init__(self) -> None:
        if (self.changed_lines is None or self.changed_files is None) and not self.unmeasured:
            raise StrategyError(
                "a size with an unknown dimension must say what could not be measured; an "
                "unexplained None is a gap a reader cannot weigh")
        if self.unmeasured and self.changed_lines is not None and self.changed_files is not None:
            raise StrategyError(
                "this size names things it could not measure and yet reports both dimensions; "
                "one of the two statements is false")


def _numstat_record(rec: str) -> tuple:
    """One `--numstat -z` record, as `(path, changed_lines, refusal)`.

    Under `-z` git emits `<added> TAB <deleted> TAB <path> NUL` with the name raw, so the
    path is what survives `split("\\t", 2)` — a path may itself contain a TAB, the two counts
    never can. Measured on git 2.53.0, a path holding a literal tab came back
    `2\\t0\\thas\\ttab.txt` and this split reads it whole.

    THREE WAYS A CELL CAN FAIL TO BE A COUNT, and none of them is zero:

      * `-` in either cell is a BINARY file. Measured on git 2.53.0 against a patch touching
        one binary and two text files:

            -\\t-\\tb.bin
            1\\t0\\tnew.txt
            1\\t0\\tt.txt

        `int("-")` raises and a `try/except: continue` would silently drop the path's lines,
        which is how a whole-blob rewrite lands under a 400-line threshold. The path is still
        returned; only its lines are refused, and the refusal is named.
      * A cell that is neither `-` nor a number git has never been measured to emit — so
        this branch is not a reading of git's behaviour but a refusal to guess past it. The
        alternative is not a late failure: it is an uncaught `ValueError` out of the middle
        of a size measurement.
      * A record with fewer than three fields describes NO path, which is the one failure
        that cannot be attributed to a file. `path` is None there, and the caller must void
        the whole patch rather than drop one record from a total it goes on reporting.
    """
    parts = rec.split("\t", 2)
    if len(parts) != 3:
        return None, None, (
            f"git emitted a --numstat record of {len(parts)} field(s) rather than three "
            f"({rec!r}), so neither the path it describes nor that path's line count can be "
            "read off it, and this patch's file list is not one this run measured")
    added, deleted, path = parts
    if added == "-" or deleted == "-":
        return path, None, (
            f"{path}: git reports a binary delta (`-`), so its changed-line count is not a "
            "number this run measured")
    try:
        return path, int(added) + int(deleted), None
    except ValueError:
        return path, None, (
            f"{path}: git gave the changed-line counts as {added!r} and {deleted!r}, which "
            "are not two numbers this run can add")


def _numstat(repo, patch: bytes) -> tuple:
    """`git apply --numstat -z` over one patch: `({path: lines}, refusals)`.

    THE PARSE IS GIT'S, NOT A HAND-WRITTEN ONE, for `bundle._patch_paths`'s reason: a
    `diff --git` header C-quotes an unusual path, so reading names off the header text means
    reimplementing `unquote_c_style`.

    THE `{path: lines}` HALF IS None WHEN THE PATCH COULD NOT BE READ AT ALL — a non-zero
    `git apply --numstat`, or a record with no path in it. That is a different fact from "one
    of its paths has no line count", and it has to travel as one: an unreadable patch leaves
    the engine not knowing which FILES it touched either, so its caller must void both of
    §12.1's dimensions rather than report a file count over the seats it could read.

    A `None` VALUE IN THE DICT IS A REFUSAL AND IT STICKS. `lines[path]` is only ever raised
    from None back to a number by a later record for the same path, and a patch really can
    carry two records for one path — measured, a concatenation of a binary and a text diff
    for `t.txt` yields both, in either order. Letting the number win would restore precisely
    the count git declined to give.
    """
    if not patch:
        # `git apply --numstat` exits 128 with "No valid patches in input" on an empty patch
        # (measured), so answering from the bytes is what keeps an empty candidate from
        # reading as an unreadable one.
        return {}, ()
    lines, unmeasured = {}, []
    with tempfile.TemporaryDirectory() as td:
        # NOT inside a seat or a verifier: a patch file dropped in either tree is an untracked
        # file the next inventory would report as the agent's work.
        f = Path(td) / "candidate.patch"
        f.write_bytes(patch)
        r = gitcmd.git(repo, "apply", "--numstat", "-z", str(f),
                       env_extra=gitcmd.READONLY, check=False, binary=True)
        if r.returncode != 0:
            return None, (f"git apply --numstat -> {r.returncode}: "
                          f"{r.stderr.decode('utf-8', 'replace').strip()}",)
        for rec in r.stdout.decode("utf-8", "surrogateescape").split("\0"):
            if not rec:
                continue
            path, n, why = _numstat_record(rec)
            if why:
                unmeasured.append(why)
            if path is None:
                return None, tuple(unmeasured)
            if n is None or lines.get(path, 0) is None:
                lines[path] = None
            else:
                lines[path] = lines.get(path, 0) + n
    return lines, tuple(unmeasured)


def _sidecar_lines(e) -> tuple:
    """One sidecar's changed-line count, or None with the reason it has none.

    A symlink is one line — its target text — which is `baseline`'s own reading of a link
    (D-1: "a symlink IS its target text"). A file whose payload is not valid UTF-8 has no
    line count at all; counting `\\n` bytes in a PNG is a number with no meaning, and a number
    with no meaning is what the threshold would then compare.
    """
    if e.kind == "symlink":
        return 1, None
    try:
        text = e.payload.decode("utf-8")
    except UnicodeDecodeError:
        return None, (f"{e.path}: this sidecar's payload is not UTF-8, so it has no "
                      "changed-line count; counting newline bytes in binary is not a measurement")
    if not text:
        return 0, None
    return text.count("\n") + (0 if text.endswith("\n") else 1), None


def measure(repo, candidates) -> Size:
    """§12.1's size over the whole candidate set, refusing every count it cannot take.

    `repo` is any git repository the patches can be `git apply --numstat`'d against — the
    numstat pass does not apply anything and does not need the patch to be applicable, only
    parseable. A verifier clone or the baseline clone is the natural one.

    A NON-EMPTY `omitted` VOIDS THE WHOLE SIZE. `CandidateBundle.omitted` is what the harvest
    could not carry, and §6.2 already treats it as displacing every other verdict
    (`verify.classify` returns HARVEST_INCOMPLETE before it reads the exit code). A size
    computed over a bundle that is missing artifacts is an undercount presented as a total,
    which is the shape §12.1's threshold is least able to survive.

    THE THREE WAYS THIS RETURNS AN ENTIRELY UNKNOWN SIZE ARE ONE ARGUMENT MADE THREE TIMES.
    No candidate at all, no candidate that carries anything, and a patch git could not read
    are all states in which 0 lines across 0 files is a true sentence about the arithmetic
    and a false one about the run: `decide` would call it a small change and select
    from-scratch fusion over an artifact set nobody measured. The second of those is not a
    variant of the first — a fleet whose three seats each returned empty-handed arrives here
    as a populated mapping — and it is the one a caller reaches without ever passing `{}`.
    """
    if not isinstance(candidates, dict):
        raise StrategyError(f"the candidate set is a mapping of seat -> CandidateBundle, "
                            f"not {type(candidates).__name__}")
    if not candidates:
        return Size(None, None, (
            "no candidate was supplied, so there is nothing to size; an empty set measuring "
            "0 lines and 0 files would select from-scratch fusion over no evidence at all",))
    wrong = sorted({name for name, c in candidates.items()
                    if not isinstance(c, bundlemod.CandidateBundle)})
    if wrong:
        raise StrategyError(f"these are not CandidateBundles: {wrong}")

    omitted = sorted({p for c in candidates.values() for p in c.omitted})
    if omitted:
        return Size(None, None, (
            f"{len(omitted)} artifact(s) were omitted from the harvest ({omitted[:5]}), so "
            "the candidate set is incomplete and any size over it is an undercount reported "
            "as a total",))

    per_path: dict = {}
    unmeasured: list = []
    unreadable = False
    for name in sorted(candidates):
        c = candidates[name]
        lines, why = _numstat(repo, c.tracked_patch)
        unmeasured.extend(f"{name}: {w}" for w in why)
        if lines is None:
            # Keep going rather than return here: the other seats' refusals are evidence the
            # reader needs too, and a report naming one unreadable patch when three were
            # unreadable is the same undercount this function refuses everywhere else.
            unreadable = True
            continue
        for path, n in lines.items():
            if n is None or per_path.get(path, 0) is None:
                per_path[path] = None
            else:
                per_path[path] = per_path.get(path, 0) + n
        for e in c.sidecars:
            n, why_one = _sidecar_lines(e)
            if why_one:
                unmeasured.append(f"{name}: {why_one}")
            if n is None or per_path.get(e.path, 0) is None:
                per_path[e.path] = None
            else:
                per_path[e.path] = per_path.get(e.path, 0) + n

    if unreadable:
        if not unmeasured:
            # Belt and braces: `_numstat` returns None only alongside the reason it did, and
            # this refuses the state where that stops being true rather than shipping a
            # silent one — `Size` would raise on the bare None and say nothing useful.
            unmeasured.append("a candidate's patch could not be read and nothing recorded why")
        return Size(None, None, tuple(unmeasured))
    if not per_path:
        return Size(None, None, (
            f"not one of the {len(candidates)} candidate(s) carries a tracked patch or a "
            "sidecar, so there is no artifact to size; 0 lines across 0 files is the empty "
            "set's own reading with seat names attached to it, and it would select "
            "from-scratch fusion over a fleet that produced nothing",))

    changed_files = len(per_path)
    changed_lines = None if any(v is None for v in per_path.values()) \
        else sum(per_path.values())
    if changed_lines is None and not unmeasured:
        # Belt and braces, as above: `per_path` only ever holds None where a reason was
        # appended, and this refuses the state where that stops being true.
        unmeasured.append("a path's changed-line count is unknown and nothing recorded why")
    return Size(changed_lines, changed_files, tuple(unmeasured))


@dataclass(frozen=True)
class Decision:
    """Which strategy, and on what kind of evidence — §10.1's method axis, reused verbatim.

    THE PAIRING IS ENFORCED HERE, in `coverage.Result`'s footsteps and for its reason: a rule
    that holds only on the path its author remembered is what this package refuses everywhere
    else, and `Decision` is a public dataclass a later phase will build for itself.

      * `mechanically_checked` belongs to exactly one branch — the size gate found both
        dimensions under §12.1's thresholds. That is a numeric comparison over a measured
        value, which is what "mechanical" means here.
      * `manual_trace_confirmed` carries a strategy a HUMAN chose: the §5 gate's confirmed
        rule, or a recorded seam analysis. §12.1 requires exactly this label for the partition
        decision and forbids presenting it as a checked predicate.
      * `unresolved` carries NO strategy. It is the state above the threshold before anyone
        has looked at the seams, and the state where the size could not be measured at all.

    WHAT `__post_init__` DOES NOT REACH, stated for `coverage.Result`'s reason: it is not a
    wall. `object.__new__` plus `object.__setattr__` goes past it, and so does an unpickle
    that restores `__dict__`, which is what default dataclass pickling does. Nothing here is
    pickled or subclassed today; the boundary is written down so the next reader learns it
    here rather than from a `mechanically_checked` partition.
    """
    strategy: str | None
    method: str
    detail: str

    def __post_init__(self) -> None:
        if self.method not in coverage.METHODS:
            raise StrategyError(f"method is one of {list(coverage.METHODS)}, "
                                f"not {self.method!r}")
        if self.strategy is not None and self.strategy not in STRATEGIES:
            raise StrategyError(f"strategy is one of {list(STRATEGIES)} or None, "
                                f"not {self.strategy!r}")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise StrategyError("a decision says why; an empty detail is a verdict with no "
                                "evidence attached to it")
        if self.method == "unresolved" and self.strategy is not None:
            raise StrategyError(
                f"an unresolved decision names no strategy, and this one names "
                f"{self.strategy!r}: 'nobody decided' and 'we chose this' are the two states "
                "this axis exists to keep apart")
        if self.method != "unresolved" and self.strategy is None:
            raise StrategyError(
                f"a {self.method!r} decision that chose nothing is a measurement with no "
                "answer, which appears in no roll-up and is invisible to every reader")
        if self.method == "mechanically_checked" and self.strategy != FROM_SCRATCH:
            raise StrategyError(
                f"{self.strategy!r} cannot be mechanically checked: §12.1 admits a partition "
                "only where stable seams exist, and §10.1 forbids presenting that "
                "natural-language criterion as a checked predicate. The only mechanical "
                f"branch is the size gate selecting {FROM_SCRATCH!r}.")


# §5's confirmed rule (`gate.STRATEGY_RULES`) mapped to what it means once artifacts exist.
# `size-gated` is the only one that consults the measurement; the other two were chosen
# before any artifact existed, which is what makes them a human's decision rather than one.
_PRECOMMITTED = {"fusion": FROM_SCRATCH, "base-and-port": BASE_AND_PORT}


def decide(rule: str, size: Size) -> Decision:
    """§12's confirmed rule applied to §12.1's measured size."""
    if not isinstance(size, Size):
        raise StrategyError(f"a Size is required, not {type(size).__name__}")
    if rule in _PRECOMMITTED:
        return Decision(_PRECOMMITTED[rule], "manual_trace_confirmed",
                        f"the §5 gate confirmed the rule {rule!r} before any artifact "
                        "existed, so this strategy is the operator's recorded decision and "
                        "not a reading of the measured size")
    if rule != "size-gated":
        raise StrategyError(
            f"the confirmed strategy rule is one of {['size-gated', *_PRECOMMITTED]}, not "
            f"{rule!r}; `gate.STRATEGY_RULES` is where §5 step 2's answer is bounded")
    if size.changed_lines is None or size.changed_files is None:
        return Decision(None, "unresolved",
                        "§12.1's threshold cannot be applied to a size this run did not "
                        f"measure: {'; '.join(size.unmeasured)}. An unmeasured size is not a "
                        "small one, and defaulting it to from-scratch fusion would spend the "
                        "run's whole synthesis budget on evidence nobody took.")
    if size.changed_lines < SIZE_LINE_THRESHOLD and size.changed_files < SIZE_FILE_THRESHOLD:
        return Decision(FROM_SCRATCH, "mechanically_checked",
                        f"{size.changed_lines} changed lines across {size.changed_files} "
                        f"file(s), both under §12.1's ~{SIZE_LINE_THRESHOLD}/"
                        f"~{SIZE_FILE_THRESHOLD}")
    return Decision(None, "unresolved",
                    f"{size.changed_lines} changed lines across {size.changed_files} file(s) "
                    f"is at or over §12.1's ~{SIZE_LINE_THRESHOLD}/~{SIZE_FILE_THRESHOLD}, "
                    "which triggers a seam analysis rather than forcing a partition. "
                    "'Stable seams exist' is §10.1's non-mechanical criterion; record the "
                    "analysis through `recorded_seam_analysis` and it becomes "
                    "`manual_trace_confirmed`.")


def recorded_seam_analysis(chosen: str, rationale: str) -> Decision:
    """§12.1's above-threshold choice, recorded as the human judgement it is.

    `from_scratch` IS REFUSED HERE. Below the threshold the size gate already produces it
    mechanically; above the threshold §12.1 offers partition or base-and-port and nothing
    else. Admitting it would let a `manual_trace_confirmed` decision overwrite a
    `mechanically_checked` one with a weaker method under the same strategy name.
    """
    if chosen not in (PARTITION, BASE_AND_PORT):
        raise StrategyError(
            f"a seam analysis chooses {PARTITION!r} or {BASE_AND_PORT!r}, not {chosen!r}: "
            "§12.1 offers those two above the threshold, and the from-scratch branch is the "
            "one the size gate decides mechanically below it")
    if not isinstance(rationale, str) or not rationale.strip():
        raise StrategyError(
            "a recorded seam analysis carries its rationale; §12.3's last sentence forbids an "
            "unrecorded intuition, and a blank one is exactly that with a field around it")
    return Decision(chosen, "manual_trace_confirmed", rationale.strip())


# §12.3's axis, which is NOT `verify.classify`'s. §6.2 answers "what did the gate say"; this
# answers "why did it say it", and the two have different vocabularies on purpose.
_INFRASTRUCTURE_OUTCOMES = (verify.BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE,
                            verify.HARVEST_INCOMPLETE)


def classify_failure(outcome: str, *, report) -> tuple:
    """§12.3's (class, reason) for one verify outcome. `None` when nothing can be said.

    THREE VALUES PLUS A REFUSAL, and the refusal is the point. §12.3 forbids falling back on
    an infrastructure failure and requires falling back when synthesis has stopped making
    progress — so the class decides money. Two outcomes are deliberately unclassifiable:

      * `FLAKY` says the run pair cannot answer at all. Calling it infrastructure would refuse
        a fallback the run may need; calling it synthesis-introduced would spend one on a coin
        flip.
      * `GATE_CHANGED` says the candidate moved a gate-defining file, so the gate that
        measured it is not the baseline's. `verify.classify`'s own reason for that outcome
        carries "on the runs alone this would have been PASS" — the failure being classified
        may not be a failure.

    THE UNRESOLVED-REPORT BRANCH IS THIS FUNCTION'S FAIL-CLOSED HALF. A `coverage.Report`
    separates "checked and false" (`unsatisfied`) from "nobody could check" (`unresolved`)
    precisely so those cannot be read as one another. A FAIL whose report holds an unresolved
    criterion has NOT established that every claim is satisfied, so it has not established
    that the failure came from synthesis — and `synthesis_introduced` is the class that
    permits spending a fallback.

    A `verify` OUTCOME WITH NO BRANCH HERE IS A RAISE, NOT A DEFAULT, and the ordering below
    is what makes it one: every value §6.2 declares is named above the ledger read, so the
    ledger read is FAIL's alone and is guarded by an explicit test for it. Falling through to
    it instead would give a newly added outcome the reading `synthesis_introduced` — the one
    class of the three that PERMITS spending a fallback — off a report that was never about
    it. That is the shape a vocabulary grows into silently, so the day §6.2 gains a row is the
    day this function is required to fail.
    """
    if outcome not in verify.OUTCOMES:
        raise StrategyError(
            f"{outcome!r} is not one of §6.2's outcomes {list(verify.OUTCOMES)}; a class "
            "assigned to a verdict this engine does not produce is a reading of nothing")
    if outcome == verify.PASS:
        return None, ("this outcome is not a failure, so §12.3 has nothing to classify; a "
                      "class here would describe a run that passed")
    if outcome in _INFRASTRUCTURE_OUTCOMES:
        return INFRASTRUCTURE, (
            f"{outcome} is a statement about the harness rather than about the candidate — "
            "§12.3: never fall back on an infrastructure failure, because base-and-port "
            "cannot help")
    if outcome == verify.FLAKY:
        return None, ("the gate disagreed with itself across reruns, so this run pair says "
                      "nothing about why the candidate failed; §12.3's three classes are all "
                      "claims this evidence does not support")
    if outcome == verify.GATE_CHANGED:
        return None, ("the candidate moved the gate surface, so the gate that produced this "
                      "verdict is not the baseline's; §6.2 records that on the runs alone the "
                      "outcome would have been different, and neither reading of the failure "
                      "survives that")
    if outcome != verify.FAIL:
        raise StrategyError(
            f"{outcome!r} is one of §6.2's outcomes and this function has no reading for it. "
            "Every other value in `verify.OUTCOMES` is named above; the coverage read below "
            f"is {verify.FAIL}'s alone, and letting a new outcome reach it would classify it "
            f"{SYNTHESIS_INTRODUCED!r} — the class that permits spending a fallback — off a "
            "report that says nothing about it.")
    if report is None:
        return None, ("no coverage report was taken for this candidate, so whether a required "
                      "claim is unmet is a question nobody asked; §12.4 makes that check the "
                      "only thing that catches a false green and it did not run")
    if not isinstance(report, coverage.Report):
        raise StrategyError(f"a coverage.Report or None is required, "
                            f"not {type(report).__name__}")
    if not report.results:
        return None, ("this coverage report holds no results at all, so it says nothing about "
                      "any claim; an empty report reading as a clean one is §10.1's own "
                      "failure shape")
    if report.contradictions:
        return REQUIREMENT_GAP, (
            f"{len(report.contradictions)} ledger contradiction(s): {report.contradictions[0]}")
    if report.unsatisfied:
        return REQUIREMENT_GAP, (
            f"{len(report.unsatisfied)} accepted claim(s) were checked and are not satisfied: "
            f"{report.unsatisfied[0]}")
    if report.unresolved:
        return None, (
            f"{len(report.unresolved)} criterion/criteria are unresolved — nobody could check "
            f"them ({report.unresolved[0]}) — so 'every claim is satisfied' is not something "
            "this run measured, and `synthesis_introduced` cannot be concluded from it")
    return SYNTHESIS_INTRODUCED, (
        f"every one of this candidate's {len(report.results)} criteria was mechanically "
        "checked and satisfied, and the gate still failed, so the failure is in the "
        "synthesis rather than in the requirements")


def fallback_disposition(failure_class) -> str:
    """Whether §12.3 permits base-and-port on this class. Three values, never a boolean.

    `permitted` for `synthesis_introduced` (§12.3: "fall back when synthesis is infeasible or
    has stopped making progress") and for `requirement_gap` (§12.4: "a missing accepted row is
    a fallback trigger *and* a report line, regardless of verify" — a seat may well have
    implemented what synthesis missed). `refused` for `infrastructure`, which §12.3 names
    outright. `undecidable` for `None`, and it is a THIRD value rather than a False so that
    "we could not tell" is never spelled the same way as "no": a caller that folded them
    together would report a run that refused to fall back and a run that could not decide
    with the same sentence.
    """
    if failure_class is None:
        return UNDECIDABLE
    if failure_class not in FAILURE_CLASSES:
        raise StrategyError(f"a failure class is one of {list(FAILURE_CLASSES)} or None, "
                            f"not {failure_class!r}")
    return REFUSED if failure_class == INFRASTRUCTURE else PERMITTED
