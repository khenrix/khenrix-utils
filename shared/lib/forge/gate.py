"""§5's confirmation gate: the cost quote, the detector that can refuse a verify command,
and the one question a run is opened on.

ONE QUESTION, AND EVERYTHING THE ANSWER IS WORTH. §5 step 2 asks once — both command
sequences, a policy for calibration failure, and the strategy rule to be applied later — and
§5 step 5 says "record the decision; do not ask again". Three functions carry that: `must_show`
is what a human reads before answering, `confirm` turns the answer into a record that cannot
be defaulted, and `open_run` writes the manifest §14.2 forbids rewriting. Nothing here spends
a provider call, and nothing after it asks again — which is why every refusal in this file
lands before a run directory exists rather than after.

A QUOTE IS A REFUSAL SURFACE, NOT A DISPLAY. Everything here is sized so that being wrong is
wrong in the OPERATOR's disfavour: a worst case that reads low is a number somebody agrees to
and then discovers at 3am, and there is no later gate to catch it — §5 step 2 asks once.

THE PROVIDER-CALL ARITHMETIC, AND WHERE IT PARTS FROM §5.2's OWN SUM. §5.2 writes
`9 + 1 + 6 = 16`: 3 builders × 3 attempts, plus one synthesis, plus two review rounds × three
reviewers at the `--retries 0` forge wires (the council's own `--retries 2` default would make
the review term 18 and the total 28 — quoted here as a line, since it is not what is wired).
The same paragraph then requires that "the quote includes post-review synthesis invocations",
and 9 + 1 + 6 has exactly ONE synthesis in it. §13's loop has two more: a round-1 blocker is
fixed before round 2 runs at all, and "a blocker fixed after round 2 is reported *verified but
not independently reviewed*" — so a fix happens there too, and §12.3 caps the count rather than
forbidding it. §13.1's deep review is DEFAULT ON and its findings "follow the exact
post-round-2 rule above: fix → fresh-verifier verify → checkpoint", which is a third one: §5.2
prices the deep-review RUN and says nothing about the FIX it triggers.
`provider_calls` therefore adds one synthesis invocation per review round and one for
the deep review, and is 22 rather than 16 at the wired settings. `--skip-deep-review` is a PARAMETER here
rather than a reason to leave the term out of the default quote. §5.2's own sum is kept visible
as its own line, so nothing is hidden in either direction, and §21's "worst case 16 → 10 runs"
for cutting the retry path reads 13 here for the same reason.

WHAT THE SETUP AND VERIFY COUNTS ARE FOR. §6 is the expensive clause in the design — "it costs
one extra setup per candidate; that cost is essential, not optional hardening" — and a quote
that folds it into the provider line is the one an operator finds wrong first, because setup is
where the wall clock and the disk actually go. Three sources, each counted separately:
calibration runs setup+verify TWICE (§5 step 3, so the second pass can show zero tracked
delta); every builder clone runs setup once, and §8.1 gives a retry a FRESH clone, so the
builder term is seats × attempts and not seats; and a fresh verifier clone runs setup+verify
per candidate (§6) plus once after each post-review fix (§13, "re-run verify and cut a new
checkpoint after every fix", and §13.1 for the deep review's).

A builder's own clone is NOT counted as a verify. §7 lists `Fverify` among a seat's four
inventories, but §6 overrides it in as many words — "`Fverify` cannot be 'the fourth inventory
of the builder clone' when verification happens elsewhere" — and the whole §6 argument is that
running the gate where the builder was measures nothing.

PEAK DISK IS COUNTED IN CLONES, NOT IN FLEETS. §5.2 gives one figure — "three no-hardlink
clones plus three dependency trees is plausibly 6-10 GB" — and it prices the BUILDER FLEET at
three seats. Quoting that as the peak understates it by the whole rest of the run, in the
operator's favour: §8.1 preserves a failed retry's clone as partial input rather than resetting
in place, and §6 gives every candidate a FRESH verifier clone. §15's automatic cleanup covers
"known-failed temporary clones only" and would reclaim the failed retries — but §8.1 preserves
those specifically, as partial input, so the specific rule wins and none is removed while the
run is alive. The
spec's own upper bound is therefore divided by the fleet it was stated for — 10 GB / 3 clones —
and multiplied by every clone the worst case builds: calibration (§5 step 3), builders
(seats × attempts), verifiers, and §13's review clone per round — one per ROUND, not per
reviewer, since the panel reads one tree together. That is 19 clones at the wired settings
(1 + 9 + 7 + 2, the formula below), not 3. Left out and
said so in the line: the synthesis WORKTREE, which §4 makes a worktree precisely so it shares
the parent's objects, and where setup never runs.

THE DETECTOR IS STATIC AND READS ONLY. §5 step 1 admits no repository-supplied code before
authorization, and a detector whose job is "would this command spend provider calls" is exactly
the one tempted to find out by running it. It executes no Makefile, no recipe, no script and no
`make -n`. The detector's own git call is `rev-parse --show-toplevel`, which carries
`NO_DAEMON_CACHE` even though it does not need it: measured on git 2.53.0, with `core.fsmonitor`
pointed at a script that touches a file, `git status` runs the script and `git rev-parse
--show-toplevel` does not, because the second loads no index. The flags are on it so the
guarantee holds for whatever git call is added here next, which is the direction this package
keeps losing the property in — see `gitcmd.NO_DAEMON_CACHE`'s own note for the measured rule
and `test_every_index_loading_call_carries_the_daemon_cache_flags` for the closure that now
fails on a new site the day it is added instead of the wave after. No running count is kept
here: this sentence has already carried two wrong mechanisms and one wrong number, each of
them falsified by a fix that landed somewhere else.

THE RULE BINDS THE WHOLE MODULE, INCLUDING THE FUNCTION PAST THE ANSWER. `quote`, `must_show`
and `confirm` all run before the operator has answered, and `open_run` runs only after — and
all four run nothing the repository supplied, measured by arming `core.fsmonitor` and the
repository's whole hooks directory and watching neither fire. `open_run` is the one that had to
be brought back inside the line rather than being there already: through `baseline.materialize`
it writes a private index copy and creates forge's own ref, which fired the user's
`post-index-change` and `reference-transaction` hooks until `gitcmd.NO_HOOKS` went on those
calls — the user's decision, argued at that constant. §5 step 1 was never the reason for it,
since that clause governs what happens BEFORE the answer. The scope is spelled function by
function rather than as "this module", because a function added later joins a module silently
and joins a named list only deliberately.
`test_the_detector_runs_nothing_the_repository_supplied`,
`test_reading_the_surface_at_this_gate_runs_nothing_the_repository_supplied` and
`test_nothing_in_this_module_runs_the_repositorys_own_program` pin the three halves against
controls showing the same recipe does run under `make`, and the same monitor and the same hooks
do run under an ordinary git.

A DETECTOR THAT MUST BE EXHAUSTIVE CANNOT BE, so the bound is declared instead of implied, and
every place the reader ran out is EMITTED rather than dropped. Findings carry one of three
prefixes, and §5.2's "detected and refused (or priced as its own explicit line)" is the reason
there are three rather than one:

  * `spends:`     a resolved recipe or argv runs a provider CLI, or executes a file naming a
                  council entry point without a hermetic flag. This is the refusal.
  * `remedy:`     a file the gate executes DOCUMENTS a `make <target>` whose own closure
                  spends. `make` does not follow it; an operator does, and only when the gate
                  fails. That is a price line, not a refusal — and it carries §5.2's steer to
                  `make verify` (receipts advisory), which is the half of that sentence an
                  operator can act on: the grep alone only names the chain.
  * `unresolved:` the reader could not follow the target: a recipe using a make function or
                  automatic variable it does not expand, a `cd` part-way through a shell line,
                  a script path the tree does not hold, an undefined target, a missing
                  Makefile, a documented remedy whose own closure could not be read. Fail
                  closed by SAYING SO — an unresolved target is a legitimate answer for a
                  human to price, and a silent one is a fail-open.

The three prefixes are EXPORTED (`SPENDS`, `REMEDY`, `UNRESOLVED`), because telling a refusal
from a price is the caller's whole reason for calling and a caller spelling `"spends:"` by hand
is one rename away from reading every refusal as a price.

`cd` inside a recipe is followed for the same reason `make -C` is: §5.1 names `cd frontend &&
npm ci` as the shape real monorepos have, and resolving the rest of the recipe against the
wrong directory finds nothing and says nothing. Only a LEADING `cd` is followed; one appearing
later in the same shell line truncates the read there and is reported, since everything after
it is relative to a directory this reader did not track.

WHAT IT CANNOT SEE, measured on this repository rather than imagined. `make precommit` and
`make verify` here come back with the same findings in the same classes — three `unresolved:`
and one `remedy:`, differing only in the trail that names which target was typed — because the
only difference between the two is `advisory=False` and `advisory=True` inside a `python3 -c`
one-liner: the first exits 1 and sends the operator to `make eval`, the second prints a warning
and exits 0. That is a difference in RUNTIME EXIT CODE, and no static read separates them. Both
get the `remedy:` line, and the operator reads which target they typed. Also unread: `include` directives,
`define`/`$(call …)` macro bodies, `$(shell …)` and the other make functions, a package.json
script chain, and anything a container image defines — `verify.gate_surface`'s docstring names
the same class of indirection as its own accepted gap. And a RECIPE is checked for a provider
CLI at program position only, so `timeout 600 claude -p …` written inside one is missed; the
loose whole-token rule is applied to the user's own argv, where the command is short and read
whole, and not to a recipe, because this repository's Makefile passes `--providers claude` as
an argument and a whole-line rule would refuse every flag shaped like it. The last gap is the
one accepted deliberately: a file handed to a RUNNER as data — `pytest -q tests/x.py` — really
is imported and run, and is not read here, because searching past the first operand refuses
`make council-test` on `tests/test_council_characterization.py`'s stubbed `run_council`, and
with it every `make verify`. See `test_a_file_handed_to_a_runner_as_data_is_not_read_as_the_program`.
"""
import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import baseline, bundle, gitcmd, journal, preflight, runstate, storage, verify


class GateError(RuntimeError):
    """What stops a run at this gate: an argument the gate will not answer for, an answer §5
    will not record, or the two conditions §2.3 and §5.2 refuse a run over.

    `Quote.lines` and `provider_invoking_verify`'s findings stay DESCRIPTIONS. They say what
    is true of a repository or a command; a raise says the run does not open. The overlap is
    deliberate and one-directional — `open_run` turns a `spends:` finding and a non-empty
    `preflight.refusals` into this, and nothing turns this back into a line."""


# The three classes a finding from `provider_invoking_verify` can carry, as the module
# docstring defines them. Public: `SPENDS` is the refusal and the other two are prices, and a
# caller cannot act on the difference without a name for it.
SPENDS = "spends:"
REMEDY = "remedy:"
UNRESOLVED = "unresolved:"

# §5 step 3: setup + verify run TWICE on the untouched baseline, so the second pass can show
# the zero tracked delta that turns §7.2's fixed-point assumption into evidence.
_CALIBRATION_PASSES = 2

# §5.2's stated upper bound, and the seat count it was stated for. Kept as a pair because the
# figure prices ONE FLEET and the run holds several at once: "three no-hardlink clones plus
# three dependency trees is plausibly 6-10 GB" is 10/3 GB per clone-and-its-dependencies, and
# that quotient is the only per-clone figure the spec gives.
_SPEC_PEAK_DISK_GB = 10.0
_SPEC_PEAK_DISK_SEATS = 3

# The sacrificial clone §5 step 3 calibrates in. Counted against the peak because §15 removes
# known-failed temporary clones only, and a calibration pass that succeeded is not one.
_CALIBRATION_CLONES = 1

# What the council's own CLI defaults to. Forge does not use it (§13 invokes review with
# --retries 0), so it is quoted as the alternative §5.2 asks to be shown, never summed in.
_COUNCIL_DEFAULT_ATTEMPTS = 3


