"""The fleet, driven end to end: three builder seats, each verified somewhere it never was
(spec §4, §5 step 3, §6, §7, §8, §8.1, §9, §14.1).

Every piece below was built and tested separately, in its own module; this is the first
caller that composes them. `run_seat` chains
clone -> F0 -> setup -> Fsetup -> launch -> Fwork -> artifact set -> candidate, classifies
what came out through §8's four dimensions, and writes the seat's record where §14.2 says a
`--collect` will look for it. `verify_candidate` then takes that candidate to a tree the
builder never had and runs §6's five steps there, in §6's order. `reclassify_seat` feeds §6's
answer back into §8. `run` is the loop that calls all three, in that order, for every seat the
§5 gate priced — journalled write-ahead, driven off the run directory and nothing else, and
stopping at three verified candidates with NOTHING CHOOSING BETWEEN THEM. §10 through §13 are
built and tested; what this loop does not do is REACH them, and the front end is where that
edge belongs — a phase taken here would choose between candidates inside the loop that made
them.

**`launch` IS INJECTED, AND NOTHING IN THIS PACKAGE'S SUITE INVOKES A REAL PROVIDER.** Every
test passes a fake that writes into the seat and hands back a provider-shaped record, so the
whole chain is exercised and only the provider is not. That is deliberate — §5.2 prices a
real fleet in provider calls, and a suite that spends them is one nobody runs — but it means
NOTHING HERE PROVES THE REAL PROVIDER PATH WORKS. Nothing has invoked a real provider through
this loop yet; the skill's own eval is the first thing that will. The `launch` signature is
kept narrow enough that the real adapter is obviously the same shape:

    launch(*, name, seat_path, token, env) -> Mapping

`name` is the seat/provider name, `seat_path` is the clone the agent works in, `token` is
the per-seat sentinel §8.1 wants RECORDED (never invalidating), and `env` is
`fleet.forge_child_env`'s scrubbed environment. The return value is
`council.engine.run_provider`'s own record: `valid` decides §8's `process` dimension and
`result_text` feeds both `seat.read_proof` and the `no_change` rationale. The adapter owns
the prompt, the model and the TIMEOUT — §19 forbids a second timeout mechanism, so there is
deliberately no timeout parameter here.

WHY THE SEAT TAKES THREE INVENTORIES AND NOT §7's FOUR. §6 is explicit that `Fverify`
"cannot be 'the fourth inventory of the builder clone' when verification happens elsewhere",
and that running the confirmed command in the seat's own clone "measures nothing" — a seat
can replace `.venv/bin/pytest` with a program that exits zero. `gate.quote` prices the same
division: `setup_runs` counts the builders, `verify_runs` does not. So `Phases.fverify` is
handed `fwork` here, which is the true statement that nothing moved in this tree after the
agent exited, and `verify` stays `"not-run"` on every `Status` `run_seat` produces.

THAT IS NOT WHERE A SEAT'S STATUS ENDS, and it used to be. §8's `no_change` needs
`verify == "pass"`, so while §6's answer was only RETURNED by `verify_candidate` and never
fed back, no seat this package could produce ever reached `no_change` at all: every argued
zero-diff seat sat at `partial` permanently and §8's dimension was decorative.
`reclassify_seat` is the step that closes it — the one place §6.2's outcome is translated
into §8's `verify` dimension and `classify_seat` is asked again. It is deliberately NOT
inside `verify_candidate`: that function measures a tree, this one revises a verdict, and the
two vocabularies are different on purpose (see `_verify_dim` for what does and does not
translate). THE SEQUENCING is `run`'s, at the bottom of this file: one call, then the other,
for every seat — because an order is not a mapping between two spec vocabularies and does not
belong beside one.

WHAT NEVER HAPPENS HERE (§8.1, verbatim): *"Every retry attempt gets a fresh clone. The
failed attempt is preserved as partial input. Never a reset-and-rerun in place."* `attempt`
is a parameter, `seat_dir` puts it in the path, and this module contains no delete of any
kind — an attempt directory that already exists is a refusal, not something to clear out.
`gate.quote` counts every attempt's clone against the peak-disk figure for the same reason,
so reclaiming one would make the number the operator agreed to a lie in the other direction.
"""
import math
from concurrent import futures
from dataclasses import dataclass, replace
from pathlib import Path

from council import engine

from . import (baseline as baselinemod, bundle, fingerprint, fleet, gate, harvest, journal,
               resume as resumemod,
               runstate, seat as seatmod, seatrecord, storage, taskbundle, verify)


class RunnerError(RuntimeError):
    """This seat cannot be driven, or cannot be described honestly once it has been.

    Not the class a seat's own FAILURE arrives in — a provider that timed out, a setup that
    exited non-zero and an agent that produced nothing usable are all outcomes, and they come
    back as a `SeatResult` whose `status` says so. This is raised when there is nothing
    truthful to return at all.
    """


@dataclass(frozen=True)
class SeatResult:
    """One attempt at one seat, at rest.

    `run` is the SETUP run — the only command this module runs inside a seat — and it is
    `None` when the manifest confirmed no setup steps, which is the same thing
    `status.builder_setup == "none"` records. It is not a verify run: §6 puts that in another
    tree entirely.

    `verifier_setup` is §6's own setup run in the verifier clone, and `None` until one has
    been taken — never confusable with the field above, which is a run in a tree §6 refuses
    to take a verdict in. It is carried for `run`'s reason: `verify.run_setup` RETURNS a
    failing setup rather than raising, so a caller holding only the outcome holds no exit
    code, and `_with_setup_caveat` puts it in prose that no later phase can branch on.

    `verification_refused` is what §6 or §8 said when this seat's verification could not be
    completed, and `None` otherwise. `verification is None` alone cannot carry it: a seat §6
    refused and a seat the loop never reached both leave that field empty, and after the run
    loop stopped killing the fleet over one seat the second state stopped implying the first.
    A record that could not tell them apart would read as "not verified yet" for a verifier
    clone that was bought and spent. Nor is a SET `verification` the complement: an §8
    refusal fills it and still refuses, which is the one state where both fields are
    populated at once.

    `seat` is typed optional because a record for a seat that never got a clone would carry
    `None` there, and `run_seat` does not produce one: a clone that fails raises, because a
    status about a tree that does not exist is a verdict with no evidence under it. Every
    result this module returns therefore has a `Seat`.

    `status` is typed optional on the SAME rule, one failure over: the record written for a
    seat that could not be classified carries `None` there, because no verdict was reached
    and inventing one would be the fabricated pass this module refuses everywhere else.
    `run_seat` never RETURNS such a result — it writes the record and raises.

    `launch_result` is the provider record exactly as the injected adapter returned it, and
    `None` means the provider was never invoked — the state a seat whose setup failed is in.
    It is carried rather than reduced to the two dimensions read off it, because `status`
    holds a verdict and this holds the evidence, and §8's whole argument is that those two
    must stay separable.

    `token` is the per-seat sentinel, carried rather than left as a local. §8's rationale is
    the answer MINUS this value, so a holder of the result who cannot see the token cannot
    recompute what was measured — and `reclassify_seat` has to recompute exactly that. It
    was a `_record` parameter before, which is the same fact stated in the shape of a
    function nobody could call twice with the same answer.

    `verification` is §6.2's `(outcome, reason)` once one has been taken, and `None` until
    then. §6.2's vocabulary, not §8's: `status.verify` holds the translation and this holds
    what was actually decided, because `_verify_dim` is lossy in the direction that matters
    and a record carrying only the translation would lose which of four non-verdicts it was.
    ONCE ONE HAS BEEN TAKEN INCLUDES ONE §8 THEN REFUSED, which the sentence above used to be
    false of: taking a verdict and carrying it into a `Status` are two events, §8 can refuse
    the second having watched the first, and a field emptied on that path describes a measured
    gate — in a clone that was built, in a run that was paid for — as a measurement never
    taken. See `_verify_a_seat`.
    """
    name: str
    attempt: int
    seat: fleet.Seat | None
    status: seatmod.Status | None
    artifacts: harvest.ArtifactSet
    candidate: bundle.CandidateBundle
    run: verify.Run | None
    path: Path
    token: str
    launch_result: object = None
    verification: tuple[str, str] | None = None
    verifier_setup: verify.SetupResult | None = None
    verification_refused: str | None = None


def seat_dir(run_dir, name: str, attempt: int) -> Path:
    """Where attempt `attempt` of seat `name` is cloned.

    A directory per seat with a subdirectory per attempt, so the attempt is structurally in
    the path rather than encoded into a name that would have to be parsed back out. One
    place says this, because a resume enumerates attempt directories a different process
    created.

    Kept clear of `storage.seat_state_path`'s `seat-<name>.json` by the `seats/` component,
    so a clone and the record describing it can never land on one name.

    Validates BOTH components even though `run_seat` already has, because this is public and
    each one becomes a path component: a caller handing it `True` would otherwise get a
    directory called `attempt-True`, and one handing it `../escape` would put a seat's clone
    outside the run directory entirely — reproduced, at this helper. `runstate.count` and
    `storage.seat_state_path` are the two predicates, called rather than re-spelled. The
    second refuses a name for what it does to a FILENAME and is reused here for what it does
    to a DIRECTORY name, which is the same rule about the same character set.
    """
    _named(run_dir, name)
    return Path(run_dir) / "seats" / name / f"attempt-{_count('attempt', attempt)}"


def verifier_dir(run_dir, name: str) -> Path:
    """Where §6's verifier clone for seat `name`'s candidate is built.

    A sibling of `seats/`, never inside it: the whole claim of §6 is that this tree is one
    "the builder never had access to", and a verifier nested under the seat directory would
    be one `rm -rf seats/<name>` — or one over-broad glob in a later `--gc` — away from
    taking the evidence with it.

    NO ATTEMPT COMPONENT, and that is the priced shape rather than an omission: `gate.quote`
    counts `verifier_runs = seats + 1 + review_fixes`, one verifier per SEAT plus one per
    post-review fix, while `builders = seats * attempts`. So a second verification of the
    same seat is a run the operator was never quoted, and it lands on a directory that
    already exists — which `verify_candidate` refuses by name and `clone_seat` would refuse
    anyway, since `git clone` will not populate a non-empty destination.

    `name` is validated for `seat_dir`'s reason and by the same predicate: a public helper
    that builds a path out of an argument is the one that has to refuse a separator in it.
    """
    _named(run_dir, name)
    return Path(run_dir) / "verifiers" / name


def _named(run_dir: Path, name: str) -> None:
    """Refuse a seat name that `storage`'s own rule will not carry, before anything is built.

    Named through `storage.seat_state_path` rather than a test spelled here, and BEFORE the
    clone: a name that cannot be a filename would otherwise put a run's record somewhere
    nothing accounts for, after the checkout had been paid for. `clone_seat` refuses one that
    cannot be a BRANCH, which is a different set — `verify` is a legal branch component and
    `..` is neither.
    """
    try:
        storage.seat_state_path(run_dir, name)
    except storage.StorageError as e:
        raise RunnerError(str(e)) from e


def _agreed_baseline(manifest, baseline) -> None:
    """Refuse a baseline that is not the one this run recorded.

    Both `run_seat` and `verify_candidate` are handed a manifest AND a baseline, and both are
    the same seam: the harvest diffs against the ARGUMENT and the verifier is built from the
    MANIFEST, so a disagreement hands a gate a candidate reconstructed from a tree the run
    never agreed to — and nothing downstream can see it, because each half is internally
    consistent. `bundle.materialize` checks the CLONE against the bundle's own baseline
    commit, which closes candidate-against-tree and says nothing about which baseline the
    manifest named.
    """
    if (baseline.commit, baseline.ref) != (manifest.baseline_commit, manifest.baseline_ref):
        raise RunnerError(
            f"this baseline is not the one run {manifest.run_id!r} recorded: the manifest "
            f"names {manifest.baseline_ref} at {manifest.baseline_commit[:12]}, the argument "
            f"names {baseline.ref} at {baseline.commit[:12]}. The harvest diffs against the "
            "argument and the verifier is built from the manifest, so a mismatch hands over "
            "a candidate reconstructed from a tree the run never agreed to.")


def _count(name: str, value) -> int:
    """`runstate`'s own run-shape predicate, raised in this module's vocabulary."""
    try:
        return runstate.count(name, value, "runner.run_seat")
    except runstate.ManifestError as e:
        raise RunnerError(str(e)) from e


