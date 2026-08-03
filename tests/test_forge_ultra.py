"""§13.1's ultrareview: minutes, the six reasons, and a diff nobody measured."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from council import engine  # noqa: E402
from forge import gitcmd, review, ultra, verify  # noqa: E402

from forge_fixtures import commit_all, git as fixture_git, make_repo, write  # noqa: E402


def _repo(tmp_path):
    r = make_repo(tmp_path)
    write(r, "a.txt", "one\n")
    base = commit_all(r, "base")
    write(r, "a.txt", "one\ntwo\n")
    write(r, "b.txt", "new\n")
    head = commit_all(r, "head")
    return r, base, head


def _proc(rc=0, out="", err=""):
    def run(argv, **kw):
        run.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, rc, out, err)
    run.calls = []
    return run


def test_the_timeout_is_in_minutes_and_a_seconds_value_is_refused():
    """MEASURED 2026-08-03: --timeout <minutes>, default 30. MODE_TIMEOUT['deep'] is 1200."""
    a = ultra.argv(timeout_minutes=30, target=None)
    assert a[:2] == ["claude", "ultrareview"]
    assert a[a.index("--timeout") + 1] == "30"
    assert "--json" in a
    with pytest.raises(ultra.UltraError):
        ultra.argv(timeout_minutes=engine.MODE_TIMEOUT["deep"], target=None)
    with pytest.raises(ultra.UltraError):
        ultra.argv(timeout_minutes=verify.Step(argv=("true",)).timeout, target=None)
    with pytest.raises(ultra.UltraError):
        ultra.argv(timeout_minutes=0, target=None)


def test_a_target_is_the_last_positional():
    assert ultra.argv(timeout_minutes=30, target="main")[-1] == "main"


def test_a_text_diff_is_measured(tmp_path):
    r, base, head = _repo(tmp_path)
    d = ultra.measure_diff(r, base, head)
    assert (d.files, d.lines) == (2, 2) and d.why == ""


def test_a_binary_diff_leaves_the_line_count_unknown(tmp_path):
    r, base, _ = _repo(tmp_path)
    (r / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    head = commit_all(r, "binary")
    d = ultra.measure_diff(r, base, head)
    assert d.lines is None and d.files == 3
    assert "blob.bin" in d.why


def test_a_truncated_refusal_list_says_how_many_there_were(tmp_path):
    """A SAMPLE IS NEVER READ AS THE WHOLE — `record_worktree_after`'s rule, one record over.
    `why` spells out five refusals; without the count beside them a reader doing the obvious
    arithmetic ("500 files, 5 unmeasured") is wrong by an order of magnitude, on the one
    record whose job is to say how much of the diff was measured."""
    r, base, _ = _repo(tmp_path)
    for i in range(9):
        (r / f"blob{i}.bin").write_bytes(bytes([0, 1, 2, i]))
    head = commit_all(r, "nine binaries")
    d = ultra.measure_diff(r, base, head)
    assert d.lines is None
    assert d.why.count("binary delta") == ultra._REFUSAL_SAMPLE, "the sample is still bounded"
    assert "9 path(s) were refused" in d.why
    # And a list that FITS is printed whole, with no count implying it was cut.
    assert "were refused" not in ultra._why(("a: no count",))


def test_a_diff_size_that_could_not_have_been_measured_is_refused():
    """IT VALIDATES ITSELF, like every other record in this package — it was the one that did
    not. A voided file count beside a line count describes no read git can perform, and an
    absent count with no `why` is a refusal that declines to say what it refused, reaching the
    operator as a blank in the line `run_ultra` writes."""
    assert ultra.DiffSize(2, 2, "").files == 2                    # the complete case
    assert ultra.DiffSize(2, None, "binary").lines is None
    with pytest.raises(ultra.UltraError):
        ultra.DiffSize(None, 5, "git said nothing about the files")
    for bad in ((None, None, ""), (2, None, "   ")):
        with pytest.raises(ultra.UltraError):
            ultra.DiffSize(*bad)
    for bad in (True, -1, "2", 1.0):
        with pytest.raises(ultra.UltraError):
            ultra.DiffSize(bad, 0, "")


def test_an_oversized_diff_is_unavailable_without_spending(tmp_path):
    r, base, head = _repo(tmp_path)
    run = _proc()
    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, round_=1,
                        run=run, file_limit=1)
    assert u.status == ultra.UNAVAILABLE and u.reason == "diff_too_large"
    assert run.calls == [], "the local pre-flight must refuse before the remote is paid for"


def test_an_unmeasurable_diff_runs_and_records_that_nobody_measured_it(tmp_path):
    """The remote is the authority on its own limits. What must never happen is a record
    saying the diff was under the limit when its line count was never taken."""
    r, base, _ = _repo(tmp_path)
    (r / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    head = commit_all(r, "binary")
    run = _proc(0, json.dumps({"bugs": []}))
    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, round_=1, run=run)
    assert u.status == ultra.RAN and u.diff_measured is False
    assert len(run.calls) == 1


def test_a_clean_json_payload_parses_into_findings(tmp_path):
    r, base, head = _repo(tmp_path)
    payload = {"bugs": [{"severity": "blocker", "description": "off-by-one",
                         "location": "a.txt:2"}]}
    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, round_=1,
                        run=_proc(0, json.dumps(payload)))
    assert u.status == ultra.RAN and len(u.bugs) == 1
    assert u.bugs[0].severity == "blocker" and "off-by-one" in u.bugs[0].claim
    assert u.bugs[0].evidence == "a.txt:2", "the location is what a fix pass reads"


def test_a_bug_with_no_location_records_that_it_had_none(tmp_path):
    """`review.Finding` refuses an empty evidence string, so a payload naming no location says
    so IN WORDS. Refusing the row instead would discard a finding out of a review that was
    paid for and cannot be re-run, over a field this module has never measured the remote to
    emit."""
    r, base, head = _repo(tmp_path)
    payload = {"bugs": [{"severity": "high", "description": "off-by-one"}]}
    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, round_=1,
                        run=_proc(0, json.dumps(payload)))
    assert u.status == ultra.RAN and u.bugs[0].severity == "blocker"
    assert "named no location" in u.bugs[0].evidence


def test_an_exit_zero_with_unreadable_json_is_not_a_clean_review(tmp_path):
    """THE FAIL-OPEN. §13.1's five reasons do not cover this; folding it into 'found no
    bugs' is the false green this whole project keeps finding."""
    r, base, head = _repo(tmp_path)
    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, round_=1,
                        run=_proc(0, "Review complete. Looks good!"))
    assert u.status == ultra.UNAVAILABLE and u.reason == "unreadable_output"
    assert u.bugs is None


