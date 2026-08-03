"""§12.3's progress tuple, and the oscillation detector that reads it back off the journal.

WHY EVERY ANSWER HERE IS THREE-VALUED. §12.3 makes "has stopped making progress" the trigger
for spending a fallback. A boolean cannot distinguish "this attempt did not improve anything"
from "nothing about this attempt could be measured", and the second one is the common case:
the run's gate is whatever command §5 step 2 confirmed, and the parser below understands
exactly one of them, so a repository whose gate is not pytest produces output this module
cannot read at all. An unreadable gate answering `frozenset()` would make every attempt a
strict improvement over its predecessor — the empty set is a subset of every set — and the
loop would run to its hard cap reporting progress at each step. `bundle.Delta.gate_surface`
is the same three states for the same reason: None is "nobody looked", `()` is "looked, and
there was nothing".

WHERE THE STATE LIVES: `events.jsonl`, not memory and not `runstate.State`. `State` is five
scalars and §14 argues for those five BECAUSE one enum cannot hold them; a sixth free-form
dimension would be a different change from the one §14 makes. The journal is where §14.1 puts
write-ahead pairs, and §13 states the rule this follows verbatim for `review_findings`: the
transition reads the RECORD, not the return value, because a compaction between "the fix
returned" and "the orchestrator classified it" must not lose the sighting.
"""
import re
from dataclasses import dataclass

from . import journal as journalmod

BETTER = "better"
NOT_BETTER = "not_better"
NOT_COMPARABLE = "not_comparable"
COMPARISONS = (BETTER, NOT_BETTER, NOT_COMPARABLE)

OSCILLATING = "oscillating"
NOT_OSCILLATING = "not_oscillating"
OSCILLATION_UNKNOWN = "unknown"
OSCILLATION_ANSWERS = (OSCILLATING, NOT_OSCILLATING, OSCILLATION_UNKNOWN)

# §14.2 names this event by this spelling; `journal.intent`/`journal.done` add the halves.
FIX_KIND = "synthesis_fix"

# pytest's own short-summary line. `FAILED <nodeid>` and `ERROR <nodeid>` both appear there,
# and only `FAILED` names a TEST — an `ERROR` row is a module that could not be collected, so
# reading it as a failing test invents a fingerprint that can never shrink.
_FAILED = re.compile(r"^FAILED (\S+)", re.MULTILINE)
# The evidence that this output came from pytest at all. Without one of these the text is
# some other runner's and nothing below may be read out of it.
_PYTEST_BANNERS = ("short test summary info", "=== FAILURES ===", " passed", " failed")


class ProgressError(RuntimeError):
    """A progress question this module will not answer on the evidence it was given."""


@dataclass(frozen=True)
class Progress:
    """§12.3's tuple: (new-failure count, failing-test fingerprint set).

    BOTH FIELDS ARE UNKNOWN TOGETHER OR NEITHER IS. The count is derived from the two sets, so
    a tuple carrying one without the other describes a measurement that cannot have happened,
    and `compare` would then have to decide which half to believe.
    """
    new_failure_count: int | None
    failing_test_fingerprints: frozenset | None

    def __post_init__(self) -> None:
        a = self.new_failure_count is None
        b = self.failing_test_fingerprints is None
        if a != b:
            raise ProgressError(
                "a progress tuple is measured whole or not at all: the new-failure count is "
                "derived from the fingerprint sets, so "
                f"(count={self.new_failure_count!r}, fingerprints="
                f"{self.failing_test_fingerprints!r}) describes a measurement that cannot "
                "have been taken")
        if not a:
            if not isinstance(self.new_failure_count, int) \
                    or isinstance(self.new_failure_count, bool) \
                    or self.new_failure_count < 0:
                raise ProgressError(
                    f"a new-failure count is a whole number, not "
                    f"{self.new_failure_count!r}")
            if not isinstance(self.failing_test_fingerprints, frozenset):
                # A mutable `set` gets this far and dies much later: it compares fine, and
                # then `oscillation` keys a sighting on it and raises `unhashable type` out
                # of the detector rather than out of the value that was wrong.
                raise ProgressError(
                    "the fingerprint set is a frozenset, so a tuple is hashable and can be "
                    f"used as a sighting key; got "
                    f"{type(self.failing_test_fingerprints).__name__}")


