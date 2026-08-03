"""§14.2's per-seat record, as a schema — written beside its first real reader.

WHY THE SCHEMA IS HERE AND NOT IN `runstate`. `runstate.write_seat` deliberately enforces
none, and its docstring argues the case: §14.2 assigns the seat record's fields to the
ORCHESTRATOR, so `runstate` refuses to become the authority on a record another module owns,
and hoisting the shape into that writer would put one rule in two places. That argument is
still right. What changed is that the record now has a real READER — §12's rubric and §13's
fusion read it, and `--collect` reconstructs a run from it — so the type-check the writer
declined belongs at the reading boundary, which is here.

MEASURED, AND THIS IS WHY IT MATTERS: `{"phse": "biulding"}` is written, read and
reconstructed by `runstate` with no complaint. On a record that decides which candidate ships,
that is the fail-open this package refuses everywhere else.

WHAT `None` MEANS ON EACH OPTIONAL FIELD, and it is the same thing every time: the measurement
was never taken. `prompt_identity: None` is a launch that returned no fingerprint;
`verification: None` is a seat §6 never reached. A MALFORMED value is NOT None — it raises,
because "nobody measured" and "somebody wrote nonsense" are different records and only one of
them is safe to act on. A key that is ABSENT is not `None` either: `None` is a measurement
this engine wrote down as not taken, and a missing key is a record that cannot say.

THE WRITER PROVES ITS OWN OUTPUT DECODES. `runner._payload` calls `decode` on the payload it
is about to hand `write_seat`, so a field the writer stops writing fails at the writer rather
than hours later on a resume. The invariant lives in the value; this is what keeps it honest.
"""
from dataclasses import dataclass, fields

from . import fingerprint


class SeatRecordError(RuntimeError):
    """This seat record cannot be read as a description of a seat."""


@dataclass(frozen=True)
class Attempt:
    """One attempt at one seat — exactly the keys `runner._record` writes.

    Kept in lockstep with that function by `runner._payload`, which decodes every record
    before it is published: a key added there and not here fails the next write, and a key
    dropped there fails it too. That is the direction that catches it while the producer is
    still present, rather than on a resume hours later.
    """
    attempt: int
    path: str
    branch: str | None
    sentinel: str | None
    status: dict | None
    verification: dict | None
    verification_refused: str | None
    setup_run: dict | None
    verifier_setup: dict | None
    artifacts: dict
    candidate: dict
    launch: dict | None
    prompt_identity: object            # fingerprint.PromptIdentity | None, after decode


@dataclass(frozen=True)
class SeatRecord:
    name: str
    attempts: tuple


_ATTEMPT_NAMES = tuple(f.name for f in fields(Attempt))

# §6.1's two halves, named here because the rule below is about the PAIR and not about either
# one: they are written together or not at all, and both keys are present on every record this
# engine writes even when nobody measured.
_GATE_PAIR = ("gate_delta", "gate_surface")