def test_each_named_unavailability_reason_is_recognised():
    cases = {
        "no_auth": (1, "", "You are not logged in to claude.ai. Run /login."),
        "zdr_org": (1, "", "This organization has zero data retention enabled."),
        "usage_credits_off": (1, "", "Extra usage credits are disabled for this account."),
        "diff_too_large": (1, "", "The diff exceeds the 500 file limit."),
    }
    for expected, (rc, out, err) in cases.items():
        assert ultra.classify(rc, out, err) == expected, expected


def test_an_unrecognised_exit_one_is_exit_one_and_never_a_guess():
    assert ultra.classify(1, "", "something nobody has seen before") == "exit_1"


def test_a_declared_phrase_matches_a_whole_token_and_not_a_substring():
    """`zdr` is three characters. As a bare substring it labels any stderr that happens to
    contain them — a path, a branch, a session id. Every reason degrades identically, so the
    cost is not the verdict; it is the line a human reads to find out what went wrong."""
    assert ultra.classify(1, "", "could not read /tmp/zdrafts/session.log") == "exit_1"
    assert ultra.classify(1, "", "This organization has ZDR enabled.") == "zdr_org"


def test_a_finding_this_module_produces_is_addressable_by_a_review_round(tmp_path):
    """CONTRADICTION 6's mechanism, such as this plan has one. `review.round_dir` refuses a
    round below 1, so a finding carrying round 0 fits in no record `terminal_from_record`
    reads — §13.1's findings could not reach the terminal this plan assigns them at all."""
    r, base, head = _repo(tmp_path)
    payload = {"bugs": [{"severity": "blocker", "description": "off-by-one"}]}
    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, round_=3,
                        run=_proc(0, json.dumps(payload)))
    assert u.bugs[0].round == 3
    assert review.round_dir(tmp_path / "run", u.bugs[0].round).name == "round-3"
    # And a Round record really will hold it — `Round.__post_init__` checks the two agree.
    assert review.Round(3, "a" * 40, u.bugs, (), ("ultrareview",), ()).round == 3
    for bad in (0, -1, True, "1"):
        with pytest.raises(ultra.UltraError):
            ultra.run_ultra(tmp_path / f"run-{bad}", checkout=r, base=base, head=head,
                            round_=bad, run=_proc(0, json.dumps(payload)))