def pytest_fingerprints(stdout: str, stderr: str, exit_code: int):
    """The failing test ids in this gate's output, or `None` when it cannot be read.

    THE DOMAIN IS DECLARED AND NARROW: pytest, recognised by its own banner. §12.3 needs a
    per-test result and a run's gate is an arbitrary command, so this is the one runner this
    module claims to understand — and for every other runner the honest answer is `None`.
    Widening this later means adding a named parser, never loosening the banner check.

    IT READS FAILURES AND NEVER PASSES, which is what keeps it out of `coverage._test`'s way:
    a set of failing ids can say a named test is among them, and can never say a named test
    ran and passed.

    A ZERO EXIT WITH A BANNER IS AN HONEST EMPTY SET: pytest ran, and nothing failed. A
    NONZERO exit with a banner and no `FAILED` line is `None`, not `frozenset()` — pytest's
    exits 2, 3 and 4 (collection error, internal error, usage error) all reach that state, and
    an empty set there is the subset-of-everything fail-open this module exists to close.
    """
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise ProgressError(f"an exit code is an int, not {exit_code!r}")
    text = f"{stdout or ''}\n{stderr or ''}"
    if not any(b in text for b in _PYTEST_BANNERS):
        return None
    ids = frozenset(_FAILED.findall(text))
    if exit_code == 0:
        # A green pytest run that nonetheless printed FAILED lines is a contradiction, and
        # believing either half would be a verdict over evidence that disagrees with itself.
        return None if ids else frozenset()
    return ids or None


def from_runs(candidate_run, baseline_run, *, parse=pytest_fingerprints) -> Progress:
    """§12.3's tuple for one synthesis attempt, measured against the run's calibration.

    "NEW" IS A STATEMENT ABOUT THE CANDIDATE, which is why the baseline is a required
    argument and not an optional one — the same reason `verify.classify` takes a calibration.
    A baseline whose output cannot be read makes "new" unanswerable, so the whole tuple is
    unknown even when the candidate parsed perfectly.
    """
    cand = parse(candidate_run.stdout, candidate_run.stderr, candidate_run.exit_code)
    base = parse(baseline_run.stdout, baseline_run.stderr, baseline_run.exit_code)
    if cand is None or base is None:
        return Progress(None, None)
    return Progress(len(cand - base), cand)


def compare(before: Progress, after: Progress) -> str:
    """§12.3's strict ordering, as three outcomes.

    Progress requires the count to DECREASE or the fingerprint set to shrink under strict set
    inclusion. Equal is not progress. Two sets neither of which contains the other — fix A
    traded failure X for Y — is not progress either, and it is `not_better` rather than
    `not_comparable`: that comparison was taken and its answer is no. `not_comparable` is
    spent on exactly one thing, an unmeasured side, so that "we could not tell" can never be
    read as "no". §12.3's consumer treats `not_comparable` as no progress claim, which means
    fall back or stop — never continue on an assumed improvement.
    """
    for name, value in (("before", before), ("after", after)):
        if not isinstance(value, Progress):
            raise ProgressError(f"{name} is a Progress, not {type(value).__name__}")
    if before.new_failure_count is None or after.new_failure_count is None:
        return NOT_COMPARABLE
    if after.new_failure_count < before.new_failure_count:
        return BETTER
    if after.failing_test_fingerprints < before.failing_test_fingerprints:
        return BETTER
    return NOT_BETTER


