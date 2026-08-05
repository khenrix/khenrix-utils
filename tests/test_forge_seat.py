"""Section 8: a seat's status as four dimensions that must not collapse into one."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import json  # noqa: E402

import pytest  # noqa: E402
from council import engine  # noqa: E402
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


def test_a_run_with_no_setup_command_withheld_nothing_and_is_not_degraded_for_it():
    """`"none"` against `"not-run"`, which are one measurement apart and were one literal.

    Rule 6 fails closed on a measurement that was not TAKEN. A run whose §5 confirmation
    named no setup command has none to take — `gate.Confirmation` admits an empty setup and
    refuses only an empty VERIFY — so degrading it is refusing to promote a seat over
    evidence that was never owed. The case above holds the other half: a command that exists
    and did not run still degrades.
    """
    assert seat.classify_seat(**{**_BASE, "setup": "none"}).forge == "completed"
    assert seat.classify_seat(**{**_BASE, "setup": "not-run"}).forge == "partial"


def test_a_zero_diff_seat_in_a_no_toolchain_repository_is_verified_not_refused():
    """§8's rule 3 asks for independent verification, and a setup command that does not exist
    is not a verification that was skipped. This exact pair — `setup` with nothing to run and
    a verify that passed — was `runner.run_seat`'s happy path for any repository needing no
    toolchain, and rule 3 refused it as *"a contradiction in the caller's own measurements"*:
    an uncaught raise out of the run loop, after three providers had been paid.

    `verify` is still asked for positively. `"not-run"` degrades to `partial` and `"fail"` is
    still the refusal, because those ARE a measurement missing and a measurement refuting.
    """
    rationale = "the retry already backs off; adding one would double-sleep"
    base = dict(process="valid", artifacts="unusable", proven_read=True, changed=False,
                setup="none", rationale=rationale)
    assert seat.classify_seat(**base, verify="pass").forge == "no_change"
    assert seat.classify_seat(**base, verify="not-run").forge == "partial"
    with pytest.raises(seat.SeatStatusError):
        seat.classify_seat(**base, verify="fail")


def test_verify_has_no_none_because_every_run_has_a_gate_to_take():
    """The two dimensions share a rule and not a vocabulary. `gate.Confirmation` refuses an
    empty verify command, so there is no run in which the gate does not exist — and a value
    nothing can ever measure is one nothing should be classifiable under. Sharing one tuple
    would have admitted it silently, which is the shape of the defect `"none"` exists to fix.
    """
    with pytest.raises(seat.SeatStatusError):
        seat.classify_seat(**{**_BASE, "verify": "none"})


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


def test_a_no_change_whose_verification_has_not_been_taken_yet_is_partial_not_a_refusal():
    """The state EVERY seat is classified in, so a refusal here would discard every one.

    §6 puts verification in a fresh clone the builder never had — "running the confirmed
    command in the seat's own clone therefore measures nothing" — and `gate.quote` prices
    that division: `verify_runs` counts the calibration and the verifier clones, `setup_runs`
    counts the builders as well. So `runner.run_seat` classifies with `verify="not-run"` on
    every seat there is, and a zero-diff seat's argued conclusion has to survive that without
    being promoted to a verdict its evidence does not carry. `partial` is both: not
    discarded, and not `no_change`.

    `verify="fail"` is a different answer and stays a refusal — a measurement that was TAKEN
    and contradicts the claim is not one it is merely too early to have. That is the case
    directly above.
    """
    rationale = "the retry already backs off; adding one would double-sleep"
    for setup_dim in ("pass", "not-run"):
        s = seat.classify_seat(process="valid", artifacts="unusable", proven_read=True,
                               changed=False, setup=setup_dim, verify="not-run",
                               rationale=rationale)
        assert (s.forge, s.verify) == ("partial", "not-run"), setup_dim
    with pytest.raises(seat.SeatStatusError):
        seat.classify_seat(process="valid", artifacts="unusable", proven_read=True,
                           changed=False, setup="pass", verify="not-run")


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


# --------------------------------------------------------------------------- #
# forge_spec / read_proof -- section 8.1's live defect: council's validity policy
# (MIN_SUBSTANTIVE_CHARS=400 + a sentinel check), run unmodified against a forge seat,
# scores a real forty-minute effort `non_substantive` on its one-line sign-off and
# re-runs the same argv IN THE SAME CWD, on top of the seat's own half-finished work.
#
# The two tests below are corrected from the plan's draft, which called
# `spec.validator(text, token)` and read a dict back via `.get("valid")`. The real
# injection point (council/engine.py:1167, run_provider) calls
# `spec.validator(exit_code, stdout, stderr, spec)` and reads `evaluate`'s own 4-tuple
# `(valid, reason, result_text, structured)` (council/engine.py:1064-65) -- calling the
# real validator with two positional args raises TypeError before any assertion runs,
# and even fixing the arity, "Done -- `make verify` passes." is not valid raw stdout
# for either provider's extractor (claude expects a JSON envelope, codex expects
# NDJSON events), so it would score `parse_failure` regardless of the fix this task
# makes. Fixed to call the validator the way `run_provider` actually does, with raw
# stdout shaped the way each provider's real CLI emits it.
# --------------------------------------------------------------------------- #
def test_a_short_but_complete_seat_is_not_invalidated():
    """§8.1's live defect: a seat that works for forty minutes and signs off in one line
    scores `non_substantive` and gets re-run IN THE SAME CWD on its own half-finished work."""
    spec = seat.forge_spec("claude", "do the thing", 900)
    assert spec.min_chars == 0
    assert spec.validator is not None
    stdout = json.dumps({"result": "Done — `make verify` passes."})
    valid, reason, result_text, structured = spec.validator(0, stdout, "", spec)
    assert valid is True
    assert reason == "ok"
    assert result_text == "Done — `make verify` passes."


def test_the_sentinel_is_recorded_and_cannot_invalidate():
    """The precise reading: the token check is RECORDED (via `read_proof`, on the same
    `result_text` the validator returns) and stripped of its power to invalidate. No
    sentinel at all would make `proven read` unmeasurable instead."""
    token = engine.make_sentinel()
    assert seat.read_proof(f"I read the task. {token}", token) is True
    assert seat.read_proof("I read the task.", token) is False

    spec = seat.forge_spec("codex", engine.apply_sentinel("p", token), 900)
    # `forge_spec` never populates `.sentinel` itself, so ALSO wire it up here the way
    # a caller reasonably could -- `ProviderSpec.sentinel` exists for exactly this in
    # council's own codepath. If the validator's neutralization is real rather than an
    # accident of `.sentinel` starting unset, invalidation must still not fire.
    spec.sentinel = token
    stdout = "\n".join([
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "no token here"}}),
        json.dumps({"type": "turn.completed"}),
    ])
    valid, reason, result_text, structured = spec.validator(0, stdout, "", spec)
    assert valid is True, "a missing token must not invalidate"
    assert reason == "ok"
    # Recorded, not vetoed: proven_read reads the same text independently and is False,
    # while `valid` above stayed True -- the exact split section 8.1 asks for.
    assert seat.read_proof(result_text, token) is False


def test_forge_spec_refuses_a_keyword_it_does_not_understand():
    """`**kw` names two optional parameters and used to drop everything else. A typo'd
    `workdirr=` built a spec silently missing what was asked for — and `sentinel=`/
    `min_chars=` are exactly what a caller reading `ProviderSpec` would try to pass, so the
    quiet reading of one is a seat running under a policy nobody chose."""
    spec = seat.forge_spec("claude", "do the thing", 900, workdir=Path("."))
    assert spec.validator is not None, "the spelled-right keyword still works"
    with pytest.raises(seat.SeatStatusError, match="workdirr"):
        seat.forge_spec("claude", "do the thing", 900, workdirr=Path("."))


def test_read_proof_fails_closed_on_a_missing_token():
    """No token configured is a measurement not taken, not a free pass -- deliberately
    the opposite of `council.engine._cites_sentinel`, which treats an absent token as
    "nothing to prove" and returns True. That is the right default for a council
    question (was a check even configured); `proven_read` asks a different question
    (was reading proven) and section 8 already has a status for "not measured":
    `partial`, never `completed`."""
    assert seat.read_proof("anything at all", None) is False
    assert seat.read_proof("", "SENTINEL-abc") is False


def test_the_agy_seat_is_given_its_clone_as_a_workspace(tmp_path):
    """MEASURED LIVE, AND NO FAKE LAUNCHER COULD HAVE FOUND IT. With `spec.cwd` set to the
    seat clone and no `--add-dir`, agy wrote its file to `~/.gemini/antigravity-cli/scratch/`
    and ANSWERED that it had created it in the current working directory — exit 0,
    `status: SUCCESS`, sentinel quoted, so the seat scored `valid` with nothing in its clone.

    That scratch path is FIXED and SHARED: not per seat, not under the run directory, not
    reclaimed by `--gc`. So it breaks §4's isolation across RUNS, not just within one.

    The assertion is that the seat path reaches agy's workspace flag — the external question
    — rather than that some literal argv list matches, which would pass over the day
    `build_real_spec` reorders its flags.
    """
    spec = seat.forge_spec("agy", "P", 600, cfg={}, workdir=str(tmp_path))
    argv = [str(a) for a in spec.argv]
    assert "--add-dir" in argv, argv
    assert argv[argv.index("--add-dir") + 1] == str(tmp_path)


def test_the_other_two_seats_do_not_get_the_flag(tmp_path):
    """The discrimination check: `--add-dir` is agy's own flag and passing it to a CLI that
    does not take it is an argv error at launch, on the one path this package cannot test
    against a fake."""
    for name in ("claude", "codex"):
        argv = [str(a) for a in seat.forge_spec(name, "P", 600, cfg={},
                                                workdir=str(tmp_path)).argv]
        assert "--add-dir" not in argv, (name, argv)