def test_a_zero_exit_classifies_as_nothing():
    assert ultra.classify(0, '{"bugs": []}', "") is None


def test_a_timeout_records_the_session_url_and_says_the_review_is_still_running(tmp_path):
    r, base, head = _repo(tmp_path)

    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0), output="",
                                        stderr="session: https://claude.ai/review/abc123\n")

    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, round_=1, run=run)
    assert u.status == ultra.TIMED_OUT
    assert u.session_url == "https://claude.ai/review/abc123"
    assert "still running" in u.detail


def test_a_review_that_did_not_run_cannot_carry_findings():
    """`bugs=()` on any status but `ran` reads as "the review ran and reported nothing",
    which is the one sentence an unavailable review must not be able to write. Measured, this
    branch had no test at all and its mutation SURVIVED."""
    for status in (ultra.UNAVAILABLE, ultra.TIMED_OUT, ultra.SKIPPED):
        reason = "no_auth" if status == ultra.UNAVAILABLE else None
        with pytest.raises(ultra.UltraError):
            ultra.Ultra(status, reason, (), None, True, "an empty tuple is a review that ran")
    with pytest.raises(ultra.UltraError):      # and the other direction
        ultra.Ultra(ultra.RAN, None, None, None, True, "a review that ran reports its findings")


def test_no_ultra_skips_without_spending(tmp_path):
    r, base, head = _repo(tmp_path)
    run = _proc()
    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, round_=1,
                        enabled=False, run=run)
    assert u.status == ultra.SKIPPED and run.calls == [] and u.bugs is None


def test_the_bugs_payload_lands_in_the_run_directory(tmp_path):
    r, base, head = _repo(tmp_path)
    d = tmp_path / "run"
    ultra.run_ultra(d, checkout=r, base=base, head=head, round_=1,
                    run=_proc(0, json.dumps({"bugs": []})))
    assert json.loads((d / "ultrareview" / "bugs.json").read_text()) == {"bugs": []}


def test_the_unreviewed_label_is_named_by_its_constant_and_never_respelled():
    """WHAT THIS CAN AND CANNOT SAY. `ultra` APPLIES no label — the terminal does, in
    `review.terminal_from_record` — so the only thing this module can get wrong about the
    phrase is spelling it a second way. It refers to the constant BY NAME, in prose; it does
    not import it, and the previous name of this test said it did. That claim was never true:
    `"VERIFIED_NOT_INDEPENDENTLY_REVIEWED" in src` is satisfied by a docstring, and no code
    path here reads the constant. The third assertion is the one with teeth."""
    src = (ROOT / "shared" / "lib" / "forge" / "ultra.py").read_text()
    assert "independently re-reviewed" not in src
    assert "VERIFIED_NOT_INDEPENDENTLY_REVIEWED" in src, \
        "the module points at the constant rather than restating what it stands for"
    assert review.VERIFIED_NOT_INDEPENDENTLY_REVIEWED not in src, \
        "the label's TEXT written out here would be the second spelling the constant exists " \
        "to prevent, and a grep for either would find half the run's states"


