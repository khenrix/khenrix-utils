"""A seat's status as four independent dimensions (spec section 8), because collapsing them
is what let a silently-failed seat read as success.

`process` and `artifacts` are the seat's own claims about itself: did the agent's process
complete normally, and is what it left behind usable. `proven_read` records whether the
sentinel round-trip proved the task prompt was actually read (section 8.1). `verify` records
what the confirmed verify command did, independently, in a fresh clone the seat never touched
(section 6). `forge` is the one dimension this module computes rather than accepts as input,
because a seat-writable verdict is the exact thing a candidate could rig to read cleaner than
its evidence.

`builder_setup` IS THE BUILDER'S CLONE, NOT THE VERIFIER'S — and the field NAME now says
so, since the old bare `setup` read exactly like §6's verifier setup one struct over. This
paragraph said otherwise until 2026-08-04. `runner.run_seat` fills it from the setup run in the SEAT's clone, before the
agent starts — which is a real and useful fact (a seat whose setup failed never had a working
tree to build in) and is not the fresh-clone measurement the sentence above claims. The
verifier's own setup is recorded separately, as `verifier_setup` on the attempt row, carrying
the exit code and step index rather than a dimension word. What made the mislabelling matter
was that a failing verifier setup then reached the gate anyway and could return `PASS`, so a
record read `builder_setup=pass verify=pass forge=completed` over a verifier whose setup exited 3;
`runner.SETUP_REFUSED` closes that, and the two facts now have two names. RENAMING this field
to `builder_setup` is the honest end state and is deferred: it is an on-disk schema change
plus a required keyword across ~42 `classify_seat` call sites, and it belongs in a task whose
reviewer is looking at a rename rather than as a rider on a verdict fix.

THREE RULES ARE LOAD-BEARING (section 8, quoted) and each has its own test:

- "A seat with useful artifacts but no proof token is `partial`, not completed."
- "A `no_change` requires a substantive rationale and independent verification -- a correct
  conclusion that the task needs no edit must not be discarded."
- "Passing verify is recorded, not required, for a seat to inform synthesis. A setup failure
  does not proceed merely because it produced files."

`Status` carries no `changed` field and no `rationale` field. Both are `classify_seat`'s
inputs, not part of the record, and `rationale` belongs on the richer per-seat record a
caller assembles around this `Status` -- carrying it here would give this module a second,
untyped place to hold prose that the surrounding record already owns.

WHAT THAT COSTS FOR `changed` IS NOW REAL, and this sentence used to deny it: it said
`forge` already encoded the fork between "produced a diff" and "argued none was needed".
That was true while an argued zero-diff seat landed on `no_change`. It stopped being true
when rule 3 below started degrading an unverified one to `partial`, which is also where
unproven work lands -- so `forge == "partial"` no longer says which of the two happened, and
NOTHING on this `Status` does. Not `artifacts` either: rule 3 fires before rule 4, so
`changed=False` with `artifacts="usable"` is an admitted combination here, even though
`runner.run_seat` cannot produce one (its `changed` is `bool(artifacts.paths)`, and an empty
path set builds an empty bundle). The evidence that separates them is the artifact path set
`changed` was computed FROM, which lives on the caller's record beside the rationale. A
reader who needs the fork reads that record, not this object.

PRECEDENCE. `classify_seat` checks these in a fixed order, and every rule below fires only if
none above it already decided `forge`:

1. `process == "invalid"` -> `failed`, unconditionally. An invalid process taints every other
   signal; nothing an invalid process produced can be trusted regardless of what its
   artifacts, proof token, setup or verify claim.
2. `builder_setup == "fail"` -> `failed`, unconditionally, and checked BEFORE artifacts or
   proven_read are read at all. This ordering is what makes "a builder_setup='fail' seat with usable
   artifacts and no proof token is failed, not partial" true rather than accidental --
   see test_a_setup_failure_does_not_proceed_on_the_strength_of_its_files and
   test_a_setup_failure_pre_empts_the_partial_rule_not_just_the_completed_one beside it.
3. `changed is False` -> `no_change`, but only when BOTH stated requirements hold:
   a substantive `rationale`, and independent verification -- `setup` in `_CONFIRMED_SETUP`
   AND `verify == "pass"`. Neither substitutes for the other. Rule 3's "verify is recorded,
   not required" is a statement about the `completed`/`partial`/`failed` branches below, where
   verify never gates `forge`; a `no_change` claim is the one place the spec text asks verify
   to gate something directly ("no_change ... requires ... independent verification"), so
   this branch reads it rather than merely recording it. A missing or decorative rationale
   raises `SeatStatusError` rather than degrading: an unargued `no_change` is
   indistinguishable from a seat that did nothing, and the rationale is the ONLY evidence
   that tells those two apart, so its absence is a refusal.

   THE VERIFICATION HALF DOES NOT RAISE WHEN THE MEASUREMENT WAS NEVER TAKEN. `no_change` is
   a POST-VERIFICATION verdict, and §6 puts every verification in a fresh clone the builder
   never had -- "running the confirmed command in the seat's own clone therefore measures
   nothing", and `gate.quote` prices it: `verify_runs` counts the calibration and the
   verifier clones and NOT the builders, while `setup_runs` counts the builders too. So
   `verify == "not-run"` is the state EVERY seat is classified in at harvest time, not a
   caller's mistake, and raising there would discard exactly the argued-but-not-yet-checked
   conclusion §8 says must not be discarded. It degrades to `partial` instead -- real work
   whose verdict cannot be taken yet -- which is still not `no_change`, so an unverified
   claim can never read as a verified one. A verification that WAS taken and refuted the
   claim still raises, and it is two states rather than one: `verify == "fail"` is the gate
   contradicting the claim outright, and a `builder_setup` that is neither `"pass"` nor `"none"`
   under a verify that DID pass is a caller that took the later measurement while
   withholding the earlier one. Neither is a measurement it is too early to have.

   THAT REFUSAL USED TO INCLUDE EVERY NO-TOOLCHAIN REPOSITORY, and this paragraph called it
   "a contradiction in the caller's own measurements" while `runner.run_seat`'s HAPPY PATH
   produced it: an empty confirmed setup recorded `"not-run"`, indistinguishable here from a
   setup command that existed and was skipped, so the first argued zero-diff seat raised out
   of the run loop -- after all three providers had been paid, with the other seats never
   verified and the journal reading clean. `"none"` is the value that tells the two apart;
   see `_SETUP` below for what each of the four means.
4. `artifacts == "unusable"` (only reachable here with `changed is True`: the seat touched
   something and what it left behind cannot be used) -> `failed`. There is no partial credit
   for an unusable result.
5. `not proven_read` -> `partial`. Useful, usable artifacts with no proof the task prompt was
   read are real work, but not a `completed` seat -- section 8's first load-bearing rule.
6. `setup` not in `_CONFIRMED_SETUP` (i.e. `"not-run"`: a setup command exists and the
   measurement was withheld) -> `partial`. Fail closed: a measurement that was not taken
   cannot promote a seat to the cleanest verdict. Rule 3 names verify as the one dimension
   exempt from this default; it does not exempt setup, so the default stands here the way it
   does not for verify. `"none"` is not withheld evidence and does not degrade a seat --
   there was no command to run, so there is no measurement missing from the record.
7. Otherwise -> `completed`. `verify`'s value is never read above this line -- rule 3,
   enforced by omission rather than by a branch that would have to be kept in step with it.
"""
from dataclasses import dataclass, replace
from pathlib import Path