@dataclass(frozen=True)
class Sighting:
    """One completed synthesis fix, as the pair oscillation is about.

    THE TREE OID, NOT THE COMMIT OID. Two checkpoints with identical content but different
    messages or timestamps are different commits and the same tree, and oscillation is about
    CONTENT recurring. §13 requires a fresh checkpoint after every fix and §14.1 makes git
    "the ordering of record", so a fix that ran to completion normally has one; §14.2's rule
    for an interrupted dirty tree (preserve it as a WIP checkpoint BEFORE touching it)
    produces one there too. A fix that recorded none forms no `Sighting` at all — hence the
    non-optional field — and `oscillation` counts it as a gap rather than as an absence.
    """
    tree_oid: str
    fingerprints: frozenset | None


def _tree(tree_oid):
    """A checkpoint identity, or None. An EMPTY STRING is refused, loudly.

    A fail-closed sentinel admits nothing, which is what `bundle.Delta`'s empty
    `generator_contract_id` is for. An empty-string tree id used as a dict key would do the
    opposite: it would make every unrecorded attempt "the same tree" and fire the stop signal
    on the second one.
    """
    if tree_oid is None:
        return None
    if not isinstance(tree_oid, str) or not tree_oid.strip():
        raise ProgressError(
            f"a checkpoint tree id is a non-empty string or None, not {tree_oid!r}: an empty "
            "one would key every unrecorded attempt to the same sighting and stop the loop "
            "on the second one, reporting a content recurrence nobody observed")
    return tree_oid


def record_fix_start(log, *, operation_id: str, tree_oid) -> None:
    """§14.1's write-ahead intent for one synthesis fix, carrying the tree it starts from."""
    log.record(journalmod.intent(FIX_KIND), operation_id=operation_id,
               tree_oid=_tree(tree_oid))


def record_fix_done(log, *, operation_id: str, tree_oid, prog: Progress) -> None:
    """The completion half, carrying the pair the detector reads back.

    `failure_fingerprints` is a SORTED LIST because JSON has no set, and `None` when the
    tuple was not measured — never `[]`, which would read back as "this attempt had no
    failing tests" and compare equal to a green run.
    """
    if not isinstance(prog, Progress):
        raise ProgressError(f"a Progress is required, not {type(prog).__name__}")
    log.record(journalmod.done(FIX_KIND), operation_id=operation_id,
               tree_oid=_tree(tree_oid),
               new_failure_count=prog.new_failure_count,
               failure_fingerprints=(None if prog.failing_test_fingerprints is None
                                     else sorted(prog.failing_test_fingerprints)))


def done_records(events) -> tuple:
    """Every completed synthesis fix, whatever it managed to record.

    THIS EXISTS SO THE GAP IS COUNTABLE. `sightings` below is the SUBSET of these that could
    form a `(tree, failing-test set)` pair; the difference between the two counts is exactly
    the evidence `oscillation` does not have. Without it a fix that ran, completed and left no
    checkpoint is discarded here and invisible there, and `oscillation` falls through to
    `not_oscillating` — *the synthesis is still making progress* — over a fix it never saw.
    """
    return tuple(e for e in events if e.event == journalmod.done(FIX_KIND))


def sightings(events) -> tuple:
    """Every completed fix that produced a usable tree id, oldest first.

    A done record with `tree_oid: None` produces NO sighting, because the pair cannot be
    formed. It is not thereby lost: `oscillation` counts `done_records` separately and reads
    the difference as a gap in the evidence rather than as the absence of a repeat.
    """
    out = []
    for e in events:
        if e.event != journalmod.done(FIX_KIND):
            continue
        oid = e.data.get("tree_oid")
        if oid is None:
            continue
        if not isinstance(oid, str) or not oid.strip():
            raise ProgressError(
                f"the {journalmod.done(FIX_KIND)} record at seq {e.seq} carries "
                f"tree_oid={oid!r}; only a non-empty string or null was ever written, so this "
                "journal was produced by something other than `record_fix_done`")
        ids = e.data.get("failure_fingerprints")
        if ids is None:
            out.append(Sighting(oid, None))
        elif isinstance(ids, list) and all(isinstance(i, str) for i in ids):
            out.append(Sighting(oid, frozenset(ids)))
        else:
            raise ProgressError(
                f"the {journalmod.done(FIX_KIND)} record at seq {e.seq} carries "
                f"failure_fingerprints={ids!r}, which is neither a list of ids nor null")
    return tuple(out)