@dataclass(frozen=True)
class Quote:
    """What §5 step 2 shows before anyone agrees to anything.

    `deep_review` is a STRING because it carries the operator's own words for §13.1's
    price. It used to be a string for a stronger reason — money is not a call count — but the
    council backend spends PROVIDER CALLS, so those calls are now counted in
    `provider_calls` like every other seat, and this line explains rather than replaces them.
    A price that lived only in prose while the scalar under-reported it is exactly the
    "scalar that reads low is not rescued by a line naming what it left out" defect below.

    `lines` is the quote a human reads; the scalars are the same quote for something that has
    to compare — so a scalar that reads low is not rescued by a line naming what it left out.
    Every line names the section it comes from, because an operator who thinks a number is
    wrong needs the derivation, not the total.

    `seats` and `attempts` are the two INPUTS kept on the record, and they are here rather
    than only in `quote`'s signature because everything downstream reads them from here:
    `confirm` records what this priced, and `open_run` writes what `confirm` recorded. A
    launcher taking them from its own argument instead would be free to build a fleet the
    quote never costed, and no test comparing two literals catches a caller that passes the
    wrong literal twice.
    """
    provider_calls: int
    deep_review: str
    setup_runs: int
    verify_runs: int
    peak_disk_gb: float
    lines: tuple[str, ...]
    seats: int
    attempts: int
    # §12.3's synthesis-fix cap, and the rounds it has to cover. They are HERE for
    # `seats`/`attempts`' reason one section on: everything downstream reads the run's shape
    # off the quote, so the number the operator was shown and the number the loop spends are
    # one number. `Manifest.attempts` cannot hold this — it is the BUILDER budget, and
    # `quote` prices post-review synthesis separately as `review_rounds + deep_fixes`, so
    # reusing it turns a 3-attempt run into a 9-attempt one and leaves `--collect` unable to
    # say which budget an `attempt` value was spending.
    review_rounds: int
    synthesis_fix_cap: int
    # WHAT THE TOTAL IS MADE OF, so a reader — human or test — can ask which STAGE a call was
    # priced for rather than re-deriving the arithmetic. A test that recomputes the formula
    # agrees with a wrong formula, which is how calls for a review nothing can convene
    # survived a suite that checked the total against the prose.
    terms: dict
    # HOW MANY BUILDERS RUN AT ONCE. It is priced rather than merely configured: the
    # wall-clock line is `ceil(seats/concurrency) x attempts x window`, so this number is one
    # the operator agrees to at §5 step 2 like every other on this record.
    #
    # DEFAULTED TO 1, WHICH IS SERIAL AND IS WHAT EVERY RUN DID BEFORE IT EXISTED. The default
    # is not a shrug: §19's window is a TIMEOUT rather than a budget, and builders competing
    # for one machine's CPU and disk can push a seat that would have finished into its cap —
    # spending it and returning no candidate. Opting in is the operator accepting that trade;
    # defaulting to it would be this engine taking it on their behalf.
    #
    # LAST, AND THE DEFAULT IS WHY — `ArtifactSet.verify_overlap` is placed the same way, so
    # every field above keeps its position for a caller that constructs one positionally.
    concurrency: int = 1
    # `ceil(seats/concurrency)`, PUBLISHED RATHER THAN LEFT TO BE RE-DERIVED — a test that
    # recomputes the formula agrees with a wrong formula, which is how nine calls for a review
    # nothing could convene survived a suite that checked the total against the prose.
    #
    # NOT IN `terms`, AND THE DISTINCTION IS LOAD-BEARING: `terms` is what the PROVIDER-CALL
    # total is made of and its values must sum to `provider_calls`. A wave is a unit of wall
    # clock, not a call, and putting it there made the sum disagree with the number the
    # operator is shown — caught by the packaging test that adds them up.
    waves: int = 1

    def __post_init__(self) -> None:
        """A quote is what an operator AGREES TO, so it may not carry a nonsense number.

        REPRODUCED: `Quote(provider_calls=-5, seats=-1, …)` was accepted whole, and every
        field downstream reads the run's shape off this record — `confirm` cross-checks it,
        `open_run` writes it into the manifest, §4's disk rejection compares against
        `peak_disk_gb`. A negative call count shown at §5 step 2 is a price nobody could
        agree to, and this is the one dataclass in the module with nothing standing behind
        its constructor.

        `quote` VALIDATES ITS INPUTS ALREADY, through `_confirmed_count`, so nothing this
        module builds can fail here. What this closes is the OTHER door: §12.4's consumer
        and every test builds a `Quote` directly, exactly as `Report` and `Confirmation` are
        built directly — and both of those validate for the same reason.
        """
        if not isinstance(self.concurrency, int) or isinstance(self.concurrency, bool) \
                or self.concurrency < 1:
            # FLOOR OF ONE, not zero, and it is the one count on this record that cannot be
            # 0: a review budget of 0 is "convene none", a legible answer, while a fleet
            # width of 0 is a fleet that never runs — priced as though it would.
            raise GateError(
                f"a quote's concurrency is how many builders run at once, at least 1, "
                f"not {self.concurrency!r}")
        for name in ("provider_calls", "setup_runs", "verify_runs", "seats", "attempts",
                     "review_rounds", "synthesis_fix_cap"):
            v = getattr(self, name)
            # `bool` is an `int` subclass and is what a boolean-ish config parse leaves
            # behind — the same value `_confirmed_count` refuses one function down.
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise GateError(
                    f"a quote's {name} is a count an operator can agree to, not {v!r}")
        # AFTER THE LOOP, BECAUSE IT COMPARES TWO FIELDS. A cross-field test run before both
        # sides are known to be counts raises `TypeError` on `seats="3"` — an error class no
        # caller of this module catches — instead of the `GateError` this gate exists to give.
        # Measured: the suite's own non-count fixtures crashed here rather than being refused.
        if self.concurrency > self.seats:
            # NOT MERELY WASTEFUL: `ceil(seats/concurrency)` saturates at 1, so every width
            # above `seats` quotes the SAME ceiling while suggesting the operator bought
            # something more — a quote reading cleaner than its evidence.
            raise GateError(
                f"a quote's concurrency is {self.concurrency} for {self.seats} seat(s); "
                "above the seat count it buys nothing and prices as though it did")
        if not isinstance(self.peak_disk_gb, (int, float)) or \
                isinstance(self.peak_disk_gb, bool) or self.peak_disk_gb < 0:
            raise GateError(
                f"a quote's peak_disk_gb is what §4 refuses a run against, not "
                f"{self.peak_disk_gb!r}")
        if not isinstance(self.deep_review, str) or not self.deep_review.strip():
            raise GateError(
                "a quote's deep_review line is §13.1's price in the operator's own words, "
                f"not {self.deep_review!r}")
        if not isinstance(self.terms, dict):
            raise GateError(f"a quote's terms are a mapping of stage to count, not "
                            f"{type(self.terms).__name__}")


def _confirmed_count(name, value, source, *, floor=1) -> int:
    """`runstate`'s own predicate for a run-shape count, raised in this module's vocabulary.

    CALLED, NEVER RE-SPELLED, and the failure that argues for it was measured at this gate:
    `quote` and `confirm` both tested `< 1` and nothing else, so `seats=True` — an `int`
    subclass, and what a boolean-ish config parse leaves behind — priced a run, opened a run
    directory, wrote `events.jsonl`, authored B1 and created a ref in the USER's repository,
    and only then met `runstate`'s own floor inside `write_manifest`. The gate's contract is
    that a run which will not happen leaves nothing behind; two spellings of one predicate is
    what made it leave three things, and a third spelling here would only move the seam.

    Re-raised as a `GateError` on `_confirmed_steps`' precedent: this module's documented
    refusal is a GateError, and a caller at §5 step 2 has no other name for this.
    """
    try:
        return runstate.count(name, value, source, floor=floor)
    except runstate.ManifestError as e:
        raise GateError(str(e)) from e