from council import engine

_PROCESS = ("valid", "invalid")
_ARTIFACTS = ("usable", "unusable")

# `setup` and `verify` had ONE vocabulary and it could not say the thing rules 3 and 6 turn
# on: whether a missing measurement was withheld or was never there to take. `"not-run"` is
# the first -- a confirmed command that did not run in this seat -- and `"none"` is the
# second: §5's confirmation named no setup command at all, which `gate.Confirmation` admits
# outright (it refuses only an empty VERIFY) and which is the ordinary shape of a repository
# that needs no toolchain. Collapsing them is what made an argued zero-diff seat in such a
# repository a crash rather than a verdict.
_SETUP = ("pass", "fail", "not-run", "none")

# `verify` has no `"none"` and must not gain one by sharing a tuple: §6 runs the confirmed
# gate in a fresh clone for EVERY run -- `Confirmation` refuses an empty verify command, so
# there is no such thing as a run with no gate to take -- and a value nothing measures is a
# value nothing should be able to be classified under. `runner._verify_dim` produces
# `"not-run"` for §6.2's four non-verdicts and `runner.run_seat` writes it literally at
# harvest time; both mean the same thing, which is why one literal serves them.
_VERIFY = ("pass", "fail", "not-run")

# The `setup` readings that leave nothing unmeasured, and the exact set rules 3 and 6 admit.
# `"pass"` is a command that ran and confirmed; `"none"` is a run with no command to run.
_CONFIRMED_SETUP = ("pass", "none")