def test_every_severity_this_module_maps_to_is_one_review_declares():
    """THE CROSS-MODULE VOCABULARY, PINNED WHERE ITS SIBLINGS ARE. `_bugs` builds
    `review.Finding`s out of these values, and `Finding.__post_init__` refuses a severity
    `review.SEVERITIES` does not declare — so a value added here alone turns a paid cloud
    review into `unreadable_output`. Equality both ways: a severity review declares that
    nothing here maps to is one §13.1's findings can never carry."""
    assert set(ultra._SEVERITY_MAP.values()) == set(review.SEVERITIES)


def test_the_module_says_what_it_did_not_measure():
    src = (ROOT / "shared" / "lib" / "forge" / "ultra.py").read_text()
    assert "NOT MEASURED" in src and "no remote" in src


# --- What the fixtures above could not reach -------------------------------------------


def test_a_rename_is_one_file_and_not_a_stream_nobody_could_parse(tmp_path):
    """THE ONE THE FIXTURES ABOVE COULD NOT REACH, and the most ordinary thing a synthesis
    agent does. MEASURED on git 2.53.0: `git diff --numstat -z` emits a rename as THREE
    NUL-terminated fields — `1\\t1\\t\\0old.txt\\0sub/new.txt\\0` — an empty path field
    followed by the source and destination names. A record-at-a-time reader splits
    `"old.txt"` into one field and raises `ValueError: not enough values to unpack` out of
    the middle of a size measurement, which is `run_ultra` FAILING where §13.1 says it
    degrades.

    `git apply --numstat -z` does NOT do this — measured on the same patch, it emits
    `1\\t1\\tsub/new.txt\\0`, one three-field record naming the destination — which is why
    `strategy._numstat` and `bundle._patch_paths` never met this shape and why their
    record-at-a-time parser cannot be reused here.

    Plain `--numstat` reports the same rename as ONE file, `old.txt => sub/new.txt`, at
    1 added / 1 deleted, and that is the count this asserts.
    """
    r = make_repo(tmp_path)
    write(r, "old.txt", "l1\nl2\nl3\nl4\nl5\n")
    write(r, "keep.txt", "x\n")
    base = commit_all(r, "base")
    (r / "old.txt").unlink()
    write(r, "sub/new.txt", "l1\nl2\nl3\nl4\nl6\n")
    write(r, "keep.txt", "x\ny\n")
    head = commit_all(r, "renamed")
    assert " => " in fixture_git(r, "diff", "--numstat", f"{base}..{head}").stdout, \
        "the premise: git detected a rename here, so the -z stream carries the two-name form"

    d = ultra.measure_diff(r, base, head)
    assert (d.files, d.lines) == (2, 3), d
    assert d.why == ""
    # And the pair is still named, so a refusal over a renamed BINARY can point at a file.
    files, lines, why = ultra._numstat_z("-\t-\t\0old.bin\0new.bin\0")
    assert (files, lines) == (1, None)
    assert "old.bin -> new.bin" in why[0]


def test_a_numstat_stream_that_ends_mid_rename_measures_nothing():
    """A rename's two names are read from the NEXT two fields, so a stream that stops
    between them describes a file this run cannot name. Voiding the whole measurement is
    the same call `strategy._numstat` makes for a record with no path in it: the engine no
    longer knows which FILES the diff touched either."""
    assert ultra._numstat_z("1\t1\t\0old.txt\0")[:2] == (None, None)
    assert ultra._numstat_z("garbage-with-no-tabs\0")[:2] == (None, None)
    assert ultra._numstat_z("1\t1\t\0\0new.txt\0")[:2] == (None, None), \
        "an unnamed half of a rename is a file this run cannot point at"


def test_two_reasons_a_line_count_could_not_be_taken_do_not_read_alike():
    """`strategy._numstat_record`'s second refusal, which the draft of this module did not
    have: `int("many")` raises, and a `try/except: continue` around it drops the path's
    lines from a total the record goes on reporting as measured. It is a DIFFERENT fact from
    a binary delta — git measured one and declined to state it, and emitted something nobody
    has measured it to emit for the other — so the two must not fold into one sentence."""
    files, lines, why = ultra._numstat_z("2\tmany\tt.txt\0")
    assert (files, lines) == (1, None)
    assert "t.txt" in why[0] and "binary" not in why[0]
    assert "binary" in ultra._numstat_z("-\t-\tb.bin\0")[2][0]