def oscillation(events) -> tuple:
    """§12.3's stop signal, as three answers.

    THE RULE: the second sighting of the same `(tree_oid, fingerprints)` pair is the stop
    signal — fix A trades failure X for Y, fix B trades back, and the run has returned to a
    state it has already been in.

    A SIGHTING WITH UNMEASURED FINGERPRINTS FORMS NO PAIR. Two of them at one tree are
    `(oid, None)` twice; matching them would manufacture the stop signal out of two
    measurements nobody could take and report it as an observed recurrence.

    A COMPLETED FIX THAT RECORDED NO CHECKPOINT FORMS NO PAIR EITHER, and it is counted HERE
    rather than in `sightings`, which is the whole reason `done_records` exists. `sightings`
    drops such a record — there is no tree to key on — so a detector reading only `sightings`
    sees an empty log and answers `not_oscillating`: a fix that ran, completed and left no
    checkpoint would read as *the synthesis is still making progress*, which is the verdict
    §12.3 spends money on.

    A PROVEN REPEAT OUTRANKS A GAP. Once a pair has been seen twice the answer is
    `oscillating` whatever else is missing — `fingerprint.agreement_label`'s rule that a
    measured difference outranks an unmeasured field, in the other direction.

    An ORPHANED start — §14.1's `outcome_unknown` shape, which `journal.orphans` already
    identifies — is a fix whose sighting was never recorded, so the answer below it is
    `unknown` rather than `not_oscillating`.
    """
    pairs = sightings(events)
    seen = set()
    # The fixes that completed and recorded no tree at all. `sightings` cannot carry them, so
    # counting them here is the only place the absence survives into the answer.
    unmeasured = len(done_records(events)) - len(pairs)
    for s in pairs:
        if s.fingerprints is None:
            unmeasured += 1
            continue
        key = (s.tree_oid, s.fingerprints)
        if key in seen:
            return OSCILLATING, (
                f"the synthesis has returned to tree {s.tree_oid} with the same failing-test "
                "set it had there before; §12.3 calls the second sighting of that pair the "
                "stop signal")
        seen.add(key)
    orphaned = tuple(e for e in journalmod.orphans(events)
                     if e.event == journalmod.intent(FIX_KIND))
    if orphaned:
        return OSCILLATION_UNKNOWN, (
            "a synthesis fix started and recorded no result (operation "
            f"{', '.join(sorted(e.operation_id for e in orphaned))}), so whether its tree and "
            "failure set repeat an earlier pair could not be measured")
    if unmeasured:
        return OSCILLATION_UNKNOWN, (
            f"{unmeasured} completed fix(es) recorded no checkpoint or no failing-test set, "
            "so their pairs could not be formed and a recurrence among them would be "
            "invisible")
    return NOT_OSCILLATING, (
        f"{len(seen)} distinct (tree, failing-test set) pair(s) were recorded and none "
        "repeats")


def cap_remaining(manifest, events) -> int:
    """§12.3's hard cap, minus what has already been spent. Never negative.

    STARTS ARE WHAT COUNT, not dones. A fix that crashed between the two halves really did
    spend a provider call — §14.1 says so in the sentence that gives it the name
    `outcome_unknown` — and counting only completions would hand that budget back and let the
    loop buy the same fix twice.
    """
    cap = getattr(manifest, "synthesis_fix_cap", None)
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
        raise ProgressError(
            f"the run's manifest records no synthesis_fix_cap (got {cap!r}); §12.3's cap is "
            "priced at §5.2 and recorded once, and a default chosen here would be a budget "
            "nobody agreed to")
    spent = sum(1 for e in events if e.event == journalmod.intent(FIX_KIND))
    return max(0, cap - spent)