_FORGE = ("completed", "partial", "no_change", "failed")

# A rationale below this many characters (after stripping leading/trailing whitespace) is
# refused as decorative rather than substantive: "ok", "n/a", "done" all fail this bar. The
# spec's own example, "the retry already backs off; adding one would double-sleep" (60
# chars), clears it with room to spare. Deliberately not council's MIN_SUBSTANTIVE_CHARS=400
# -- that bounds a builder's whole turn of work, not a one-sentence justification, and 400
# would refuse the spec's own example outright.
_MIN_RATIONALE_CHARS = 10


class SeatStatusError(RuntimeError):
    """An argument to `classify_seat` -- or the combination of several -- it will not answer
    for. Never a state a `Status` can be built to represent; see `Status` for those."""


@dataclass(frozen=True)
class Status:
    """Section 8's classification of one seat, at rest.

    Six fields: the seat's own claims (`process`, `artifacts`), what the engine
    independently measured (`proven_read`, `setup`, `verify`), and the one dimension this
    module computes (`forge`). No seventh field carries `forge`'s reasoning -- that is what
    the module docstring's precedence list is for, and it is the same for every `Status`
    with a given `forge` value, so restating it per instance would only be able to drift
    from the code that actually decided it.
    """
    process: str
    artifacts: str
    proven_read: bool
    forge: str
    builder_setup: str
    verify: str


def _require_literal(name: str, value, allowed: tuple) -> None:
    if value not in allowed:
        raise SeatStatusError(f"{name}={value!r} is not one of {allowed}")


def _require_bool(name: str, value) -> None:
    # isinstance(x, bool) rejects 0/1/None outright even though bool subclasses int: an int
    # object is never an instance of bool, so a stray 0/1 (what a boolean-ish config parse
    # can leave behind) or a missing measurement can never be silently read as False.
    if not isinstance(value, bool):
        raise SeatStatusError(f"{name} must be a bool, not {type(value).__name__}")


def _require_rationale(rationale) -> None:
    if not isinstance(rationale, str) or len(rationale.strip()) < _MIN_RATIONALE_CHARS:
        raise SeatStatusError(
            "no_change requires a substantive rationale ("
            f">= {_MIN_RATIONALE_CHARS} characters after stripping whitespace); a "
            "conclusion that the task needs no edit must be argued, not merely asserted, "
            "or it is indistinguishable from a seat that did nothing")


