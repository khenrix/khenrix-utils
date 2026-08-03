"""§14.2's per-seat record, with the schema its first real reader needs."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import pytest  # noqa: E402
from forge import fingerprint, seatrecord  # noqa: E402


def _pi_row():
    return fingerprint.as_row(fingerprint.PromptIdentity(
        "a" * 64, "b" * 64, "c" * 64, "/usr/bin/claude", "2.1.220", "opus-5", "opus-5",
        "d" * 64))


def _attempt(**kw):
    base = dict(attempt=1, path="/run/seat-claude-1", branch="khenrix-forge/claude",
                sentinel="SENTINEL-abc", status=None, verification=None,
                verification_refused=None, setup_run=None, verifier_setup=None,
                artifacts={"paths": [], "origin": {}, "setup_overlap": [],
                           "verify_overlap": []},
                candidate={"baseline_ref": "refs/x", "baseline_commit": "0" * 40,
                           "tracked_patch_bytes": 0, "sidecars": [], "omitted": [],
                           "generator_contract_id": "", "gate_delta": None,
                           "gate_surface": None},
                launch=None, prompt_identity=_pi_row())
    base.update(kw)
    return base


def test_a_well_formed_record_decodes(tmp_path):
    rec = seatrecord.decode({"name": "claude", "attempts": [_attempt()]})
    assert rec.name == "claude" and len(rec.attempts) == 1
    assert rec.attempts[0].prompt_identity.cli_version == "2.1.220"


def test_the_measured_seat_record_typo_is_now_refused():
    """MEASURED on runstate.write_seat: `{"phse": "biulding"}` is written, read and
    reconstructed with no complaint. This is the reader that ends that."""
    with pytest.raises(seatrecord.SeatRecordError, match="does not know"):
        seatrecord.decode({"name": "claude", "attempts": [{**_attempt(), "phse": "biulding"}]})


def test_a_missing_field_is_refused_rather_than_defaulted():
    a = _attempt()
    del a["sentinel"]
    with pytest.raises(seatrecord.SeatRecordError, match="missing"):
        seatrecord.decode({"name": "claude", "attempts": [a]})


def test_a_launch_that_returned_no_fingerprint_is_null_not_absent():
    """§8's proven_read/partial rule: a measurement never taken is `partial`, not a free
    pass. The KEY is always present; its value is None."""
    rec = seatrecord.decode({"name": "claude",
                             "attempts": [_attempt(prompt_identity=None)]})
    assert rec.attempts[0].prompt_identity is None


def test_a_malformed_fingerprint_is_a_refusal_not_a_none():
    """The difference that matters: 'nobody measured' and 'somebody wrote nonsense' are not
    the same record."""
    bad = _attempt(prompt_identity={"prompt_sha256": "a" * 64})
    with pytest.raises(seatrecord.SeatRecordError, match="missing"):
        seatrecord.decode({"name": "claude", "attempts": [bad]})


def test_an_attempt_number_that_repeats_is_refused():
    """§8.1 preserves every attempt as partial input; two records under one number make
    'which clone is this' unanswerable."""
    with pytest.raises(seatrecord.SeatRecordError, match="twice"):
        seatrecord.decode({"name": "claude",
                           "attempts": [_attempt(attempt=1), _attempt(attempt=1)]})


def test_a_half_gate_measurement_is_refused():
    """`with_gate_measurement`'s rule: a delta with no surface beside it cannot say whether
    `()` means 'measured, nothing moved' or 'nothing was looked at'."""
    half = _attempt(candidate={**_attempt()["candidate"], "gate_delta": []})
    with pytest.raises(seatrecord.SeatRecordError, match="gate_surface"):
        seatrecord.decode({"name": "claude", "attempts": [half]})


def test_a_candidate_missing_the_gate_pair_entirely_is_refused(tmp_path):
    """The half-gate rule read the two keys with `.get`, so a candidate carrying NEITHER
    passed — two absent keys are equal, and equal is what that check admits. A record whose
    candidate cannot say what §6.1 measured is exactly the record this reader exists for, and
    `None` (nobody measured) is a value that has to be WRITTEN rather than inferred from a
    missing key."""
    thin = _attempt(candidate={"baseline_ref": "refs/x"})
    with pytest.raises(seatrecord.SeatRecordError, match="gate_delta"):
        seatrecord.decode({"name": "claude", "attempts": [thin]})


def test_identities_returns_one_entry_per_attempt_including_the_unmeasured_ones():
    rec = seatrecord.decode({"name": "claude",
                             "attempts": [_attempt(attempt=1),
                                          _attempt(attempt=2, prompt_identity=None)]})
    got = seatrecord.identities(rec)
    assert len(got) == 2 and got[1] is None


def test_an_empty_record_is_refused():
    with pytest.raises(seatrecord.SeatRecordError, match="at least one"):
        seatrecord.decode({"name": "claude", "attempts": []})