def quote(report, *, seats=3, attempts=3, review_rounds=2, deep_review=True,
          concurrency=1,
          seat_timeout_sec) -> Quote:
    """Price the worst case of a run over `report`'s repository.

    A `preflight.Report` is required rather than a path: the quote reads the repository's
    SHAPE — that a static look happened, and what it already refuses — not its contents. A
    caller who has not run preflight has not reached §5 step 2 yet.

    `seat_timeout_sec` IS REQUIRED AND HAS NO DEFAULT. It is §19's window, and §19 exists
    because a second timeout mechanism silently degraded a three-seat panel to two. A default
    here would be this function inventing the number the bound below is computed from, which
    is the same defect one layer out.

    `deep_review` defaults True because §13.1 is titled "default on"; `--skip-deep-review`
    sets it False. MEASURED by diffing the two quotes field by field, it moves five numeric
    fields — provider_calls, setup_runs, verify_runs, peak_disk_gb and synthesis_fix_cap —
    because the council fan-out is `seats` provider calls on top of the fix it triggers, plus
    that fix's fresh verifier setup+verify and the clone that verify runs in.
    """
    if not isinstance(report, preflight.Report):
        raise GateError(f"a preflight.Report is required, not {type(report).__name__}; "
                        "the quote is shown at §5 step 2, after the static look")
    # A run with no seat is not a run and an attempt budget below one is not a retry policy;
    # zero review rounds IS a run, which is the only reason the third floor differs. All three
    # go through the predicate `runstate` owns rather than a comparison spelled here — a
    # magnitude test alone let `seats=True` through to `write_manifest` and answered
    # `seats="3"` with a bare `TypeError`, where every other refusal on this path is a
    # `GateError`.
    seats = _confirmed_count("seats", seats, "§5.2's quote")
    attempts = _confirmed_count("attempts", attempts, "§5.2's quote")
    review_rounds = _confirmed_count("review_rounds", review_rounds, "§5.2's quote", floor=0)
    if not isinstance(seat_timeout_sec, int) or isinstance(seat_timeout_sec, bool) \
            or seat_timeout_sec < 1:
        raise GateError(
            f"seat_timeout_sec={seat_timeout_sec!r} is not a window this run can be bounded "
            "by. §19 forbids a second timeout mechanism, so this does not pick one — resolve "
            "it from `council.engine.MODE_TIMEOUT['forge']` and pass it.")

    builders = seats * attempts
    synthesis = 1
    review = review_rounds * seats
    deep_fixes = 1 if deep_review else 0
    review_fixes = review_rounds + deep_fixes
    # THE FAN-OUT ITSELF IS NOW A COST. The cloud review was billed in credits and spent no
    # provider call, so it was priced only in prose; a local panel is `seats` calls and
    # under-reporting them would put the headline number below what the run actually spends.
    deep_calls = seats if deep_review else 0
    calls = builders + synthesis + review + review_fixes + deep_calls

    # One candidate per seat, plus synthesis's own; §6 gives each a fresh verifier clone, and
    # §13/§13.1 add one more verification after each post-review fix.
    verifier_runs = seats + 1 + review_fixes
    setup_runs = _CALIBRATION_PASSES + builders + verifier_runs
    verify_runs = _CALIBRATION_PASSES + verifier_runs
    # §13's review rounds each take their own clone at their own checkpoint — the containment
    # `review.clone_review_tree` exists for, taken through `fleet.clone_seat` with `--no-local
    # --no-hardlinks` like every other. It is one per ROUND and not one per reviewer: the panel
    # reads one tree together, which is what makes the round's bracket a single measurement.
    clones = _CALIBRATION_CLONES + builders + verifier_runs + review_rounds
    peak_disk_gb = round(clones * _SPEC_PEAK_DISK_GB / _SPEC_PEAK_DISK_SEATS, 1)

    concurrency = _confirmed_count("concurrency", concurrency, "§5 step 2")
    if concurrency > seats:
        raise GateError(
            f"concurrency={concurrency} exceeds seats={seats}; above the seat count it buys "
            "nothing and would price as though it did")
    # THE CEILING, AND WHY IT IS A PRODUCT OF THREE THINGS RATHER THAN TWO. `runner.run` drives
    # the fleet `concurrency` seats at a time; each seat then takes its OWN attempts one after
    # another, and every attempt gets the whole §19 window. So the longest the builders can take
    # is `ceil(seats/concurrency) waves x attempts x window`, reached only if every attempt in
    # the slowest wave runs to its cap.
    #
    # AT concurrency=1 THIS IS THE OLD FORMULA EXACTLY — `seats x attempts x window` — which is
    # the check that it generalises rather than replaces: the serial run's quote does not move.
    waves = -(-seats // concurrency)          # ceil, without importing math for one call
    builder_hours = waves * attempts * seat_timeout_sec / 3600.0

    deep_line = (f"one llm-council fan-out over the fused diff = {seats} provider call(s), "
                 "counted in the total above (§13.1, default on); --skip-deep-review opts "
                 "out. Its fix is one of the post-review synthesis invocations, also above"
                 if deep_review else
                 "not run — --skip-deep-review (§13.1 is default on, so this is the "
                 "opted-out quote); no fan-out, and no post-review fix for it above")

    blocked = preflight.refusals(report)
    lines = []
    if seats < 2:
        # SAID AT THE GATE, BEFORE THE SPEND. `seats` has a floor of 1 and the skill's own
        # description promises "all three agentic CLIs" — so a one-seat run is permitted and
        # is not the thing the description sells. There is nothing to FUSE with one candidate:
        # §16's deliverable is "a new answer assembled from the best of all three", and a
        # fleet of one can only produce that one seat's work.
        #
        # A LINE, NOT A REFUSAL. A single seat is a legitimate cheap run — a smoke of the
        # machinery, or a deliberate one-provider build — and refusing it would remove a mode
        # the flag exists to offer. What must not happen is an operator paying for it while
        # believing they bought a fusion.
        lines.append(
            f"seats: {seats}, so this run cannot produce a FUSION. §16's deliverable is an "
            "answer assembled from several candidates and there will be one; `--collect` "
            "will describe that seat's own work. Use --seats 3 for what this skill is for.")
    if blocked:
        lines.append(f"refused first: preflight names {len(blocked)} thing(s) that stop this "
                     "run, so nothing below is spent until they are cleared (§5 step 1's "
                     "static look is what found them)")
    lines += [
        f"provider calls: {calls} worst case = builders {seats}x{attempts}={builders} "
        f"(§8.1 gives a retry a fresh clone) + synthesis 1 + review {review_rounds} round(s) "
        f"x {seats} reviewers = {review} + post-review synthesis {review_fixes}",
        f"  of which §5.2 states the first three as {builders} + {synthesis} + {review} = "
        f"{builders + synthesis + review}; the post-review synthesis invocations are the ones "
        "the same paragraph requires the quote to include (§13's round-1 fix, a blocker fixed "
        f"after round 2, and §13.1's deep-review fix = {review_fixes})",
        f"  review retries: §13 has forge invoke the council with --retries 0, so each reviewer "
        f"is asked once per round; §5.2 asks for whichever is wired. At the council CLI's own "
        f"--retries 2 the review term alone would be "
        f"{review_rounds * seats * _COUNCIL_DEFAULT_ATTEMPTS}",
        f"deep review: {deep_line}",
        f"setup runs: {setup_runs} = calibration {_CALIBRATION_PASSES} (§5 step 3 runs "
        f"setup+verify twice on the untouched baseline) + one per builder clone {builders} + "
        f"one per verifier clone {verifier_runs} (§6: a fresh verifier per candidate, plus one "
        "after each post-review fix). §6 calls that per-candidate setup essential, not "
        "optional hardening",
        f"verify runs: {verify_runs} = calibration {_CALIBRATION_PASSES} + verifier "
        f"{verifier_runs}. A builder's own clone is not verified — §6 puts every verification "
        "in a clone the builder never had access to",
        f"peak disk: ~{peak_disk_gb} GB under XDG_STATE_HOME = {clones} clones x "
        f"{_SPEC_PEAK_DISK_GB}/{_SPEC_PEAK_DISK_SEATS} GB, which is §5.2's upper bound for "
        f"'three no-hardlink clones plus three dependency trees' divided by the fleet it "
        f"prices. The {clones} are calibration {_CALIBRATION_CLONES} (§5 step 3) + builders "
        f"{builders} (§8.1 preserves a failed attempt's clone rather than resetting in place) "
        f"+ verifiers {verifier_runs} (§6, fresh per candidate) + review {review_rounds} "
        f"(§13, one per round: reviewers get their own clone at the round's checkpoint rather "
        f"than the synthesis worktree, which shares the user's git directory). §8.1's "
        f"\"preserved as partial "
        "input\" is the SPECIFIC rule and wins over §15's general auto-removal of known-failed "
        "temporary clones, which would otherwise reclaim the retries; nothing reclaims the rest "
        "until `--gc`. Not counted: the synthesis worktree, which §4 keeps a worktree so it "
        "shares the parent's objects",
        f"wall clock: builders alone have an UPPER BOUND of {builder_hours:.1f} h = "
        f"{waves} wave(s) of {concurrency} seat(s) x {attempts} attempt(s) x "
        f"{seat_timeout_sec}s, because each attempt gets the whole §19 window."
        + ("" if concurrency == 1 else
           f" YOU HAVE ASKED FOR {concurrency} BUILDERS AT ONCE, and the window is a TIMEOUT "
           "rather than a budget: builders sharing this machine's CPU, disk and network can "
           "push a seat that would have finished serially past its cap, which spends the seat "
           "and returns no candidate. The ceiling below is lower; the risk of a timed-out seat "
           "is higher, and that trade is the one you are agreeing to here.")
        + " It is a bound and not an "
        "estimate: a run whose seats all finish early takes a fraction of it. NOT INCLUDED — "
        f"the {setup_runs} setup runs, the {verify_runs} verify runs, §13's review rounds, "
        "§13.1's deep review, and every clone. Those are provider and shell time this engine "
        "cannot bound statically, so the real ceiling is above this number rather than near it",
        "provider cost: quoted as a call count, and every call is counted — §13.1's deep "
        "review is a local council fan-out inside the total above, not a separate currency. "
        "Shell-command time is the wall-clock line above",
        "verify command: run `provider_invoking_verify` on it once it is named — §5.2 refuses "
        "one that reaches a provider CLI, or prices it as its own explicit line, and steers "
        "the operator to `make verify` (receipts advisory) rather than the gate whose "
        "documented remedy is what spends",
    ]
    return Quote(provider_calls=calls, deep_review=deep_line, setup_runs=setup_runs,
                 verify_runs=verify_runs, peak_disk_gb=peak_disk_gb, lines=tuple(lines),
                 seats=seats, attempts=attempts, concurrency=concurrency,
                 review_rounds=review_rounds, synthesis_fix_cap=review_fixes,
                 waves=waves,
                 # THE BREAKDOWN MUST ADD UP TO THE TOTAL IT EXPLAINS. The deep review used
                 # to spend no provider call, so it was legitimately absent here; with the
                 # council backend it is `seats` calls, and leaving it out left an operator
                 # reading terms that summed three short of the headline number.
                 terms={"builders": builders, "synthesis": synthesis,
                        "review": review, "review_fixes": review_fixes,
                        "deep_review": deep_calls})


# ---------------------------------------------------------------------------------------
# §5.2's detector.

_PROVIDER_CLIS = frozenset({"claude", "codex", "agy"})

# Matched against a PROGRAM position only — start of a recipe, or just after a shell separator
# or a command substitution. `--providers claude` is an argument to something else and must not
# read as an invocation, and it occurs in this repository's own Makefile.
_PROVIDER_CMD = re.compile(r"(?:^|[;&|(`]|\$\()\s*[@+-]?\s*(?:\S*/)?(claude|codex|agy)\b")

_MAKE = frozenset({"make", "gmake"})

# A program that takes a repo file as its operand, so the operand is what actually runs. `uvx`
# and `uv` are here because this repository's own gate reaches pytest through them.
_INTERPRETERS = frozenset({"python", "python2", "python3", "bash", "sh", "zsh", "dash",
                           "node", "ruby", "perl", "uv", "uvx", "env"})
_PYTHON_MINOR = re.compile(r"^python3\.\d+$")

# Literal substrings, never regexes, and the difference is measured rather than stylistic:
# `grep -rl "council.engine"` selects this repository's `scripts/lib/checks.py`, because the
# dot matches the slash in the PATH `council/engine.py` that checks.py carries in a closure
# list while spending nothing — and checks.py is executed by `make verify`. `grep -rlF` on the
# same string does not. Across the tree the two literals select `scripts/eval_harness.py`,
# `scripts/eval_trigger.py`, `shared/lib/council/engine.py` and llm-council's `fanout.py`
# facade, plus council test files and plan documents, which are only opened when a recipe
# executes them.
_SPEND_SYMBOLS = ("run_council", "council.engine")

# A flag whose presence the invoker is declaring hermetic. This repository's `make verify`
# reaches `scripts/eval_harness.py --self-test`, which is the same file `make eval SKILL=…`
# reaches to spend ~24 provider calls; without this the steer §5.2 makes would refuse its own
# recommendation. The bound: a program taking one of these AND still calling out is misread.
_HERMETIC_FLAGS = frozenset({"--self-test", "--check", "--status", "--dry-run", "--seed-receipt",
                             "--help", "-h", "--version"})

# What the reader is willing to call a script the tree should hold. A path-shaped token with
# one of these suffixes that does not exist is `unresolved:` rather than ignored.
_SCRIPT_SUFFIXES = frozenset({"py", "sh", "bash", "mk", "js", "ts", "mjs", "rb", "pl", "bats"})

_TOKENS = re.compile(r"[A-Za-z0-9_./+-]+")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VARREF = re.compile(r"\$[({]([A-Za-z_.][A-Za-z0-9_.]*)[)}]")
_UNEXPANDED = re.compile(r"\$[({][^)}]{0,60}")
# Every OTHER `$` make can still be holding. `$$` is folded out before this runs, so what is
# left is an automatic variable (`$<`, `$@`, `$^`, `$*`, `$?`, `$+`, `$|`, `$%`) or a
# one-letter name — none of which reach `_TOKENS`, so without this the token does not merely
# fail to resolve, it disappears, and `@python3 $<` reads as a recipe running nothing. The
# rule is "any `$` this reader did not resolve" rather than the list somebody remembered,
# because a list is how `$(UNDEFINED)` came to fail closed while `$<` failed open.
_UNEXPANDED_SHORT = re.compile(r"\$.?")
_CALL_MACRO = re.compile(r"\$[({]call\s+([A-Za-z_][A-Za-z0-9_]*)")
# A remedy in prose or in an error message: "run `make eval SKILL=<skill>`".
_MAKE_REMEDY = re.compile(r"\bmake\s+([A-Za-z][A-Za-z0-9_.-]*)")

# A recipe's own working directory, which §5.1 names as the shape real monorepos have
# (`cd frontend && npm ci`). `_CD_LEAD` consumes a leading one; `_CD_ANY` finds one at any
# later program position, where following it would need the shell this module refuses to run.
_CD_LEAD = re.compile(r"\s*cd\s+([^\s;&|()`]+)\s*(?:&&|;|\|\|)?\s*")
# `\s` alone missed a BARE `cd` — no argument, which moves to $HOME — because nothing
# follows it but a separator. Contrived inside a Makefile, and the one hole left in this
# family once the rest is closed, so it is matched rather than left to a later reader.
_CD_ANY = re.compile(r"(?:^|[;&|(`])\s*cd(?:\s|;|$|&|\|)")

_ASSIGN = re.compile(r"^([A-Za-z_.][A-Za-z0-9_.]*)\s*(::=|:=|\?=|\+=|=)\s*(.*)$")
_RULE = re.compile(r"^([^\t#][^:=]*?)\s*::?\s*(.*)$")
_MAKEFILE_NAMES = ("GNUmakefile", "makefile", "Makefile")
_DOLLAR = "\x00"                 # make's `$$` — a literal `$` for the shell, not a reference
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_DEPTH = 12


def _basename(token: str) -> str:
    return PurePosixPath(token).name


def _logical_lines(text: str):
    """(is_recipe, joined) per LOGICAL make line — backslash continuations folded.

    Folding matters in both directions here: this repository's `FORGE_TESTS :=` spans four
    physical lines, and its `bats-test` recipe is one continued line eighteen deep.
    """
    out, buf, recipe = [], None, False
    for raw in text.splitlines():
        if buf is None:
            recipe, buf = raw.startswith("\t"), raw
        else:
            buf += " " + raw.strip()
        if buf.endswith("\\"):
            buf = buf[:-1]
            continue
        out.append((recipe, buf))
        buf = None
    if buf is not None:
        out.append((recipe, buf))
    return out


class _Makefile:
    """The subset of GNU make this reader claims: simple variables, rules, recipes.

    `define` bodies are recorded by NAME and skipped: expanding one means running the shell
    grammar inside it, and a `$(call …)` left unexpanded is reported rather than guessed at.

    `cwd` is the repo-relative directory MAKE WAS RUNNING IN when this file was read, which is
    not the directory the file sits in: `-C sub` moves both, `-f sub/build.mk` moves only the
    latter. Every path in a recipe resolves against the former, so it travels with the parsed
    makefile rather than being re-derived by each caller — the re-derivation is what
    `_scan_remedies` got wrong, resolving a `-C sub` remedy chain against the repository root
    and reporting the clean `()` that a chain which spends must never produce.
    """

    def __init__(self, path: Path, cwd: str, text: str):
        self.path = path
        self.cwd = cwd
        self.vars: dict[str, str] = {}
        self.recipes: dict[str, list[str]] = {}
        self.prereqs: dict[str, list[str]] = {}
        self.defines: set[str] = set()
        self.includes = False
        self.default_goal = ""
        self.first_target = ""
        current: list[str] = []
        in_define = False
        for is_recipe, line in _logical_lines(text):
            if is_recipe:
                if not in_define:
                    for t in current:
                        self.recipes.setdefault(t, []).append(line.lstrip("\t"))
                continue
            stripped = line.strip()
            if in_define:
                in_define = not stripped.startswith("endef")
                continue
            if stripped.startswith("define "):
                in_define = True
                self.defines.add(stripped.split()[1])
                continue
            if not stripped or stripped.startswith("#"):
                continue
            stripped = stripped.split("#", 1)[0].strip()
            if not stripped:
                continue
            if stripped.startswith(("include ", "-include ", "sinclude ")):
                self.includes = True
                current = []
                continue
            m = _ASSIGN.match(stripped)
            if m:
                name, op, value = m.groups()
                if op == "+=" and name in self.vars:
                    self.vars[name] = f"{self.vars[name]} {value}"
                elif op != "?=" or name not in self.vars:
                    self.vars[name] = value
                if name == ".DEFAULT_GOAL":
                    self.default_goal = value.strip()
                current = []
                continue
            m = _RULE.match(stripped)
            if m:
                current = self.expand(m.group(1))[0].split()
                for t in current:
                    self.prereqs.setdefault(t, []).extend(self.expand(m.group(2))[0].split())
                    if not self.first_target and not t.startswith("."):
                        self.first_target = t
                continue
            current = []

    def expand(self, text: str) -> tuple[str, str]:
        """(expanded, unresolved-reference) — the second is "" when everything resolved.

        `$$` is folded out first because it is make's escape for a literal `$`: without that,
        a recipe's own `$$(bash …)` reads as an unexpanded make reference and every shell
        command substitution in the tree reports as unresolved.
        """
        text = text.replace("$$", _DOLLAR)
        for _ in range(8):
            new = _VARREF.sub(lambda m: self.vars.get(m.group(1), m.group(0)), text)
            if new == text:
                break
            text = new
        macro = _CALL_MACRO.search(text)
        if macro:
            left = (f"the make macro `{macro.group(1)}`" if macro.group(1) in self.defines
                    else f"`$(call {macro.group(1)} …)`")
        elif (m := _UNEXPANDED.search(text)):
            left = f"`{m.group(0)}…`"           # cut at the closing paren this did not reach
        elif (m := _UNEXPANDED_SHORT.search(text)):
            left = f"`{m.group(0)}`"
        else:
            left = ""
        return text.replace(_DOLLAR, "$"), left

    @property
    def goal(self) -> str:
        return self.default_goal or self.first_target


class _Resolver:
    """One static walk of what a verify command would run. Holds no state between calls."""

    def __init__(self, root: Path, *, follow_remedies=True):
        self.root = root
        self.follow_remedies = follow_remedies
        self.findings: list[str] = []
        self.makefiles: dict[str, _Makefile | None] = {}
        self.seen_targets: set[tuple[str, str]] = set()
        # rel -> the text read from it, so a file reached twice is opened and reported once
        # while a SECOND reach that arrived through a makefile can still be asked the question
        # the first one had no makefile to answer — see `_scan_file`.
        self.seen_files: dict[str, str] = {}
        self.seen_remedies: set[tuple[str, str, str]] = set()

    # -- reporting ----------------------------------------------------------------------
    def _add(self, kind: str, trail, message: str) -> None:
        where = " -> ".join(trail)
        self.findings.append(f"{kind} {where}: {message}")

    def _of(self, findings, kind: str) -> list:
        return [f for f in findings if f.startswith(kind)]

    # -- paths --------------------------------------------------------------------------
    def _contained(self, rel: str) -> str | None:
        """The repo-relative name, or None when the token leaves the tree.

        `bundle._assert_contained` is IMPORTED rather than restated, for the reason
        `preflight` gives for importing it: this package already spells the lexical rule in
        four places and a fifth spelling is the drift the reuse exists to avoid.
        """
        rel = rel.strip().strip("'\"")
        if not rel or rel in (".", "./"):
            return ""
        try:
            bundle._assert_contained(rel, "verify command")
        except bundle.BundleError:
            return None
        return rel.rstrip("/")

    def _join(self, base: str, rel: str) -> str | None:
        joined = f"{base}/{rel}" if base and not rel.startswith("/") else rel
        return self._contained(joined)

    def _repo_file(self, cwd: str, token: str) -> str | None:
        rel = self._join(cwd, token)
        if rel is None or not rel:
            return None
        p = self.root / rel
        return rel if p.is_file() else None

    def _exists(self, cwd: str, name: str) -> bool:
        rel = self._join(cwd, name)
        return rel is not None and bool(rel) and (self.root / rel).exists()

    def _repo_dir(self, cwd: str, token: str) -> str | None:
        rel = self._join(cwd, token)
        if rel is None or not rel:
            return None
        return rel if (self.root / rel).is_dir() else None

    # -- entry --------------------------------------------------------------------------
    def scan_step(self, index: int, step) -> None:
        argv = tuple(getattr(step, "argv", ()) or ())
        trail = [f"verify step {index + 1} `{' '.join(argv)}`"]
        if not argv:
            return
        cwd = self._contained(getattr(step, "cwd", "") or "")
        if cwd is None:
            self._add(UNRESOLVED, trail, "its cwd leaves the repository, so nothing under "
                                         "it was read")
            return
        prog = _basename(argv[0])
        if prog in _PROVIDER_CLIS:
            self._add(SPENDS, trail, f"the gate's own program is the provider CLI {prog!r}")
            return
        if prog in _MAKE:
            self._scan_make(trail, cwd, list(argv[1:]))
            return
        self._scan_tokens(trail, cwd, list(argv), None)

    # -- make ---------------------------------------------------------------------------
    def _makefile(self, trail, cwd: str, named: str | None) -> _Makefile | None:
        key = f"{cwd}\0{named or ''}"
        if key in self.makefiles:
            return self.makefiles[key]
        names = (named,) if named else _MAKEFILE_NAMES
        mk = None
        for name in names:
            rel = self._join(cwd, name)
            if rel is None:
                continue
            p = self.root / rel
            if p.is_file():
                try:
                    mk = _Makefile(p, cwd, p.read_text(encoding="utf-8", errors="replace"))
                except OSError as e:
                    self._add(UNRESOLVED, trail, f"{rel} could not be read: {e}")
                break
        if mk is None:
            self._add(UNRESOLVED, trail,
                      f"no makefile at {cwd or '.'}/{named or 'Makefile'} — what `make` would "
                      "run here could not be read")
        elif mk.includes:
            self._add(UNRESOLVED, trail, f"{mk.path.name} uses `include`, and this reader "
                                         "does not follow it")
        self.makefiles[key] = mk
        return mk

    def _scan_make(self, trail, cwd: str, args: list) -> None:
        named, targets, i = None, [], 0
        while i < len(args):
            a = args[i]
            # EVERY SPELLING make ACCEPTS, not just the separated one. `--directory=sub` and
            # the attached `-Csub` both fell through to the catch-all below and were IGNORED,
            # so the scan stayed in the wrong directory — reproduced: `make --directory=sub go`
            # over a `sub/Makefile` whose recipe runs `claude` reported `unresolved` where the
            # truth is `spends`. An operator shown "could not resolve" may accept the gap; one
            # shown "this spends a provider call" refuses. Wrong is worse than unknown here.
            moved_to = None
            if a in ("-C", "--directory") and i + 1 < len(args):
                i += 1
                moved_to = args[i]
            elif a.startswith("--directory="):
                moved_to = a.split("=", 1)[1]
            elif a.startswith("-C") and len(a) > 2:
                moved_to = a[2:]
            if moved_to is not None:
                moved = self._join(cwd, moved_to)
                if moved is None:
                    self._add(UNRESOLVED, trail, f"`-C {moved_to}` leaves the repository")
                    return
                cwd = moved
                i += 1
                continue
            if a in ("-f", "--file", "--makefile") and i + 1 < len(args):
                i += 1
                named = args[i]
            elif a.startswith(("--file=", "--makefile=")):
                named = a.split("=", 1)[1]
            elif a.startswith("-f") and len(a) > 2:
                named = a[2:]
            elif a.startswith("-") or "=" in a:
                pass
            else:
                targets.append(a)
            i += 1
        mk = self._makefile(trail, cwd, named)
        if mk is None:
            return
        for target in targets or [mk.goal]:
            self._scan_target(trail, cwd, mk, target)

    def _scan_target(self, trail, cwd: str, mk: _Makefile, target: str) -> None:
        if not target:
            self._add(UNRESOLVED, trail, "`make` was given no target and the makefile "
                                         "declares no default goal")
            return
        # KEYED BY `(cwd, target)` AND NOT BY THE MAKEFILE, WHICH WAS TRIED AND REVERTED.
        # Adding `mk.path` looks right — `make -f a.mk build` and `make -f b.mk build` in one
        # directory are different targets — but it also stops the memo suppressing the
        # SELF-REFERENCE this walk depends on: a target whose recipe documents its own
        # invocation (`scripts/lib/checks.py` naming `make verify` while being reached by it,
        # a real shape on this repository) re-entered and reported itself as its own remedy.
        # `test_a_target_that_documents_itself_is_not_reported_as_its_own_remedy` measured it.
        # The `-f` case is real but rarer than the self-reference, and buying it with a
        # regression in the common path is the wrong trade.
        key = (cwd, target)
        if key in self.seen_targets or len(trail) > _MAX_DEPTH:
            return
        self.seen_targets.add(key)
        trail = [*trail, f"target `{target}`"]
        if target not in mk.recipes and target not in mk.prereqs:
            if not self._exists(cwd, target):
                self._add(UNRESOLVED, trail, f"{mk.path.name} defines no such target and the "
                                             "tree holds no such file")
            return
        for prereq in mk.prereqs.get(target, []):
            if prereq in mk.recipes or prereq in mk.prereqs:
                self._scan_target(trail, cwd, mk, prereq)
            elif not self._exists(cwd, prereq):
                self._add(UNRESOLVED, trail, f"prerequisite `{prereq}` is neither a target "
                                             "nor a file in this tree")
        for line in mk.recipes.get(target, []):
            self._scan_recipe(trail, cwd, mk, line)

    def _scan_recipe(self, trail, cwd: str, mk: _Makefile, line: str) -> None:
        # `@`, `-` and `+` at the head of a recipe are make's own modifiers, not part of the
        # program. Left on, `@python3` is a program name nothing recognises and every silenced
        # recipe in the tree reads as running something unknown.
        expanded, left = mk.expand(line.lstrip("\t @-+"))
        if left:
            self._add(UNRESOLVED, trail, f"a recipe uses {left}, which this reader does not "
                                         "expand, so what it runs is unknown")
        # The whole line, before any `cd` is consumed: `cd tools && claude -p …` still runs a
        # provider, and `_PROVIDER_CMD` reads the `&&` as the program position it is.
        found = _PROVIDER_CMD.search(expanded)
        if found:
            self._add(SPENDS, trail,
                      f"a recipe runs the provider CLI {found.group(1)!r}")
        moved = self._chdir(trail, cwd, expanded)
        if moved is None:
            return
        cwd, body = moved
        self._scan_tokens(trail, cwd, _TOKENS.findall(body), mk, raw=body)

    def _chdir(self, trail, cwd: str, expanded: str):
        """`(cwd, body)` after following the recipe's leading `cd`, or None when it cannot be.

        Returning the truncated body rather than refusing the whole line keeps what came
        BEFORE a mid-line `cd` resolved — the part that really does run in `cwd`.
        """
        for _ in range(_MAX_DEPTH):
            m = _CD_LEAD.match(expanded)
            if not m:
                break
            moved = self._repo_dir(cwd, m.group(1))
            if moved is None:
                self._add(UNRESOLVED, trail, f"a recipe runs `cd {m.group(1)}`, which is not a "
                                             "directory in this tree, so what it runs there "
                                             "could not be read")
                return None
            cwd, expanded = moved, expanded[m.end():]
        later = _CD_ANY.search(expanded)
        if later:
            self._add(UNRESOLVED, trail, "a recipe changes directory part-way through a shell "
                                         "line; this reader follows only a leading `cd`, so "
                                         "everything after it went unread")
            expanded = expanded[:later.start()]
        return cwd, expanded

    # -- commands -----------------------------------------------------------------------
    def _operand(self, cwd: str, rest: list) -> tuple[str | None, list]:
        """The repo file an interpreter would actually run, and the arguments after it.

        Stops at the first non-flag operand rather than searching on: `uvx --with pytest pytest
        -q tests/a.py` runs `pytest`, and continuing past it would report the test files it was
        handed as data as though the recipe executed them.
        """
        for i, token in enumerate(rest):
            if token.startswith("-") or "=" in token.split("/")[0]:
                continue
            return self._repo_file(cwd, token), list(rest[i + 1:])
        return None, []

    def _inline_modules(self, cwd: str, tokens: list) -> list:
        """Repo modules a `python -c` one-liner imports off a path it inserts itself.

        The shape is this repository's own gate: `sys.path.insert(0,'scripts/lib'); import
        checks`. Nothing shorter reaches `scripts/lib/checks.py`, which is where §5.2's whole
        chain on this repository starts, and nothing here parses Python — a directory token and
        an identifier token from the SAME command, joined and tested for existence.
        """
        dirs = [t for t in tokens[:80]
                if (self._join(cwd, t) or None) and (self.root / self._join(cwd, t)).is_dir()]
        idents = [t for t in tokens[:80] if _IDENT.match(t)]
        out = []
        for d in dirs[:8]:
            for name in idents[:40]:
                rel = self._repo_file(cwd, f"{d}/{name}.py")
                if rel:
                    out.append(rel)
        return out

    def _scan_tokens(self, trail, cwd: str, tokens: list, mk, raw: str = "") -> None:
        if not tokens:
            return
        direct = self._repo_file(cwd, tokens[0])
        if direct:
            self._scan_file(trail, direct, tokens[1:], mk)
        for i, token in enumerate(tokens):
            base = _basename(token)
            rest = list(tokens[i + 1:])
            if base in _MAKE:
                self._scan_make(trail, cwd, rest)
                continue
            if base in _PROVIDER_CLIS and not raw:
                self._add(SPENDS, trail, f"the argv names the provider CLI {base!r}")
                continue
            if base not in _INTERPRETERS and not _PYTHON_MINOR.match(base):
                continue
            if "-c" in rest[:3]:
                for rel in self._inline_modules(cwd, tokens):
                    self._scan_file([*trail, f"`-c` imports {rel}"], rel, rest, mk)
                continue
            script, args = self._operand(cwd, rest)
            if script:
                self._scan_file(trail, script, args, mk)
        for token in tokens:
            self._unresolved_script(trail, cwd, token)

    def _unresolved_script(self, trail, cwd: str, token: str) -> None:
        if "/" not in token or token.rsplit(".", 1)[-1] not in _SCRIPT_SUFFIXES:
            return
        rel = self._join(cwd, token)
        if rel is None or (self.root / rel).exists():
            return
        self._add(UNRESOLVED, trail, f"a recipe names `{token}`, which this tree does not "
                                     "hold, so what it does could not be read")

    # -- files --------------------------------------------------------------------------
    def _scan_file(self, trail, rel: str, args: list, mk) -> None:
        trail = [*trail, rel]
        # A hermetic reach is NOT remembered: `make verify` reaches `scripts/eval_harness.py
        # --self-test` before anything else does, and remembering it there would mask the
        # spending reach of the same file from a later target in the same walk.
        if set(args) & _HERMETIC_FLAGS:
            return
        if rel in self.seen_files:
            # Read once, but asked the remedy question again, because "have I opened this" and
            # "have I asked which makefile's targets it documents" are different questions and
            # only the first is a property of the file. A step running the file DIRECTLY
            # arrives with `mk is None` and can answer neither; memoizing on the first question
            # alone made `[[python3, gate.py], [make, verify]]` come back clean on a tree where
            # `[[make, verify]]` alone reports the remedy.
            text = self.seen_files[rel]
        else:
            p = self.root / rel
            try:
                if p.stat().st_size > _MAX_FILE_BYTES:
                    self.seen_files[rel] = ""
                    self._add(UNRESOLVED, trail, "is larger than this reader will open")
                    return
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                self.seen_files[rel] = ""
                self._add(UNRESOLVED, trail, f"could not be read: {e}")
                return
            self.seen_files[rel] = text
            for symbol in _SPEND_SYMBOLS:
                if symbol in text:
                    self._add(SPENDS, trail, f"the gate executes it and it names `{symbol}` — a "
                                             "council entry point that launches real providers")
                    break
        if text and self.follow_remedies and mk is not None:
            self._scan_remedies(trail, rel, text, mk)

    def _scan_remedies(self, trail, rel: str, text: str, mk: _Makefile) -> None:
        """`make <target>` NAMED IN a file the gate executes — §5.2's own chain on this repo.

        `make` does not follow a sentence in an error message; an operator does, and only when
        the gate fails. So this is priced rather than refused, and the finding says which.

        The sub-resolver walks the remedy from `mk.cwd`, which is where an operator typing
        `make <target>` after reading that sentence would be — not the repository root. And its
        `unresolved:` findings are carried out rather than filtered away with everything that
        is not a spend: a remedy whose own closure could not be read is the case where BOTH
        backstops would otherwise be silent, and silence here reads as a chain that is free.
        """
        key = (rel, str(mk.path), mk.cwd)
        if key in self.seen_remedies:
            return
        self.seen_remedies.add(key)
        steer = ("`make verify` (receipts advisory)" if "verify" in mk.recipes
                 else "the repository's advisory gate, which this makefile does not declare")
        for target in dict.fromkeys(_MAKE_REMEDY.findall(text)):
            # A target this walk already resolved directly needs no second, weaker account of
            # itself: `scripts/lib/checks.py` names `make verify` while being reached BY `make
            # verify`, and reporting that reads as a chain nobody followed when it is the one
            # already reported above.
            # ORDER-DEPENDENT, and declared rather than left to be rediscovered: whether a
            # target was already walked depends on prerequisite order, so `verify: a b` and
            # `verify: b a` can differ in whether this remedy is reported. Nothing is LOST —
            # the stronger `spends:` finding is present either way, which is why this is
            # noise rather than the refusal-deciding order dependence `_scan_file` guards
            # against — but a caller diffing two findings tuples should know.
            if target not in mk.recipes or (mk.cwd, target) in self.seen_targets:
                continue
            sub = _Resolver(self.root, follow_remedies=False)
            sub._scan_target([f"`make {target}`"], mk.cwd, mk, target)
            spent = self._of(sub.findings, SPENDS)
            if spent:
                self._add(REMEDY, trail,
                          f"it documents `make {target}` as the remedy, and that target does "
                          f"spend: {spent[0]}. `make` does not follow it — an operator does, "
                          f"once per failing candidate. §5.2 steers that operator to {steer} "
                          "rather than to this gate")
                continue
            unread = self._of(sub.findings, UNRESOLVED)
            if unread:
                self._add(UNRESOLVED, trail,
                          f"it documents `make {target}` as the remedy, and whether that target "
                          f"spends could not be read: {unread[0]}")


def _worktree_root(repo) -> Path:
    try:
        out = gitcmd.git(repo, *gitcmd.NO_DAEMON_CACHE, "rev-parse", "--show-toplevel",
                         env_extra=gitcmd.READONLY).stdout.strip()
    except (gitcmd.GitError, OSError, ValueError) as e:
        raise GateError(f"{repo}: not a git worktree the detector can resolve against: {e}") from e
    return Path(out)


def provider_invoking_verify(repo, command) -> tuple[str, ...]:
    """Everything about `command` that would spend provider calls, or that could not be read.

    `()` means this reader followed the command to the end and found no provider spend — never
    that the command is cheap, and the module docstring names what it does not follow.
    """
    steps = getattr(command, "steps", None)
    if steps is None:
        raise GateError(f"a verify Command is required, not {type(command).__name__}; "
                        "parse the spec with verify.Command.parse first")
    resolver = _Resolver(_worktree_root(repo))
    for i, step in enumerate(steps):
        resolver.scan_step(i, step)
    return tuple(dict.fromkeys(resolver.findings))


# ---------------------------------------------------------------------------------------
# §5 step 2: the one question, and the record its answer writes.

# The gaps a run may be asked to accept, as IDS rather than as sentences. `Confirmation`
# records the id, so a handover cites a name that resolves to a line `must_show` CAN emit
# rather than a paraphrase of one, and a caller can tell the empty-surface condition from the
# other three without matching prose that is free to be reworded. "can emit" is the bound and
# not a hedge: `confirm` checks the id against this tuple, never that the line was raised for
# the repository at hand — see its own docstring for why the narrower check is not available
# there. A recorded gap is therefore evidence of what was accepted, never of what was shown.
GATE_SURFACE_EMPTY = "gate-surface-empty"
REMOTES_AND_CONFIGURATION = "remotes-and-configuration-unrecorded"
# RETIRED 2026-08-04, when `--gc` was built: `must_show` no longer raises this gap, so it is
# no longer a gap a `Confirmation` may cite — `_confirmed_gaps` accepts only ids this engine
# can raise, and an accepted gap that resolves to no line is a paraphrase again. The CONSTANT
# stays defined because a run confirmed before that commit carries the string in its journalled
# `accepted_gaps`, and an operator reading that record back needs the name to still mean
# something.
GC_UNBUILT = "gc-unbuilt"
GENERATOR_CONTRACT_EMPTY = "generator-contract-empty"
# `SCREEN_RACE` WAS HERE AND IS CLOSED. It declared that §3's screen ran at preflight while
# `open_run` reused that report, so a file created or edited between the two was force-added
# into B1 with no scan — `materialize` guarded only the index hash, which does not move for an
# unstaged edit or a new file under a selected directory. `baseline.materialize` now screens
# the path set B1 ACTUALLY HOLDS, after the manifest is built and before the ref is written,
# so there is no window left to lose. The gap line is deleted with it: a gap announced after
# it has been closed tells an operator to accept a risk that is not there and trains them to
# skim the ones that matter — the same rule as GATE_SURFACE_EMPTY's, broken the other way.
ACCEPTABLE_GAPS = (GATE_SURFACE_EMPTY, REMOTES_AND_CONFIGURATION, GENERATOR_CONTRACT_EMPTY)

# §5 step 2's first policy, spelled as the two branches a later phase can act on. §5 writes
# the second as "continue as degraded"; the value is the branch name, and `degraded` is also
# §14's terminal phase for a run that took it.
CALIBRATION_POLICIES = ("abort", "degraded")

# §12's strategy rule, and the reason `partition` is NOT one of these. §12.1 makes the size
# gate "trigger analysis, it does not force partitioning", and admits a partition only "where
# stable seams exist" — a natural-language criterion §10.1 forbids presenting as a checked
# predicate, so it cannot be pre-committed to at a gate that has seen no artifact. What CAN
# be confirmed in advance is which of §12.1's three rules the run applies to a measured size:
#
#   * `size-gated`     §12.1 itself — fusion below ~400 changed lines / ~15 files, and above
#                      it, partition only where stable seams exist, else base-and-port.
#   * `fusion`         from-scratch fusion whatever the size measures.
#   * `base-and-port`  §12.1's "correct primary strategy" where no stable seams exist,
#                      chosen up front rather than after the analysis.
STRATEGY_RULES = ("size-gated", "fusion", "base-and-port")

# What §5 step 2 asks. `accepted_gaps` is the one that may be left out, and the asymmetry is
# the point: either calibration policy is an ACTION a later phase takes, so silence there has
# no safe reading, while silence about a gap means the operator accepted none — which is the
# reading that cannot later be cited as agreement.
_ANSWERS = ("setup", "verify", "on_calibration_failure", "strategy", "author",
            "accepted_gaps", "deep_review")
_REQUIRED = ("setup", "verify", "on_calibration_failure", "strategy", "author", "deep_review")

# What git's ident line cannot hold: `<` and `>` delimit the email and a newline ends the
# header. Refused HERE because git does not refuse it — measured on git 2.53.0, `commit-tree`
# with GIT_AUTHOR_NAME `Ada <x>` exits 0 and the commit reads `Ada x`, and `Ada\nNewline`
# reads `AdaNewline`. So the alternative to this check is not a late failure but a permanent
# false attribution on the commit the user is asked to merge, with nothing raised.
_IDENT_FORBIDDEN = "<>\n\r"


def propose_identity(repo):
    """The identity git would use for a commit here — `(name, email)`, or None.

    THE ONE CALL IN THIS PACKAGE THAT READS THE USER'S OWN CONFIG, and it exists because
    `baseline._resolve_author` cannot: `gitcmd` pins the global and system files to /dev/null
    on every other call, so the engine is blind to the place an identity normally lives, and
    "no local user.name" is the ordinary state of an ordinary repository rather than a fault.
    §5 step 2 is where the operator is present, which is where a fact this hardened path
    cannot see has to enter.

    NOT `git var GIT_AUTHOR_IDENT`, which is what a reader reaches for and what
    `baseline`'s own docstring recommended. Measured on git 2.53.0, `user.name` set and
    `user.email` unset: it answers `Configured <khenrix@Surface-Book-2.localdomain>` at rc=0,
    inventing the email from user@hostname with nothing in the output marking which half was
    guessed — and B1 is history the user is asked to merge, so a guess that reaches the record
    is the permanent false attribution `baseline` refuses to make. `config --get` on the same
    repository exits 1 for the unset half, which is the answer that can be shown as missing.
    The cost is what `git var` reads and this does not: `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`
    and `EMAIL` in the ambient environment. An operator using those passes `author` directly.

    None, never a half-filled pair: an email without a name is not an identity, and a caller
    handed one field would fill the other in — which is the guess again, one level up.

    RETURNED, NEVER RECORDED. This is a proposal for the operator to read and confirm; what
    reaches `Confirmation` is what they answered. A run authored by this function's return
    value without anybody seeing it is the same false attribution reached politely.
    """
    def value(key):
        # `user_config=True` is argued at `gitcmd.git`; measured on git 2.53.0 with a
        # `core.fsmonitor` armed and a full hook set planted, `config --get` ran neither, so
        # restoring the file that can NAME those programs cannot run one here.
        return gitcmd.git(repo, "config", "--get", key, env_extra=gitcmd.READONLY,
                          check=False, user_config=True).stdout.strip()

    name, email = value("user.name"), value("user.email")
    return (name, email) if name and email else None


@dataclass(frozen=True)
class Confirmation:
    """The answer to §5 step 2, in the shape the manifest records it.

    `setup` and `verify` are `verify.Step` tuples — the manifest's own type, so `open_run`
    performs no conversion. A conversion here is where a `cwd` or a `timeout` would go
    missing between what a human agreed to and what a resume runs.

    WHAT A `Step` DOES NOT CARRY, and why that is permanent rather than pending. `Step` has
    four fields (`argv`, `cwd`, `env`, `timeout`). §5.1 also makes a per-step `retryable`
    mark contractual — "part of the contract, not an implementation choice", feeding §12.3's
    failure classification — and nothing in this package implements it. For a run THIS gate
    opens that cannot be repaired later: §5 step 5 forbids re-asking, §14.2 forbids rewriting
    the manifest, and `runstate.read_manifest` refuses a step record carrying a key it does
    not know (`test_a_step_record_that_is_not_section_5_1s_shape_is_refused`, the
    `unknown-field` case, is exactly `retryable=True`). So every step of every run opened here
    aborts its sequence on a nonzero exit, and §12.3 classifies a contention-class failure for
    these runs without the input §5.1 promises it.

    `author` is who B1 is recorded as — the one commit forge writes into the user's own
    history, and the one §2.1 makes theirs rather than forge's. It is an ANSWER and not a
    probe: every call `gitcmd` makes has the global config pinned at /dev/null, so
    `baseline._resolve_author`'s fallback sees repo-LOCAL identity only and a repository whose
    identity lives in `~/.gitconfig` — the ordinary one, this one included — had no route to
    supply one at all. `propose_identity` is what fills this in for the ordinary case; the
    field is what makes the run refusable before anything is on disk rather than three writes
    in. Carried here rather than in the manifest because B1's commit object already records
    it and `baseline_commit` is in the manifest, so a second copy is a second thing to be
    right about.

    `seats`, `attempts`, `review_rounds` and `synthesis_fix_cap` come off the `Quote` the
    operator was shown, and `confirm` refuses to take them from anywhere else. They are the
    run's shape and §5.2 priced the run BY them, so the alternative — a launcher passing its
    own — is a fleet nobody costed.

    `deep_review` goes the OTHER way and is an ANSWER, though `quote` also prices it. The two
    are not the same reading: the four numbers above are computed BY `quote` and have no
    second source, while §13.1's opt-out is the operator's own decision, made once, spent an
    hour later in another process by `deepreview.run_deep_review(enabled=…)`. Recorded here rather than
    read back off `Quote.deep_review`, which is a SENTENCE for a human (§13.1 prices the
    review in the operator's own words, not as a number) and not a value another process may branch on.
    `confirm` is where the answer and the price are checked against each other.

    NO DEFAULTS, including on `accepted_gaps`, on `runstate.State`'s rule: a field the
    constructor supplies is a fact nobody answered for. `confirm` is where an omitted
    `accepted_gaps` KEY becomes the empty tuple, because that is a reading of a silent answer
    sheet; every other normalization is below, where the record itself does it.

    THE VALUE ENFORCES ITS OWN INVARIANTS, AND SO DOES EVERY VALUE IT HOLDS, which is the
    part that took three findings to get right. All of it was measured on this class. It is a
    public frozen dataclass, so one assembled beside `confirm` carried none of `confirm`'s
    checks: `Confirmation(author=None)` raised `BaselineError` out of `materialize` with a run
    directory and an `events.jsonl` already orphaned, and `author=("Ada <x>", "a@b\\ninvalid")`
    opened a run whose B1 reads `Ada x|a@binvalid`, because git 2.53 strips those characters
    rather than refusing them. Then `seats` was decided by a magnitude test here and by
    `runstate.count` in the manifest, and the two disagreed on `True`. Then — with this class
    validating all eight of its own fields — the `Step`s in `setup` and `verify` did not:
    `timeout=True`, `timeout=1.5`, `env={"A": 1}` and `cwd=5` passed `confirm` and were
    refused by `write_manifest`, with B1's ref already in the user's repository.

    So the checks live in `__post_init__` rather than in the caller, at every level rather
    than at this one. `confirm`, `open_run` and `write_manifest` then inherit ONE predicate
    per field instead of spelling three that are free to drift apart — which is the defect
    above, three times — and `open_run`'s `isinstance` check becomes a real one: a value of
    this type cannot be a run the gate would have refused, down to the fields of the steps it
    carries. The closure is checked rather than asserted, and by a walk over
    `dataclasses.fields` rather than a list of today's types, since the list is what was wrong
    each time: `test_no_value_a_confirmation_can_hold_is_one_the_manifest_would_refuse`.

    TWO STATES THAT SENTENCE DOES NOT COVER, both deliberate and neither closed. A caller can
    reach past a constructor — `object.__new__` plus `object.__setattr__`, an unpickle that
    restores `__dict__`, a SUBCLASS overriding `__post_init__` (which `open_run`'s isinstance
    admits), or mutating the dict a `Step` holds in `env` after the fact. And this class is
    the ANSWER to §5 step 2, so the operator at this door is trusted by construction; what the
    gate exists to refuse is a wrong answer, not a caller who has decided to forge one. The
    boundary is chosen, and it is stated here so the next reader knows it was not missed.
    """
    setup: tuple
    verify: tuple
    on_calibration_failure: str
    strategy: str
    accepted_gaps: tuple[str, ...]
    author: tuple[str, str]
    seats: int
    attempts: int
    review_rounds: int
    synthesis_fix_cap: int
    deep_review: bool
    # OFF THE QUOTE, never from a caller: the wall-clock ceiling §5.2 showed was computed FROM
    # this width, so a fleet driven at another one is a fleet the operator did not agree to.
    # Defaulted to serial so every `Confirmation` assembled beside `confirm` — §12.4's
    # consumer and the suite's fixtures — keeps meaning what it meant before the field existed.
    concurrency: int = 1

    def __post_init__(self):
        # NORMALIZING as well as refusing, through the same helpers `confirm` used to call:
        # an argv list becomes `verify.Step`s and an author is stripped HERE, so the answer to
        # "what does this field hold" is one answer rather than one per construction route.
        # `open_run` performs no conversion, and this is what lets it not have to.
        object.__setattr__(self, "setup", _confirmed_steps("setup", self.setup))
        steps = _confirmed_steps("verify", self.verify)
        if not steps:
            # An empty SETUP is an ordinary repository that needs none. An empty VERIFY is a
            # gate that decides nothing, and §6.2's outcomes are all read off its exit code —
            # so every candidate would pass, including one that deleted the tests.
            raise GateError(
                "the verify command names no step, so the gate would decide nothing and every "
                "candidate would pass it. §6.2 reads every outcome off this command.")
        object.__setattr__(self, "verify", steps)

        if self.on_calibration_failure not in CALIBRATION_POLICIES:
            raise GateError(
                f"on_calibration_failure is {self.on_calibration_failure!r}; §5 step 2 offers "
                f"{list(CALIBRATION_POLICIES)} — abort the run when the untouched baseline "
                "fails its own gate, or continue and carry the run to §14's `degraded` ending")
        if self.strategy not in STRATEGY_RULES:
            raise GateError(
                f"strategy is {self.strategy!r}; §12's rule is confirmed here and applied to "
                f"measured artifact size later, so it is one of {list(STRATEGY_RULES)}. "
                "`partition` is deliberately absent: §12.1 admits one only where stable seams "
                "exist, which is §10.1's non-mechanical criterion and cannot be "
                "pre-committed to.")
        if not isinstance(self.deep_review, bool):
            raise GateError(
                f"deep_review is §13.1's opt-in/out and is a bool, not {self.deep_review!r}. "
                "A truthy string here would price one run and collect another.")
        object.__setattr__(self, "author", _confirmed_author(self.author))
        object.__setattr__(self, "accepted_gaps", _confirmed_gaps(self.accepted_gaps))
        # Returned values discarded: there is nothing to normalize about a whole number, and
        # the call is here for the refusal. §5.2 priced the run BY these two, so the record
        # cannot hold a shape the manifest will not take.
        _confirmed_count("seats", self.seats, "§5 step 2")
        _confirmed_count("attempts", self.attempts, "§5 step 2")
        _confirmed_count("review_rounds", self.review_rounds, "§5 step 2", floor=0)
        _confirmed_count("synthesis_fix_cap", self.synthesis_fix_cap, "§5 step 2", floor=0)
        # VALIDATED HERE TOO, not only on the `Quote`. `confirm` builds this off a quote whose
        # `__post_init__` already checked it, but §12.4's consumer and every test build a
        # `Confirmation` directly — and this is the record `open_run` writes into the manifest,
        # so an unchecked width here reaches disk without ever passing the quote's door.
        # BOTH SIDES ARE PROVEN COUNTS FIRST, then compared — `Quote.__post_init__` records
        # why: a cross-field test on an unvalidated field raises TypeError out of a gate whose
        # whole job is to answer in GateError.
        _confirmed_count("concurrency", self.concurrency, "§5 step 2")
        _confirmed_count("seats", self.seats, "§5 step 2")
        if self.concurrency > self.seats:
            raise GateError(
                f"concurrency={self.concurrency} exceeds seats={self.seats}: above the seat "
                "count it buys nothing and was priced as though it did")
        if self.synthesis_fix_cap < self.review_rounds:
            # Each round can produce at most one fix, and §13.1 adds one more. A cap under
            # the rounds it must cover is a budget the loop is guaranteed to exhaust before
            # the review it was priced for finishes, which would report `review_blocked` for
            # an arithmetic mistake made at the gate.
            raise GateError(
                f"synthesis_fix_cap={self.synthesis_fix_cap} is below review_rounds="
                f"{self.review_rounds}: §5.2 prices one post-review synthesis per round plus "
                "§13.1's, so a cap under the round count cannot fund the review it agreed to")


def free_bytes(path) -> int:
    """Free bytes on the filesystem that will hold `path`, or a refusal.

    IT WALKS TO THE NEAREST EXISTING ANCESTOR because the forge root under `XDG_STATE_HOME`
    does not exist before the first run, and a directory's filesystem is its ancestor's. This
    is not an error-swallow: the walk stops at the root, and a path with no readable ancestor
    at all RAISES.

    IT NEVER ANSWERS A NUMBER IT DID NOT READ. §4 rejects a run the disk cannot sustain, so
    "free space could not be read" answered as a large number is the refusal disarmed by its
    own failure mode — nobody measured and there is room have to be two different answers.
    """
    p = Path(path).resolve()
    last = None
    for cand in (p, *p.parents):
        try:
            return shutil.disk_usage(cand).free
        except OSError as e:
            last = e
    raise GateError(f"free space at {p} could not be read: {last}. §4 rejects a run the disk "
                    "cannot sustain, and a run whose disk this engine could not measure is "
                    "not one it can say fits.")


def refuse_for_disk(quote_, *, root=None) -> str | None:
    """§4's rejection sentence, or `None` when the run fits.

    `None` MEANS MEASURED AND SUFFICIENT AND NOTHING ELSE. Every other outcome — a shortfall,
    or a read that failed — comes back as a sentence, and the two sentences differ: a caller
    that printed one for the other would tell an operator with a broken `statvfs` that their
    disk is full, or an operator with a full disk that the engine could not look.

    THE PEAK IS THE QUOTE'S OWN AND NOT A SECOND ESTIMATE. `quote.peak_disk_gb` is what §5.2
    showed the operator; comparing against a number computed here would refuse a run on a
    figure nobody was shown.
    """
    if not isinstance(quote_, Quote):
        raise GateError(f"a Quote is required, not {type(quote_).__name__}; §4's rejection is "
                        "against the peak §5.2 showed, and there is no other peak to use")
    target = Path(root) if root is not None else storage.forge_root()
    need = int(quote_.peak_disk_gb * 1e9)
    try:
        have = free_bytes(target)
    except GateError as e:
        return (f"§4 rejects this run: {e} The quote's peak is ~{quote_.peak_disk_gb} GB and "
                "this engine could not check it against anything.")
    if have >= need:
        return None
    return (f"§4 rejects this run: it peaks at ~{quote_.peak_disk_gb} GB under {target} and "
            f"that filesystem has {have / 1e9:.1f} GB free. §4 says not to trade source-object "
            "safety for space, so the clones are not made shallow, hardlinked or shared — free "
            "space, reduce --seats or --attempts, and re-price.")


def must_show(report, quote_, command, *, setup) -> tuple[str, ...]:
    """Everything a human must see before answering §5 step 2, in the order it matters.

    THE COMMAND IS AN ARGUMENT BECAUSE THE FIRST LINE CANNOT BE READ OFF A REPORT.
    `preflight.Report.gate_surface` is None and says why: §6.1's surface belongs to the
    RESOLVED gate, and preflight has no command to resolve. Here the command is in hand — it
    is what the human is being asked to confirm — so the surface is computable, and the
    difference between "measured, found nothing" and "nobody looked" is exactly the condition
    the qualified-PASS ruling rests on.

    Measured on a repository holding `check.sh` and nothing else a role rule names:
    `gate_surface` with no command returns `()`, and with `[["./check.sh"]]` returns
    `('check.sh',)`. So the surface resolved WITHOUT the command reports the empty condition
    for an ordinary repository whose gate is a named script — a false alarm on the one line
    whose whole value is that it is rare. The CONFIRMATION is deliberately not what is taken
    instead: it is the answer, and this is the question.

    WHY THE USER'S OWN WORKTREE MAY BE RESOLVED AGAINST, when `gate_surface` requires a tree
    the engine built. That requirement is about a tree the party under suspicion could have
    written — a seat's index, where `git rm --cached Makefile` would delete a gate file from
    its own surface. At §5 step 2 no seat exists and no provider has run, so the worktree is
    the user's own. The other half of "engine-built" is that reading it must run nothing the
    repository supplies; `verify._enumerate` carries `NO_DAEMON_CACHE` so that holds here.

    BOTH COMMANDS ARE SCREENED, AND `setup` IS REQUIRED FOR THE REASON THE VERIFY COMMAND IS.
    §5.2's rule is about a command that transitively reaches a provider CLI, and it is worse
    for setup than for verify: setup runs once per builder clone AND once per verifier clone —
    18 times on a default run against 9 verifies — so an unscreened setup is the more expensive
    miss. `None` is refused rather than read as "no setup", because a command nobody screened
    and one screened clean must not leave the same record.
    """
    if not isinstance(report, preflight.Report):
        raise GateError(f"a preflight.Report is required, not {type(report).__name__}; "
                        "§5 step 2 is what the static look at step 1 is shown for")
    if not isinstance(quote_, Quote):
        raise GateError(f"a Quote is required, not {type(quote_).__name__}; §5.2 prices the "
                        "run at this gate and there is no later one to correct it")
    if getattr(command, "steps", None) is None:
        raise GateError(f"a verify Command is required, not {type(command).__name__}; the "
                        "surface below is §6.1's answer for the RESOLVED gate, and an "
                        "unresolved one reports the empty condition for any repository")
    if getattr(setup, "steps", None) is None:
        raise GateError(f"a setup Command is required, not {type(setup).__name__}; §5.2's "
                        "provider-invocation rule covers it and it runs more often than verify")

    lines = list(preflight.refusals(report))
    if lines:
        # First, and in `quote`'s own order: nothing below is reachable while one of these
        # stands, and §2.3 does not let the operator answer past them.
        lines.append("the lines above are §2.3 refusals — they fail closed, this gate cannot "
                     "be answered past them, and `open_run` will not open a run over one")

    disk = refuse_for_disk(quote_)
    if disk:
        # AT THE TOP, with §2.3's refusals, because it is one: `open_run` will not open a run
        # over it and nothing below is reachable while it stands.
        lines.insert(0, disk)

    surface = verify.gate_surface(report.facts.root, report.contract, command=command)
    if surface:
        lines.append(f"gate surface: §6.1's rules and this command resolve to {len(surface)} "
                     f"file(s) — {', '.join(surface[:6])}"
                     f"{' …' if len(surface) > 6 else ''}. A candidate that rewrites one of "
                     "them is marked `gate_changed` and never silently keeps independent-gate "
                     "status")
    else:
        lines.append(
            f"gap {GATE_SURFACE_EMPTY}: §6.1's rules and this command resolve to NO file in "
            "this repository, so the engine cannot see what defines your gate. A candidate "
            "can gut the gate and still earn a PASS whose reason says the surface was "
            "measured and unchanged. That verdict qualifies itself rather than being "
            "displaced — displacing it would attribute to the candidate a fact about the "
            "engine — on the condition that you are told once, before a token is spent. "
            "This line is that condition.")

    # §3'S SCOPE AND COUNT, ALWAYS, ON `GATE_SURFACE_EMPTY`'S PRECEDENT. `refusals()` returning
    # `()` was two claims under one value — "this baseline holds no credential" and "nobody
    # looked" — and on the default path it was always the second, because the screen was scoped
    # to a selection `cli` defaults to empty. It now covers what B1 carries, and the count is
    # shown because an operator pricing a run over three cloud CLIs is entitled to know the
    # scan happened and how far it reached.
    lines.append(
        f"§3 screen: {report.screened} file(s) read — every tracked path plus your selection, "
        f"which is what B1 carries to three cloud CLIs. {len(report.secrets)} finding(s), "
        f"{len(report.breaches)} unreadable")
    lines += [
        f"gap {REMOTES_AND_CONFIGURATION}: §9 protects seven things and the t0 snapshot "
        "records five. A remote added or a repository-local config key set while the run is "
        "out leaves no t0 fact behind it, so `drift` cannot speak for either and a handover "
        "will not stop for one",
        # NOT A `gap` LINE ANY MORE, AND THAT IS THE WHOLE OF WHAT BUILDING `--gc` CHANGED
        # HERE. A gap is something the operator is asked to ACCEPT, and there is nothing left
        # to accept: what remains is a standing instruction, because the peak disk below is
        # still what accumulates for anyone who never runs it.
        "every interrupted write leaves a staging file nothing collects, and a finished run "
        "keeps its seats' clones, so `--gc <run-id>` is mandatory rather than tidy. The peak "
        "disk below is what accumulates until you run it; `--gc all` reports what is held now",
    ]
    # READ OFF `report.contract`, never asserted. "The contract is empty" is true of every
    # repository `detect_generators` can read today, so a hardcoded sentence and this branch
    # are indistinguishable until they are not — and the case they then differ on is the one
    # deciding whether a candidate may rewrite a tracked file and keep its PASS.
    relations = report.contract.relations
    if relations:
        shown = ", ".join(f"{s} -> {o}" for s, o in relations[:4])
        lines.append(
            f"generator contract {report.contract.id!r}: {len(relations)} relation(s) — "
            f"{shown}{' …' if len(relations) > 4 else ''}. A verify-origin rewrite of a "
            "tracked file matching one of the OUTPUT globs is admitted and keeps the "
            "candidate's PASS; the source glob is provenance and decides nothing")
    else:
        lines.append(
            f"gap {GENERATOR_CONTRACT_EMPTY}: this repository's generator contract is empty, "
            "so no relation admits a verify-origin rewrite of a TRACKED file. A PASS "
            "therefore requires a gate that rewrites no tracked file, whatever this "
            "repository's own generators do")
    lines += list(quote_.lines)
    lines += list(provider_invoking_verify(report.facts.root, command))

    setup_findings = provider_invoking_verify(report.facts.root, setup)
    if setup_findings:
        lines.append(
            f"the setup command reaches a provider CLI in {len(setup_findings)} place(s) — "
            f"{'; '.join(setup_findings[:4])}"
            f"{' …' if len(setup_findings) > 4 else ''}. §5.2 refuses one that does, or prices "
            f"it as its own explicit line, and setup runs {quote_.setup_runs} times on this "
            "quote — so this spend is a multiple of the quote above and is not in it")
    else:
        lines.append(
            "setup command: followed to the end and no provider spend was found in it. That "
            "is this reader's answer over what it can follow, never a claim that the command "
            "is cheap")
    return tuple(lines)


def _confirmed_steps(name: str, value) -> tuple:
    """§5.1's steps as the manifest's own type, from either shape a caller can hold.

    ALREADY-BUILT `verify.Step`s are accepted whole, and that route is the only one that can
    express a per-step `cwd`: `verify.Command.parse` takes a list of ARGV LISTS and has no
    place to put one, so §5.1's own motivating example — `cd frontend && npm ci` as two steps
    in two directories — is unconfirmable through it. The argv route is still the ordinary
    one, and it reaches `parse` whole rather than step by step so its refusals keep naming
    the index of the step at fault.

    WHOLE means the same thing here as `open_run`'s type check means one level up, and it did
    not before: a `Step` now decides all four of §5.1's fields in its own `__post_init__`, so
    accepting one whole imports its refusals rather than skipping them. While `cwd`, `env` and
    `timeout` were decided only by `runstate._steps`, this line let `timeout=True`,
    `timeout=1.5`, `env={"A": 1}` and `cwd=5` through `confirm` into a run that
    `write_manifest` then refused — four writes and one ref in the user's repository later.

    A MIXED sequence is refused rather than handled. The two routes differ in what they
    settle: a `Step` carries a cwd, an env and a timeout the caller chose, while an argv list
    takes `Step`'s defaults. Reading half a command each way makes which of those a given
    step got depend on how it happened to be written.
    """
    if isinstance(value, (str, bytes)):
        raise GateError(
            f"{name} is {value!r}, a single string. A command is a LIST of steps and a step "
            'is a list of words — write [["make", "verify"]]. Nothing here runs a shell.')
    try:
        rows = list(value)
    except TypeError as e:
        raise GateError(f"{name} is not a sequence of steps: {value!r}") from e
    built = [isinstance(r, verify.Step) for r in rows]
    if any(built) and not all(built):
        raise GateError(
            f"{name} mixes verify.Step records with bare argv lists. A Step carries the cwd, "
            "env and timeout the caller chose and an argv list takes the defaults, so a mixed "
            "command hides which of the two each step was agreed under.")
    if all(built) and rows:
        return tuple(rows)
    try:
        return verify.Command.parse(rows).steps
    except verify.VerifyError as e:
        # Re-raised in this module's vocabulary, on `runstate._steps`' precedent: `confirm`'s
        # documented contract is that an answer it will not stand behind arrives as a
        # GateError, and the caller catching it has no other name for this.
        raise GateError(f"{name}: {e}") from e


def _confirmed_author(value) -> tuple[str, str]:
    """`(name, email)` for B1, or a GateError naming what is wrong with the answer.

    A bare STRING is refused first, on `_confirmed_steps`' argument: `"Ada <ada@x>"` unpacks
    into two characters without complaint, and the run would be authored by `A <d>`.
    """
    if isinstance(value, (str, bytes)):
        raise GateError(
            f"author is {value!r}, a single string. It is a (name, email) PAIR — write "
            '("Ada Lovelace", "ada@example.com"). A string unpacks into its first two '
            "characters, and B1 would carry them as a name and an address.")
    try:
        name, email = value
    except (TypeError, ValueError):
        raise GateError(
            f"author is {value!r}; §5 step 2 asks for the (name, email) B1 is recorded "
            "as. `gate.propose_identity(repo)` reads the one git would use, including the "
            "global config every other call this package makes is blind to.") from None
    for label, part in (("name", name), ("email", email)):
        if not isinstance(part, str) or not part.strip():
            raise GateError(
                f"author's {label} is {part!r}. B1 is the commit §2.1 makes the USER's and "
                "the branch they are asked to merge is rooted on it, so half an identity is "
                "refused rather than completed here — see `baseline`'s own refusal to guess.")
        if any(c in part for c in _IDENT_FORBIDDEN):
            raise GateError(
                f"author's {label} is {part!r}, which carries one of {_IDENT_FORBIDDEN!r}. "
                "Those delimit git's ident line, and git 2.53 strips them rather than "
                "failing — so B1 would record a name the operator did not type.")
    return name.strip(), email.strip()


def _confirmed_gaps(value) -> tuple[str, ...]:
    """The accepted gaps as ids, or a GateError naming the one that resolves to no line.

    WHAT IS CHECKED is that each id is one this engine can raise — `ACCEPTABLE_GAPS` — and
    not that `must_show` raised it for THIS repository. The narrower check needs the command
    the surface was measured with, and a `Confirmation` is the answer to a question its caller
    rendered. So accepting a gap nobody raised is recorded rather than refused; it is noise in
    a handover, never a licence.
    """
    if isinstance(value, (str, bytes)):
        raise GateError(
            f"accepted_gaps is {value!r}, a single string; iterating one yields its "
            "characters, so a run would record acceptance of a list of letters")
    try:
        gaps = tuple(value)
    except TypeError as e:
        raise GateError(f"accepted_gaps is not a sequence of gap ids: {value!r}") from e
    stray = sorted(set(gaps) - set(ACCEPTABLE_GAPS))
    if stray:
        raise GateError(
            f"accepted_gaps names {stray}, which `must_show` cannot raise. An accepted gap has "
            f"to resolve to a line the operator was shown; the ids are {list(ACCEPTABLE_GAPS)}.")
    return gaps


def confirm(report, quote_, answers) -> Confirmation:
    """§5 step 2's answer, validated into the record §14.2 writes once.

    `report` and `quote_` are required and type-checked though neither is stored, for
    `quote`'s own reason one step on: a caller holding neither has not reached this gate, and
    an answer collected before the static look and the price is an answer to a different
    question.

    NO POLICY IS DEFAULTED. §5 asks for both once and §5 step 5 says the decision is not
    re-asked, so a missing one cannot be filled in here — the value this function would
    choose IS the decision, taken by the reader, at the one gate whose whole purpose is that
    the operator takes it. `accepted_gaps` is the single omissible answer and its silence
    reads as "accepted none", which is the reading that cannot later be cited as agreement.

    An UNKNOWN answer key is refused rather than ignored, and its only real subject is
    `accepted_gaps`. A misspelled REQUIRED key cannot reach it — the misspelling leaves the
    real key absent, so the missing-key check above raises first. `accepted_gaps` is the one
    key whose absence is legal, so `acccepted_gaps` passes every other check and is read as
    "accepted none": a gap the operator did accept, dropped from the record, with nothing
    raised. Spelled as a whole-key check rather than a special case for that one name,
    because the next optional answer would arrive with the same hole.

    WHAT THIS FUNCTION DECIDES IS WHICH QUESTIONS WERE ANSWERED; `Confirmation` decides
    whether an answer is usable. The split is not stylistic: every value check used to live
    here, so it held for a record reached THROUGH this function and for no other — and
    `Confirmation` is a public frozen dataclass, so one assembled beside it carried none of
    them. Its `__post_init__` is where they are now, which is the only placement that makes
    `open_run`'s type check a real check. What is left here is what only an answer SHEET can
    be wrong about: a missing key, a key nobody asked for, and a caller who has not reached
    this gate.

    THE ANSWER IS REFUSED WHERE REFUSING IT COSTS NOTHING, which is still this call. B1 cannot
    be authored from what this package can see — `gitcmd` pins the global config to /dev/null
    on every call, so a repository whose identity is in `~/.gitconfig` has none as far as
    `baseline._resolve_author` is concerned. Refusing at `open_run` instead would refuse after
    a run directory and a journalled intent already existed, which is the orphan §14.1 reserves
    for a crash. `propose_identity` is what an operator's front end fills the field from, and it
    is deliberately not called here: this function records an answer, and a value it went and
    fetched is not one.

    The four numbers of the run's shape — `seats`, `attempts`, `review_rounds` and
    `synthesis_fix_cap` — are read OFF THE QUOTE and are not answer keys. §5.2 prices the run
    by them, and a second route in is a run whose recorded shape and quoted shape are two
    numbers a reader has to compare — so there is one number, and the only way to change it is
    to price the change first.
    """
    if not isinstance(report, preflight.Report):
        raise GateError(f"a preflight.Report is required, not {type(report).__name__}")
    if not isinstance(quote_, Quote):
        raise GateError(f"a Quote is required, not {type(quote_).__name__}")
    if not isinstance(answers, dict):
        raise GateError(f"the answers to §5 step 2 are a mapping, not {type(answers).__name__}")
    missing = [k for k in _REQUIRED if k not in answers]
    if missing:
        raise GateError(
            f"§5 step 2 is asked once and {missing} went unanswered. Neither policy has a "
            f"safe default — calibration failure is one of {list(CALIBRATION_POLICIES)} and "
            f"the strategy rule is one of {list(STRATEGY_RULES)} — and a value chosen here "
            "would be the reader's decision recorded as the operator's.")
    unknown = sorted(set(answers) - set(_ANSWERS))
    if unknown:
        raise GateError(f"§5 step 2 does not ask {unknown}; it asks {list(_ANSWERS)}")

    # THE INVARIANT LIVES IN THE VALUE, not in a second check downstream. `quote` prices
    # §13.1 on or off and re-prices the quote doing it; `deepreview.run_deep_review` obeys the
    # decision an hour later in another process. If those were two readings of one intent they
    # would eventually disagree, and the disagreement is spend: a run priced without the deep
    # review that then convenes one, or the reverse — a user shown a call count they were never
    # charged and a review they were told they would get.
    #
    # `Quote.deep_review` is a STRING by design (it is the operator's own words for the price),
    # so the comparison is against what `quote` recorded rather than a second boolean field.
    priced_on = not quote_.deep_review.startswith("not run")
    if bool(answers["deep_review"]) is not priced_on:
        raise GateError(
            f"this run was priced with the deep review {'ON' if priced_on else 'OFF'} and the "
            f"answer says {'ON' if answers['deep_review'] else 'OFF'}. §5 step 5 forbids "
            "re-asking, so the quote the operator saw is the one this run may spend — "
            "re-price with `quote(..., deep_review=...)` and show it again.")

    # Every value below goes to `Confirmation` as it was answered. `accepted_gaps` is the one
    # key whose ABSENCE is legal, and reading that silence as "accepted none" is a fact about
    # the sheet rather than about the record, so it is read here.
    return Confirmation(setup=answers["setup"], verify=answers["verify"],
                        on_calibration_failure=answers["on_calibration_failure"],
                        strategy=answers["strategy"],
                        accepted_gaps=answers.get("accepted_gaps", ()),
                        author=answers["author"],
                        deep_review=answers["deep_review"],
                        seats=quote_.seats, attempts=quote_.attempts,
                        review_rounds=quote_.review_rounds,
                        synthesis_fix_cap=quote_.synthesis_fix_cap,
                        concurrency=quote_.concurrency)


def open_run(report, confirmation: Confirmation, run_id: str, *, quote_) -> Path:
    """Open the run this confirmation agreed to, and record it where a resume will read it.

    THE REPOSITORY IS THE REPORT'S, not a second argument. `report.facts.root` is git's own
    answer for the repository preflight looked at, and it is what `baseline.materialize`
    builds from and what `drift` resolves the manifest's `repo_path` against. A `repo`
    argument beside it would be a second answer to a settled question, and the wrong one
    answers cleanly — the failure `runstate._refuse_a_repository_the_manifest_did_not_record`
    exists for. It also decides `repo_path`: a caller who ran preflight on a SUBDIRECTORY has
    a `report.repo` that is not the toplevel, and a manifest recording it would be refused by
    every later `drift` as a repository this run was not recorded against.

    ORDER, AND WHAT EACH STEP MAKES TRUE. Every refusal comes before every write, so a run
    that is not going to happen leaves nothing at all on disk — not a run directory, not
    `baseline.index`, not an `events.jsonl`, not an object and, above all, not a ref in the
    user's own repository. That is a claim about VALIDATION COMPLETING first, not merely about
    where these two `raise`s sit, and it has been false three times for the same structural
    reason: a predicate spelled once here and again downstream, disagreeing. B1's author was
    resolved inside `materialize` and raised two writes in. `seats=True` passed a magnitude
    test at `confirm` and met `runstate`'s type test inside `write_manifest`, four writes
    later, with the ref already made. Then a `Step`'s `cwd`, `env` and `timeout` were decided
    only by `runstate._steps`, so `timeout=True` reached the same place by the same route —
    and `cwd=5` reached the spends detector below and raised a bare `AttributeError` out of
    it. None of the three is a refusal this function performs today, and their absence is the
    point: all are invariants of the `Confirmation` it is handed and of the `verify.Step`s
    that record holds, checked when those values were constructed, so a run this function
    cannot finish cannot be started. The remaining two — a repository preflight refuses, and a verify
    command that spends — are re-decided here because they are facts about the REPOSITORY and
    the command rather than about the answer, and both are read before anything is created.
    Then the run root, which fails on a run id already taken rather than sharing a directory
    with the run that owns it. Then `journal.intent("confirm")`, before anything
    touches the user's repository. Then B, whose ref and OID cannot be recorded any earlier
    because §9's whitelist is "the exact OID recorded AT CREATION" and only `materialize`'s
    return value knows it. Then t0 — §14.2 puts it at this gate, which is why `preflight`
    deliberately takes no snapshot — with B's ref declared, so forge's own baseline is not
    reported as the user's ref appearing. Then the manifest, once. Then
    `journal.done("confirm")`, which closes the write-ahead pair; §14.1's completion receipt
    belongs to the run and not to this gate.

    WRITE-ONCE IS TWO KERNEL REFUSALS, neither of which is a check this function performs: a
    second `open_run` for the same run id meets `mkdir`'s, and a second manifest inside one
    run meets `os.link`'s. `mkdir`'s is what a repeated call actually hits, for the ordinary
    reason that it comes first in this function. What that ordering buys is that the SECOND
    refusal is never needed: were the run root shared instead, B's ref is created with a
    compare-and-swap against "must not already exist" and would refuse before the manifest
    was reached at all, so no arrangement of this function reaches `os.link`'s refusal by
    repeating a call. It is exercised directly instead.

    THE POLICIES AND THE AUTHOR ARE JOURNALED, not in the manifest — §14.2's list of what the
    manifest holds is the repository, `base_commit`, B's identity, the selected paths and the
    confirmed commands, and `events.jsonl` is one of the six sources it names for a resume.
    What that costs is that no decoder type-checks them on the way back in, the way
    `read_manifest` does for every field it carries; `Confirmation.__post_init__` is the only
    thing that ever validated them. The AUTHOR is here because it was nowhere: it reached
    `materialize` and no record, so a clean-tree run — where B1 is the user's own base commit
    and forge writes no commit of its own — left nothing on disk saying whose name the
    operator agreed forge could work under. `runner.run` still takes it as an argument and
    does not yet read it back. `deep_review` is here for the same reason one process over:
    §13.1's deep review is requested by `--collect`, an hour later and in another process,
    and this record is the only copy of what the operator agreed to spend on it.
    The run's four shape numbers go the OTHER way and are in the manifest, because a launcher
    reads them to decide how many providers to spend — see `runstate.Manifest` for why those
    are not a policy — and `read_manifest` type-checks them on the way back. That launcher is
    `runner.run`: `seats` resolves to that many of `council.engine`'s providers and `attempts`
    is the per-seat retry budget; `review_rounds` and `synthesis_fix_cap` bound §12.3's
    post-review loop, which `progress.cap_remaining` reads back off the manifest. `on_calibration_failure` is read back from HERE, off this
    record, which is the only copy of it — see `runner._confirmed_policy`, and
    `cli._confirmed_deep_review` beside it for §13.1's.

    The record is WRITE-AHEAD around everything that touches the user's repository, so a
    crash between the two halves leaves the orphan §14.1 requires rather than a run directory
    that cannot say whether a ref was made in the user's repository on its behalf.
    """
    if not isinstance(report, preflight.Report):
        raise GateError(f"a preflight.Report is required, not {type(report).__name__}")
    if not isinstance(confirmation, Confirmation):
        # The type IS the check: a `Confirmation` validates every field in `__post_init__` and
        # every `verify.Step` it holds validates its own four, so this refuses the only shape
        # left — a mapping or a stand-in that never went through that door. It was once a
        # check on the class alone with no field behind it, which is how a hand-built one
        # carrying `author=None` reached `materialize`.
        #
        # `isinstance` and not `type(...) is`, so a SUBCLASS overriding `__post_init__` passes
        # here carrying nothing. Deliberate: this value is the operator's own answer to §5
        # step 2 and the operator is trusted at this gate — see `Confirmation` for the two
        # other ways past a constructor and why none of them is closed. The refusal is aimed
        # at a caller who assembled the wrong thing, never at one who set out to forge one.
        raise GateError(
            f"a Confirmation is required, not {type(confirmation).__name__}; the manifest is "
            "written once and never rewritten, so what it records has to be what a human "
            "answered rather than what a caller assembled")
    if not isinstance(run_id, str) or not run_id:
        raise GateError(f"a run needs an id: {run_id!r}")
    # §4's rejection, before anything is created. `quote_` is keyword-only with NO DEFAULT: a
    # default is how a caller opens a run against no disk check at all, and this refusal is
    # only worth having if it cannot be reached around.
    disk = refuse_for_disk(quote_)
    if disk:
        raise GateError(disk)

    blocked = preflight.refusals(report)
    if blocked:
        raise GateError(
            f"preflight refuses this repository, so there is nothing to agree to: {list(blocked)}. "
            "§2.3 fails closed before a run starts, and a manifest opened over one of these "
            "would record an agreement about a repository the engine said it could not handle.")
    spends = [f for f in provider_invoking_verify(report.facts.root,
                                                  verify.Command(confirmation.verify))
              if f.startswith(SPENDS)]
    if spends:
        # §5.2's own disposition of the three classes: a verify command that reaches a
        # provider CLI is "detected and refused", while a documented remedy is what the same
        # sentence prices "as its own explicit line" — which is `must_show`'s job and not
        # this one's. Re-detected here rather than trusted from `must_show`, because the
        # command a human confirmed is allowed to differ from the one they were shown.
        raise GateError(
            f"the confirmed verify command reaches a provider CLI: {spends}. §5.2 refuses one "
            "— it would be re-run fresh per candidate, two orders of magnitude above the "
            "quote. Point the gate at the advisory target instead.")
    # SETUP GETS THE SAME DOOR, AND THE MULTIPLIER IS THE ARGUMENT. The refusal above says a
    # verify command that reaches a provider is refused because it is "re-run fresh per
    # candidate" — and setup is re-run fresh per candidate AND per builder attempt AND in
    # calibration: `2 + (seats x attempts) + verifiers`, a LARGER multiplier than verify's.
    # `must_show` already discloses it with that count; only this door was verify-only, so the
    # identical spend was refused in one command and confirmed in the other.
    #
    # `SPENDS` ONLY, exactly as above: §5.2's three classes are dispositioned differently, and
    # a documented remedy is priced as its own line by `must_show` rather than refused here.
    setup_spends = [f for f in provider_invoking_verify(report.facts.root,
                                                       verify.Command(confirmation.setup))
                    if f.startswith(SPENDS)]
    if setup_spends:
        raise GateError(
            f"the confirmed setup command reaches a provider CLI: {setup_spends}. §5.2 "
            "refuses one that does, and setup carries a LARGER multiplier than verify — it "
            "is re-run fresh per candidate, per builder attempt, and in calibration. Move "
            "that step out of setup, or point it at the advisory target.")

    try:
        run_dir = storage.run_root(report.facts.root, run_id, must_be_new=True)
    except FileExistsError as e:
        raise GateError(
            f"run {run_id!r} already has a directory for this repository, and a second run "
            "sharing it would write over the first run's record of what it agreed to. Draw "
            "another id with `storage.new_run_id()`.") from e

    log = journal.Journal(storage.journal_path(run_dir))
    log.record(journal.intent("confirm"), operation_id=run_id)
    # `author=` is passed ALWAYS, including over a clean tree where `materialize` will not use
    # it. The alternative is this function re-deriving `dirty` to decide whether to bother,
    # which is `materialize`'s own predicate spelled a second time — and the branch where the
    # two disagree is the one that raises three writes into an opened run.
    b = baseline.materialize(report.facts.root, run_dir, report.facts,
                             list(report.selected), run_id, author=confirmation.author)
    protected, status_digest = runstate.snapshot_refs(report.facts.root, report.selected,
                                                      forge_refs=(b.ref,))
    runstate.write_manifest(run_dir, runstate.Manifest(
        run_id=run_id,
        repo_path=str(report.facts.root),
        base_commit=b.base_commit,
        baseline_ref=b.ref,
        baseline_commit=b.commit,
        tracked_tree_oid=b.tracked_tree_oid,
        # `report.selected` rather than a contained subset: an escaping entry is one of the
        # refusals above, so by here the two are the same tuple and narrowing it would only
        # hide a disagreement between what was refused and what is recorded.
        selected_paths=tuple(report.selected),
        generator_contract=report.contract,
        setup=confirmation.setup,
        verify=confirmation.verify,
        protected_refs=protected,
        forge_refs={b.ref: b.commit},
        status_digest=status_digest,
        index_digest=runstate.snapshot_index(report.facts.root),
        created_at=datetime.now(timezone.utc).isoformat(),
        seats=confirmation.seats,
        attempts=confirmation.attempts,
        review_rounds=confirmation.review_rounds,
        synthesis_fix_cap=confirmation.synthesis_fix_cap,
        concurrency=confirmation.concurrency))
    log.record(journal.done("confirm"), operation_id=run_id,
               on_calibration_failure=confirmation.on_calibration_failure,
               strategy=confirmation.strategy,
               accepted_gaps=list(confirmation.accepted_gaps),
               # THE ONE ANSWER THAT REACHED NO RECORD AT ALL. `author` went straight to
               # `materialize` and was written nowhere: not into the manifest (§14.2 lists
               # what that holds and an identity is not among them, and `_decode` refuses a
               # field it does not know) and not here — so a run could not say who authored
               # its own commits. On a DIRTY tree B1 carries it, but over a clean tree B1 is
               # the user's own base commit and forge created no commit to read it off.
               # Journalled beside the other three for the reason `open_run` gives them:
               # this record is where §5 step 2's answers live. A list because a tuple comes
               # back from json as one, and one spelling on disk beats two.
               author=list(confirmation.author),
               # §13.1's opt-out, keyed by the FIELD NAME exactly as the three above are, so
               # `cli._confirmed_deep_review` reads it back under the name this writes —
               # `runner._confirmed_policy`'s rule, and a policy read back by a name the
               # writer does not use is the same silence one field over. It is here rather
               # than in the manifest for the reason the other three are: §14.2 lists what the
               # manifest holds and a policy is not among them, and `read_manifest` refuses a
               # field it does not know.
               deep_review=confirmation.deep_review,
               # THE CONTROL-PLANE ANCHOR. `write_manifest` is exclusive only at CREATION and
               # `read_manifest` checks types, never identity — so a same-UID process, or a
               # confused cleanup script, replacing `manifest.json` with VALID JSON that
               # changes the confirmed verify command from `pytest` to `true` passes every
               # check, and a later `--collect` reports a decision nobody confirmed.
               #
               # ANCHORED HERE AND NOT BESIDE THE MANIFEST, which is the whole point: a
               # digest stored next to the file it describes is replaced with it. The journal
               # is append-only, fsynced per record, and this `done` row is written at the
               # same moment the manifest is — so it is the one copy a later rewrite cannot
               # reach without leaving the log inconsistent.
               manifest_sha256=hashlib.sha256(
                   storage.manifest_path(run_dir).read_bytes()).hexdigest())
    return run_dir