def test_an_unnamed_checkpoint_is_refused_rather_than_measured_against_head(tmp_path):
    """MEASURED: `git diff --numstat -z "..<head>"` exits 0 and prints NOTHING, because the
    empty side resolves to HEAD and HEAD is the head. A run whose base was never resolved
    would therefore record a 0-file, 0-line diff — comfortably under §13.1's limits — and
    pay for a review of a change this engine never measured."""
    r, base, head = _repo(tmp_path)
    for bad_base, bad_head in (("", head), (base, ""), (None, head), ("--output=/tmp/x", head)):
        with pytest.raises(ultra.UltraError):
            ultra.measure_diff(r, bad_base, bad_head)


def test_a_git_diff_that_failed_is_not_a_diff_this_run_measured(tmp_path):
    """The pre-flight has no counts at all here, so it cannot refuse; the record has to say
    so instead of inheriting the silence as "small enough"."""
    r, base, _ = _repo(tmp_path)
    d = ultra.measure_diff(r, base, "0" * len(base))
    assert (d.files, d.lines) == (None, None) and "git diff" in d.why
    run = _proc(0, json.dumps({"bugs": []}))
    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head="0" * len(base), run=run,
                        round_=1)
    assert u.status == ultra.RAN and u.diff_measured is False and len(run.calls) == 1


def test_a_reason_belongs_only_to_an_unavailable_review():
    """The other half of the reason rule. `Ultra(RAN, "no_auth", …)` is a review that ran AND
    was unavailable, and a consumer reading either field alone gets a different answer about
    the same run — the same defect as `bugs=()` on a review that did not run, one field over.
    """
    for status in (ultra.RAN, ultra.TIMED_OUT, ultra.SKIPPED):
        bugs = () if status == ultra.RAN else None
        with pytest.raises(ultra.UltraError):
            ultra.Ultra(status, "no_auth", bugs, None, True, "a reason without unavailability")
    assert ultra.Ultra(ultra.UNAVAILABLE, "no_auth", None, None, True, "ok").reason == "no_auth"


def test_the_child_that_reviews_a_checkout_cannot_be_pointed_at_another_repository(
        tmp_path, monkeypatch):
    """`ultrareview` reviews "the current branch" (its own `--help`), so it resolves a git
    repository — and `cwd=` is not what decides which one. An ambient GIT_DIR wins over the
    working directory for every git, and a hook, `git rebase --exec` and `git bisect run`
    each export one. That review is UPLOADED, so the cost of inheriting one is the user's
    own repository leaving the machine in place of the synthesis checkout.

    `gitcmd.HOSTILE_ENV` is dropped for the same reason `gitcmd.git`, `fleet.forge_child_env`
    and `verify._gate_env` drop it, and the assertion is over the whole tuple rather than
    over GIT_DIR alone — that docstring's own rule is that a name dropped in one consumer
    and kept in another is a hole.
    """
    r, base, head = _repo(tmp_path)
    for name in gitcmd.HOSTILE_ENV:
        monkeypatch.setenv(name, "/somewhere/that/is/not/the/checkout")
    run = _proc(0, json.dumps({"bugs": []}))
    ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, round_=1, run=run)
    env = run.calls[0][1]["env"]
    assert [n for n in gitcmd.HOSTILE_ENV if n in env] == []
    assert env.get("PATH") == os.environ.get("PATH"), \
        "narrowing git's view of the repository is not a licence to strip the toolchain"


def test_a_timeout_whose_stderr_is_bytes_does_not_report_that_nothing_was_printed(tmp_path):
    """"No session URL was printed" and "I could not read what was printed" are two different
    facts, and the second one loses a review that is still running and still costs money."""
    r, base, head = _repo(tmp_path)

    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0),
                                        stderr=b"session: https://claude.ai/review/b7\n")

    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, round_=1, run=run)
    assert u.session_url == "https://claude.ai/review/b7"


