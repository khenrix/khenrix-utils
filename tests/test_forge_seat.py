"""Section 8: a seat's status as four dimensions that must not collapse into one."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import pytest  # noqa: E402
from forge import seat  # noqa: E402

# The base case used by several tests: every dimension at its strongest reading, so a single
# override below shows that ONE dimension moving is enough to change the verdict.
_BASE = dict(process="valid", artifacts="usable", proven_read=True, changed=True,
             setup="pass", verify="pass")


def test_useful_artifacts_without_the_proof_token_are_partial_not_completed():
    """§8: the seat did the work and did not prove it read the task. Collapsing this into
    `completed` is what let a silently-failed seat read as success."""
    s = seat.classify_seat(process="valid", artifacts="usable", proven_read=False,
                           changed=True, setup="pass", verify="pass")
    assert s.forge == "partial"


def test_a_no_change_without_a_rationale_is_refused():
    """§8: a correct conclusion that the task needs no edit must not be discarded — but it
    has to be argued, or `no_change` is indistinguishable from a seat that did nothing."""
    with pytest.raises(seat.SeatStatusError):
        seat.classify_seat(process="valid", artifacts="unusable", proven_read=True,
                           changed=False, setup="pass", verify="pass")
    s = seat.classify_seat(process="valid", artifacts="unusable", proven_read=True,
                           changed=False, setup="pass", verify="pass",
                           rationale="the retry already backs off; adding one would double-sleep")
    assert s.forge == "no_change"


def test_a_failing_verify_is_recorded_and_does_not_change_the_forge_status():
    """§8: passing verify is RECORDED, not required, for a seat to inform synthesis."""
    a = seat.classify_seat(process="valid", artifacts="usable", proven_read=True,
                           changed=True, setup="pass", verify="fail")
    b = seat.classify_seat(process="valid", artifacts="usable", proven_read=True,
                           changed=True, setup="pass", verify="pass")
    assert a.forge == b.forge == "completed"
    assert (a.verify, b.verify) == ("fail", "pass")


def test_a_setup_failure_does_not_proceed_on_the_strength_of_its_files():
    """§8, verbatim: "A setup failure does not proceed merely because it produced files.\""""
    s = seat.classify_seat(process="valid", artifacts="usable", proven_read=True,
                           changed=True, setup="fail", verify="not-run")
    assert s.forge == "failed"


def test_the_four_dimensions_are_independent():
    """A status that can be reconstructed from one field has collapsed. Every dimension must
    vary while the others are held."""
    base = dict(process="valid", artifacts="usable", proven_read=True, changed=True,
                setup="pass", verify="pass")
    assert seat.classify_seat(**{**base, "process": "invalid"}).forge == "failed"
    assert seat.classify_seat(**{**base, "artifacts": "unusable"}).forge != "completed"
    assert seat.classify_seat(**{**base, "proven_read": False}).forge == "partial"


# --- Precedence: the plan requires the ORDER between rules to be a checkable decision, not
# just their individual outcomes. Each test below pins one interaction the five tests above
# do not, because two dimensions are wrong at once and only one of them can decide `forge`.

def test_a_setup_failure_pre_empts_the_partial_rule_not_just_the_completed_one():
    """§8 Step 3's own example: "a setup='fail' seat with usable artifacts and no proof
    token is failed, not partial." Usable artifacts and a missing proof token are, on their
    own, exactly the partial rule's trigger — this shows setup='fail' is checked first and
    wins over it, not merely over a seat that would otherwise have been `completed`."""
    s = seat.classify_seat(**{**_BASE, "proven_read": False, "setup": "fail",
                              "verify": "not-run"})
    assert s.forge == "failed"


def test_unusable_artifacts_from_a_seat_that_did_change_something_is_failed_not_partial():
    """`artifacts="unusable"` is only reachable past the no_change branch when `changed` is
    True: the seat touched something and what it left behind cannot be used. That is a
    failure, not partial credit — pinned to the exact value, not just "not completed"."""
    s = seat.classify_seat(**{**_BASE, "artifacts": "unusable"})
    assert s.forge == "failed"


def test_setup_that_never_ran_does_not_let_a_seat_read_as_completed():
    """Fail closed: `setup="not-run"` is a measurement that was not taken, and rule 3 names
    `verify` — not `setup` — as the one dimension exempt from that default. A seat otherwise
    strong enough to be `completed` degrades to `partial` instead of reading as clean on
    evidence that was never collected."""
    s = seat.classify_seat(**{**_BASE, "setup": "not-run"})
    assert s.forge == "partial"


def test_no_change_also_requires_independent_verification_not_only_a_rationale():
    """§8, verbatim: "a `no_change` requires a substantive rationale AND independent
    verification." A rationale alone is not enough — an unfailing setup/verify pair has to
    back it, or the claim that nothing needed to change was never actually checked."""
    rationale = "the retry already backs off; adding one would double-sleep"
    with pytest.raises(seat.SeatStatusError):
        seat.classify_seat(process="valid", artifacts="unusable", proven_read=True,
                           changed=False, setup="pass", verify="fail", rationale=rationale)
    with pytest.raises(seat.SeatStatusError):
        seat.classify_seat(process="valid", artifacts="unusable", proven_read=True,
                           changed=False, setup="not-run", verify="pass", rationale=rationale)
    s = seat.classify_seat(process="valid", artifacts="unusable", proven_read=True,
                           changed=False, setup="pass", verify="pass", rationale=rationale)
    assert s.forge == "no_change"


def test_a_rationale_present_but_not_substantive_is_still_refused():
    """A non-empty rationale is not automatically a substantive one — "ok" is exactly the
    seat-did-nothing case the rule exists to catch."""
    with pytest.raises(seat.SeatStatusError):
        seat.classify_seat(process="valid", artifacts="unusable", proven_read=True,
                           changed=False, setup="pass", verify="pass", rationale="ok")
    with pytest.raises(seat.SeatStatusError):
        seat.classify_seat(process="valid", artifacts="unusable", proven_read=True,
                           changed=False, setup="pass", verify="pass", rationale="   ")


def test_an_unrecognized_literal_is_refused_rather_than_silently_carried_through():
    """Fail closed on the input side too: a caller mistyping `process="Valid"` must not
    silently classify as though it were a recognized value — the four-way vocabulary in
    §8's table is exhaustive, and treating a fifth spelling as data hides the typo instead
    of surfacing it."""
    with pytest.raises(seat.SeatStatusError):
        seat.classify_seat(**{**_BASE, "process": "ok"})
    with pytest.raises(seat.SeatStatusError):
        seat.classify_seat(**{**_BASE, "setup": "unknown"})


def test_proven_read_and_changed_must_be_real_booleans():
    """`bool` is a subclass of `int`; a 0/1 or a `None` that slipped through from an
    upstream measurement must not be silently read as a real False rather than refused."""
    with pytest.raises(seat.SeatStatusError):
        seat.classify_seat(**{**_BASE, "proven_read": 1})
    with pytest.raises(seat.SeatStatusError):
        seat.classify_seat(**{**_BASE, "changed": None})


def test_status_is_frozen():
    s = seat.classify_seat(**_BASE)
    with pytest.raises(AttributeError):
        s.forge = "no_change"