def classify_seat(*, process: str, artifacts: str, proven_read: bool, changed: bool,
                   builder_setup: str, verify: str, rationale: str | None = None) -> Status:
    """Classify one seat's outcome per section 8. See the module docstring for the exact
    precedence this function implements, in the same order, with the reasoning for each step.
    """
    _require_literal("process", process, _PROCESS)
    _require_literal("artifacts", artifacts, _ARTIFACTS)
    _require_literal("builder_setup", builder_setup, _SETUP)
    _require_literal("verify", verify, _VERIFY)
    _require_bool("proven_read", proven_read)
    _require_bool("changed", changed)

    if process == "invalid":
        forge = "failed"
    elif builder_setup == "fail":
        forge = "failed"
    elif not changed:
        _require_rationale(rationale)
        if verify == "not-run":
            # The claim is argued and not yet checked. See rule 3 in the module docstring:
            # §6 runs every verification somewhere this seat is not, so this is where a
            # seat's own classification always lands, and `partial` withholds the promotion
            # without throwing the argument away.
            forge = "partial"
        elif builder_setup not in _CONFIRMED_SETUP or verify != "pass":
            raise SeatStatusError(
                "no_change requires independent verification: verify must have run and "
                f"passed and setup must be one of {list(_CONFIRMED_SETUP)} "
                f"(builder_setup={builder_setup!r}, verify={verify!r}) -- a claim that nothing needed to "
                "change is only as credible as the check that confirms the current state is "
                "already correct")
        else:
            forge = "no_change"
    elif artifacts == "unusable":
        forge = "failed"
    elif not proven_read:
        forge = "partial"
    elif builder_setup not in _CONFIRMED_SETUP:
        forge = "partial"
    else:
        forge = "completed"

    assert forge in _FORGE, forge  # every branch above must land in the declared set
    return Status(process=process, artifacts=artifacts, proven_read=proven_read,
                  forge=forge, builder_setup=builder_setup, verify=verify)


# --------------------------------------------------------------------------- #
# Section 8.1's live defect: the council engine's own validity policy, run unmodified
# against a forge seat, scores a real forty-minute effort `non_substantive` on its
# one-line sign-off and re-runs the same argv IN THE SAME CWD, on top of the seat's own
# half-finished work. `council.engine.evaluate()` -- what `run_provider` falls back to
# when `ProviderSpec.validator` is None -- enforces MIN_SUBSTANTIVE_CHARS=400 plus a
# sentinel citation because a COUNCIL seat's whole job is to answer in one turn, so a
# short or unproven reply there really does mean the seat did nothing. A forge seat's
# job is the opposite: open-ended edits across a whole clone, where a terse sign-off is
# normal and re-running it destroys evidence rather than collecting it.
# --------------------------------------------------------------------------- #
def _forge_validator(exit_code, stdout: str, stderr: str, spec) -> tuple:
    """`ProviderSpec.validator`'s real contract, not the one its field names suggest.
    `run_provider` calls `(spec.validator or evaluate)(exit_code, stdout, stderr, spec)`
    (council/engine.py:1167) and reads back `evaluate`'s own signature (:1064-65):
    `(valid, reason, result_text, structured)` -- a 4-tuple keyed by position, not a
    `validator(text, token) -> {"valid": ...}` dict a caller could otherwise guess from
    `min_chars`/`sentinel` alone.

    Delegates to `evaluate()` on a COPY of `spec` with `min_chars` forced to 0 and
    `sentinel` forced to None, regardless of what either field carries on the `spec`
    actually passed in -- so a real process failure (nonzero exit, a parse failure, a
    provider's own structured error, truly empty output) still invalidates the seat,
    and neither the length floor nor a missing sentinel can. "No sentinel invalidation"
    is a property of THIS function, not of `forge_spec` merely leaving two fields
    unset: `ProviderSpec.sentinel` exists precisely so a caller can wire it up, and a
    caller who later sets it on a forge spec (reasonably expecting that to be how the
    sentinel gets used) must not silently reopen the retry-on-half-finished-work bug
    this validator exists to close. The sentinel is not discarded -- `read_proof` reads
    the same `result_text` this returns, independently -- only its power to fail this
    validator is.
    """
    neutral = replace(spec, min_chars=0, sentinel=None)
    return engine.evaluate(exit_code, stdout, stderr, neutral)