def test_a_session_url_is_one_the_cli_could_have_printed():
    assert ultra.session_url("see https://claude.ai/review/x1") == "https://claude.ai/review/x1"
    assert ultra.session_url("see https://evil.example/?claude.ai/review/x1") is None
    assert ultra.session_url(None) is None


def test_the_wait_the_record_names_is_the_wait_that_elapsed(tmp_path):
    """The local wrapper waits the remote's own bound PLUS a grace, so a record naming the
    bound alone sends a reader looking for a review that timed out a minute earlier than it
    did. A comment or a message asserting something the code does not do is the defect."""
    r, base, head = _repo(tmp_path)
    seen = {}

    def run(argv, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(argv, kw["timeout"], stderr="")

    u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, round_=1,
                        timeout_minutes=7, run=run)
    assert seen["timeout"] == 7 * 60 + ultra.GRACE_SECONDS
    assert "7 minute(s)" in u.detail and f"{ultra.GRACE_SECONDS}s" in u.detail
    assert u.session_url is None and "no session URL" in u.detail


def test_an_unreadable_payload_is_kept_where_a_human_can_read_it(tmp_path):
    """The review was paid for and cannot be re-run for free, so the bytes that could not be
    parsed are the only evidence anyone will ever have about what came back."""
    r, base, head = _repo(tmp_path)
    d = tmp_path / "run"
    u = ultra.run_ultra(d, checkout=r, base=base, head=head, round_=1,
                        run=_proc(0, "Review complete. Looks good!"))
    assert u.reason == "unreadable_output"
    assert (d / "ultrareview" / "unreadable-output.txt").read_text() == \
        "Review complete. Looks good!"
    assert not (d / "ultrareview" / "bugs.json").exists(), \
        "a payload nobody could read must not land under the name a readable one takes"


def test_a_target_cannot_smuggle_an_option_into_a_paid_invocation():
    for bad in ("--dangerously-skip-permissions", "-p"):
        with pytest.raises(ultra.UltraError):
            ultra.argv(timeout_minutes=30, target=bad)


def test_a_payload_this_module_cannot_map_is_unavailable_and_not_an_empty_review(tmp_path):
    """Every way `_bugs` refuses lands on the same degrade, and none of them lands on RAN.

    The first two are JSON that parses and is not an object: `.get` on a list raises
    `AttributeError`, which the `except (ValueError, TypeError)` around this does NOT catch,
    so a payload of `[]` FAILS the run where §13.1 says it degrades."""
    r, base, head = _repo(tmp_path)
    for payload in ([],
                    None,
                    {"bugs": {}},
                    {"bugs": ["not an object"]},
                    {"bugs": [{"severity": "spicy", "description": "x"}]},
                    {"bugs": [{"severity": "blocker"}]},
                    {"findings": []}):
        u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, round_=1,
                            run=_proc(0, json.dumps(payload)))
        assert (u.status, u.reason, u.bugs) == (ultra.UNAVAILABLE, "unreadable_output", None), \
            payload


def test_every_status_and_reason_this_module_can_produce_is_a_declared_one(tmp_path):
    """A constant set nothing enumerates is a set that grows a spelling nobody validates."""
    assert set(ultra.STATUSES) == {ultra.RAN, ultra.UNAVAILABLE, ultra.TIMED_OUT,
                                   ultra.SKIPPED}
    assert len(set(ultra.REASONS)) == len(ultra.REASONS) == 6
    r, base, head = _repo(tmp_path)
    produced = set()
    for kw, run in ((dict(enabled=False), _proc()),
                    (dict(file_limit=1), _proc()),
                    ({}, _proc(0, json.dumps({"bugs": []}))),
                    ({}, _proc(0, "not json")),
                    ({}, _proc(1, "", "You are not logged in."))):
        u = ultra.run_ultra(tmp_path / "run", checkout=r, base=base, head=head, round_=1,
                            run=run, **kw)
        produced.add((u.status, u.reason))
    assert {s for s, _ in produced} <= set(ultra.STATUSES)
    assert {x for _, x in produced if x} <= set(ultra.REASONS)
    assert len(produced) == 5, produced