def _scalar(value):
    """`value` if JSON can carry it and `write_seat` will read it back unchanged, else None.

    The seat record is assembled from an injected adapter's return value, so every field
    off it is untrusted shape. `write_seat` refuses a payload that does not survive its own
    round trip and refuses NaN/Infinity outright, and a whole seat record lost to one stray
    float is a crash where the honest answer is a missing field. Fails closed to `None`,
    which reads as "not recorded" rather than as a value.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _result_text(result) -> str:
    """The provider's own answer text, or "" when there is none to read.

    ONLY the extracted answer — never the raw stdout. Both `seat.read_proof` and §8's
    `no_change` rationale are read from this, and the sentinel instruction is part of the
    PROMPT: a provider that echoes its prompt into stdout would prove it read the task by
    quoting a token the engine put there itself. "" fails both readings closed.
    """
    if not isinstance(result, dict):
        return ""
    text = result.get("result_text")
    return text if isinstance(text, str) else ""


def _rationale(answer: str, token: str) -> str:
    """The seat's own argument: its answer with the text FORGE supplied removed.

    MEASURED, and it defeats §8's rule outright without this. `make_sentinel` returns
    `SENTINEL-` plus twelve hex characters — 21 — and `seat._MIN_RATIONALE_CHARS` is 10, so
    a seat that signs off with "ok" and obeys the proof-of-reading instruction hands
    `classify_seat` a 24-character "rationale" and its `no_change` claim clears the
    substantive bar on the strength of a token the ENGINE told it to print. §8 asks for an
    argued conclusion; text forge itself demanded is not the seat's argument, and counting it
    is a verdict reading cleaner than its evidence.

    THE TOKEN WAS NEVER THE ONLY SUCH TEXT. `engine.apply_sentinel` prefixes the task with
    `SENTINEL_NOTE`, 280-odd characters of instruction, and a seat that quotes the
    instruction back clears a 10-character floor on nothing it wrote — the same finding at
    the paragraph the token arrived wrapped in. The note is removed FIRST, because it
    contains the token: strip the token first and the note no longer matches itself.

    WHAT THIS DOES NOT DO, so the next reader does not assume it: it removes the note's
    EXACT text as `apply_sentinel` writes it. A reflowed or paraphrased echo survives, and no
    10-character floor is a plagiarism detector. It closes the one echo the engine's own
    instruction makes free, which is the one every seat is invited to make.

    Case-insensitively, because `seat.read_proof` folds case when it looks for the same
    token: if a differently-cased echo counts as proof, it has to count as the engine's text
    here too, or one spelling would be proof AND rationale at once.

    Removed rather than merely discounted by length: the instruction says "on its own line",
    and a rule that subtracted 21 characters would still credit a seat that pasted the token
    three times.

    THE RULE ITSELF LIVES IN `fingerprint.without_engine_text`, because §11's nonce-stripped
    semantic hash needs the identical strip and two spellings of one predicate eventually
    disagree. This function is the §8 reading of it; the docstring above is why the rule is
    what it is, and it stays here because this is where it was measured.
    """
    return fingerprint.without_engine_text(answer, token)


def _process(result) -> str:
    """§8's `process` dimension from the adapter's record.

    `is True`, not truthiness: a record whose `valid` is a non-empty string, or that is not
    a mapping at all, is a measurement this engine could not take, and §8 has no third value
    for that — so it takes the one that cannot promote a seat. A launch that never ran
    (`result is None`, the setup-failed path) reads `invalid` by the same rule, which agrees
    with the verdict rule 2 reaches for that seat anyway.
    """
    return "valid" if isinstance(result, dict) and result.get("valid") is True else "invalid"


def _artifacts_usable(candidate: bundle.CandidateBundle) -> str:
    """§8's `artifacts` dimension: does the candidate carry anything at all.

    "Usable" is asked of the BUNDLE, not of the path set, because the path set is the seat's
    claim and the bundle is what a verifier will actually receive: a seat whose every changed
    path was omitted (a submodule's contents, a FIFO, an escaping symlink) claims work that
    nothing downstream can run, and §8 gives that no partial credit.

    A bundle carrying SOME of the work is `usable` and its gaps stay in
    `CandidateBundle.omitted`, which §6.2 turns into `HARVEST_INCOMPLETE` — a harvesting
    gap, explicitly "not a candidate defect". Reading it as `unusable` here would pre-empt
    that classification with a `failed` seat and lose the distinction §6.2 exists to draw.
    """
    return "usable" if (candidate.tracked_patch or candidate.sidecars) else "unusable"


# How much of one text field reaches the record. A seat record is read by a human working
# out what a run did, and an `npm ci` log is megabytes of it — but a field silently cut to
# length is a record reading cleaner than its evidence, so every clipped field is written
# beside its true length in characters. The TAIL is kept for both kinds of text this holds:
# a command's failure is at the end of its output, and an agent's conclusion — the sentence
# §8 measures as the rationale — is at the end of its answer.
_MAX_RECORDED_CHARS = 20_000


def _clip(text) -> tuple:
    """`text` capped at `_MAX_RECORDED_CHARS`, and how long it really was."""
    text = text if isinstance(text, str) else ""
    return text[-_MAX_RECORDED_CHARS:], len(text)


def _prompt_identity(launch_result):
    """§11's fingerprint as the launcher returned it, type-checked here.

    `_prior_attempts` type-checks `attempts` on the read side for the same reason: a damaged
    record costs nothing yet at the one point where the producer is still present.
    """
    if not isinstance(launch_result, dict) or launch_result.get("prompt_identity") is None:
        return None
    try:
        return fingerprint.as_row(
            fingerprint.from_row(launch_result["prompt_identity"]))
    except fingerprint.FingerprintError as e:
        raise RunnerError(f"this launch returned an unreadable prompt_identity: {e}") from e


def _record(result: SeatResult) -> dict:
    """ONE ATTEMPT at one seat, as §14.2's per-seat atomic file wants it.

    Lists, never tuples: `write_seat` decodes what it is about to write and refuses a payload
    that comes back a different type, and every sequence on an `ArtifactSet` or a
    `CandidateBundle` is a tuple.

    The candidate is recorded by SHAPE — patch length, sidecar names, omissions, and §6.1's
    gate measurement — not by payload. Nothing serializes a `CandidateBundle`, so this file
    says what the candidate was, and the seat clone beside it is where the bytes still are.

    `gate_delta` AND `gate_surface` GO TOGETHER OR NOT AT ALL, on
    `bundle.with_gate_measurement`'s own rule: a delta with no surface beside it cannot say
    whether `()` means "the gate was measured over four files and none moved" or "nothing
    was looked at", and `None` for both is the third state — nobody measured. They are
    `None` on every record until §6 has run, because `run_seat`'s bundle is the
    PRE-verification one and only `verify.build_verifier` has the two trees the measurement
    needs. `reclassify_seat` is what carries the measured bundle back here; before it did,
    §6.1's whole measurement was taken in `verify_candidate` and dropped at its call site.

    WHAT EACH TEXT FIELD IS DOING HERE, because the rule that put them here is the one this
    record kept failing: any conclusion that survives in a `SeatResult` has to survive in the
    record, since the record is what a later phase and a `--collect` reconstruction actually
    read. `result_text` is §8's rationale — the seat's ARGUMENT that the task needs no edit,
    which §8 says must not be discarded and which this file used to omit entirely, discarding
    it on disk instead of in memory. The setup run's `stdout`/`stderr` are the other half:
    `setup == "fail"` is one of the two ways §8 lands a seat on `failed`, a command's output
    is not a file it leaves in the tree, so a record holding only the exit code sends a
    reader to a clone that never had the answer. `sentinel` is beside `result_text` because
    the rationale is the answer MINUS the token and neither is recomputable alone.

    WHAT IS DELIBERATELY NOT HERE: F0 and Fsetup. Their only consumer downstream is the
    `Fsetup -> Fwork` difference they were taken to produce, which IS recorded — as
    `artifacts.paths`, per attempt, for every attempt. Persisting two inventories of a tree
    `Quota.for_harvest` sizes at 200,000 files to re-derive a path set already written would
    be the expensive way to hold the same fact.
    """
    s, a, c = result.status, result.artifacts, result.candidate
    answer, answer_chars = _clip(
        result.launch_result.get("result_text")
        if isinstance(result.launch_result, dict) else None)
    out, out_chars = _clip(result.run.stdout if result.run else None)
    err, err_chars = _clip(result.run.stderr if result.run else None)
    return {
        "attempt": result.attempt,
        "path": str(result.path),
        "branch": result.seat.branch if result.seat else None,
        "sentinel": result.token,
        "status": None if s is None else {
            "process": s.process, "artifacts": s.artifacts,
            "proven_read": s.proven_read, "forge": s.forge,
            "builder_setup": s.builder_setup, "verify": s.verify},
        "verification": None if result.verification is None else {
            "outcome": result.verification[0], "reason": result.verification[1]},
        "verification_refused": result.verification_refused,
        "setup_run": None if result.run is None else {
            "exit_code": result.run.exit_code,
            "step_index": result.run.step_index,
            "duration_sec": _scalar(result.run.duration_sec),
            "stdout": out, "stdout_chars": out_chars,
            "stderr": err, "stderr_chars": err_chars,
        },
        # The verifier's own setup, by exit code and overlap rather than by output: unlike
        # the seat's setup above, a non-zero one here is not a verdict about the seat — §6.2
        # has no outcome for it and `_with_setup_caveat` deliberately does not invent one —
        # so what a later phase needs is the fact that it happened and what it exited.
        "verifier_setup": None if result.verifier_setup is None else {
            "exit_code": result.verifier_setup.run.exit_code,
            "step_index": result.verifier_setup.run.step_index,
            "overlap": list(result.verifier_setup.overlap),
        },
        "artifacts": {
            "paths": list(a.paths),
            "origin": dict(a.origin),
            "setup_overlap": list(a.setup_overlap),
            "verify_overlap": list(a.verify_overlap),
        },
        "candidate": {
            "baseline_ref": c.baseline_ref,
            "baseline_commit": c.baseline_commit,
            "tracked_patch_bytes": len(c.tracked_patch),
            "sidecars": [e.path for e in c.sidecars],
            "omitted": list(c.omitted),
            "generator_contract_id": c.generator_contract_id,
            "gate_delta": None if c.gate_delta is None else list(c.gate_delta),
            "gate_surface": None if c.gate_surface is None else list(c.gate_surface),
        },
        "launch": None if not isinstance(result.launch_result, dict) else {
            **{k: _scalar(result.launch_result.get(k))
               for k in ("status", "reason", "valid", "structured", "exit_code",
                         "duration_sec", "attempts")},
            "result_text": answer, "result_text_chars": answer_chars,
        },
        # §11's four values, ALWAYS PRESENT AS A KEY. `None` is a launch that returned no
        # fingerprint — §8's `proven_read`/`partial` rule: a measurement never taken is
        # recorded as absent, never omitted, because an omitted key and an unmeasured one read
        # the same to `--collect` and only one of them is a run that lost a measurement.
        # A MALFORMED one raises: "nobody measured" and "somebody wrote nonsense" are
        # different records and only one is safe to act on.
        "prompt_identity": _prompt_identity(result.launch_result),
    }


def _prior_attempts(run_dir: Path, name: str, attempt: int) -> list:
    """Every attempt this seat has already recorded, and a refusal if `attempt` is among them.

    READ BEFORE ANYTHING IS BUILT, which is what makes the append below safe to write. §8.1
    requires the failed attempt "preserved as partial input", and the seat's record is keyed
    by NAME — one file per seat, which `storage.seat_names` enumerates and
    `runstate.reconstruct` reads — so a retry that rewrote it would erase its predecessor's
    path set and patch size, the only description of a clone a later phase can interpret. The
    file therefore holds every attempt rather than the latest, and this reads what is there.

    KEYED BY ATTEMPT INSIDE ONE FILE, not spread across a file per attempt, and the choice is
    the reader's rather than the writer's: `seat_names` globs `seat-*.json` and `reconstruct`
    reads exactly one record per seat, so a second file would be evidence NOTHING enumerates
    — the failure mode this whole fix exists to close, rebuilt one directory over. One file
    also means one atomic publish per attempt, so there is no window in which two records of
    the same seat disagree.

    A DAMAGED PRIOR RECORD IS A REFUSAL, and taking it here is what keeps that from costing
    the current attempt: at this point nothing has been cloned and no provider has been paid,
    so the refusal is free. Read after the work, it would have forced a choice between losing
    attempt 1 and losing attempt 2.

    "DAMAGED" IS `_payload`'S DEFINITION, NOT A WEAKER ONE, and it was weaker. This function
    asked only that `attempts` be a list of dicts while `_payload` runs the whole
    `seatrecord.decode` schema over `[*priors, this]` — so a prior attempt missing a field, or
    carrying a malformed `prompt_identity`, passed here and failed at the write, which is AFTER
    the clone, the setup command and the provider call. The refusal that is free before the
    work is the same refusal that is expensive after it; running the reader's own schema here
    is what puts it on the cheap side. It is also the only reading that makes the paragraph
    above true rather than aspirational.

    An `attempt` already recorded is refused for the reason its clone directory is: nothing
    in this module deletes, so an overwrite is the one way §8.1's evidence can still be lost.
    The clone check answers the TREE and this answers the RECORD; they are the same rule at
    two layers, and a tree moved out from under the run passes the first and not this one.
    """
    try:
        row = runstate.read_seat(run_dir, name)
    except runstate.StateError as e:
        raise RunnerError(
            f"seat {name!r} has a record that cannot be read, so an attempt appended to it "
            f"would silently drop what it already held: {e}") from e
    if row is None:
        return []
    priors = row.get("attempts")
    if not isinstance(priors, list) or not all(isinstance(x, dict) for x in priors):
        raise RunnerError(
            f"seat {name!r} has a record this module did not write: its 'attempts' is "
            f"{type(priors).__name__}, not a list of attempt objects. Appending to it would "
            "publish a record whose earlier half nothing can read.")
    try:
        seatrecord.decode(row)
    except seatrecord.SeatRecordError as e:
        raise RunnerError(
            f"seat {name!r} has a record its own reader refuses, so appending to it would "
            f"publish a record whose earlier half nothing can read: {e}") from e
    if any(x.get("attempt") == attempt for x in priors):
        raise RunnerError(
            f"attempt {attempt} of seat {name!r} is already recorded at "
            f"{storage.seat_state_path(run_dir, name)}. §8.1 preserves the failed attempt as "
            "partial input, so a record is appended and never replaced — pass the next "
            "attempt number.")
    return priors


def _payload(name: str, attempts: list) -> dict:
    """The ONE shape a seat record is written in, proved to decode before it is published.

    BOTH WRITERS COME THROUGH HERE, and that is the whole point. `runstate.write_seat` enforces
    no schema by design — §14.2 assigns the record's fields to the orchestrator, so that module
    refuses to be the authority on a record it does not own — and `seatrecord` is the reader's
    schema, so running it at the writer fails a dropped or renamed field where the producer is
    still present rather than hours later on a resume.

    A check installed at `_write` alone would validate nothing that matters. `_revise` is the
    writer on `reclassify_seat`'s success path and on BOTH of `_verify_a_seat`'s refusal paths,
    which is every post-verification record — including the one carrying §6.1's recovered gate
    measurement and the verifier's setup. A validation added at one writer while a second
    writer exists is not a validation.

    `SeatRecordError` is WRAPPED. `_drive_a_seat` catches `RunnerError` alone and this module's
    callers are documented as facing that one class for a record it will not act on; a new
    exception type escaping `run_seat` and `_verify_a_seat` would pass straight through the
    containment both of them rely on, ending a run over one seat's damaged record.
    """
    payload = {"name": name, "attempts": attempts}
    try:
        seatrecord.decode(payload)
    except seatrecord.SeatRecordError as e:
        raise RunnerError(
            f"seat {name!r}: this module built a record its own reader refuses: {e}") from e
    return payload


def _write(run_dir: Path, priors: list, result: SeatResult) -> None:
    """Publish the seat's record: everything it had already recorded, plus this attempt.

    THE BUNDLE IS WRITTEN BESIDE IT, AND THAT IS WHAT MAKES A RESUME POSSIBLE. `_record`
    stores the candidate by SHAPE — patch length, sidecar names — because when it was written
    nothing serialized a `CandidateBundle`. `bundle.dumps` does, so the bytes no longer have
    to be re-derived from a clone whose state nobody can vouch for: the file holds the
    candidate AND the Fwork binding it was built against, so a later reader can prove what it
    loaded is what this seat produced.

    A FAILURE TO WRITE IT IS NOT A FAILURE OF THE SEAT. The record is §14.2's deliverable and
    the run is complete without the bundle; refusing here would lose a paid-for seat over a
    convenience for a resume that may never happen. It is recorded on the seat instead, so a
    resume finds an explicit absence rather than an unexplained one.
    """
    runstate.write_seat(run_dir, result.name,
                        _payload(result.name, [*priors, _record(result)]))
    path = storage.seat_bundle_path(run_dir, result.name)
    try:
        path.write_bytes(bundle.dumps(result.candidate))
    except (OSError, bundle.BundleError) as e:
        # THE ABSENCE EXPLAINS ITSELF. Nothing in this package logs, and a missing bundle with
        # no cause beside it is the "could not look" that reads as "nothing found" — a resume
        # would report this seat unresumable without being able to say why. Written next to
        # where the bundle would have been, so the two are found together or not at all.
        try:
            path.with_suffix(".bundle-error").write_text(
                f"this seat's candidate bundle was not persisted, so resuming it would have "
                f"to re-spend its provider call: {type(e).__name__}: {e}\n")
        except OSError:
            # The directory itself is unwritable, which the seat record above would already
            # have failed on. Nothing further to record it WITH, and the seat is complete.
            pass


def _materialize_the_task(run_dir: Path, seat_path) -> None:
    """§20's bundle into this seat, before anything else runs in it.

    BEFORE `F0`, AND THE ORDER IS NOT WHAT KEEPS IT OUT OF THE ARTIFACT SET.
    `taskbundle.task_dir` puts the bundle under the seat's GIT DIRECTORY, and `harvest.record`
    walks with `snapshot.take`'s `.git` skip — so the inventory never sees it whatever the
    order is. What the order buys is that the bundle is in place before the confirmed SETUP
    command runs, which is the first thing in the seat that could read it.

    A BUNDLE THAT CANNOT BE READ STOPS THE SEAT HERE, before `launch`. §5.2 quotes the provider
    calls; spending one on a seat that was handed no task is the expensive half of this
    refusal, and `read_task_bundle_if_recorded` is what keeps "not recorded" and "not readable"
    from arriving at this line as the same value.

    NOT AN ANSWER ABOUT THE CLONE. Nothing here is written into the worktree, so a seat whose
    bundle was laid down and a seat that recorded none are the same tree to `clone_seat`'s
    manifest check, which has already run by the time this is called.
    """
    b = taskbundle.read_task_bundle_if_recorded(run_dir)
    if b is None:
        return
    taskbundle.materialize(b, storage.task_source_path(run_dir), seat_path)
    # Read back what was written rather than trusting the writer. `materialize` copies from the
    # SOURCE while `verify_materialized` re-derives from the SEAT, so they walk the same
    # manifest from opposite ends — which is the only way "laid down" and "laid down what the
    # manifest describes" stop being one claim. `_read_entry` reads the source live, so a byte
    # that moved between the scan and now arrives in the seat unnoticed without this line.
    taskbundle.verify_materialized(b, seat_path)


def run_seat(manifest, run_dir, baseline, *, name, attempt, identity, launch) -> SeatResult:
    """Drive one attempt at one seat and record what it produced.

    The order is §7's, and every step of it is load-bearing:

      clone -> task bundle -> F0 -> setup -> Fsetup -> launch -> Fwork -> artifact set
      -> candidate

    The task bundle sits where it does for one reason only: it must be in place before the
    confirmed SETUP command runs, which is the first thing in this seat that could read it.
    It is not what keeps the bundle out of the artifact set — `_materialize_the_task` says
    what does.

    F0 and Fsetup are two inventories of the same tree with only the confirmed setup command
    between them, which is what lets `harvest.artifact_set` DIFFERENCE setup's output back
    out of the agent's path set. Collapsing them — reusing F0 as Fsetup — hands every
    `node_modules` file over as the agent's work.

    `manifest` supplies the repository, the confirmed setup steps and the run's
    `GeneratorContract`; `baseline` supplies what the seat is cloned from and what the
    harvest diffs against. Both carry B's identity, and they are checked against each other
    before anything is created: a baseline that is not the one the manifest recorded would
    put a verifier in front of a candidate diffed from a different tree, with nothing
    downstream able to see it.

    Raises `RunnerError` for an argument this function will not act on, for a seat that
    cannot be classified, and — through `_payload` — for a record this module's own reader
    refuses, which is the one new way this can refuse and is still that same class.
    `fleet.SeatError`/`GitError` (the clone), `taskbundle.TaskBundleError` (a recorded task
    bundle that could not be read, or that did not arrive in the seat as its manifest
    describes), `harvest.HarvestError` (an inventory over quota), `verify.VerifyError` (a setup
    step that would not start or timed out) and whatever the injected `launch` raises all
    propagate UNWRAPPED — each already names the tree and the argument at fault. Past the clone
    the seat survives every one of them, because nothing in this module deletes anything; the
    clone's own failure is the exception, and it is `clone_seat` that cleans up after its
    refusal, so that a seat it never finished building does not outlive the refusal as a
    half-populated tree. `materialize` unwinds its own half-written task directory on the same
    terms and leaves the clone standing, which is what lets §8.1's fresh-attempt retry proceed.
    """
    run_dir = Path(run_dir)
    attempt = _count("attempt", attempt)
    _named(run_dir, name)
    _agreed_baseline(manifest, baseline)

    path = seat_dir(run_dir, name, attempt)
    if path.exists():
        # §8.1: a retry gets a FRESH clone and the failed attempt is preserved as partial
        # input. Both halves are this one refusal — nothing here clears the directory, so a
        # repeated attempt number is a caller bug reported rather than an earlier attempt's
        # evidence quietly overwritten.
        raise RunnerError(
            f"attempt {attempt} of seat {name!r} already has a clone at {path}. §8.1 gives "
            "every retry a fresh clone and preserves the failed attempt as partial input, so "
            "this is never cleared and reused — pass the next attempt number.")
    # The same rule about the record rather than the tree, and read here so the append at the
    # end of this function cannot be the thing that discovers a problem — see `_prior_attempts`.
    priors = _prior_attempts(run_dir, name, attempt)

    repo = manifest.repo_path
    st = fleet.clone_seat(repo, baseline, path, name=name, identity=identity)
    # §20's bundle, into the clone `clone_seat` has just verified against B's manifest and
    # before anything in the seat runs. `template_dir` is deliberately not used for this — see
    # `taskbundle`'s module docstring for the three measured ways git's template path cannot
    # carry a closure.
    _materialize_the_task(run_dir, st.path)
    child_env = fleet.forge_child_env(repo)

    f0 = harvest.record(st.path)

    setup = verify.Command(steps=manifest.setup)
    if setup.steps:
        run = verify.run_command(st.path, setup, env=child_env)
        setup_dim = "pass" if run.exit_code == 0 else "fail"
    else:
        # `run_command` refuses a command with no steps rather than reporting exit 0 for a
        # gate that ran nothing, and this is the other side of that refusal: a run whose
        # confirmation named no setup gets `none`. Never a fabricated pass — `"none"` says
        # there was no command, not that one passed, and §8 rule 2 still lands a seat on
        # `failed` for the only setup reading that condemns it.
        #
        # NOT `"not-run"`, which this recorded until an argued zero-diff seat in a
        # no-toolchain repository was measured end to end: §8 reads `"not-run"` as a
        # withheld measurement, so §8 rule 3 refused the re-classification and the refusal
        # came out of `run` — three providers paid, the two seats behind this one never
        # verified. THIS LINE IS THE ONLY PRODUCER OF `"none"` and the branch above is the
        # only producer of `"pass"`/`"fail"`; nothing in this module produces `"not-run"`
        # for `setup` at all, since a confirmed command is always run here and one that was
        # not confirmed is this branch. §8 keeps the value because it is a legal thing for
        # ANOTHER caller of `classify_seat` to report, and rules on it by degrading.
        # `reclassify_seat` carries whichever value this decided through and takes none of
        # its own, so the distinction is settled once, where `manifest.setup` is in scope.
        run, setup_dim = None, "none"

    fsetup = harvest.record(st.path)

    token = engine.make_sentinel()
    result = None
    if setup_dim != "fail":
        result = launch(name=name, seat_path=st.path, token=token, env=child_env)
    # A seat whose setup failed is `failed` under §8 rule 2 whatever it produces next, so the
    # provider call could only spend money and move the tree — §5.2 quotes those calls and
    # this is the one place a seat can decline to make one without changing its verdict.
    # `Fwork` is still taken: with no launch it equals `Fsetup`, and taking it is what makes
    # that an OBSERVATION rather than an assumption this function wrote into the record.

    fwork = harvest.record(st.path)

    # `fverify=fwork` — see the module docstring. No verify runs in a builder seat, so
    # nothing moved after the agent exited, and this says exactly that: `snapshot.diff` of a
    # tree against itself is empty, so no path is labelled verify-origin and `verify_overlap`
    # is empty because there was no verify phase here, not because one was skipped.
    phases = harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=fwork)
    artifacts = harvest.artifact_set(phases, st.path, baseline.commit)
    candidate = bundle.build(st.path, artifacts, baseline,
                             contract=manifest.generator_contract)

    answer = _result_text(result)
    common = dict(name=name, attempt=attempt, seat=st, artifacts=artifacts,
                  candidate=candidate, run=run, path=path, token=token,
                  launch_result=result)
    try:
        status = seatmod.classify_seat(
            process=_process(result),
            artifacts=_artifacts_usable(candidate),
            proven_read=seatmod.read_proof(answer, token),
            # §7.1's path set IS the change predicate: `Fsetup -> Fwork` is "net state
            # present when the builder exited", so an empty one is a seat that left the tree
            # as setup handed it over.
            changed=bool(artifacts.paths),
            builder_setup=setup_dim,
            # Never anything else from this module. §6 verifies in a clone the builder never
            # had, so at harvest time this measurement has not been taken — and §8 has a rule
            # for one that has not.
            verify="not-run",
            # The answer MINUS the sentinel — see `_rationale`. `read_proof` above reads the
            # answer whole, because the token is exactly what it is looking for.
            rationale=_rationale(answer, token))
    except seatmod.SeatStatusError as e:
        # WRITTEN BEFORE THE RAISE, because a refusal is exactly when someone needs the
        # evidence and this message sends them to go and read it. The seat is refused over
        # what it SAID, and what it said is the provider's answer — which is nowhere in the
        # clone, so the older form of this message named a tree that had never held the
        # thing it was pointing at. `status` is None: no verdict was reached and none is
        # invented.
        _write(run_dir, priors, SeatResult(status=None, **common))
        raise RunnerError(
            f"seat {name!r} attempt {attempt} cannot be classified: {e}. Its clone is at "
            f"{path}, its answer and sentinel are recorded at "
            f"{storage.seat_state_path(run_dir, name)}, and nothing has been deleted, so "
            "the evidence is still there to be read.") from e

    result_ = SeatResult(status=status, **common)
    # AFTER the harvest, and that ordering is the whole point of writing it at all: §14.2
    # lists the per-seat file among `--collect`'s inputs, and a record written before the
    # inventories would describe a seat mid-run and then be believed by a resume that finds
    # no process behind it. `write_seat` publishes by rename, so a reader arriving during
    # this call sees the previous record whole rather than a prefix of this one.
    _write(run_dir, priors, result_)
    return result_


# §6.2's six outcomes against §8's three values for `verify`, and the map is not onto.
# Only PASS confirms the candidate and only FAIL refutes it. Each of the other four says the
# measurement produced NO VERDICT ABOUT THIS CANDIDATE: §6.2 calls HARVEST_INCOMPLETE a
# harvesting gap "not a candidate defect", BASELINE_RED_… "degraded rather than an
# equivalence", GATE_CHANGED is a fact about the gate, and FLAKY is the gate disagreeing with
# itself over a pair §6.2 refuses to convert to a pass. They are absent from this table and
# `_verify_dim` answers "not-run" for them — §8's own value for a measurement not taken.
# A RUNNER VERDICT, DELIBERATELY NOT A MEMBER OF `verify.OUTCOMES`. That vocabulary's stated
# contract is "every value `classify` can return", and `classify` cannot return this — it is
# decided here, before §6 step 4 runs at all. Putting it there would also break three totality
# tests that hold the vocabulary honest: `test_outcomes_holds_exactly_what_classify_can_return`
# AST-walks `verify.py`'s own returns; `strategy.classify_failure` raises BY DESIGN for an
# outcome it has no branch for, so §6.2 gaining a row is meant to fail loudly. `GATE_RANK` is
# contiguous 0-5 with no free integer, and the renumber this comment used to defer — it warned
# that ranking here would re-bless `GATE_CHANGED` at rank 2 as a side effect — has since been
# taken deliberately: `GATE_CHANGED` sits below `FAIL` now, and `rubric.GATE_RANK` carries the
# argument.
SETUP_REFUSED = "setup-refused"

# TOTAL AND EXPLICIT OVER §6.2's SIX OUTCOMES, rather than two entries and a `.get` default.
# Four of them reached "not-run" by falling through, and three of those DID execute the gate:
# `GATE_CHANGED` ran and the candidate rewrote what judges it, `FLAKY` ran twice and
# disagreed, `BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE` ran and found nothing the baseline did
# not already have. "The gate did not run" is a different fact from all three.
#
# THE VALUES DO NOT MOVE, AND THAT IS A DECISION RATHER THAN INERTIA. §8's vocabulary is
# ("pass", "fail", "not-run") and has no word for "ran, and the verdict is neither" — calling
# them "fail" would report a CANDIDATE failure for a gate the candidate did not fail. Nothing
# reads this value in a way that changes a verdict: §8 says outright that "passing verify is
# recorded, not required", so `forge` is identical for all three, and §16.1's seat table
# carries `verification.outcome` — the REAL §6.2 outcome — beside it.
#
# What being total buys is that the lossiness is now a written-down choice at each key. A
# `.get` default silently absorbs the seventh outcome §6.2 gains next, and this dict would go
# on answering "not-run" for something nobody had considered.
_VERIFY_DIM = {
    verify.PASS: "pass",
    verify.FAIL: "fail",
    verify.GATE_CHANGED: "not-run",
    verify.FLAKY: "not-run",
    verify.HARVEST_INCOMPLETE: "not-run",
    verify.BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE: "not-run",
}


def _verify_dim(outcome) -> str:
    """§8's `verify` dimension for a §6.2 outcome, or a refusal for something that is neither.

    FAIL-CLOSED ON THE INPUT SIDE FIRST: an outcome this engine does not produce must not
    fall through to the default, because "not-run" is a plausible-looking answer and a typo
    would be read as "no verdict yet" forever. `SETUP_REFUSED` is checked BEFORE that guard
    rather than added to `verify.OUTCOMES`: it is a runner verdict, `classify` cannot return
    it, and admitting it to the vocabulary the guard reads would make the guard accept
    exactly what it exists to refuse.

    THE TRANSLATION IS LOSSY AND THE RECORD IS NOT. Reading one of the four non-verdicts as
    "pass" would promote a seat on evidence nobody has; reading it as "fail" would make
    `classify_seat` refuse a `changed=False` seat over a contradiction that was never
    measured — §8's rule 3 raises for a verification that was TAKEN and refuted the claim,
    which none of them is. "not-run" is the only honest cell, and it is why `SeatResult`
    carries `verification` beside `status`: §6.2's own word for what happened is written to
    the record, so `verify: "not-run"` is never all the record says.
    """
    if outcome == SETUP_REFUSED:
        # AHEAD OF THE INPUT GUARD, AND NAMED RATHER THAN ADMITTED TO `verify.OUTCOMES`. This
        # is the one value reaching "not-run" having spent nothing on a gate: §6 step 4 did
        # not execute, so there is no verdict to translate and nothing was measured to lose.
        # Widening `OUTCOMES` to hold it would make the guard below accept a value `classify`
        # cannot return, which is the opposite of what the guard is for.
        return "not-run"
    if outcome not in verify.OUTCOMES:
        raise RunnerError(
            f"{outcome!r} is not one of §6.2's outcomes {verify.OUTCOMES}; §8's verify "
            "dimension is only ever set from a verdict this engine actually took")
    # NO DEFAULT. The guard above already refused anything outside `verify.OUTCOMES`, and
    # the dict is total over it — so a `KeyError` here means §6.2 gained a row and this table
    # was not updated, which is exactly the loud failure `runner`'s own note asks for.
    return _VERIFY_DIM[outcome]


def _revise(run_dir: Path, out: SeatResult) -> SeatResult:
    """Rewrite `out`'s own attempt in the seat's record, leaving every other attempt alone.

    The attempt is REPLACED in place rather than appended: this is the same attempt saying
    more, not another one. Every other attempt is carried through untouched, so §8.1's
    preserved predecessors survive a revision exactly as they survive a retry.

    Spelled once because there are now two things a seat can learn after it was harvested —
    §6's verdict, and that §6's verdict could not be taken — and a second copy of this
    read-modify-write is a second chance for one of them to lose the other's record.
    """
    try:
        row = runstate.read_seat(run_dir, out.name)
    except runstate.StateError as e:
        # Wrapped for `_prior_attempts`'s reason and by its rule: a caller of this module
        # names one class for a record it cannot act on, and a damaged one is exactly that.
        raise RunnerError(
            f"seat {out.name!r} has a record that cannot be read, so a revision written "
            f"over it would drop what it already held: {e}") from e
    attempts = row.get("attempts") if isinstance(row, dict) else None
    if not isinstance(attempts, list) or not any(
            isinstance(x, dict) and x.get("attempt") == out.attempt for x in attempts):
        raise RunnerError(
            f"seat {out.name!r} has no recorded attempt {out.attempt} to revise at "
            f"{storage.seat_state_path(run_dir, out.name)}; a revision written where no "
            "seat wrote would be a verdict with no run under it")
    runstate.write_seat(run_dir, out.name, _payload(out.name, [
        _record(out) if x.get("attempt") == out.attempt else x for x in attempts]))
    return out


def _measured(result: SeatResult, candidate, verifier_setup) -> SeatResult:
    """`result` carrying §6's own measurements, or a refusal for one that is not this seat's.

    SPELLED APART FROM `reclassify_seat` BECAUSE TWO PATHS HAVE TO ADMIT THE SAME PAIR ON THE
    SAME TERMS. §6.1's `gate_delta`/`gate_surface` live on `Verifier.candidate` — NOT on the
    bundle handed in, which is the pre-verification one `run_seat` built — and the verifier's
    own `SetupResult` exists nowhere else at all, since `run_setup` returns a failing setup
    rather than raising. Both are measured whether or not §8 goes on to accept the verdict
    they were taken beside, so `_verify_a_seat` attaches them on its refusal path too, and a
    second copy of these two checks would be a second chance for one path to admit what the
    other refuses.

    THE MEASUREMENT IS ADMITTED, THE CANDIDATE IS NOT. §6.1's whole claim is that the gate
    surface was measured over the bundle THIS seat harvested, so a bundle that differs
    anywhere else is a different candidate's measurement filed under this seat's name — and
    `_record` writes shape, so nothing downstream could see it. The comparison strips exactly
    the two fields `with_gate_measurement` fills, which is what makes "the same bundle, now
    measured" the only thing that passes.

    Both are OPTIONAL because a caller that took no such measurement must not have to invent
    one: `None` for `candidate` leaves the record's `gate_delta` and `gate_surface` at
    `bundle`'s own value for "nobody looked", and `None` for `verifier_setup` leaves it
    saying no verifier setup was taken. Neither default claims anything.
    """
    if candidate is not None and replace(
            candidate, gate_delta=None, gate_surface=None) != result.candidate:
        raise RunnerError(
            f"the candidate handed back for seat {result.name!r} is not the one this seat "
            "harvested, once §6.1's gate measurement is set aside; only the bundle "
            "`verify.build_verifier` returned for THIS candidate can describe it")
    if verifier_setup is not None and not isinstance(verifier_setup, verify.SetupResult):
        raise RunnerError(
            f"a verify.SetupResult is required, not {type(verifier_setup).__name__}; the "
            "verifier's setup reaches the record as an exit code and an overlap, and a "
            "stand-in would put an unmeasured pair there")
    return replace(result,
                   candidate=result.candidate if candidate is None else candidate,
                   verifier_setup=verifier_setup)


def reclassify_seat(run_dir, result: SeatResult, outcome: str, reason: str, *,
                    candidate: bundle.CandidateBundle | None = None,
                    verifier_setup: verify.SetupResult | None = None) -> SeatResult:
    """Re-run §8's classification with the verification §6 took, and record the result.

    THE STEP WITHOUT WHICH §8's `no_change` IS UNREACHABLE. `run_seat` classifies with
    `verify="not-run"` because §6 verifies in a tree the builder never had, and §8's rule 3
    needs `verify == "pass"`. While nothing fed §6's answer back, every argued zero-diff seat
    stayed `partial` permanently — §8 exists to stop *"a correct conclusion that the task
    needs no edit"* being discarded, and a verdict that can never be reached discards it just
    as thoroughly as deleting it would.

    WHY HERE AND NOT IN `verify_candidate`. That function measures a TREE and answers in
    §6.2's vocabulary; this one revises a VERDICT in §8's. Keeping them apart is what lets
    `_verify_dim` be explicit about a translation that is lossy, and `verify_candidate`
    carries neither a `Status` nor an `attempt`, so it cannot even name the record that has
    to be rewritten. WHY HERE AND NOT IN THE RUN LOOP: the loop's job is sequencing — call
    one, then the other — and a mapping between two spec vocabularies is not sequencing. It
    belongs beside the only other caller of `classify_seat`, which is this module.

    THE INPUTS ARE THE ORIGINAL ONES, re-read rather than re-derived. `changed` and
    `rationale` are not fields on `Status` (deliberately — see `seat`'s module docstring), so
    they come off the same `ArtifactSet` and the same answer `run_seat` used, with the same
    `_rationale` applied to them. A second, looser reading of either here would be a seat
    classified twice by two rules.

    `candidate` AND `verifier_setup` ARE THE MEASUREMENTS §6 TOOK ALONG THE WAY, admitted
    through `_measured` — which is where the rule for both now lives, because §8 refusing the
    re-classification does not unmeasure them and the run loop has to record them anyway. See
    that function for what it admits and what it will not.

    Raises `RunnerError` for an outcome that is not §6.2's, for a seat whose record this
    cannot find, for a candidate that is not this seat's own, and for a re-classification §8
    refuses — a `changed=False` claim the gate positively REFUTED is a contradiction in the
    caller's own measurements, and the record is left carrying the pre-verification verdict
    rather than rewritten to something nothing decided. THIS FUNCTION WRITES NOTHING ON THAT
    LAST PATH: the verdict §6 took is still in the caller's hands, and `_verify_a_seat` is
    what records it beside the unrevised status.
    """
    run_dir = Path(run_dir)
    if not isinstance(result, SeatResult) or result.status is None:
        raise RunnerError(
            "re-classification revises a status this module already took; a seat with none "
            "was never classified in the first place")
    measured = _measured(result, candidate, verifier_setup)
    dim = _verify_dim(outcome)
    answer = _result_text(result.launch_result)
    try:
        status = seatmod.classify_seat(
            process=result.status.process,
            artifacts=result.status.artifacts,
            proven_read=result.status.proven_read,
            changed=bool(result.artifacts.paths),
            builder_setup=result.status.builder_setup,
            verify=dim,
            rationale=_rationale(answer, result.token))
    except seatmod.SeatStatusError as e:
        raise RunnerError(
            f"seat {result.name!r} attempt {result.attempt} cannot be re-classified under "
            f"verify={dim!r} from §6.2's {outcome}: {e}. Its record still carries the "
            "verdict taken before verification, which is the one its evidence supports."
        ) from e

    return _revise(run_dir, replace(measured, status=status, verification=(outcome, reason)))


def _with_setup_caveat(reason: str, setup_run) -> str:
    """`reason` with the verifier's own failed setup appended as a separate fact.

    A setup that exits non-zero in the verifier is not a refusal — `run_setup` returns it,
    and only a tracked overlap raises — so without this the run exists nowhere in the value
    `verify_candidate` hands back, and a `FAIL` would read as "the candidate broke the gate"
    when the gate ran in a tree the confirmed setup never finished preparing. §6.2 has no
    outcome for it and this does not invent one: which side it belongs to is genuinely open,
    because a candidate really can break the setup that passed in its own clone. So it is
    recorded beside the verdict rather than made into one.

    "also", on `verify._also`'s reasoning: the outcome was already decided by the runs and
    the bundle, so a causal connective would claim something nothing here established.
    """
    if setup_run is None or setup_run.exit_code == 0:
        return reason
    return (f"{reason}; also, the verifier's own setup command exited "
            f"{setup_run.exit_code} at step {setup_run.step_index}, so this gate ran in a "
            "tree the confirmed setup did not finish preparing")


def verify_candidate(manifest, run_dir, baseline, candidate, *, name, identity,
                     calibration, on_measurement=None) -> tuple:
    """Run §6's five steps over one harvested candidate, in the order §6 gives them.

    Returns `classify`'s `(outcome, reason)`, the `Verifier` the verdict was taken in, and
    the verifier's own `SetupResult` — `None` when the confirmation named no setup command.
    The `Verifier` rather than the path, because `Verifier.candidate` is NOT the bundle
    passed in: it is that bundle with §6.1's whole measurement — `gate_delta` and the
    `gate_surface` it ranged over — filled from the two trees `build_verifier` had. A caller
    that keeps the input bundle instead holds `gate_delta is None`, which `classify` reads as
    UNKNOWN and answers `GATE_CHANGED`.

    THE SETUP RESULT IS RETURNED RATHER THAN ONLY DESCRIBED. `run_setup` returns a failing
    setup instead of raising, so before this it existed nowhere a caller could reach: the
    exit code reached an operator as prose inside `reason` and no later phase could put it in
    a record. A `Run` that survives in the function and not in the value is the same class of
    defect as a rationale that survives in memory and not on disk.

    ON THE RETURN PATH, AND NOW ALSO ON THE REFUSAL PATH. Everything here is measured in order
    — `build_verifier` fills §6.1's `gate_delta`/`gate_surface`, then `run_setup` runs — and a
    refusal from any step AFTER them used to leave both as locals this function dropped on its
    way out. Measured through the loop: a candidate that repoints `core.hooksPath` through its
    own setup script is refused at `assert_hooks_pinned`, and the seat's record came back
    `gate_delta: null, gate_surface: null, verifier_setup: null` for a verifier clone that was
    built, whose gate surface WAS measured over two trees, and whose setup ran and exited 0 —
    the very fact explaining how the hooks moved.

    `on_measurement` closes it WITHOUT CHANGING WHAT THIS FUNCTION RAISES. It is called
    `on_measurement(candidate_or_None, setup_result_or_None)` the instant each measurement
    exists, before any step that can refuse it — `journal.intent`'s write-ahead discipline,
    applied to a value rather than to an operation. A narrower `except` at the call site cannot
    do this: the values never leave this frame, and a handler reaching back for `v.candidate`
    cannot distinguish "§8 refused this status" from "this candidate is not this seat's". The
    admission rule stays in `_measured`, where it already is.

    §6'S FIVE STEPS, AND WHERE EACH ONE IS:

      1. *"Harvest the seat (§7) — before verification."* Structural, not a line here: the
         `candidate` parameter is a `bundle.CandidateBundle`, which only `run_seat`'s harvest
         produces, so a caller with no harvest has nothing to pass.
      2. *"Materialize the harvested candidate in a brand-new clone built through the same
         path as §4."* `build_verifier` — `fleet.clone_seat`, the same call `run_seat` makes,
         then `bundle.materialize` on top. Followed immediately by the hash validation, see
         below.
      3. *"Run the confirmed setup command there."* `run_setup`, when the confirmation named
         a setup command at all, refusing a tracked effect the generator contract does not
         declare (`SetupOverlap`).
      4. *"Run the confirmed verify command there."* `fixed_point`, not a bare
         `run_command`: §6.2's PASS is "exit 0 AND no unexplained tracked delta", and only
         the `FixedPoint` carries the second half — `classify` hands a caller that passed a
         bare `Run` a weaker PASS, in as many words, and this one has no reason to earn it.
      5. *"Repository hooks and any post-seat git configuration are disabled in verifier
         clones."* `build_verifier` pins `core.hooksPath` to /dev/null before the candidate
         is laid down and asserts it again afterwards, `clone_seat` builds under an empty
         template, and every `gitcmd` call pins the global and system config to /dev/null.
         THAT WAS NOT ENOUGH, and this function is where the gap was: `build_verifier`'s
         assertion is taken when the candidate is the only writer so far, and step 3 runs
         AFTER it, in the candidate's own tree, through a script the candidate may own.
         Measured — a candidate that rewrote `./setup.sh` to `git config --local
         core.hooksPath .githooks` had the gate run under hooks it supplied, and the run came
         back PASS. `verify.assert_hooks_pinned` below is that closed. It is a second READ,
         never a second pin: re-pinning would restore the property and destroy the evidence
         it had been lost, which is the loss this paragraph used to cite as the reason for
         adding nothing at all. An assertion detects, which is what the reasoning called for.

    THE HASH VALIDATION RUNS ON BOTH PATHS, and only one of the two is delegated. §6: *"The
    materialized candidate is hash-validated against the bundle before setup runs."*
    `run_setup` makes that check as its first statement and owns the ordering when there IS a
    setup command — `calibrate` relies on exactly that and deliberately does not repeat it,
    and neither does the branch below. But `run_command` refuses a command with no steps, so a
    run whose confirmation named no setup never calls `run_setup` at all, and the one
    ordering §6 states outright would be skipped entirely for every repository that needs no
    toolchain. The `else` branch below is that hole closed; it is not a duplicate check,
    because the two branches are exclusive.

    WHAT IT VALIDATES IS THE SIDECARS, and "hash validation" reads wider than that — measured
    on a candidate whose whole change was a 146-byte tracked patch, `validate_materialized`
    checked ZERO entries and passed. That is not an open hole and the mechanism is right:
    `git apply --index` fails loudly on a context mismatch, onto a clone `bundle.materialize`
    has already checked against the bundle's own baseline commit, so re-hashing the patch's
    postimages would only re-derive what git enforced. A sidecar is a raw write and has no
    such enforcement, which is why it is the half that is hashed. Said here because a
    maintainer reading only the sentence above concludes tracked content is content-checked
    at this step, and it is not — `verify.validate_materialized` says the same of itself.

    `contract` IS NOT A PARAMETER, though every function it delegates to takes one. A
    contract argument here would be a second place for one run to disagree with itself: the
    run has exactly one generator contract, `manifest` is the record of the human confirming
    it at the §5 gate, and `run_seat` already builds the bundle from
    `manifest.generator_contract`. `build_verifier` compares the candidate's contract ID
    against what it is handed, so a contract argument sharing the candidate's ID while
    carrying different RELATIONS would pass that check and change what `fixed_point` admits —
    the manifest-records-X-while-the-gate-admitted-Y shape, reachable by one wrong argument.
    THE EMPTY ID IS NOT THAT CASE, though this paragraph used to say it was:
    `inspect.GeneratorContract.__post_init__` raises for relations under an empty id, so the
    empty-id-with-relations contract it named cannot be built at all. A shared NON-EMPTY id
    with different relations is constructible, is what makes the argument dangerous, and is
    what this says now. Sourcing it from the manifest removes the argument rather than
    checking it.

    `calibration` IS REQUIRED, and it is a `verify.Calibration` rather than a `Run`. It
    carries no `None` default because there is no honest value to run with when it is
    missing. `classify` reads `baseline_run` only once the candidate's gate has already exited
    non-zero, to choose between `BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE` and `FAIL` — so a
    fabricated green `Run` would report a NEW failure that nothing measured, and a fabricated
    red one would report §6.2's baseline-red outcome, which is a claim ABOUT a calibration,
    on the evidence of no calibration at all. Both are a verdict reading cleaner than its
    evidence, in opposite directions. Nor is one ever absent in a run that gets this far: §5
    step 3 calibrates before any provider spends a token, `GeneratorUnstable` stops the run
    there, and the operator's `on_calibration_failure` policy chooses between aborting and
    carrying a RED calibration — which is still a `Calibration`. WHAT THE TYPE CHECK BUYS IS
    ONLY THE OBVIOUS MISTAKE: `Calibration` is a plain dataclass anyone can build, so it is no
    evidence about the tree its `run` came from, exactly as `verify._as_run` says of `Run`. It
    refuses a bare `Run` handed over from wherever the caller was standing, which is the
    mistake a caller actually makes.

    NO RERUN, SO `FLAKY` IS NOT REACHABLE FROM HERE. `gate.quote` prices
    `verify_runs = 2 + verifier_runs` — one gate run per verifier clone — so a second run for
    every candidate is a cost the operator was never shown. §6.2's fail->pass rerun therefore
    has no caller yet; `classify` takes `rerun` and this passes none.

    WHAT THIS DOES NOT DO IS UPDATE THE SEAT'S §8 STATUS, and that is a division of labour
    rather than the gap it used to be. The outcome is RETURNED, and the seat's record still
    reads `verify: "not-run"` until `reclassify_seat` is handed this outcome — which is the
    call that lets an argued zero-diff seat stop being `partial`. This signature could not do
    it if it wanted to: it carries no `Status` in or out and no `attempt`, so it cannot name
    the record that has to be rewritten. It measures a tree; the other revises a verdict.

    Raises `RunnerError` for an argument this function will not act on. `fleet.SeatError`,
    `bundle.BundleError`, `verify.VerifyError` and its subclasses — `ContractMismatch` for a
    candidate built under another contract, `SetupOverlap` for §6's `setup_overlap`,
    `GeneratorUnstable` for a gate with no fixed point — all propagate UNWRAPPED, on
    `run_seat`'s stated precedent: each already names the tree and the argument at fault, and
    the last two are §6's own vocabulary for a candidate failed closed and an infrastructure
    failure that is never a candidate's verdict.
    """
    run_dir = Path(run_dir)
    _named(run_dir, name)
    _agreed_baseline(manifest, baseline)
    if not isinstance(calibration, verify.Calibration):
        raise RunnerError(
            f"a verify.Calibration is required, not {type(calibration).__name__}: §6.2's "
            "BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE is a claim about the untouched "
            "baseline's own gate, and a Run from anywhere else cannot support it")

    dest = verifier_dir(run_dir, name)
    if dest.exists():
        # §6 step 2 says a BRAND-NEW clone. A second candidate materialized over the first
        # one's tree is not one, and it would be measured against a gate the previous
        # candidate had already had its hands on — the exact premise §6 exists to establish.
        raise RunnerError(
            f"seat {name!r} already has a verifier clone at {dest}. §6 materializes each "
            "candidate in a brand-new clone and §5.2 prices one verifier per seat, so this "
            "is never reused — nothing here deletes it either.")

    # One contract and one gate for the whole run, both off the manifest. The environment is
    # `run_seat`'s and `calibrate`'s: `classify` differences this run against the calibration,
    # so a candidate measured in a different environment is differencing two machines.
    contract = manifest.generator_contract
    # `command`, not `gate`: this module imports `gate` for §5 step 2's policy names, and a
    # local of that name would shadow it inside this function only — the shape where the two
    # meanings read identically at every call site and differ at exactly one.
    command = verify.Command(steps=manifest.verify)
    child_env = fleet.forge_child_env(manifest.repo_path)

    v = verify.build_verifier(manifest.repo_path, baseline, candidate, dest,
                              identity=identity, contract=contract, command=command)
    # THE FIRST MEASUREMENT EXISTS NOW. §6.1's gate_delta/gate_surface are on `v.candidate`
    # from this line onward, and everything below can refuse. Handing it over here is
    # `journal.intent`'s write-ahead discipline applied to a value.
    if on_measurement is not None:
        on_measurement(v.candidate, None)

    setup = verify.Command(steps=manifest.setup)
    if setup.steps:
        setup_result = verify.run_setup(v, setup, env=child_env)
        # §6.1 RE-READ, in the same window and for the same reason `assert_hooks_pinned` is
        # re-read below: this is the only point where candidate-owned setup has executed and
        # the gate has not started. `core.hooksPath` was the ONE post-setup fact anything
        # checked, and a stub interpreter written into `.venv/bin/` by the setup entrypoint
        # moves neither it nor any tracked path — so a candidate could author the binary its
        # own gate runs and still be told PASS. `supersedes` because the reading above was
        # taken against a tree setup had not touched yet.
        v = verify.remeasure_gate_surface(v, command=command)
        # BEFORE `assert_hooks_pinned`, which is the step the adversarial rig is refused at.
        if on_measurement is not None:
            on_measurement(v.candidate, setup_result)
        if setup_result.run.exit_code != 0:
            # §6 STEP 4 DOES NOT RUN. `_with_setup_caveat` put this in PROSE beside a
            # structured PASS, and its argument — that ATTRIBUTION is open, because a
            # candidate really can break the setup that passed in its own clone — is right
            # and is not what this decides. The verdict is a different claim: a gate that ran
            # in a tree the confirmed setup never finished preparing has not measured the
            # candidate, whoever's fault the failure was. AFTER `on_measurement`, because the
            # §6.1 reading above was bought and a refusal is exactly when someone needs it.
            return (SETUP_REFUSED,
                    f"the verifier's own setup command exited {setup_result.run.exit_code}, "
                    "so §6 step 4 was not run: a gate result from a tree setup did not "
                    "finish preparing measures the preparation, not the candidate.",
                    v, setup_result)
    else:
        # NOTHING IS HANDED OVER IN THIS BRANCH, and the asymmetry is deliberate: there is no
        # `SetupResult` to hand over, and the call above has already delivered `v.candidate`,
        # so a second call here would repeat one value and invent nothing. `validate_materialized`
        # can refuse, and the candidate it would drop is already in the sink's hands.
        verify.validate_materialized(v)
        setup_result = None

    # §6 step 5, re-read between step 3 and step 4 — see the step list above. This is the
    # only point in the run where the confirmed setup command has already executed in the
    # candidate's tree and the gate has not yet started, which is exactly the window a
    # candidate-owned setup script can move `core.hooksPath` in.
    verify.assert_hooks_pinned(v)

    fp = verify.fixed_point(v.path, command, v.contract, env=child_env)
    outcome, reason = verify.classify(fp, calibration.run, v.candidate)
    setup_run = setup_result.run if setup_result else None
    return outcome, _with_setup_caveat(reason, setup_run), v, setup_result


# --------------------------------------------------------------------------- #
# The run loop: §5 step 3, then §7 for every seat, then §6 for every candidate.
# --------------------------------------------------------------------------- #

# WHICH providers a seat count names. §5.2 prices a run by a NUMBER and `gate.confirm`
# records that number, because that is what an operator can be quoted; a number is not a
# fleet. The list is `council.engine`'s own rather than a second one spelled here: the
# adapter `launch` wraps is `engine.run_provider`, so a name invented in this module is one
# no provider answers to. Its ORDER is the fleet's order, which is why the tuple is taken
# whole rather than through a set.
_SEATS = tuple(engine.DEFAULT_PROVIDERS)

# §14's chain, as far as this module's loop drives it. `synthesizing` is the next edge and
# it is fusion, which this module deliberately does not reach — §10 through §13 have no
# implementation, so a loop that entered `synthesizing` would be claiming a phase with
# nothing in it. It is the ROUTE and not the graph: `runstate._EDGES` is still the only
# statement of what follows what, `advance` is what refuses a step this route got wrong, and
# all `_reach` reads here is whether a phase is on the route at all.
_SPINE = ("confirmed", "setting_up", "building", "harvested", "comparing")

# §14.1's write-ahead pairs, one kind per operation this loop performs. `gate.open_run`
# already uses `confirm` for the operation that opened the run; these three are disjoint from
# it, which is what lets `_refuse_a_second_pass` tell this loop's own records from the gate's.
_CALIBRATION = "calibration"
_SEAT = "seat"
_VERIFICATION = "verification"
_OPERATIONS = (_CALIBRATION, _SEAT, _VERIFICATION)

# The kind `gate.open_run` records the §5 gate under, and the field it carries. Spelled here
# because `gate` exports neither — `open_run` writes `journal.intent("confirm")` inline — and
# the alternative is reading the operator's answer out of a name this module guessed at
# silently. `_confirmed_policy` fails closed when it is not found, so a rename over there is a
# refusal here rather than a policy quietly ignored.
_CONFIRM = "confirm"
_CALIBRATION_POLICY = "on_calibration_failure"


def _op(manifest, *parts) -> str:
    """One operation's id: the run, then what inside it. Never the run id alone — `orphans`
    pairs a start with a done on (kind, operation_id), so an id naming a whole seat rather
    than one attempt would let attempt 2's done close attempt 1's start."""
    return "/".join((manifest.run_id, *parts))


def _fleet(manifest) -> tuple:
    """The seat names this run's agreed COUNT resolves to.

    Refused rather than truncated when the count exceeds the providers that exist: §5.2
    priced `seats * attempts` provider calls and the operator agreed to that number, so
    running fewer is a run they did not confirm — and running the same provider twice under
    two seat names would put two records where §14.2 has one file per seat.
    """
    if manifest.seats > len(_SEATS):
        raise RunnerError(
            f"this run agreed to {manifest.seats} seats and there are {len(_SEATS)} providers "
            f"to fill them ({list(_SEATS)}). §5.2 priced the run by that count, so neither "
            "running fewer nor giving one provider two seats is the run that was confirmed.")
    return _SEATS[:manifest.seats]


def _advance(run_dir: Path, state, phase: str):
    """One DECLARED edge, taken and recorded. `advance` is the refusal, not a check here."""
    try:
        nxt = runstate.advance(state, phase)
    except runstate.TransitionError as e:
        raise RunnerError(
            f"this run cannot move from {state.phase!r} to {phase!r}: {e}") from e
    runstate.write_state(run_dir, nxt)
    return nxt


def _reach(run_dir: Path, state, phase: str):
    """`state` at `phase`, by the one edge §14 declares between them — or already there.

    THROUGH `advance`, never by assignment: §14's graph is `runstate._EDGES` and the only
    thing that knows it is `advance`, so a loop that set `phase` directly would hold a graph
    the spec does not and nothing would say so. Every phase this function writes was therefore
    a transition §14 declares — including the ones it gets WRONG, which `advance` refuses
    rather than this function re-deriving them from `_SPINE`.

    ONE EDGE, not a walk to an arbitrary target, because the gap is always one: `run` reaches
    each phase of `_SPINE` in turn, and the only position it can be resumed from is one where
    the phase was written and the next operation was not yet journalled — which is `setting_up`
    and nothing else, since every later phase is followed immediately by a record that
    `_refuse_a_second_pass` then sees. That equal case is the no-op below and is the whole of
    what a second call can legitimately pick up.

    A phase OFF the route is refused here rather than at `advance`: `reviewing` and the five
    terminals are real phases §14 declares, so `advance` would answer about the GRAPH ("no edge
    from `ready`") for a run whose actual problem is that this loop drives no route through it.
    """
    if state.phase == phase:
        return state
    if state.phase not in _SPINE:
        raise RunnerError(
            f"this run is at {state.phase!r}, which is not on the route this loop drives "
            f"({list(_SPINE)}); §14 declares it, and what follows it belongs to a phase with "
            "no implementation here.")
    return _advance(run_dir, state, phase)


def _at_confirmed():
    """The position a run directory holding a manifest and no state record is in.

    DERIVED, not defaulted. §14.2 writes the manifest AT `confirmed` and never rewrites it,
    so a directory with one and no `state.json` has reached exactly that phase and no
    further — `gate.open_run` writes no state at all. The counters are zero for the same
    reason and not as a fallback: no round and no attempt have been spent by a run that has
    not started. `State` deliberately has no defaults so that this reasoning has to be
    written down somewhere, and this is where.
    """
    return runstate.State(phase="confirmed", round=0, attempt=0,
                          verified_checkpoint=None, deliverable_checkpoint=None)


def _source_diverged(run_dir: Path, state, moved) -> None:
    """§9: the user's repository moved, so record that and stop. Always raises.

    THE TRANSITION IS TAKEN BEFORE THE REFUSAL, which is the half a `raise` alone would lose:
    §9 says the run "transitions to `source_diverged` and does not continue to handover
    automatically", and a run that only raised would be left at whatever phase it had reached
    — a position a later reader resumes from, into the very handover §9 stopped.

    §14 declares the edge out of every non-terminal phase, so it can be taken from wherever
    the run was standing. From a terminal it cannot, and is not: a run already reported ended
    is not moved, and the refusal below still names what moved.
    """
    if state.phase in runstate.PHASES and state.phase not in runstate.TERMINAL:
        _advance(run_dir, state, "source_diverged")
    raise RunnerError(
        f"this run's repository has moved since it was agreed: {list(moved)}. §9 transitions "
        "to `source_diverged` and does not continue to handover automatically, because a "
        "clean merge can silently revert the user's own subsequent work on any hunk forge "
        "also touched.")


def _refuse_an_unknown_outcome(orphans) -> None:
    """§14.1: an operation that started and left no receipt is `outcome_unknown`, and it is
    NEVER SILENTLY RETRIED.

    Retrying is exactly what this loop would otherwise do — it is the only thing it knows how
    to do — and the operation may have completed: a seat whose provider ran for forty minutes
    and whose `_done` never landed has a clone, a record and a spent provider call behind it.
    So the run stops and says which operation it cannot classify. `runstate.reconstruct` is
    where a reader goes next; the identity fields on each event are the half of "is it still
    running" that cannot be re-derived later.
    """
    named = ", ".join(f"{e.event} {e.operation_id!r} (pid {e.data.get('pid')})"
                      for e in orphans)
    raise RunnerError(
        f"this run has {len(orphans)} operation(s) that started and recorded no result, which "
        f"§14.1 names {runstate.OUTCOME_UNKNOWN} and never silently retries: {named}. Read "
        "the run with `runstate.reconstruct` and decide; this loop will not guess whether "
        "they ran.")


# The phases whose meaning is "the providers have been paid". §5.2 prices the fleet, so past
# `building` a resume has nothing left to buy — and re-entering one of these must not walk the
# state machine backwards to re-take work whose evidence is already on disk.
_FLEET_IS_BOUGHT = ("harvested", "comparing")


def _drive_the_fleet(run_dir, manifest, base, log, *, names, identity, launch) -> list:
    """Every owed seat, `manifest.concurrency` of them at a time, in `names` order.

    THREADS AND NOT PROCESSES, because every expensive thing a seat does releases the GIL:
    `fleet.clone_seat` and the gate shell out through `subprocess`, and the provider call is a
    subprocess that this thread does nothing but wait on. A process pool would buy no
    parallelism the GIL was taking and would cost the shared `Journal` and `Baseline` a
    pickling boundary neither is built to cross.

    WHAT MAKES THIS SAFE, AND IT IS NOT THAT NOTHING IS SHARED. Three things are:
      - the JOURNAL, whose `record` now derives `seq` and appends inside one advisory lock;
        before that a parallel fleet corrupted the run's whole record, which is why this
        function could not exist.
      - the SEAT FILES, which never collide: `storage.seat_state_path` is keyed by seat name
        and `seat_dir` by name and attempt, so two builders share no path.
      - the BASELINE and the source repo, which are read-only here — clones read, and B1's
        ref was written before the fleet started.

    ORDER IS `names`, NOT COMPLETION. §8's comparison reads the fleet as a sequence, so a
    fast seat finishing first must not reorder the record: results are collected into a dict
    and re-emitted in the order the caller asked for.

    ONE REFUSAL ENDS THE FLEET, exactly as it did serially. `as_completed` would let this
    swallow an exception into a result slot; `map`-style re-raising keeps a `RunnerError` out
    of `_drive_a_seat` doing what it always did — leaving the loop — rather than silently
    becoming a seat that returned nothing.

    SERIAL AT concurrency=1 AND MEASURABLY SO: the pool is skipped entirely, so a run that did
    not ask for parallelism takes the identical path it took before this function existed and
    cannot inherit a scheduling bug from a feature it declined.
    """
    # `manifest.concurrency` DIRECTLY, never `getattr(..., 1)`. The manifest decoder refuses a
    # record missing the field, so a default here could only mask a manifest this engine failed
    # to read — and it would mask it as "the operator chose serial", which they did not.
    width = manifest.concurrency
    if width <= 1:
        return [_drive_a_seat(run_dir, manifest, base, log, name=name,
                              identity=identity, launch=launch)
                for name in names]
    out = {}
    with futures.ThreadPoolExecutor(max_workers=width,
                                    thread_name_prefix="forge-builder") as pool:
        pending = {pool.submit(_drive_a_seat, run_dir, manifest, base, log, name=name,
                               identity=identity, launch=launch): name
                   for name in names}
        # `.result()` IN SUBMISSION ORDER re-raises the first refusal, and the `with` block
        # then waits for the seats already running rather than abandoning them mid-clone —
        # which is what leaves a run directory §15 cannot describe.
        for fut, name in pending.items():
            out[name] = fut.result()
    return [out[n] for n in names if n in out]


def _refuse_a_second_pass(events) -> None:
    """Refuse a run directory this loop has already driven, whatever the outcome was.

    NOT A TIDINESS RULE. §14.1 concedes at the top that exactly-once is not deliverable and
    asks instead for a record in which never-started, partly-ran and completed are
    DISTINGUISHABLE — which the journal delivers. A blind second pass is none of those: it
    would re-take another calibration clone into a directory that already holds one, and
    another provider call per seat that §5.2 priced once, refused here before anything is
    spent rather than three clones in.

    WHAT THIS DOCSTRING USED TO CLAIM, AND WHY IT WAS WRONG. It argued no resume could ever
    exist, because "the two values a second pass would need are `verify.Calibration` and
    `bundle.CandidateBundle`, and nothing in this package serializes either". The second half
    was a true statement about what had been WRITTEN and a false one about what was POSSIBLE —
    every field of a `CandidateBundle` is plain data, and `bundle.dumps` now serializes it.
    The calibration was never the obstacle it was named as: re-taking it costs one clone and
    NO provider call, and a resume that re-measures it is comparing against a baseline this
    pass observed rather than one it inherited.

    So the refusal stands for a BLIND pass and no longer for every pass. `resume.plan`
    classifies each seat before anything is spent, and `run(..., resume=True)` takes that
    path — reconstructing what was paid for, re-driving only what was not, and refusing
    outright when a settled seat cannot be proven rather than quietly buying it twice.
    """
    mine = [e for e in events
            if any(e.event in (journal.intent(k), journal.done(k)) for k in _OPERATIONS)]
    if mine:
        raise RunnerError(
            f"this run directory already records {len(mine)} operation(s) from a previous "
            f"pass, the last being {mine[-1].event} {mine[-1].operation_id!r}. Driving it "
            "again from the top would re-spend the provider calls §5.2 quoted once. Resume it "
            "instead — `forge --resume <run-id>` reloads every seat that already settled and "
            "re-drives only the ones that never did.")


def _confirmed_policy(events, manifest) -> str:
    """The operator's §5 step 2 answer for a baseline that fails its own gate.

    IT IS IN THE JOURNAL AND NOT THE MANIFEST — `gate.open_run` says so, and records it on the
    `confirm` operation's own done record. That is the only copy, so this reads it there, and
    FAILS CLOSED when it is missing or is not one of §5 step 2's two branches: neither has a
    safe default, `gate.confirm` refuses to invent one, and a loop that carried on past a red
    baseline because it could not find the answer would be making the operator's decision
    quietly and in the direction that spends money.
    """
    done = journal.done(_CONFIRM)
    for e in reversed(events):
        if e.event == done and e.operation_id == manifest.run_id:
            policy = e.data.get(_CALIBRATION_POLICY)
            if policy in gate.CALIBRATION_POLICIES:
                return policy
            raise RunnerError(
                f"this run's {done} record carries {_CALIBRATION_POLICY}={policy!r}, which is "
                f"not one of §5 step 2's {list(gate.CALIBRATION_POLICIES)}")
    raise RunnerError(
        f"this run's journal has no {done} record for {manifest.run_id!r}, so what the "
        "operator answered about a baseline that fails its own gate is not recorded anywhere. "
        "§5 step 2 has no safe default, so this loop will not choose one.")


def _calibrate(run_dir: Path, manifest, base, log, *, identity, env):
    """§5 step 3, journalled: the confirmed commands on the untouched baseline.

    The ORDER is `run`'s and not this function's, and it is what §5 step 3 is for: calibrating
    before a provider spends a token means a gate with no fixed point stops the run at the cost
    of one clone rather than after three providers have been paid.
    """
    op = _op(manifest, _CALIBRATION)
    log.record(journal.intent(_CALIBRATION), operation_id=op)
    cal = verify.calibrate(manifest.repo_path, base, run_dir / "calibration",
                           identity=identity, contract=manifest.generator_contract,
                           setup=verify.Command(steps=manifest.setup),
                           command=verify.Command(steps=manifest.verify), env=env)
    # THE BASELINE'S OUTPUT, NOT ONLY ITS EXIT CODE. §12.3 asks which failures are NEW, and
    # `progress.from_runs` answers it by parsing the candidate's gate output against the
    # BASELINE's — so a record carrying only `exit_code` leaves the baseline half of every
    # progress question unanswerable, and `oscillation` cannot form a pair without it. This
    # is the only copy: the calibration runs once, hours before any review, in a clone `--gc`
    # reclaims.
    #
    # CLIPPED, AND THE TRUE LENGTH BESIDE IT, on `_clip`'s own rule. A clipped tail is a
    # LOWER BOUND on what failed — pytest prints its summary at the end, so the tail is the
    # right half to keep, but a FAILED line that scrolled past the cap is one nobody can see.
    # `fix.baseline_run` therefore refuses a clipped record rather than parsing it, because a
    # subset of the failures reported as the whole set is the fail-open `progress` exists to
    # close.
    cal_out, cal_out_len = _clip(cal.run.stdout)
    cal_err, cal_err_len = _clip(cal.run.stderr)
    log.record(journal.done(_CALIBRATION), operation_id=op,
               exit_code=cal.run.exit_code,
               setup_exit_code=None if cal.setup is None else cal.setup.exit_code,
               stdout=cal_out, stdout_chars=cal_out_len,
               stderr=cal_err, stderr_chars=cal_err_len,
               unexplained=list(cal.unexplained))
    return cal


def _obey_the_calibration_policy(run_dir: Path, state, manifest, events, cal) -> None:
    """Abort or carry on, as §5 step 2 was answered. Raises on `abort`.

    THE GATE'S OWN EXIT CODE IS THE SUBJECT, literally: §5 step 2 asks what to do "when the
    untouched baseline fails its own gate". A calibration whose SETUP exited non-zero is a
    different fact and is deliberately not read here — it is carried on `Calibration.setup`,
    and reading it as a gate failure would abort runs the operator never agreed to abort.
    That leaves it unread by this loop, which is stated rather than implied.

    `failed` and not `degraded`: §14's `degraded` is the ending of a run that CONTINUED and
    reached a review, and `reviewing` is the only phase that declares an edge to it. A run
    that stopped in `setting_up` never got there, so `failed` is the ending it actually had.
    """
    if cal.run.exit_code == 0:
        return
    if _confirmed_policy(events, manifest) != "abort":
        return
    _advance(run_dir, state, "failed")
    raise RunnerError(
        f"the untouched baseline fails its own confirmed gate (exit {cal.run.exit_code}) and "
        "this run's operator answered `abort` at §5 step 2. Nothing has been spent on a "
        f"provider; the calibration clone is at {cal.path}.")


def _drive_a_seat(run_dir: Path, manifest, base, log, *, name, identity, launch):
    """Every attempt this seat is allowed, until one settles. The result, or None.

    §8.1, and each half is somewhere: *"Every retry attempt gets a fresh clone"* is
    `run_seat`'s `attempt` parameter and `seat_dir`'s path component; *"the failed attempt is
    preserved as partial input"* is that nothing in this module deletes; *"never a
    reset-and-rerun in place"* is the refusal `run_seat` raises for a repeated attempt. This
    function is only the budget — `manifest.attempts`, which §5.2 priced as
    `builders = seats * attempts` and the operator agreed to.

    A RETRY IS SPENT ON `failed` AND NOTHING ELSE. §8 lands a seat there for an invalid
    process or a setup failure, which are the two states where the seat produced nothing
    trustworthy; `partial` is a seat whose work §8 explicitly keeps ("a correct conclusion
    that the task needs no edit must not be discarded"), so retrying one would pay a second
    provider call to replace evidence the spec asked be kept.

    A CLASSIFICATION REFUSAL IS A KNOWN OUTCOME, not an orphan, and that is why `RunnerError`
    is the one exception caught here. `run_seat` writes the seat's record BEFORE it raises —
    the answer, the sentinel, the path set, everything the refusal was taken on — so the
    operation ended in a way this loop observed and the journal says so. Everything else
    (`SeatError`, `HarvestError`, `VerifyError`, whatever `launch` raises) propagates with the
    intent record still open, which is §14.1's `outcome_unknown` and the truthful record of a
    process that stopped without saying how the operation ended.

    A REFUSED ATTEMPT DOES NOT ERASE A VERDICT AN EARLIER ONE REACHED. `settled` is only
    replaced by a result, so `failed` on attempt 1 followed by a refusal on attempt 2 still
    hands back attempt 1's status — the best-described thing that happened to this seat. `None`
    is reserved for the seat that reached no verdict at ALL, which is the one case where the
    loop has nothing to say and says nothing rather than the last thing it heard.
    """
    settled, refused_with = None, None
    for attempt in range(1, manifest.attempts + 1):
        op = _op(manifest, name, f"attempt-{attempt}")
        log.record(journal.intent(_SEAT), operation_id=op, seat=name, attempt=attempt)
        try:
            result = run_seat(manifest, run_dir, base, name=name, attempt=attempt,
                              identity=identity, launch=launch)
        except RunnerError as e:
            log.record(journal.done(_SEAT), operation_id=op, seat=name, attempt=attempt,
                       refused=str(e))
            # A REFUSAL THAT REPEATS ITSELF IS DETERMINISTIC, AND §8.1's RETRY IS NOT FOR IT.
            # §8.1 buys a fresh clone because a SEAT can fail differently the second time; a
            # refusal this engine makes for the same reason twice will make it a third time,
            # and each further attempt spends another clone — and, for any refusal raised
            # AFTER the launch, another provider call — to reach the identical outcome.
            #
            # THE MESSAGE IS THE DISCRIMINATOR BECAUSE THE TYPE IS NOT. `RunnerError` covers
            # both "this engine refused" and "this seat failed" across 29 raise sites, and
            # splitting it is a change to every one of them; two identical messages are
            # evidence of determinism that needs no such classification. Two are required
            # rather than one, so a genuinely transient failure still gets its retry.
            if refused_with == str(e):
                break
            refused_with = str(e)
            continue
        settled = result
        log.record(journal.done(_SEAT), operation_id=op, seat=name, attempt=attempt,
                   forge=settled.status.forge)
        if settled.status.forge != "failed":
            break
    return settled


def _verify_a_seat(run_dir: Path, manifest, base, log, result, *, identity, calibration):
    """§6 for one candidate, then §8's re-classification with what §6 found.

    A `failed` SEAT IS NOT VERIFIED, and that is a division rather than a shortcut. §8's rules
    1 and 2 fix that verdict on the process and the setup, so `classify_seat` returns `failed`
    for any value of `verify` — the run would buy a verifier clone §5.2 priced to change an
    answer that cannot move. Every other seat IS verified, including the zero-diff one, which
    is the only route by which §8's `no_change` is reachable at all.

    The two calls are ONE journalled operation because they are one question: §6.2's outcome
    with no re-classification behind it leaves the seat's record saying `verify: "not-run"`
    for a measurement that was taken. `verify_candidate` returns FOUR values and all four are
    used: the outcome and the reason decide §8's verify dimension, and the `Verifier`'s own
    candidate and `SetupResult` are what `reclassify_seat` writes into the record — §6.1's
    gate measurement and the verifier's setup exit code respectively, both of which existed
    nowhere else and were dropped here.

    A REFUSAL IS CONTAINED TO ITS OWN SEAT, for `_drive_a_seat`'s reason and NOT on its rule
    — the two halves share the argument and catch different sets, so each states its own. The
    reason is the fleet's economics: while this half re-raised, ONE seat's refused
    verification ended the whole run, with every provider already paid and the seats behind
    this one never verified at all. §6 gives no reason the three should share a fate — each
    candidate is measured in its own clone, against the same calibration, and nothing about
    seat A's verdict is evidence about seat B's.

    THE SET IS WIDER HERE THAN IN THE BUILD HALF. `_drive_a_seat` catches `RunnerError` alone
    and says so in as many words: `SeatError`, `HarvestError`, `VerifyError` and whatever
    `launch` raises all propagate out of it. This half catches `RunnerError` AND
    `verify.VerifyError`, because `GeneratorUnstable` and `SetupOverlap` are §6's own
    vocabulary for a candidate failed closed — a statement about ONE candidate, taken in one
    clone, which is exactly the thing the paragraph above says is not evidence about the
    others. What still escapes here is what escapes there: `fleet.SeatError`,
    `bundle.BundleError` and `GitError`, so a verifier clone that cannot be BUILT still ends
    the run and still costs the seats behind it the verification their providers were paid
    for. Stated rather than implied, because it is the half of C1's economics this does not
    close.

    THE TWO CALLS ARE CAUGHT SEPARATELY, and that is what makes a bought verdict survive §8's
    refusal. They used to share one `except`, so a §6.2 outcome that WAS taken — beside
    §6.1's gate surface, measured over two trees, and the verifier's own setup run — was
    discarded whole when §8 then refused to carry it into a `Status`. Everything §6 measured
    is therefore bound through `_measured` BEFORE the call that can refuse it, which is the
    only ordering under which the handler holds a value rather than reaching back into a
    scope the exception left. `_measured` raising in the `try` lands in the FIRST handler,
    which is what contains it: a candidate that is not this seat's is a measurement that must
    not reach this seat's record at all, and that refusal must not cost the fleet.

    THE FIRST HANDLER NOW CARRIES WHAT §6 HAD ALREADY MEASURED, through `on_measurement`. A
    refusal from `assert_hooks_pinned` or from the gate arrives after `build_verifier` filled
    §6.1's surface and after the verifier's own setup ran, and both used to die as locals in
    `verify_candidate`. The sink hands each value over the instant it exists; the handler puts
    what it caught through `_measured` — the same admission rule, not a second one — and
    CONTAINS that call, because there is no handler left above it and an escape would end the
    fleet on the refusal path.

    WHAT THE CONTAINED SEAT KEEPS IS ITS PRE-VERIFICATION VERDICT, never a promotion: the
    record still reads `verify: "not-run"` and `status.forge` is untouched, because §8 is the
    only thing that revises a verdict and §8 is what refused. `verification_refused` beside it
    says which half would not answer — without it the seat would read exactly like one the
    loop had not reached yet, a verifier clone bought and spent, described on disk as work not
    yet begun. A record where `verification` is set and `status.verify` is `"not-run"` is the
    §8 refusal exactly: the gate answered and the classification did not move. And when the
    recovered measurement is REFUSED, `verification_refused` names that too rather than
    reading as a seat nothing was measured for — a verdict never reads cleaner than its
    evidence.

    Raises `RunnerError` for a record this module's own reader refuses (`_payload`), which
    `_revise` and `reclassify_seat` can both reach. That is not contained here: a seat record
    that cannot be written is not a refusal this loop can absorb, because absorbing it means
    writing it somewhere.
    """
    if result.status.forge == "failed":
        return result
    op = _op(manifest, result.name)
    log.record(journal.intent(_VERIFICATION), operation_id=op, seat=result.name)
    taken = {"candidate": None, "setup": None}

    def _sink(candidate, setup_result):
        # Last write wins by design: the second call carries the same candidate plus the
        # setup result, so a refusal between the two leaves the first call's value in place
        # rather than nothing.
        if candidate is not None:
            taken["candidate"] = candidate
        if setup_result is not None:
            taken["setup"] = setup_result

    try:
        outcome, reason, v, setup_result = verify_candidate(
            manifest, run_dir, base, result.candidate, name=result.name, identity=identity,
            calibration=calibration, on_measurement=_sink)
        measured = replace(_measured(result, v.candidate, setup_result),
                           verification=(outcome, reason))
    except (RunnerError, verify.VerifyError) as e:
        # Recorded rather than re-raised, and RECORDED TWICE: in the journal, because this
        # loop watched the operation end and an open intent would report `outcome_unknown`
        # for an outcome it knows; and on the seat, because the journal is the run's log and
        # the seat file is what §14.2 hands `--collect`.
        log.record(journal.done(_VERIFICATION), operation_id=op, seat=result.name,
                   refused=str(e))
        # WHAT WAS MEASURED SURVIVES THE REFUSAL, and it goes through `_measured` rather than
        # around it: a candidate that is not this seat's is a measurement that must not reach
        # this seat's record at all, which is the whole of what that check is for.
        #
        # CONTAINED, because this call is INSIDE the handler and nothing above it catches. In
        # the `try` above, `_measured` raising lands in this very handler — that is what
        # contains it, and it is why the docstring can call the raise deliberate. Here there is
        # no handler left: an uncaught RunnerError would leave `_verify_a_seat`, leave
        # `runner.run`, and end the fleet over ONE seat's refused verification with every
        # provider already paid and the seats behind this one never verified — the Critical
        # this half's docstring records as closed, reopened on the refusal path.
        #
        # The seat then keeps its PRE-VERIFICATION verdict and the refusal NAMES the dropped
        # measurement. Recording only `e` would read as a seat whose verification simply
        # refused, which is cleaner than the evidence: something was measured and this run
        # declined to file it.
        try:
            kept, refused = _measured(result, taken["candidate"], taken["setup"]), str(e)
        except RunnerError as bad:
            kept, refused = result, f"{e}; a measurement was taken and not admitted: {bad}"
        return _revise(run_dir, replace(kept, verification_refused=refused))
    try:
        out = reclassify_seat(run_dir, result, outcome, reason,
                              candidate=v.candidate, verifier_setup=setup_result)
    except RunnerError as e:
        # `RunnerError` alone, because that is the class `reclassify_seat` names for a
        # re-classification it will not make. `runstate`'s own write errors are deliberately
        # NOT caught: a seat record that cannot be written is not a refusal this loop can
        # contain, because containing it means writing it somewhere. The outcome goes into
        # the journal beside the refusal, so the run's log says §6 answered and §8 would not
        # carry it rather than reporting a refusal over no stated measurement.
        log.record(journal.done(_VERIFICATION), operation_id=op, seat=result.name,
                   outcome=outcome, refused=str(e))
        return _revise(run_dir, replace(measured, verification_refused=str(e)))
    log.record(journal.done(_VERIFICATION), operation_id=op, seat=result.name,
               outcome=outcome, forge=out.status.forge)
    return out


def run(run_dir, repo, *, identity, launch, resume=False) -> tuple:
    """Drive one run: calibrate, build every seat, then verify every candidate.

    EVERY FACT ABOUT THE RUN COMES OFF THE DISK, which §14 makes a requirement rather than a
    style: resuming "always from disk and never from conversation state ... makes compaction
    and restart indistinguishable, one code path instead of two". So the commands, the
    baseline, the generator contract, the seat count and the retry budget are all read from
    `run_dir`, and the three arguments that are not are the three the directory does not hold
    — `repo`, which `drift` refuses unless it is the recorded one; `identity`; and `launch`,
    which is a callable and could not be recorded at all.

    `identity` IS STILL AN ARGUMENT THOUGH THE RUN NOW RECORDS ONE, and the difference is
    worth stating because "everything comes off the disk" would otherwise read as though it
    were complete. `gate.open_run` journals the confirmed author on the `confirm` done record
    — it did not, which meant a run could not say who authored its own commits at all — so
    the answer IS on disk now, in the same journalled shape `_confirmed_policy` already knows
    how to read back. What this loop does not yet do is CHECK the argument against it: no
    caller compares `identity` to the journalled author, and until one does, a caller
    supplying a different one gets seats and verifiers attributed to a name the §5 gate never
    saw. Reading it instead of taking it is the same change one step further, and it needs a
    front end that can refuse a mismatched run before anything is spent — which nothing here
    is.

    THE ORDER, and each step's phase:

      `setting_up`  §5 step 3 — calibrate on the untouched baseline, before a provider
                    spends anything. `on_calibration_failure` is obeyed here.
      `building`    §7 — every seat, up to `manifest.attempts` fresh clones each.
      `harvested`   every seat's `Fsetup -> Fwork` difference has been taken; `run_seat`
                    harvests as part of building, so this is the boundary rather than work.
      `comparing`   §6 — one verifier clone per surviving seat, then §8's re-classification.

    IT STOPS AT `comparing`, with the fleet's candidates verified on disk and NOTHING CHOOSING
    BETWEEN THEM. §10 through §13 — the claim ledger, the agreement rule, the strategy and the
    review — are built and tested; this loop reaches none of them, and that is the front end's
    edge to take rather than a gap in the modules. The
    returned tuple is one `SeatResult` per seat that produced one, which is not always
    `manifest.seats` of them: a seat every one of whose attempts was REFUSED has no verdict to
    return, and the loop reports that by absence rather than by inventing one. Its record is
    still on disk, one attempt object per attempt.

    A SEAT IN THAT TUPLE IS NOT NECESSARILY A VERIFIED ONE, and every unverified state names
    itself: a `failed` seat was never verified because §8 fixes its verdict whatever §6 finds,
    and a seat whose verification was REFUSED carries `verification_refused` and keeps its
    pre-verification verdict. Neither is silence — see `_verify_a_seat` for why one seat's
    refusal no longer costs the others the verification the operator has already paid for.

    WRITE-AHEAD, on every operation (§14.1): `journal.intent(kind)` before, `journal.done(kind)`
    after. A crash between them leaves an ORPHAN, which §14.1 names `outcome_unknown` and says
    is never silently retried — so a later call refuses the run rather than re-running an
    operation that may have completed and spent a provider call doing it. Exactly-once is not
    on offer here and §14.1 opens by conceding it is not deliverable; DISTINGUISHABILITY is,
    and that is what this discipline buys.

    A SECOND PASS IS REFUSED whatever the first one did — see `_refuse_a_second_pass`. Nothing
    in this package serializes a `Calibration` or a `CandidateBundle`, so a second pass cannot
    carry the first one's products forward and could only re-spend what §5.2 quoted once.
    `runstate.reconstruct` is what reads a run that has already been driven.

    Raises `RunnerError` for a run this loop will not drive — drifted, orphaned, already
    driven, at a phase off its route, or aborted by the operator's own calibration policy.
    Every failure a seat or a verifier raises propagates as it does from `run_seat`, and so
    does everything `reconstruct` raises for a run directory it cannot read whole: that is
    `ManifestError` for a directory recording no run or a repository that is not the recorded
    one, but ALSO `StateError` for a damaged position or seat record, `JournalError` and
    `StorageError` — `reconstruct` is the first statement of this function and its own
    docstring is the list, which is why this one names the class rather than re-spelling it.
    Naming only `ManifestError` read as though the others could not reach here.

    NOTHING HERE PROVES THE REAL PROVIDER PATH WORKS — see the module docstring. `launch` is
    injected and the whole suite passes a fake.
    """
    run_dir = Path(run_dir)
    # The one read that takes no fact from the caller the directory already holds: the
    # manifest, the position, every seat's record, the journal's orphans, and `drift`'s answer
    # about the repository the MANIFEST names rather than the one the caller believes in.
    recon = runstate.reconstruct(run_dir, repo)
    manifest = recon.manifest
    state = recon.state or _at_confirmed()
    log = journal.Journal(storage.journal_path(run_dir))
    events = log.read()

    # §9 first: a repository that moved makes every question below moot, and the transition
    # has to leave from the phase that saw it.
    if recon.diverged:
        _source_diverged(run_dir, state, recon.diverged)
    if recon.orphans:
        _refuse_an_unknown_outcome(recon.orphans)
    if not resume:
        _refuse_a_second_pass(events)

    names = _fleet(manifest)
    for name in names:
        # Before the calibration clone and long before a provider call: a seat whose name
        # cannot be a filename would otherwise be discovered after the run had been paid for.
        _named(run_dir, name)

    # Rebuilt from what the run recorded, because `gate.open_run` returns only the run
    # directory and the `Baseline` it built is gone by the time anything drives the run. The
    # filesystem manifest comes off the disk rather than defaulting to `{}` — see
    # `baseline.read_filesystem_manifest` for what an empty one would silently disarm.
    base = baselinemod.restore(run_dir, base_commit=manifest.base_commit,
                               tracked_tree_oid=manifest.tracked_tree_oid,
                               commit=manifest.baseline_commit, ref=manifest.baseline_ref)
    # One environment for the calibration and for everything compared against it: `classify`
    # differences the two, so a candidate measured in another one is differencing machines.
    child_env = fleet.forge_child_env(manifest.repo_path)

    # RE-ENTRY AT A PHASE THE RUN ALREADY REACHED, which is the question a resume actually
    # poses. §14 declares no edge from `comparing` back to `setting_up` — correctly, because a
    # position on disk means what it says and a run that walked backwards would be claiming to
    # re-do work whose evidence is already written. So a resume does not re-declare a phase it
    # has passed: past `building` the fleet is bought and §6 has run, and the only honest thing
    # left is to hand back what the run recorded.
    #
    # NOTHING IS SPENT ON THIS PATH — no calibration clone, no provider call, no verifier. The
    # seats come from `resume.plan`, which has already proven each one against its preserved
    # clone or refused the whole resume.
    if resume and state.phase in _FLEET_IS_BOUGHT:
        reclaimed, owed = resumemod.plan(run_dir, manifest.repo_path, names, seat_dir=seat_dir)
        if owed:
            raise RunnerError(
                f"this run is at {state.phase!r}, which says its fleet finished, but "
                f"{sorted(owed)} never settled. §14's position on disk and its seat records "
                "disagree, and this loop will not pick which one to believe.")
        return tuple(resumemod.seat_result(run_dir, n, reclaimed[n], make=SeatResult,
                                           seat_dir=seat_dir)
                     for n in names if n in reclaimed)

    state = _reach(run_dir, state, "setting_up")
    cal = _calibrate(run_dir, manifest, base, log, identity=identity, env=child_env)
    _obey_the_calibration_policy(run_dir, state, manifest, events, cal)

    state = _reach(run_dir, state, "building")
    # WHAT A RESUME RECLAIMS AND WHAT IT STILL OWES, decided before a provider is reached.
    # `resume.plan` refuses outright if a settled seat cannot be proven against its preserved
    # clone, so by the time anything is spent every seat here is either reconstructed from
    # evidence or genuinely unpaid-for.
    reclaimed, owed = {}, names
    if resume:
        reclaimed, owed = resumemod.plan(run_dir, manifest.repo_path, names,
                                         seat_dir=seat_dir)
    built = [r for r in _drive_the_fleet(run_dir, manifest, base, log, names=owed,
                                        identity=identity, launch=launch)
             if r is not None]
    # ORDER FOLLOWS `names`, NOT "reclaimed first". §8's comparison reads the fleet as a
    # sequence and a resumed run that reordered it would put a different candidate in front
    # of the operator than the same run driven straight through.
    if reclaimed:
        by_name = {r.name: r for r in built}
        by_name.update({n: resumemod.seat_result(run_dir, n, c, make=SeatResult,
                                                 seat_dir=seat_dir)
                        for n, c in reclaimed.items()})
        built = [by_name[n] for n in names if n in by_name]

    if not built:
        # AN EMPTY FLEET DOES NOT REACH `comparing`, and the phase is the whole point. Every
        # seat refused every attempt, so there is no candidate on disk — while `comparing` is
        # §6's phase, whose meaning is "the fleet's candidates are verified". Advancing into
        # it with zero candidates records a run that got further than it did, and a resume
        # reading that position would look for verifier clones nobody made.
        #
        # THE RETURN VALUE IS UNCHANGED — `()` , the same empty tuple this loop has always
        # handed back for a fleet that produced nothing. What changes is only that the run
        # directory keeps its LAST TRUE phase, `building`, because §14.1's rule is that a
        # position on disk means what it says. Every refusal is already on the journal, one
        # `done` record per attempt, so the caller loses nothing by being told less.
        return ()

    state = _reach(run_dir, state, "harvested")
    state = _reach(run_dir, state, "comparing")
    return tuple(_verify_a_seat(run_dir, manifest, base, log, r, identity=identity,
                                calibration=cal)
                 for r in built)
