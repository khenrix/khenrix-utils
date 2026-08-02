"""A seat's status as four independent dimensions (spec section 8), because collapsing them
is what let a silently-failed seat read as success.

`process` and `artifacts` are the seat's own claims about itself: did the agent's process
complete normally, and is what it left behind usable. `proven_read` records whether the
sentinel round-trip proved the task prompt was actually read (section 8.1). `setup` and
`verify` record what the confirmed setup/verify commands did, independently, in a fresh
clone the seat never touched (section 6). `forge` is the one dimension this module computes
rather than accepts as input, because a seat-writable verdict is the exact thing a candidate
could rig to read cleaner than its evidence.

THREE RULES ARE LOAD-BEARING (section 8, quoted) and each has its own test:

- "A seat with useful artifacts but no proof token is `partial`, not completed."
- "A `no_change` requires a substantive rationale and independent verification -- a correct
  conclusion that the task needs no edit must not be discarded."
- "Passing verify is recorded, not required, for a seat to inform synthesis. A setup failure
  does not proceed merely because it produced files."

`Status` carries no `changed` field and no `rationale` field. Both are `classify_seat`'s
inputs, not part of the record: `changed` only ever mattered as the fork between "produced a
diff" and "argued none was needed", which `forge` already encodes, and `rationale` belongs on
the richer per-seat record a caller assembles around this `Status` -- carrying it here would
give this module a second, untyped place to hold prose that the surrounding record already
owns.

PRECEDENCE. `classify_seat` checks these in a fixed order, and every rule below fires only if
none above it already decided `forge`:

1. `process == "invalid"` -> `failed`, unconditionally. An invalid process taints every other
   signal; nothing an invalid process produced can be trusted regardless of what its
   artifacts, proof token, setup or verify claim.
2. `setup == "fail"` -> `failed`, unconditionally, and checked BEFORE artifacts or
   proven_read are read at all. This ordering is what makes "a setup='fail' seat with usable
   artifacts and no proof token is failed, not partial" true rather than accidental --
   see test_a_setup_failure_does_not_proceed_on_the_strength_of_its_files and
   test_a_setup_failure_pre_empts_the_partial_rule_not_just_the_completed_one beside it.
3. `changed is False` -> `no_change`, but only when BOTH stated requirements hold:
   a substantive `rationale`, and independent verification -- `setup == "pass"` AND
   `verify == "pass"`. Neither substitutes for the other. Rule 3's "verify is recorded, not
   required" is a statement about the `completed`/`partial`/`failed` branches below, where
   verify never gates `forge`; a `no_change` claim is the one place the spec text asks verify
   to gate something directly ("no_change ... requires ... independent verification"), so
   this branch reads it rather than merely recording it. Anything short of both raises
   `SeatStatusError` rather than silently degrading to another status: an unargued or
   unverified `no_change` is indistinguishable from a seat that did nothing, and refusing to
   construct one is how that collapse is prevented rather than merely documented.
4. `artifacts == "unusable"` (only reachable here with `changed is True`: the seat touched
   something and what it left behind cannot be used) -> `failed`. There is no partial credit
   for an unusable result.
5. `not proven_read` -> `partial`. Useful, usable artifacts with no proof the task prompt was
   read are real work, but not a `completed` seat -- section 8's first load-bearing rule.
6. `setup != "pass"` (i.e. `"not-run"`: setup never positively confirmed) -> `partial`. Fail
   closed: a measurement that was not taken cannot promote a seat to the cleanest verdict.
   Rule 3 names verify as the one dimension exempt from this default; it does not exempt
   setup, so the default stands here the way it does not for verify.
7. Otherwise -> `completed`. `verify`'s value is never read above this line -- rule 3,
   enforced by omission rather than by a branch that would have to be kept in step with it.
"""
from dataclasses import dataclass

_PROCESS = ("valid", "invalid")
_ARTIFACTS = ("usable", "unusable")
_PHASE = ("pass", "fail", "not-run")  # setup, verify
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
    setup: str
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
                   setup: str, verify: str, rationale: str | None = None) -> Status:
    """Classify one seat's outcome per section 8. See the module docstring for the exact
    precedence this function implements, in the same order, with the reasoning for each step.
    """
    _require_literal("process", process, _PROCESS)
    _require_literal("artifacts", artifacts, _ARTIFACTS)
    _require_literal("setup", setup, _PHASE)
    _require_literal("verify", verify, _PHASE)
    _require_bool("proven_read", proven_read)
    _require_bool("changed", changed)

    if process == "invalid":
        forge = "failed"
    elif setup == "fail":
        forge = "failed"
    elif not changed:
        _require_rationale(rationale)
        if setup != "pass" or verify != "pass":
            raise SeatStatusError(
                "no_change requires independent verification: setup and verify must both "
                f"have run and passed (setup={setup!r}, verify={verify!r}) -- a claim that "
                "nothing needed to change is only as credible as the check that confirms "
                "the current state is already correct")
        forge = "no_change"
    elif artifacts == "unusable":
        forge = "failed"
    elif not proven_read:
        forge = "partial"
    elif setup != "pass":
        forge = "partial"
    else:
        forge = "completed"

    assert forge in _FORGE, forge  # every branch above must land in the declared set
    return Status(process=process, artifacts=artifacts, proven_read=proven_read,
                  forge=forge, setup=setup, verify=verify)
