"""§13.1's post-fusion deep review: the council backend, the five reasons, and a diff
nobody measured.

The diff-measurement half is ported unchanged from the cloud-review module this replaced —
it was never about the cloud, and its tests are the ones that caught a rename stream no
record-at-a-time parser can read. The invocation half is new: three local seats instead of
one remote fleet, so the tests that asserted minutes-vs-seconds, session URLs and exit
classification are gone with the code they covered.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import deepreview, review  # noqa: E402

from forge_fixtures import commit_all, git as fixture_git, make_repo, write  # noqa: E402


def _repo(tmp_path):
    r = make_repo(tmp_path)
    write(r, "a.txt", "one\n")
    base = commit_all(r, "base")
    write(r, "a.txt", "one\ntwo\n")
    write(r, "b.txt", "new\n")
    head = commit_all(r, "head")
    return r, base, head


def _panel(tmp_path, *seats):
    """A fake council: `seats` are (name, valid, answer) triples. Returns the callable
    `run_deep_review` accepts as `council=`, plus the calls it recorded."""
    calls = []

    def council(prompt, workdir, *, mode="deep"):
        calls.append({"prompt": prompt, "workdir": workdir, "mode": mode})
        providers = []
        for i, (name, valid, answer) in enumerate(seats):
            f = Path(tmp_path) / f"seat-{i}-{name}.txt"
            f.write_text(answer)
            providers.append({"name": name, "valid": valid,
                              "reason": "ok" if valid else "timeout",
                              "result_file": str(f)})
        return {"providers": providers}

    council.calls = calls
    return council


def _payload(*rows):
    return "prose before\n```json\n" + json.dumps({"findings": list(rows)}) + "\n```\nafter"


# --------------------------------------------------------------------------- #
# Diff measurement — ported with the code it covers.
# --------------------------------------------------------------------------- #
def test_a_text_diff_is_measured(tmp_path):
    r, base, head = _repo(tmp_path)
    d = deepreview.measure_diff(r, base, head)
    assert (d.files, d.lines) == (2, 2) and d.why == ""


def test_a_binary_diff_leaves_the_line_count_unknown(tmp_path):
    r, base, _ = _repo(tmp_path)
    (r / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    head = commit_all(r, "binary")
    d = deepreview.measure_diff(r, base, head)
    assert d.lines is None and d.files == 3
    assert "blob.bin" in d.why


def test_a_truncated_refusal_list_says_how_many_there_were(tmp_path):
    """A SAMPLE IS NEVER READ AS THE WHOLE. `why` spells out five refusals; without the
    count beside them a reader doing the obvious arithmetic ("500 files, 5 unmeasured") is
    wrong by an order of magnitude, on the one record whose job is to say how much of the
    diff was measured."""
    r, base, _ = _repo(tmp_path)
    for i in range(9):
        (r / f"blob{i}.bin").write_bytes(bytes([0, 1, 2, i]))
    head = commit_all(r, "nine binaries")
    d = deepreview.measure_diff(r, base, head)
    assert d.lines is None
    assert d.why.count("binary delta") == deepreview._REFUSAL_SAMPLE
    assert "9 path(s) were refused" in d.why
    assert "were refused" not in deepreview._why(("a: no count",))


def test_a_diff_size_that_could_not_have_been_measured_is_refused():
    assert deepreview.DiffSize(2, 2, "").files == 2
    assert deepreview.DiffSize(2, None, "binary").lines is None
    with pytest.raises(deepreview.DeepReviewError):
        deepreview.DiffSize(None, 5, "git said nothing about the files")
    for bad in ((None, None, ""), (2, None, "   ")):
        with pytest.raises(deepreview.DeepReviewError):
            deepreview.DiffSize(*bad)
    for bad in (True, -1, "2", 1.0):
        with pytest.raises(deepreview.DeepReviewError):
            deepreview.DiffSize(bad, 0, "")


def test_a_rename_is_one_file_and_not_a_stream_nobody_could_parse(tmp_path):
    """MEASURED on git 2.53.0: `git diff --numstat -z` emits a rename as THREE
    NUL-terminated fields — an empty path field then the source and destination names — so
    a record-at-a-time reader raises out of the middle of a size measurement."""
    r = make_repo(tmp_path)
    write(r, "old.txt", "l1\nl2\nl3\nl4\nl5\n")
    write(r, "keep.txt", "x\n")
    base = commit_all(r, "base")
    (r / "old.txt").unlink()
    write(r, "sub/new.txt", "l1\nl2\nl3\nl4\nl6\n")
    write(r, "keep.txt", "x\ny\n")
    head = commit_all(r, "renamed")
    assert " => " in fixture_git(r, "diff", "--numstat", f"{base}..{head}").stdout

    d = deepreview.measure_diff(r, base, head)
    assert (d.files, d.lines) == (2, 3), d
    assert d.why == ""
    files, lines, why = deepreview._numstat_z("-\t-\t\0old.bin\0new.bin\0")
    assert (files, lines) == (1, None)
    assert "old.bin -> new.bin" in why[0]


def test_a_numstat_stream_that_ends_mid_rename_measures_nothing():
    assert deepreview._numstat_z("1\t1\t\0old.txt\0")[:2] == (None, None)
    assert deepreview._numstat_z("garbage-with-no-tabs\0")[:2] == (None, None)
    assert deepreview._numstat_z("1\t1\t\0\0new.txt\0")[:2] == (None, None)


def test_two_reasons_a_line_count_could_not_be_taken_do_not_read_alike():
    files, lines, why = deepreview._numstat_z("2\tmany\tt.txt\0")
    assert (files, lines) == (1, None)
    assert "t.txt" in why[0] and "binary" not in why[0]
    assert "binary" in deepreview._numstat_z("-\t-\tb.bin\0")[2][0]


def test_an_unnamed_checkpoint_is_refused_rather_than_measured_against_head(tmp_path):
    """MEASURED: `git diff --numstat -z "..<head>"` exits 0 and prints NOTHING, because the
    empty side resolves to HEAD. A run whose base was never resolved would record a
    0-file diff and review a change this engine never measured."""
    r, base, head = _repo(tmp_path)
    for bad_base, bad_head in (("", head), (base, ""), (None, head), ("--output=/tmp/x", head)):
        with pytest.raises(deepreview.DeepReviewError):
            deepreview.measure_diff(r, bad_base, bad_head)


# --------------------------------------------------------------------------- #
# The record's invariants.
# --------------------------------------------------------------------------- #
def test_a_review_that_did_not_run_cannot_carry_findings():
    """`None` and `()` are different facts and the record refuses to blur them: an empty
    tuple says "ran and found nothing", which is the one sentence a review that never
    happened must not be able to write."""
    with pytest.raises(deepreview.DeepReviewError):
        deepreview.DeepReview(deepreview.RAN, None, None, (), False, "d")
    with pytest.raises(deepreview.DeepReviewError):
        deepreview.DeepReview(deepreview.SKIPPED, None, (), (), False, "d")


def test_a_reason_belongs_only_to_an_unavailable_review():
    with pytest.raises(deepreview.DeepReviewError):
        deepreview.DeepReview(deepreview.UNAVAILABLE, None, None, (), False, "d")
    with pytest.raises(deepreview.DeepReviewError):
        deepreview.DeepReview(deepreview.RAN, "no_valid_seats", (), (), False, "d")
    with pytest.raises(deepreview.DeepReviewError):
        deepreview.DeepReview("invented", None, None, (), False, "d")
    with pytest.raises(deepreview.DeepReviewError):
        deepreview.DeepReview(deepreview.UNAVAILABLE, "made_up", None, (), False, "d")


def test_seats_is_a_tuple_so_a_degraded_panel_is_visible():
    """`seats` replaced the cloud record's session_url precisely so a 1-of-3 review cannot
    read like a full one."""
    with pytest.raises(deepreview.DeepReviewError):
        deepreview.DeepReview(deepreview.RAN, None, (), ["claude"], False, "d")
    assert deepreview.DeepReview(deepreview.RAN, None, (), ("claude",), True, "d").seats \
        == ("claude",)


# --------------------------------------------------------------------------- #
# The findings contract.
# --------------------------------------------------------------------------- #
def test_a_clean_payload_parses_into_findings():
    got = deepreview._parse_findings(
        _payload({"severity": "blocker", "location": "a.py:1", "description": "off by one"}),
        "claude", 2)
    assert len(got) == 1 and got[0].severity == "blocker" and got[0].seat == "claude"
    assert got[0].round == 2 and got[0].evidence == "a.py:1"


def test_a_finding_with_no_location_records_that_it_had_none():
    """`review.Finding` refuses an empty evidence string, so a row that named no location
    says so in words rather than being dropped — a finding out of a review that cost three
    provider calls is not discarded over a missing field."""
    got = deepreview._parse_findings(
        _payload({"severity": "minor", "description": "vague worry"}), "agy", 1)
    assert len(got) == 1 and "named no location" in got[0].evidence


def test_a_repeated_finding_is_one_finding_not_two():
    """Content-derived ids collide when a seat restates one finding — ordinary model
    output, not malformed input — and `Round` refuses duplicate ids."""
    row = {"severity": "blocker", "location": "x.py", "description": "same"}
    got = deepreview._parse_findings(_payload(row, row), "codex", 1)
    assert len(got) == 1


def test_an_unmapped_severity_is_dropped_rather_than_guessed():
    got = deepreview._parse_findings(
        _payload({"severity": "spicy", "description": "d"},
                 {"severity": "low", "description": "e"}), "claude", 1)
    assert len(got) == 1 and got[0].severity == "minor"


def test_prose_with_no_payload_parses_to_nothing():
    assert deepreview._parse_findings("I looked and found nothing.", "claude", 1) == ()
    assert deepreview._parse_findings("```json\nnot json\n```", "claude", 1) == ()
    assert deepreview._parse_findings("", "claude", 1) == ()


def test_every_severity_this_module_maps_to_is_one_review_declares():
    assert set(deepreview._SEVERITY_MAP.values()) <= set(review.SEVERITIES)


# --------------------------------------------------------------------------- #
# The runner.
# --------------------------------------------------------------------------- #
def test_opting_out_skips_without_convening_anything(tmp_path):
    r, base, head = _repo(tmp_path)
    council = _panel(tmp_path, ("claude", True, _payload()))
    u = deepreview.run_deep_review(tmp_path / "run", checkout=r, base=base, head=head,
                                   round_=1, enabled=False, council=council)
    assert u.status == deepreview.SKIPPED and u.findings is None and u.seats == ()
    assert council.calls == [], "an opted-out review spends nothing"


def test_an_oversized_diff_is_unavailable_without_convening_anything(tmp_path, monkeypatch):
    r, base, head = _repo(tmp_path)
    monkeypatch.setattr(deepreview, "DIFF_BYTE_CAP", 10)
    council = _panel(tmp_path, ("claude", True, _payload()))
    u = deepreview.run_deep_review(tmp_path / "run", checkout=r, base=base, head=head,
                                   round_=1, council=council)
    assert u.status == deepreview.UNAVAILABLE and u.reason == "diff_too_large"
    assert council.calls == [], "the pre-flight refuses before the panel costs anything"


def test_a_panel_where_no_seat_answered_is_unavailable_not_an_empty_review(tmp_path):
    r, base, head = _repo(tmp_path)
    council = _panel(tmp_path, ("claude", False, ""), ("codex", False, ""))
    u = deepreview.run_deep_review(tmp_path / "run", checkout=r, base=base, head=head,
                                   round_=1, council=council)
    assert u.status == deepreview.UNAVAILABLE and u.reason == "no_valid_seats"
    assert u.findings is None, "no seat answered, so there is nothing to report"
    assert "claude (timeout)" in u.detail


def test_seats_that_answer_unreadably_are_not_a_review_that_found_nothing(tmp_path):
    """THE FALSE GREEN THIS MODULE EXISTS AGAINST. Three seats can answer at length and
    still produce no payload; recording that as `ran` with zero findings would report a
    clean review nobody performed."""
    r, base, head = _repo(tmp_path)
    council = _panel(tmp_path, ("claude", True, "I read it all. Looks fine to me!"),
                     ("codex", True, "No structured output here either."))
    u = deepreview.run_deep_review(tmp_path / "run", checkout=r, base=base, head=head,
                                   round_=1, council=council)
    assert u.status == deepreview.UNAVAILABLE and u.reason == "unreadable_output"
    assert u.findings is None and u.seats == ("claude", "codex")


def test_an_empty_payload_IS_a_review_that_found_nothing(tmp_path):
    """The mirror of the case above, and the reason `_parse_findings` cannot decide this
    alone: a seat that returned an empty `findings` list DID answer the question."""
    r, base, head = _repo(tmp_path)
    council = _panel(tmp_path, ("claude", True, _payload()))
    u = deepreview.run_deep_review(tmp_path / "run", checkout=r, base=base, head=head,
                                   round_=1, council=council)
    assert u.status == deepreview.RAN and u.findings == () and u.reason is None


def test_findings_from_several_seats_merge_and_deduplicate(tmp_path):
    r, base, head = _repo(tmp_path)
    shared = {"severity": "blocker", "location": "a.py:2", "description": "both saw this"}
    council = _panel(
        tmp_path,
        ("claude", True, _payload(shared, {"severity": "minor", "description": "only claude"})),
        ("codex", True, _payload(shared)),
        ("agy", False, ""))
    u = deepreview.run_deep_review(tmp_path / "run", checkout=r, base=base, head=head,
                                   round_=3, council=council)
    assert u.status == deepreview.RAN
    claims = sorted(f.claim for f in u.findings)
    assert claims == ["both saw this", "only claude"], "the shared finding is not doubled"
    assert u.seats == ("claude", "codex"), "the failed seat is not listed as answering"
    assert all(f.round == 3 for f in u.findings)
    both = next(f for f in u.findings if f.claim == "both saw this")
    assert "corroborated by claude, codex" in both.evidence, \
        "cross-seat agreement is the panel's whole value and is recorded, not discarded"
    only = next(f for f in u.findings if f.claim == "only claude")
    assert "corroborated" not in only.evidence, "a single-seat finding claims no agreement"


def test_one_seat_restating_a_finding_is_not_corroboration(tmp_path):
    """The merge keys on CONTENT, so a seat that repeats itself must not read as two
    reviewers agreeing — that would manufacture the exact signal the merge exists to
    report."""
    r, base, head = _repo(tmp_path)
    row = {"severity": "blocker", "location": "a.py", "description": "same bug"}
    council = _panel(tmp_path, ("claude", True, _payload(row, row)))
    u = deepreview.run_deep_review(tmp_path / "run", checkout=r, base=base, head=head,
                                   round_=1, council=council)
    assert len(u.findings) == 1
    assert "corroborated" not in u.findings[0].evidence


def test_the_diff_reaches_the_panel_in_the_prompt(tmp_path):
    """NON-VACUITY: a panel asked to review nothing would answer about nothing."""
    r, base, head = _repo(tmp_path)
    council = _panel(tmp_path, ("claude", True, _payload()))
    deepreview.run_deep_review(tmp_path / "run", checkout=r, base=base, head=head,
                               round_=1, council=council)
    prompt = council.calls[0]["prompt"]
    assert "b.txt" in prompt and "+two" in prompt, "the fused diff is what was reviewed"
    assert "```json" in prompt, "and the findings contract was stated"


def test_an_unmeasurable_diff_still_runs_and_records_that_nobody_measured_it(tmp_path):
    """Refusing on a byte count nobody could take would report `diff_too_large` about a
    diff whose size is unknown — a reason stated more confidently than the evidence."""
    r, base, _ = _repo(tmp_path)
    council = _panel(tmp_path, ("claude", True, _payload()))
    u = deepreview.run_deep_review(tmp_path / "run", checkout=r, base=base,
                                   head="0" * len(base), round_=1, council=council)
    assert u.diff_measured is False
    assert u.status in (deepreview.RAN, deepreview.UNAVAILABLE)


def test_every_status_and_reason_this_module_can_produce_is_a_declared_one(tmp_path):
    """The vocabulary is closed: a fifth status added to `STATUSES` without a rendering in
    `handover._deep_review_line` is the drift this asserts against."""
    r, base, head = _repo(tmp_path)
    seen = set()
    for enabled, seats in ((False, []), (True, [("claude", False, "")]),
                           (True, [("claude", True, _payload())]),
                           (True, [("claude", True, "prose only")])):
        u = deepreview.run_deep_review(
            tmp_path / f"run{len(seen)}", checkout=r, base=base, head=head, round_=1,
            enabled=enabled, council=_panel(tmp_path, *seats))
        seen.add((u.status, u.reason))
    for status, reason in seen:
        assert status in deepreview.STATUSES
        assert reason is None or reason in deepreview.REASONS
    assert len(seen) >= 3, "the fixtures reached more than one outcome"