def _attempt(row, where) -> Attempt:
    if not isinstance(row, dict):
        raise SeatRecordError(f"{where}: an attempt is an object, not {type(row).__name__}")
    missing = [n for n in _ATTEMPT_NAMES if n not in row]
    if missing:
        raise SeatRecordError(f"{where} is missing {missing}")
    unknown = sorted(set(row) - set(_ATTEMPT_NAMES))
    if unknown:
        raise SeatRecordError(
            f"{where} carries fields this engine does not know: {unknown}. A recorder that "
            "once wrote a fact this reader drops would let a later phase answer questions "
            "about the seat out of a record it only partly understands.")
    if not isinstance(row["attempt"], int) or isinstance(row["attempt"], bool):
        raise SeatRecordError(f"{where}: attempt is an int, not {row['attempt']!r}")
    if not isinstance(row["path"], str) or not row["path"]:
        raise SeatRecordError(f"{where}: path is a non-empty string")
    for n in ("status", "verification", "setup_run", "verifier_setup", "launch"):
        if row[n] is not None and not isinstance(row[n], dict):
            raise SeatRecordError(f"{where}: {n} is an object or null, not {row[n]!r}")
    for n in ("artifacts", "candidate"):
        if not isinstance(row[n], dict):
            raise SeatRecordError(f"{where}: {n} is an object, not {row[n]!r}")
    cand = row["candidate"]
    # PRESENCE FIRST, and it is not redundant with the pairing rule below: two ABSENT keys
    # read as two `None`s and would satisfy it, so a candidate that says nothing at all about
    # §6.1 would decode clean. `None` is a measurement recorded as not taken; a missing key is
    # a record that cannot say which.
    absent = [n for n in _GATE_PAIR if n not in cand]
    if absent:
        raise SeatRecordError(
            f"{where}: this candidate is missing {absent}. §6.1's measurement is recorded as "
            "`null` when nobody looked, so an absent key is not that state — it is a record "
            "with nothing to say about the gate at all.")
    # `with_gate_measurement`'s rule, enforced where the record is read: §6.1's delta and the
    # surface it ranged over go together or not at all. A delta with no surface cannot say
    # whether `()` means "measured over four files and none moved" or "nothing was looked at",
    # and `None` for both is the third state — nobody measured.
    if (cand["gate_delta"] is None) != (cand["gate_surface"] is None):
        raise SeatRecordError(
            f"{where}: gate_delta and gate_surface go together or not at all; this record has "
            f"delta={cand['gate_delta']!r} beside surface={cand['gate_surface']!r}, "
            "which cannot say whether an empty delta means a clean gate or an unlooked-at one")
    pi = row["prompt_identity"]
    if pi is not None:
        try:
            pi = fingerprint.from_row(pi)
        except fingerprint.FingerprintError as e:
            # NOT swallowed into None. `None` is "the launch returned no fingerprint", and a
            # malformed one read as that would file a damaged measurement as an absent one.
            raise SeatRecordError(f"{where}: prompt_identity: {e}") from e
    body = {n: row[n] for n in _ATTEMPT_NAMES}
    body["prompt_identity"] = pi
    return Attempt(**body)


def decode(payload) -> SeatRecord:
    """`runstate.read_seat`'s payload, type-checked into a value a later phase can act on."""
    if not isinstance(payload, dict):
        raise SeatRecordError(f"a seat record is an object, not {type(payload).__name__}")
    names = ("name", "attempts")
    missing = [n for n in names if n not in payload]
    if missing:
        raise SeatRecordError(f"this seat record is missing {missing}")
    unknown = sorted(set(payload) - set(names))
    if unknown:
        raise SeatRecordError(
            f"this seat record carries fields this engine does not know: {unknown}")
    if not isinstance(payload["name"], str) or not payload["name"]:
        raise SeatRecordError("a seat record's name is a non-empty string")
    if not isinstance(payload["attempts"], list) or not payload["attempts"]:
        raise SeatRecordError(
            "a seat record holds at least one attempt. An empty list is falsy exactly as the "
            "None a seat that never wrote reads back as, and §14.1 requires those stay apart.")
    attempts, seen = [], set()
    for i, row in enumerate(payload["attempts"]):
        a = _attempt(row, f"{payload['name']} attempt {i}")
        if a.attempt in seen:
            raise SeatRecordError(
                f"{payload['name']}: attempt {a.attempt} is recorded twice. §8.1 preserves "
                "every failed attempt as partial input, so two records under one number make "
                "'which clone is this' unanswerable.")
        seen.add(a.attempt)
        attempts.append(a)
    return SeatRecord(payload["name"], tuple(attempts))


def identities(rec: SeatRecord) -> tuple:
    """One entry per attempt, `None` where the launch returned no fingerprint.

    One per attempt rather than one per seat, and never filtered: §11's comparison is over
    what each seat was GIVEN, and dropping the unmeasured ones would make a fleet of three
    seats with one fingerprint between them read as a fleet of one.
    """
    return tuple(a.prompt_identity for a in rec.attempts)