def forge_spec(name: str, prompt: str, timeout: int, **kw) -> engine.ProviderSpec:
    """The `ProviderSpec` a forge seat runs with: the real per-provider argv from
    `council.engine.build_real_spec`, with council's council-only validity policy
    (section 8.1) replaced by `_forge_validator` so a seat that worked for forty
    minutes and signed off in one line is not re-run on top of itself.

    `cfg` (provider -> {"model", "thinking"}) and `workdir` (where agy's log file
    lands) are `build_real_spec`'s own remaining parameters; pass them via
    `cfg=`/`workdir=` when a seat needs a specific model/tier or a real run directory.
    Left at their defaults (no model override, the current directory) they are
    harmless at spec-construction time -- neither is touched until the spec is run.

    ANYTHING ELSE IS REFUSED rather than dropped. `**kw` exists to name two optional
    parameters, and a swallow makes `workdirr=` -- or a `sentinel=`/`min_chars=` a caller
    reasonably expects to reach the spec -- construct a spec silently missing what was
    asked for. The two `_forge_validator` neutralizes are exactly the ones a caller is
    most likely to try to set, so the quiet reading of a typo there is a seat running
    under a policy nobody chose.
    """
    cfg = kw.pop("cfg", {})
    workdir = kw.pop("workdir", Path("."))
    if kw:
        raise SeatStatusError(
            f"forge_spec does not take {sorted(kw)}; besides name/prompt/timeout it takes "
            "cfg= and workdir=, which are `council.engine.build_real_spec`'s own remaining "
            "parameters")
    spec = engine.build_real_spec(name, prompt, timeout, cfg, workdir)
    spec.min_chars = 0
    spec.validator = _forge_validator
    if name == "agy":
        # `--add-dir <seat>`, BECAUSE `spec.cwd` IS NOT AGY'S WORKSPACE. Measured live
        # 2026-08-05 through this very function: asked to create a file "in your current
        # working directory" with `cwd` set to the seat clone, agy wrote it to
        # `~/.gemini/antigravity-cli/scratch/` and ANSWERED that it had created it in the
        # current working directory. Exit 0, `status: SUCCESS`, sentinel quoted — so the seat
        # scored `valid` having put nothing in its clone at all.
        #
        # Two things that makes false, neither of which any test in this package could see,
        # because every one of them injects a fake launcher:
        #   - §4's isolation. That scratch directory is FIXED and SHARED — not per seat, not
        #     under the run directory, and not reclaimed by `--gc`. Two agy seats in different
        #     runs write to one place, so run N+1's seat can read run N's work.
        #   - the seat's own answer, which describes a file that is not where it says.
        # With `--add-dir` the same prompt writes into the seat. The council does not need it
        # (its agy seat is read-only under `--mode plan`), which is why it is added HERE and
        # not in `build_real_spec`.
        spec.argv = [spec.argv[0], "--add-dir", str(workdir), *spec.argv[1:]]
    return spec


def read_proof(output: str, token: str | None) -> bool:
    """Whether `output` demonstrably quotes the per-run sentinel `token` -- the ONLY
    place forge consults the sentinel, and the sole input to section 8's `proven_read`
    dimension. Deliberately separate from `_forge_validator`: `proven_read` reads the
    token, `forge_spec`'s validity never does -- recorded, and stripped of the power to
    invalidate, exactly as section 8.1 asks; the other reading (no sentinel at all)
    would make `proven_read` unmeasurable instead.

    A missing token fails closed: returns False, not True. This is deliberately NOT
    `council.engine._cites_sentinel`, which treats a missing token as "nothing to
    prove" and returns True -- correct for ITS question (was a check even configured
    for this council run) but wrong for this one. Section 8 already has a rule for a
    measurement that was never taken: `partial`, not a free pass to `completed`.
    """
    if not token:
        return False
    return token.lower() in (output or "").lower()
