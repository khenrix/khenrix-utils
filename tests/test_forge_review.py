"""§13's reviewer input set, the in-process council call, and the durable findings record."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from council import engine  # noqa: E402
from forge import journal, ledger, review, snapshot, storage  # noqa: E402

from forge_fixtures import commit_all, make_repo, write  # noqa: E402


def _checkout(tmp_path, name="synthesis"):
    r = make_repo(tmp_path, name)
    write(r, "src.py", "print('hi')\n")
    commit_all(r, "synthesis checkpoint")
    return r


def _run_dir(tmp_path, name="run"):
    d = tmp_path / name
    d.mkdir()
    return d


def _ledger(run_dir):
    # `requirement_sha256` sits between `requirement_span` and `kind` and is REQUIRED
    # (`ledger.py:156`): §10 asks for the requirement id "+ source span/hash", and one string
    # cannot carry three facts a reader has to compare separately. Omitting it raises
    # `TypeError` out of `Row.__init__` before any assertion in this file runs.
    row = ledger.Row(
        id=ledger.row_id("R1", "the cache layer is rejected"), requirement_id="R1",
        requirement_span="spec.md:1-3", requirement_sha256="0" * 64,
        kind="architecture", component="core",
        semantic_claim="the cache layer is rejected", status="rejected", dependencies=(),
        seat_evidence=(), counterevidence="", acceptance_criteria=(),
        synthesis_evidence=None, verification_receipt=None, risk="", rationale="none")
    l = ledger.Ledger(version=ledger.VERSION, rows=(row,), union_diff_bytes=10,
                      degrade_threshold_bytes=ledger.DEGRADE_UNION_DIFF_BYTES,
                      degraded=False)
    ledger.write_ledger(run_dir, l)
    return l


def test_the_reviewer_inputs_name_every_item_section_13_requires(tmp_path):
    co = _checkout(tmp_path)
    d = review.write_reviewer_inputs(co, 1, checkpoint="a" * 40, baseline_commit="b" * 40,
                                     baseline_tree="c" * 40, artifact_manifest=None,
                                     token="TOKEN-1", task_bundle_present=True)
    inputs = json.loads((d / "inputs.json").read_text())
    assert inputs["synthesis_checkpoint"] == "a" * 40
    assert inputs["baseline_commit"] == "b" * 40 and inputs["baseline_tree"] == "c" * 40
    assert inputs["artifact_manifest"] is None
    instr = (d / "REVIEW.md").read_text()
    assert "TOKEN-1" in instr
    assert "git diff" in instr


def test_a_missing_artifact_manifest_is_stated_to_the_reviewer(tmp_path):
    """§16's manifest is a later plan's artifact. A four-item input set described as five is
    a reviewer told it has evidence it does not have."""
    co = _checkout(tmp_path)
    d = review.write_reviewer_inputs(co, 1, checkpoint="a" * 40, baseline_commit="b" * 40,
                                     baseline_tree="c" * 40, artifact_manifest=None,
                                     token="T", task_bundle_present=True)
    assert "no out-of-band artifact manifest" in (d / "REVIEW.md").read_text()


def test_a_missing_task_bundle_is_stated_rather_than_omitted(tmp_path):
    co = _checkout(tmp_path)
    d = review.write_reviewer_inputs(co, 1, checkpoint="a" * 40, baseline_commit="b" * 40,
                                     baseline_tree="c" * 40, artifact_manifest=None,
                                     token="T", task_bundle_present=False)
    assert "no task bundle" in (d / "REVIEW.md").read_text()


def test_the_review_directory_is_asked_for_never_joined(tmp_path):
    co = _checkout(tmp_path)
    gd = subprocess.run(["git", "-C", str(co), "rev-parse", "--absolute-git-dir"],
                        check=True, capture_output=True, text=True).stdout.strip()
    assert review.review_dir(co, 2) == Path(gd) / "khenrix-forge" / "review" / "round-2"


def test_the_ledger_is_out_of_reach_of_a_clean_checkout(tmp_path):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    review.assert_ledger_is_out_of_reach(run, checkout=co, other_clones=())   # does not raise


def test_a_ledger_copied_into_the_checkout_is_caught_by_its_bytes(tmp_path):
    """STRUCTURAL, NOT TEXTUAL. §13 gives every reviewer a shell in this tree, so 'do not
    read the ledger' in prose is not a guarantee. The bytes must not be here under ANY name."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    (co / "notes.txt").write_bytes(Path(storage.ledger_path(run)).read_bytes())
    with pytest.raises(review.ReviewError) as e:
        review.assert_ledger_is_out_of_reach(run, checkout=co, other_clones=())
    assert "notes.txt" in str(e.value)


def test_a_ledger_copied_into_the_review_directory_is_caught(tmp_path):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    d = review.write_reviewer_inputs(co, 1, checkpoint="a" * 40, baseline_commit="b" * 40,
                                     baseline_tree="c" * 40, artifact_manifest=None,
                                     token="T", task_bundle_present=True)
    (d / "extra.json").write_bytes(Path(storage.ledger_path(run)).read_bytes())
    with pytest.raises(review.ReviewError):
        review.assert_ledger_is_out_of_reach(run, checkout=co, other_clones=())


def test_a_run_directory_inside_the_checkout_is_refused_outright(tmp_path):
    """The path check and the byte check are complementary, not redundant: a run directory
    UNDER the checkout puts the real ledger in the reviewer's tree with no copy involved."""
    co = _checkout(tmp_path)
    run = co / "state"
    run.mkdir()
    _ledger(run)
    with pytest.raises(review.ReviewError) as e:
        review.assert_ledger_is_out_of_reach(run, checkout=co, other_clones=())
    assert "which is under" in str(e.value)
    # NOT a bare `"under" in ...`: the CONTENT message says "a copy under another name", so
    # the loose assertion passes with the path branch removed — measured, that mutation
    # SURVIVED. The two checks are complementary and this test is the path one's.
    assert "exact bytes" not in str(e.value)


def test_an_unreadable_directory_refuses_rather_than_scanning_nothing(tmp_path):
    """os.walk with no `onerror` returns [] for an unreadable subtree — measured on this
    project three times — and an empty scan finding no ledger reads as a clean tree."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    blind = co / "blind"
    blind.mkdir()
    (blind / "x.txt").write_text("x\n")
    blind.chmod(0o000)
    try:
        with pytest.raises(review.ReviewError):
            review.assert_ledger_is_out_of_reach(run, checkout=co, other_clones=())
    finally:
        blind.chmod(0o755)


def test_a_missing_ledger_refuses_rather_than_certifying_absence(tmp_path):
    run = _run_dir(tmp_path)
    co = _checkout(tmp_path)
    with pytest.raises(review.ReviewError) as e:
        review.assert_ledger_is_out_of_reach(run, checkout=co, other_clones=())
    assert "no ledger" in str(e.value)


def _worktree(main, dest, branch="synth"):
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(dest), "-b", branch],
                   check=True, capture_output=True)
    return dest


def test_a_ledger_in_the_task_bundle_of_a_linked_worktree_is_caught(tmp_path):
    """THE GUARANTEE THAT PASSED BY ACCIDENT. In an ordinary clone the git directory is under
    the worktree, so one walk sweeps the task bundle up and the scan is total by luck. In a
    LINKED WORKTREE — §16's synthesis worktree — the git dir is `<main>/.git/worktrees/<n>`,
    outside the checkout, and `<git-dir>/khenrix-forge/task` is §20's bundle: the one place
    Decision 3 names above all others, and the one a version that scanned only `.../review`
    never looked at. Measured: that version PASSED this exact tree."""
    run = _run_dir(tmp_path)
    _ledger(run)
    main = _checkout(tmp_path, "main")
    wt = _worktree(main, tmp_path / "synth")
    assert review.reviewer_roots(wt) != (Path(wt).resolve(),), \
        "a linked worktree's git dir is OUTSIDE it; if this holds the test proves nothing"
    task = Path(subprocess.run(["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"],
                               check=True, capture_output=True,
                               text=True).stdout.strip()) / "khenrix-forge" / "task"
    task.mkdir(parents=True)
    (task / "notes.json").write_bytes(Path(storage.ledger_path(run)).read_bytes())
    with pytest.raises(review.ReviewError) as e:
        review.assert_ledger_is_out_of_reach(run, checkout=wt, other_clones=())
    assert "notes.json" in str(e.value)


def test_an_ordinary_clone_and_a_worktree_are_scanned_by_the_same_rule(tmp_path):
    """One rule, two configurations. In a clone the git dir is inside the checkout and the
    root list is one entry; in a worktree it is two. Neither is a special case written by
    hand, which is what makes the coverage structural."""
    main = _checkout(tmp_path, "main")
    wt = _worktree(main, tmp_path / "synth")
    assert review.reviewer_roots(main) == (Path(main).resolve(),)
    roots = review.reviewer_roots(wt)
    assert len(roots) == 2 and not roots[1].is_relative_to(roots[0])


def test_a_seat_clone_the_caller_names_is_scanned_too(tmp_path):
    """Decision 3 lists the seats and the verifier as well, and this module cannot derive
    their paths — `fleet.clone_seat` takes its destination from its caller. So the argument
    is required: `()` is a claim, an omission is not."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    seat = _checkout(tmp_path, "seat-claude")
    (seat / "copied.json").write_bytes(Path(storage.ledger_path(run)).read_bytes())
    review.assert_ledger_is_out_of_reach(run, checkout=co, other_clones=())   # co is clean
    with pytest.raises(review.ReviewError) as e:
        review.assert_ledger_is_out_of_reach(run, checkout=co, other_clones=(seat,))
    assert "copied.json" in str(e.value)


def test_a_named_root_that_does_not_exist_is_a_refusal_not_an_empty_walk(tmp_path):
    """`os.walk` over a missing directory yields nothing and calls `onerror` — measured,
    `FileNotFoundError`. A root that could not be scanned must never read as a clean one."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    with pytest.raises(review.ReviewError):
        review.assert_ledger_is_out_of_reach(run, checkout=co,
                                             other_clones=(tmp_path / "never-created",))


# --------------------------------------------------------------------------- specs
def test_every_reviewer_runs_from_the_synthesis_checkout(tmp_path):
    co = _checkout(tmp_path)
    specs = review.reviewer_specs(["claude", "codex", "agy"], prompt="go",
                                  timeout=review.REVIEW_TIMEOUT_SEC, cwd=co,
                                  token="TOK", workdir=tmp_path / "wd")
    assert {s.cwd for s in specs} == {str(co)}


def test_the_codex_reviewer_uses_codex_exec_json_and_not_codex_review(tmp_path):
    """MEASURED: `codex review` has no --json, so the engine's extractor would turn every
    review into a silent parse_failure and 'found nothing' would be unreadable from
    'could not be read'."""
    co = _checkout(tmp_path)
    codex = [s for s in review.reviewer_specs(["codex"], prompt="go", timeout=60, cwd=co,
                                              token="TOK", workdir=tmp_path / "wd")][0]
    assert codex.argv[:4] == ["codex", "exec", "-", "--json"]
    assert "review" not in codex.argv
    assert codex.extract is engine.extract_codex_json


def test_a_reviewer_that_never_quoted_the_token_is_not_valid(tmp_path):
    """THE FAIL-OPEN. `seat.forge_spec`'s validator forces sentinel=None on a COPY of the
    spec regardless of what it carries, so a reviewer wired through it scores valid having
    read nothing. Reviewers get the council's own `evaluate`."""
    co = _checkout(tmp_path)
    spec = review.reviewer_specs(["claude"], prompt="go", timeout=60, cwd=co, token="TOK-9",
                                 workdir=tmp_path / "wd")[0]
    assert spec.validator is None and spec.sentinel == "TOK-9"
    assert spec.min_chars == engine.MIN_SUBSTANTIVE_CHARS
    body = json.dumps({"type": "result", "result": "x" * 600})
    valid, reason, _, _ = engine.evaluate(0, body, "", spec)
    assert not valid and reason != "ok"
    body_ok = json.dumps({"type": "result", "result": "TOK-9 " + "x" * 600})
    assert engine.evaluate(0, body_ok, "", spec)[0]


def test_the_declared_mode_and_the_passed_timeout_are_the_same_number():
    """`mode` is a manifest LABEL; passing 'deep' does not select MODE_TIMEOUT['deep']."""
    assert review.REVIEW_TIMEOUT_SEC == engine.MODE_TIMEOUT["deep"]


# --------------------------------------------------------------------------- parsing
def test_a_well_formed_block_parses():
    row = {"severity": "blocker", "claim": "c", "evidence": "src.py:10"}
    rows, why = review.parse_findings(
        'prose\n```json\n{"findings": [' + json.dumps(row) + ']}\n```\n')
    assert rows == [row] and why == ""


def test_an_empty_findings_list_is_a_real_answer():
    rows, why = review.parse_findings('```json\n{"findings": []}\n```')
    assert rows == [] and why == ""


def test_no_block_at_all_is_unreadable_not_empty():
    """THE FAIL-OPEN. 'Found nothing' and 'could not be read' must not be one value."""
    rows, why = review.parse_findings("I reviewed it and it looks fine to me.")
    assert rows is None and "no fenced" in why


def test_two_blocks_are_unreadable_because_nobody_may_pick_one():
    rows, why = review.parse_findings(
        '```json\n{"findings": []}\n```\n```json\n{"findings": [{"severity": "blocker",'
        ' "claim": "c", "evidence": "src.py:10"}]}\n```')
    # NOT `rows is None and "two" in why or "more than one" in why` — `and` binds tighter, so
    # that reads `(A and B) or C`, and C is true of the message whatever `rows` is.
    assert rows is None and "more than one" in why


def test_a_severity_outside_the_declared_set_is_unreadable():
    rows, why = review.parse_findings(
        '```json\n{"findings": [{"severity": "catastrophic", "claim": "c",'
        ' "evidence": "src.py:10"}]}\n```')
    assert rows is None and "severity" in why


def test_a_finding_with_no_claim_is_unreadable():
    rows, why = review.parse_findings(
        '```json\n{"findings": [{"severity": "blocker", "evidence": "src.py:10"}]}\n```')
    assert rows is None and "claim" in why


def test_a_finding_that_cites_no_evidence_is_unreadable_when_nothing_else_survives():
    """REVIEW.md tells every reviewer to cite changed-file evidence for every finding, and a
    demand nothing checks is a sentence rather than a requirement — the blocker line the
    terminal prints would name a claim with nothing behind it. Dropped like a missing claim;
    with no OTHER row in the answer to salvage, dropping the only row leaves nothing, which
    is unreadable rather than "found nothing" (see the next test for the case where a sibling
    row DOES survive)."""
    for block in ('{"findings": [{"severity": "blocker", "claim": "c"}]}',
                  '{"findings": [{"severity": "blocker", "claim": "c", "evidence": "  "}]}',
                  '{"findings": [{"severity": "blocker", "claim": "c", "evidence": 7}]}'):
        rows, why = review.parse_findings(f"```json\n{block}\n```")
        assert rows is None and "evidence" in why, block


def test_a_malformed_row_drops_only_that_row():
    """THE FIX: a version of this voided the WHOLE answer over one bad row, which discarded
    the OTHER, properly-evidenced finding in the same block too — a real blocker a
    three-provider panel was paid to produce, thrown out for a sibling's missing field. That
    read cleaner than the evidence the panel actually returned. The good row survives; `why`
    is non-empty so the drop is recorded rather than silently vanishing (`run_round` puts it
    on the journal — see `test_a_dropped_row_is_journalled_not_silently_absorbed`)."""
    good = {"severity": "blocker", "claim": "unbounded cache", "evidence": "cache.py:12"}
    bad = {"severity": "minor", "claim": "no evidence for this one"}
    rows, why = review.parse_findings(
        '```json\n{"findings": [' + json.dumps(good) + ', ' + json.dumps(bad) + ']}\n```')
    assert rows == [good], "the malformed row is dropped, the good one is not"
    assert why and "evidence" in why and "no evidence for this one" in why


def test_malformed_json_is_unreadable():
    rows, why = review.parse_findings('```json\n{"findings": [\n```')
    assert rows is None and "json" in why.lower()


# --------------------------------------------------------------------------- the record
def _finding(round_=1, seat="claude", severity="blocker", claim="c", resolution="open",
             evidence="src.py:10"):
    return review.Finding(
        id=review.finding_id(round_, seat, severity, claim, evidence), round=round_,
        seat=seat, severity=severity, claim=claim, evidence=evidence, resolution=resolution)


def test_a_findings_id_is_content_derived_and_stable():
    a = review.finding_id(1, "claude", "blocker", "the cache is unbounded", "c.py:1")
    b = review.finding_id(1, "claude", "blocker", "the cache is unbounded", "c.py:1")
    c = review.finding_id(2, "claude", "blocker", "the cache is unbounded", "c.py:1")
    assert a == b and a != c and len(a) == 12


def test_one_claim_cited_at_two_places_is_two_findings():
    """Evidence is in the hash, so a reviewer that raises one wording against two call sites
    — ordinary output — produces two ids rather than one written twice. Without this the
    second `Finding` collided with the first, `Round` and `write_round` took the pair, and
    `write_resolutions` refused it: a crash in `loop` AFTER the panel had been paid for."""
    a = review.finding_id(1, "claude", "blocker", "unbounded cache", "cache.py:12")
    b = review.finding_id(1, "claude", "blocker", "unbounded cache", "cache.py:88")
    assert a != b


def test_a_round_round_trips_through_disk(tmp_path):
    run = _run_dir(tmp_path)
    r = review.Round(round=1, checkpoint="a" * 40, findings=(_finding(),),
                     identities=({"prompt_sha256": "x"},), seats_responded=("claude",),
                     seats_silent=(("codex", "parse_failure"),))
    h = review.write_round(run, r)
    assert len(h) == 64
    assert review.read_round(run, 1) == r


def test_a_round_is_written_once_and_never_rewritten(tmp_path):
    run = _run_dir(tmp_path)
    r = review.Round(1, "a" * 40, (), (), (), ())
    review.write_round(run, r)
    with pytest.raises(review.ReviewError):
        review.write_round(run, r)


def test_a_silent_seat_is_recorded_rather_than_dropped(tmp_path):
    run = _run_dir(tmp_path)
    review.write_round(run, review.Round(1, "a" * 40, (), (), ("claude",),
                                         (("agy", "auth_or_quota"),)))
    assert review.read_round(run, 1).seats_silent == (("agy", "auth_or_quota"),)


def test_a_finding_with_an_undeclared_severity_cannot_be_recorded():
    with pytest.raises(review.ReviewError):
        review.Finding(id="x" * 12, round=1, seat="claude", severity="catastrophic",
                       claim="c", evidence="src.py:10", resolution="open")


def test_a_finding_with_an_undeclared_resolution_cannot_be_recorded():
    with pytest.raises(review.ReviewError):
        review.Finding(id="x" * 12, round=1, seat="claude", severity="blocker",
                       claim="c", evidence="src.py:10", resolution="probably-fine")


def test_a_finding_that_cites_nothing_cannot_be_recorded():
    """The record's half of the same rule `parse_findings` applies to the answer: an empty
    string would let "no evidence was given" be spelled exactly like "this field was never
    filled in", and the fix pass reads this field to find what is being complained about."""
    for blank in ("", "   "):
        with pytest.raises(review.ReviewError):
            review.Finding(id="x" * 12, round=1, seat="claude", severity="blocker",
                           claim="c", evidence=blank, resolution="open")


def test_a_round_validates_its_own_fields():
    """`Round` is the record the TERMINAL reads and the only one in this plan that a
    hand-built caller could populate freely. `read_round` builds one off disk, so these run
    on the reconstructed record too."""
    with pytest.raises(review.ReviewError):
        review.Round(0, "a" * 40, (), (), (), ())            # `round_dir` refuses 0
    with pytest.raises(review.ReviewError):
        review.Round(1, "   ", (), (), (), ())               # an unnamed checkpoint
    with pytest.raises(review.ReviewError):
        review.Round(1, "a" * 40, (_finding(round_=2),), (), (), ())
    with pytest.raises(review.ReviewError):
        review.Round(1, "a" * 40, (), (), (), (("agy", ""),))
    with pytest.raises(review.ReviewError):
        review.Round(1, "a" * 40, (), (), ("claude",), (("claude", "parse_failure"),))


def test_two_findings_sharing_one_id_are_refused_on_the_way_in(tmp_path):
    """THE WRITE HALF OF `_one_row_per_finding`. Resolutions are keyed by finding id, so a
    round holding the same id twice is resolved by whichever row a fix pass wrote last — and
    `write_resolutions` then refuses the pair, crashing `loop` after the panel was paid for.
    Reproduced: two `Finding`s with one id used to be accepted here and stored by
    `write_round`, and only the resolutions write objected."""
    f = _finding()
    with pytest.raises(review.ReviewError) as e:
        review.Round(1, "a" * 40, (f, f), (), ("claude",), ())
    assert f.id in str(e.value)


# --------------------------------------------------------------------------- the round
ANSWER = ('I reviewed the diff.\n' + 'x' * 500 + '\nToken: {tok}\n'
          '```json\n{{"findings": [{{"severity": "blocker", "claim": "unbounded cache",'
          ' "evidence": "cache.py:12"}}]}}\n```')


def _fake_council(tmp_path, *, answers, record):
    """A `run_council` stand-in. NO PROVIDER IS INVOKED ANYWHERE IN THIS SUITE."""
    # The signature MIRRORS `engine.run_council`, `env=` included. A fake that omitted it
    # would make `run_round`'s call a TypeError nothing else in this file would explain.
    def run_council(specs, *, retries, timeout, backoff, workdir, prompt=None,
                    requested=None, mode=None, read_only=None, install_signal_handler=True,
                    env=None):
        record.update(retries=retries, timeout=timeout, workdir=Path(workdir), prompt=prompt,
                      mode=mode, read_only=read_only,
                      install_signal_handler=install_signal_handler, env=env,
                      cwds=[s.cwd for s in specs], sentinels=[s.sentinel for s in specs])
        Path(workdir).mkdir(parents=True, exist_ok=True)
        providers = []
        for s in specs:
            text, valid, reason = answers[s.name]
            rf = Path(workdir) / f"{s.name}.result.txt"
            rf.write_text(text)
            providers.append({"name": s.name, "valid": valid, "reason": reason,
                              "result_text": text[:80], "result_file": str(rf),
                              "model": s.model})
        (Path(workdir) / "manifest.json").write_text(json.dumps({"providers": providers}))
        return {"providers": providers, "prompt_sha256": None}
    return run_council


def _probe(**kw):
    from forge import fingerprint
    return fingerprint.build(prompt=kw["prompt"], token=kw["token"], cli=kw["cli"],
                             bundle_sha256=kw.get("bundle_sha256"),
                             model_requested=kw.get("model_requested"),
                             model_reported=kw.get("model_reported"),
                             run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "1.0", ""),
                             closure=lambda cli: "closure-" + cli)


def test_a_round_records_findings_a_silent_seat_and_three_identities(tmp_path):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    seen = {}
    answers = {"claude": (ANSWER.format(tok="TOK"), True, "ok"),
               "codex": ("I found nothing." + "y" * 500, True, "ok"),
               "agy": ("", False, "auth_or_quota")}
    r = review.run_round(
        run, round_=1, checkout=co, checkpoint="a" * 40, baseline_commit="b" * 40,
        baseline_tree="c" * 40, artifact_manifest=None, other_clones=(),
        log=journal.Journal(storage.journal_path(run)),
        run_council=_fake_council(tmp_path, answers=answers, record=seen),
        probe=_probe, make_token=lambda: "TOK")

    assert [f.claim for f in r.findings] == ["unbounded cache"]
    assert r.seats_responded == ("claude",)
    assert dict(r.seats_silent) == {"agy": "auth_or_quota",
                                    "codex": "unreadable_findings"}, \
        "a reviewer whose answer could not be read is SILENT, never a reviewer with no findings"
    assert len(r.identities) == 3


def test_a_dropped_row_is_journalled_not_silently_absorbed(tmp_path):
    """A seat whose answer carried one malformed row alongside a good one still RESPONDS —
    `parse_findings` drops the row, not the seat — but the drop must not vanish just because
    the rest of the answer was clean. `responded` alone cannot say that; the journal can."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    good = {"severity": "blocker", "claim": "unbounded cache", "evidence": "cache.py:12"}
    bad = {"severity": "minor", "claim": "no evidence for this one"}
    mixed = ("I reviewed the diff.\n" + "x" * 500 + "\nToken: TOK\n"
             '```json\n{"findings": [' + json.dumps(good) + ", " + json.dumps(bad)
             + "]}\n```")
    answers = {"claude": (mixed, True, "ok"),
               "codex": (ANSWER.format(tok="TOK"), True, "ok"),
               "agy": (ANSWER.format(tok="TOK"), True, "ok")}
    log = journal.Journal(storage.journal_path(run))
    r = review.run_round(
        run, round_=1, checkout=co, checkpoint="a" * 40, baseline_commit="b" * 40,
        baseline_tree="c" * 40, artifact_manifest=None, other_clones=(), log=log,
        run_council=_fake_council(tmp_path, answers=answers, record={}),
        probe=_probe, make_token=lambda: "TOK")

    assert r.seats_responded == ("agy", "claude", "codex")
    assert [f.claim for f in r.findings if f.seat == "claude"] == ["unbounded cache"], \
        "the malformed row from claude's answer is gone; the good one from it is not"

    done = next(e for e in log.read() if e.event == journal.done(review.COUNCIL_KIND))
    assert done.data["dropped_rows"], "the drop must be recorded, not merely tolerated"
    name, why = done.data["dropped_rows"][0]
    assert name == "claude" and "evidence" in why


def test_the_council_never_writes_into_the_run_directory(tmp_path):
    """HAZARD 1. `run_council` ends with (workdir/'manifest.json').write_text — plain,
    non-atomic — and `storage.manifest_path(run_dir)` is the same filename, written once."""
    run = _run_dir(tmp_path)
    _ledger(run)
    storage.exclusive_write(storage.manifest_path(run), b'{"the run": "identity"}\n')
    co = _checkout(tmp_path)
    seen = {}
    review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                     baseline_commit="b" * 40, baseline_tree="c" * 40,
                     artifact_manifest=None, other_clones=(),
                     log=journal.Journal(storage.journal_path(run)),
                     run_council=_fake_council(
                         tmp_path, answers={n: (ANSWER.format(tok="TOK"), True, "ok")
                                            for n in ("claude", "codex", "agy")},
                         record=seen),
                     probe=_probe, make_token=lambda: "TOK")
    assert Path(storage.manifest_path(run)).read_bytes() == b'{"the run": "identity"}\n'
    assert seen["workdir"] != Path(run)
    assert Path(run) in seen["workdir"].parents


def test_the_signal_handler_is_not_installed_and_retries_are_zero(tmp_path):
    """HAZARD 2. The installed handler ends in os._exit(128+signum) (engine.py:942), which
    skips every finally — so `council_round_done` never lands on a plain Ctrl-C."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    seen = {}
    review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                     baseline_commit="b" * 40, baseline_tree="c" * 40,
                     artifact_manifest=None, other_clones=(),
                     log=journal.Journal(storage.journal_path(run)),
                     run_council=_fake_council(
                         tmp_path, answers={n: (ANSWER.format(tok="TOK"), True, "ok")
                                            for n in ("claude", "codex", "agy")},
                         record=seen),
                     probe=_probe, make_token=lambda: "TOK")
    assert seen["install_signal_handler"] is False
    assert seen["retries"] == 0
    assert seen["timeout"] == review.REVIEW_TIMEOUT_SEC
    assert seen["prompt"] is None, \
        "the council manifest's prompt_sha256 is one hash of one argument and is not any " \
        "seat's identity; §11's per-seat fingerprints are recorded by this module"
    assert set(seen["sentinels"]) == {"TOK"}


def test_the_round_is_journalled_write_ahead(tmp_path):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))
    review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                     baseline_commit="b" * 40, baseline_tree="c" * 40,
                     artifact_manifest=None, other_clones=(), log=log,
                     run_council=_fake_council(
                         tmp_path, answers={n: (ANSWER.format(tok="TOK"), True, "ok")
                                            for n in ("claude", "codex", "agy")},
                         record={}),
                     probe=_probe, make_token=lambda: "TOK")
    events = [e.event for e in log.read()]
    assert events == [journal.intent("council_round"), journal.done("council_round")]
    assert journal.orphans(log.read()) == ()


def test_a_round_refuses_to_start_when_the_ledger_is_reachable(tmp_path):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    (co / "leak.json").write_bytes(Path(storage.ledger_path(run)).read_bytes())
    with pytest.raises(review.ReviewError):
        review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                         baseline_commit="b" * 40, baseline_tree="c" * 40,
                         artifact_manifest=None, other_clones=(),
                         log=journal.Journal(storage.journal_path(run)),
                         run_council=_fake_council(tmp_path, answers={}, record={}),
                         probe=_probe, make_token=lambda: "TOK")


def test_a_truncated_result_text_is_not_what_gets_parsed(tmp_path):
    """`run_provider` truncates `result_text` (engine._truncate). Parsing it would make a
    long, correct review whose JSON block fell past the cut read as unparseable."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    long_answer = ANSWER.format(tok="TOK")
    r = review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                         baseline_commit="b" * 40, baseline_tree="c" * 40,
                         artifact_manifest=None, other_clones=(),
                         log=journal.Journal(storage.journal_path(run)),
                         run_council=_fake_council(
                             tmp_path,
                             answers={n: (long_answer, True, "ok")
                                      for n in ("claude", "codex", "agy")},
                             record={}),
                         probe=_probe, make_token=lambda: "TOK")
    assert len(r.findings) == 3 and r.seats_silent == ()


def test_an_unreadable_result_file_is_a_silent_seat(tmp_path):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)

    def broken(specs, **kw):
        Path(kw["workdir"]).mkdir(parents=True, exist_ok=True)
        return {"providers": [{"name": s.name, "valid": True, "reason": "ok",
                               "result_text": "x", "result_file": str(Path(kw["workdir"])
                                                                     / "gone.txt"),
                               "model": None} for s in specs],
                "prompt_sha256": None}

    r = review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                         baseline_commit="b" * 40, baseline_tree="c" * 40,
                         artifact_manifest=None, other_clones=(),
                         log=journal.Journal(storage.journal_path(run)),
                         run_council=broken, probe=_probe, make_token=lambda: "TOK")
    assert r.seats_responded == ()
    assert {reason for _, reason in r.seats_silent} == {"unreadable_result_file"}


# ------------------------------------------------------- what the fixtures above cannot reach
def test_a_symlink_to_the_ledger_is_reachable_and_is_caught(tmp_path):
    """THE FAIL-OPEN THE SKIP-SYMLINKS VERSION SHIPPED. Its argument was that a link's target
    is "either in this tree and visited on its own, or outside it and not in the reviewer's
    tree". The second half is false: `cat notes.txt` follows the link, so a link OUT of the
    tree is exactly how the ledger's bytes stay readable while no file in the walk holds
    them. Measured: the version that skipped symlinks PASSED this tree."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    (co / "notes.txt").symlink_to(Path(storage.ledger_path(run)))
    with pytest.raises(review.ReviewError) as e:
        review.assert_ledger_is_out_of_reach(run, checkout=co, other_clones=())
    assert "notes.txt" in str(e.value)


def test_a_directory_symlink_out_of_the_checkout_is_scanned_through(tmp_path):
    """`os.walk` does not descend a symlinked directory, so `state -> <run dir>` put the REAL
    ledger one `cd` away from the reviewer while the walk listed the entry and looked past it.
    A link a reviewer can walk is a root this scan has to walk."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    (co / "state").symlink_to(run, target_is_directory=True)
    with pytest.raises(review.ReviewError) as e:
        review.assert_ledger_is_out_of_reach(run, checkout=co, other_clones=())
    assert "ledger.json" in str(e.value)


def test_a_dangling_symlink_is_not_a_refusal(tmp_path):
    """The other direction of the same rule. A link nothing can dereference carries no bytes
    to the reviewer either, so refusing on it would make an ordinary tree unreviewable —
    fail-closed is about what could not be ANSWERED, not about every error."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    (co / "dead.txt").symlink_to(co / "never-existed")
    review.assert_ledger_is_out_of_reach(run, checkout=co, other_clones=())   # does not raise


def test_a_symlink_loop_terminates_rather_than_recursing(tmp_path):
    """A self-referencing directory link and one pointing at an ancestor are the two shapes
    that turn "walk what the link reaches" into a non-terminating scan."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    (co / "loop").symlink_to(co / "loop", target_is_directory=True)
    (co / "self").symlink_to(co, target_is_directory=True)
    review.assert_ledger_is_out_of_reach(run, checkout=co, other_clones=())   # terminates


def test_a_reviewer_built_through_seat_forge_spec_is_refused(tmp_path):
    """THE HAZARD NAMED IN §8.1, WIRED UP THE WAY A CALLER WOULD ACTUALLY WIRE IT. `forge_spec`
    is this package's own builder and its validator forces `sentinel=None` on a copy, so a
    reviewer built through it scores `valid` having never opened the bundle. The guard is
    what makes that a refusal rather than a clean review by a seat that read nothing."""
    from forge import seat
    co = _checkout(tmp_path)
    with pytest.raises(review.ReviewError) as e:
        review.reviewer_specs(
            ["claude"], prompt="go", timeout=60, cwd=co, token="TOK",
            workdir=tmp_path / "wd",
            build=lambda n, p, t, c, w: seat.forge_spec(n, p, t, cfg=c, workdir=w))
    assert "validator" in str(e.value)


def test_a_panel_of_nobody_is_refused(tmp_path):
    """A zero-seat round records no findings AND no silent seats, which is byte-for-byte the
    record three reviewers who found nothing would leave — and the `ready` transition refuses
    only on a non-empty `seats_silent`."""
    co = _checkout(tmp_path)
    with pytest.raises(review.ReviewError) as e:
        review.reviewer_specs([], prompt="go", timeout=60, cwd=co, token="TOK",
                              workdir=tmp_path / "wd")
    assert "at least one reviewer" in str(e.value)


def test_a_reviewer_named_twice_is_refused(tmp_path):
    """`run_round` resolves each spec against the council record of its OWN NAME, so two specs
    called `claude` read one answer twice and a panel of one reports as a panel of two."""
    co = _checkout(tmp_path)
    with pytest.raises(review.ReviewError) as e:
        review.reviewer_specs(["claude", "claude"], prompt="go", timeout=60, cwd=co,
                              token="TOK", workdir=tmp_path / "wd")
    assert "more than once" in str(e.value)


def test_an_empty_proof_token_is_refused(tmp_path):
    co = _checkout(tmp_path)
    with pytest.raises(review.ReviewError):
        review.reviewer_specs(["claude"], prompt="go", timeout=60, cwd=co, token="   ",
                              workdir=tmp_path / "wd")


def test_a_seat_the_council_never_reported_is_silent_not_absent(tmp_path):
    """A manifest that names two providers for a three-seat panel must not shrink the panel
    to two: the missing seat is recorded silent, so `responded + silent` still counts three."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)

    def partial(specs, **kw):
        Path(kw["workdir"]).mkdir(parents=True, exist_ok=True)
        out = []
        for s in specs:
            if s.name == "agy":
                continue
            rf = Path(kw["workdir"]) / f"{s.name}.result.txt"
            rf.write_text(ANSWER.format(tok="TOK"))
            out.append({"name": s.name, "valid": True, "reason": "ok",
                        "result_text": "x", "result_file": str(rf), "model": None})
        return {"providers": out, "prompt_sha256": None}

    r = review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                         baseline_commit="b" * 40, baseline_tree="c" * 40,
                         artifact_manifest=None, other_clones=(),
                         log=journal.Journal(storage.journal_path(run)),
                         run_council=partial, probe=_probe, make_token=lambda: "TOK")
    assert r.seats_silent == (("agy", "no_record"),)
    assert len(r.seats_responded) + len(r.seats_silent) == 3


def test_a_record_that_names_no_result_file_is_a_silent_seat(tmp_path):
    """Not the same branch as an unreadable file: this record never named one at all, and a
    `None` path must not be str()'d into a filename nobody wrote."""
    assert review._result_text({"result_text": "x"}) == (None, "unreadable_result_file")
    assert review._result_text({"result_file": None}) == (None, "unreadable_result_file")


def test_a_block_that_carries_no_findings_list_is_unreadable():
    rows, why = review.parse_findings('```json\n{"verdict": "fine"}\n```')
    assert rows is None and "findings" in why
    rows, why = review.parse_findings('```json\n[1, 2]\n```')
    assert rows is None and "findings" in why
    rows, why = review.parse_findings('```json\n{"findings": ["a blocker"]}\n```')
    assert rows is None and "object" in why


def test_a_non_string_answer_is_unreadable_rather_than_empty():
    rows, why = review.parse_findings(None)
    assert rows is None and "text" in why


def test_a_round_record_with_unknown_or_missing_fields_is_refused(tmp_path):
    """`read_round` reconstructs the record a crashed run is resumed from, so a field this
    engine does not know is a fact nobody will apply — refused rather than dropped."""
    run = _run_dir(tmp_path)
    review.write_round(run, review.Round(1, "a" * 40, (), (), ("claude",), ()))
    path = review.findings_path(run, 1)
    row = json.loads(path.read_text())
    row["verdict"] = "ready"
    path.write_text(json.dumps(row))
    with pytest.raises(review.ReviewError) as e:
        review.read_round(run, 1)
    assert "verdict" in str(e.value)
    del row["verdict"], row["seats_silent"]
    path.write_text(json.dumps(row))
    with pytest.raises(review.ReviewError) as e:
        review.read_round(run, 1)
    assert "seats_silent" in str(e.value)


def test_a_round_never_recorded_is_refused_rather_than_read_as_empty(tmp_path):
    run = _run_dir(tmp_path)
    with pytest.raises(review.ReviewError) as e:
        review.read_round(run, 1)
    assert "never recorded" in str(e.value)


def test_write_round_refuses_anything_that_is_not_a_round(tmp_path):
    run = _run_dir(tmp_path)
    with pytest.raises(review.ReviewError):
        review.write_round(run, {"round": 1, "checkpoint": "a" * 40})


def test_a_seat_counted_twice_in_one_column_is_refused():
    """`seats_responded` and `seats_silent` are counted BY LENGTH downstream, so the overlap
    check between them does not cover a name repeated within one of them."""
    with pytest.raises(review.ReviewError) as e:
        review.Round(1, "a" * 40, (), (), ("claude", "claude"), ())
    assert "seats_responded" in str(e.value)
    with pytest.raises(review.ReviewError) as e:
        review.Round(1, "a" * 40, (), (), (), (("agy", "x"), ("agy", "y")))
    assert "seats_silent" in str(e.value)
    with pytest.raises(review.ReviewError):
        review.Round(1, "a" * 40, (), (), ("  ",), ())


def test_every_directory_this_module_names_refuses_round_zero(tmp_path):
    co = _checkout(tmp_path)
    for call in (lambda: review.review_dir(co, 0),
                 lambda: review.round_dir(tmp_path, 0),
                 lambda: review.round_dir(tmp_path, True),
                 lambda: review.findings_path(tmp_path, -1)):
        with pytest.raises(review.ReviewError):
            call()


def test_a_second_write_into_a_live_round_is_refused(tmp_path):
    """What a reviewer was given must not change after it was given it — the round's recorded
    prompt identity would otherwise describe a bundle that no longer exists."""
    co = _checkout(tmp_path)
    kw = dict(checkpoint="a" * 40, baseline_commit="b" * 40, baseline_tree="c" * 40,
              artifact_manifest=None, token="T", task_bundle_present=True)
    review.write_reviewer_inputs(co, 1, **kw)
    with pytest.raises(review.ReviewError) as e:
        review.write_reviewer_inputs(co, 1, **kw)
    assert "already holds" in str(e.value)


def test_an_unnamed_input_is_refused_rather_than_written_blank(tmp_path):
    co = _checkout(tmp_path)
    kw = dict(checkpoint="a" * 40, baseline_commit="b" * 40, baseline_tree="c" * 40,
              artifact_manifest=None, token="T", task_bundle_present=True)
    for field in ("checkpoint", "baseline_commit", "baseline_tree", "token"):
        with pytest.raises(review.ReviewError) as e:
            review.write_reviewer_inputs(co, 1, **{**kw, field: "  "})
        assert field in str(e.value)
    assert not review.review_dir(co, 1).exists(), \
        "a refused input set must not leave a directory a later write would then refuse"


def test_the_bundle_a_reviewer_is_pointed_at_is_the_one_that_was_written(tmp_path):
    co = _checkout(tmp_path)
    d = review.write_reviewer_inputs(co, 3, checkpoint="a" * 40, baseline_commit="b" * 40,
                                     baseline_tree="c" * 40, artifact_manifest=None,
                                     token="TOK-3", task_bundle_present=True)
    assert (d / "proof-token.txt").read_text() == "TOK-3\n"
    assert str(d) in review.launcher_prompt(d)
    assert "REVIEW.md" in review.launcher_prompt(d)


def test_a_present_artifact_manifest_is_named_rather_than_denied(tmp_path):
    """The other side of `test_a_missing_artifact_manifest_is_stated_to_the_reviewer`: the
    branch that has one must not also print the sentence saying there is none."""
    co = _checkout(tmp_path)
    d = review.write_reviewer_inputs(co, 1, checkpoint="a" * 40, baseline_commit="b" * 40,
                                     baseline_tree="c" * 40,
                                     artifact_manifest="/run/artifacts.json", token="T",
                                     task_bundle_present=True)
    text = (d / "REVIEW.md").read_text()
    assert "/run/artifacts.json" in text
    assert "no out-of-band artifact manifest" not in text


def test_a_present_task_bundle_is_named_rather_than_denied(tmp_path):
    co = _checkout(tmp_path)
    d = review.write_reviewer_inputs(co, 1, checkpoint="a" * 40, baseline_commit="b" * 40,
                                     baseline_tree="c" * 40, artifact_manifest=None,
                                     token="T", task_bundle_present=True)
    text = (d / "REVIEW.md").read_text()
    assert "khenrix-forge/task" in text and "no task bundle" not in text


def test_the_round_the_council_ran_is_the_round_recorded(tmp_path):
    """Round 2's inputs, workdir and record all carry 2 — a round written under another
    number is a review of one checkpoint filed against a different one."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    seen = {}
    review.run_round(run, round_=2, checkout=co, checkpoint="d" * 40,
                     baseline_commit="b" * 40, baseline_tree="c" * 40,
                     artifact_manifest=None, other_clones=(),
                     log=journal.Journal(storage.journal_path(run)),
                     run_council=_fake_council(
                         tmp_path, answers={n: (ANSWER.format(tok="TOK"), True, "ok")
                                            for n in ("claude", "codex", "agy")},
                         record=seen),
                     probe=_probe, make_token=lambda: "TOK")
    assert seen["workdir"] == review.round_dir(run, 2) / "council"
    assert review.review_dir(co, 2).is_dir() and not review.review_dir(co, 1).exists()
    rec = review.read_round(run, 2)
    assert rec.round == 2 and rec.checkpoint == "d" * 40
    assert all(f.round == 2 for f in rec.findings)


def test_the_recorded_round_is_the_one_returned(tmp_path):
    """§13: the record is the fact and the return value is the convenience. If they can
    differ, `--collect` and this process disagree about what the reviewers said."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    answers = {"claude": (ANSWER.format(tok="TOK"), True, "ok"),
               "codex": ("nothing here" + "y" * 500, True, "ok"),
               "agy": ("", False, "auth_or_quota")}
    r = review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                         baseline_commit="b" * 40, baseline_tree="c" * 40,
                         artifact_manifest=None, other_clones=(),
                         log=journal.Journal(storage.journal_path(run)),
                         run_council=_fake_council(tmp_path, answers=answers, record={}),
                         probe=_probe, make_token=lambda: "TOK")
    assert review.read_round(run, 1) == r


def test_a_reviewer_that_found_nothing_is_recorded_as_having_answered(tmp_path):
    """`[]` and `None` are the two answers this module exists to keep apart, and this is the
    `[]` side END TO END: an empty block is a seat that ANSWERED, never a silent one."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    clean = "I read the diff and found nothing.\n" + "z" * 500 + \
            '\n```json\n{"findings": []}\n```'
    r = review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                         baseline_commit="b" * 40, baseline_tree="c" * 40,
                         artifact_manifest=None, other_clones=(),
                         log=journal.Journal(storage.journal_path(run)),
                         run_council=_fake_council(
                             tmp_path,
                             answers={n: (clean, True, "ok")
                                      for n in ("claude", "codex", "agy")},
                             record={}),
                         probe=_probe, make_token=lambda: "TOK")
    assert r.findings == () and r.seats_silent == ()
    assert r.seats_responded == ("agy", "claude", "codex")


def test_the_task_bundle_section_reflects_the_checkout_rather_than_a_default(tmp_path):
    """`run_round` decides `task_bundle_present` by asking the checkout, so §20's bundle being
    absent is stated to the reviewer instead of being asserted into existence."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                     baseline_commit="b" * 40, baseline_tree="c" * 40,
                     artifact_manifest=None, other_clones=(),
                     log=journal.Journal(storage.journal_path(run)),
                     run_council=_fake_council(
                         tmp_path, answers={n: (ANSWER.format(tok="TOK"), True, "ok")
                                            for n in ("claude", "codex", "agy")},
                         record={}),
                     probe=_probe, make_token=lambda: "TOK")
    assert "no task bundle" in (review.review_dir(co, 1) / "REVIEW.md").read_text()


def test_a_zero_byte_ledger_is_refused_rather_than_matching_everything(tmp_path):
    """A truncated write leaves a file, not a ledger. With no bytes to look for, every empty
    file in the tree "holds" them and the sweep's size test stops excluding fifos — so the
    check would either fire on nothing or block on the first one."""
    run = _run_dir(tmp_path)
    Path(storage.ledger_path(run)).write_bytes(b"")
    co = _checkout(tmp_path)
    with pytest.raises(review.ReviewError) as e:
        review.assert_ledger_is_out_of_reach(run, checkout=co, other_clones=())
    assert "zero bytes" in str(e.value)


def test_a_fifo_in_the_tree_is_never_opened_by_the_scan(tmp_path):
    """A read of a fifo with no writer never returns, and this scan has to terminate. The
    alarm is what makes a regression FAIL rather than hang the suite: measured on this
    machine, a fifo (like every socket, char and block device) reports `st_size == 0`, so a
    non-empty ledger excludes it before anything opens it."""
    import os
    import signal
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    os.mkfifo(co / "pipe")

    def _boom(signum, frame):
        raise AssertionError("the ledger-exclusion scan blocked — it opened the fifo")

    old = signal.signal(signal.SIGALRM, _boom)
    signal.setitimer(signal.ITIMER_REAL, 10)
    try:
        review.assert_ledger_is_out_of_reach(run, checkout=co, other_clones=())
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def test_a_scan_that_ran_out_of_budget_refuses_rather_than_reporting_no_hits(tmp_path):
    """The cap is `Quota.for_harvest().max_files` — 200 000 — which no fixture can reach, so
    the branch is exercised at the helper. A partial sweep that returned `[]` would read
    exactly like a clean tree, which is the one answer it may never give."""
    root = tmp_path / "tree"
    root.mkdir()
    for i in range(3):
        (root / f"f{i}.txt").write_text("x\n")
    assert review._digests_under([root], b"nothing like these", 10) == []
    with pytest.raises(review.ReviewError) as e:
        review._digests_under([root], b"nothing like these", 2)
    assert "stopped before it finished" in str(e.value)


# --------------------------------------------------------------------------- the loop
class _Cap:
    def __init__(self, cap=3, rounds=2):
        self.synthesis_fix_cap = cap
        self.review_rounds = rounds


def _blocker(round_=1, seat="claude", claim="unbounded cache", evidence="cache.py:12"):
    return review.Finding(
        id=review.finding_id(round_, seat, "blocker", claim, evidence),
        round=round_, seat=seat, severity="blocker", claim=claim, evidence=evidence,
        resolution="open")


def _reviewing():
    """§14's position a review loop starts from. `runstate.advance` declares the three review
    terminals as `reviewing`'s successors and nothing else, so a loop handed any other phase
    refuses rather than recording one the graph does not reach."""
    from forge import runstate
    return runstate.State(phase="reviewing", round=1, attempt=0,
                          verified_checkpoint="a" * 40, deliverable_checkpoint="a" * 40)


def _bracket(run, rounds=1, name="bracket"):
    """The journal a bracketed run leaves: one matched, digest-EQUAL worktree pair per round.

    `terminal_from_record` requires it, so `events=()` is no longer "a round with nothing said
    against its checkout" — it is a round nobody measured, and it refuses. Every terminal test
    below therefore has to state that the bracket ran, which is the point: without it, a round
    nobody bracketed left the same record as a round bracketed clean.
    """
    log = journal.Journal(Path(run) / f"{name}.jsonl")
    for n in range(1, rounds + 1):
        review.record_worktree_before(log, round_=n, digest="d" * 64, entries=3)
        review.record_worktree_after(log, round_=n, digest="d" * 64, entries=3, changed={})
    return log.read()


def _clean_round(run, n, checkpoint="a" * 40, silent=()):
    # A silent seat is NOT also a responding one. `terminal_from_record` counts the panel as
    # `responded + silent`, so listing agy in both describes a panel of four; `Round` refuses
    # the pair outright.
    quiet = {s for s, _ in silent}
    review.write_round(run, review.Round(
        n, checkpoint, (), (),
        tuple(s for s in ("claude", "codex", "agy") if s not in quiet), tuple(silent)))


def test_one_string_for_the_unreviewed_label():
    assert review.VERIFIED_NOT_INDEPENDENTLY_REVIEWED == \
        "verified but not independently reviewed"


def test_a_clean_single_round_with_a_full_panel_is_ready(tmp_path):
    run = _run_dir(tmp_path)
    _clean_round(run, 1)
    assert review.terminal_from_record(
        run, rounds_run=1, events=_bracket(run))[0] == review.READY


def test_a_silent_seat_degrades_a_round_that_found_nothing(tmp_path):
    """A panel of one describing itself as a panel of three is what §13's whole record is
    written against."""
    run = _run_dir(tmp_path)
    _clean_round(run, 1, silent=(("agy", "auth_or_quota"),))
    answer, why = review.terminal_from_record(run, rounds_run=1, events=_bracket(run))
    assert answer == review.DEGRADED and "agy" in why


def test_an_open_blocker_with_no_resolution_record_is_review_blocked(tmp_path):
    """Absence of a fix record is an unfixed blocker, never a resolved one."""
    run = _run_dir(tmp_path)
    review.write_round(run, review.Round(1, "a" * 40, (_blocker(),), (),
                                         ("claude", "codex", "agy"), ()))
    answer, _ = review.terminal_from_record(run, rounds_run=1, events=_bracket(run))
    assert answer == review.REVIEW_BLOCKED


def test_a_blocker_fixed_and_then_re_reviewed_clean_is_ready(tmp_path):
    run = _run_dir(tmp_path)
    b = _blocker()
    review.write_round(run, review.Round(1, "a" * 40, (b,), (),
                                         ("claude", "codex", "agy"), ()))
    review.write_resolutions(run, 1, (review.Resolution(b.id, "fixed", "b" * 40, True),))
    _clean_round(run, 2, checkpoint="b" * 40)
    assert review.terminal_from_record(
        run, rounds_run=2, events=_bracket(run, 2))[0] == review.READY


def test_a_blocker_fixed_in_the_last_round_is_degraded_not_ready(tmp_path):
    """CONTRADICTION 6's resolution: fixed-but-not-re-reviewed is `degraded`, whether the
    finding came from review round 2 or from ultrareview."""
    run = _run_dir(tmp_path)
    b = _blocker(round_=2)
    _clean_round(run, 1)
    review.write_round(run, review.Round(2, "b" * 40, (b,), (),
                                         ("claude", "codex", "agy"), ()))
    review.write_resolutions(run, 2, (review.Resolution(b.id, "fixed", "c" * 40, True),))
    answer, why = review.terminal_from_record(run, rounds_run=2, events=_bracket(run, 2))
    assert answer == review.DEGRADED
    assert review.VERIFIED_NOT_INDEPENDENTLY_REVIEWED in why


def test_a_fix_that_broke_verify_leaves_the_finding_unresolved(tmp_path):
    """§13: 'a fix that breaks verify is reverted and the finding reported unresolved'."""
    run = _run_dir(tmp_path)
    b = _blocker()
    review.write_round(run, review.Round(1, "a" * 40, (b,), (),
                                         ("claude", "codex", "agy"), ()))
    review.write_resolutions(run, 1, (review.Resolution(b.id, "unresolved", None, False),))
    assert review.terminal_from_record(run, rounds_run=1, events=_bracket(run))[0] \
        == review.REVIEW_BLOCKED


def test_a_rejected_blocker_is_still_an_open_one(tmp_path):
    """THE FAIL-OPEN. `rejected` matched none of the branches, so the blocker was appended to
    neither roll-up and vanished: measured, the run answered
    `('ready', '1 round(s), every panel whole and no blocker left open')` over a blocker
    somebody had dismissed. A `Resolution` records no rationale and no author, so this record
    cannot tell a considered rejection from a finding nobody acted on."""
    run = _run_dir(tmp_path)
    b = _blocker()
    review.write_round(run, review.Round(1, "a" * 40, (b,), (),
                                         ("claude", "codex", "agy"), ()))
    review.write_resolutions(run, 1, (review.Resolution(b.id, "rejected", None, False),))
    answer, why = review.terminal_from_record(run, rounds_run=1, events=_bracket(run))
    assert answer == review.REVIEW_BLOCKED and "rejected" in why


def test_every_terminal_this_module_declares_is_a_declared_successor_of_reviewing():
    """THE CROSS-MODULE VOCABULARY, PINNED WHERE ITS SIBLINGS ARE. `settle` and `_stop` hand
    `terminal_from_record`'s answer to `runstate.advance` from `reviewing`, which refuses an
    edge §14 does not declare — so a terminal added here and not there turns every run that
    reaches it into a `TransitionError` instead of a verdict. Held today by two lists agreeing
    by hand, next door to `set(rubric.GATE_RANK) == set(verify.OUTCOMES)` and
    `set(review.RESOLUTIONS) == set(review._READINGS)`, which are the same seam pinned."""
    from forge import runstate
    assert set(review.TERMINALS) <= runstate._EDGES["reviewing"], \
        "a terminal `advance` would refuse the edge to is one no review can ever record"
    assert set(review.TERMINALS) <= runstate.TERMINAL, \
        "§13's three END the run; one with a successor is a run that reports and carries on"


def test_every_declared_resolution_has_a_terminal_reading(tmp_path):
    """TOTAL BY CONSTRUCTION, not by four branches happening to cover four values. A
    resolution added to `RESOLUTIONS` later must land in a roll-up or raise — never
    disappear."""
    assert set(review.RESOLUTIONS) == set(review._READINGS)
    for res in review.RESOLUTIONS:
        run = _run_dir(tmp_path, f"run-{res}")
        b = _blocker()
        review.write_round(run, review.Round(1, "a" * 40, (b,), (),
                                             ("claude", "codex", "agy"), ()))
        row = (review.Resolution(b.id, res, "b" * 40, True) if res == "fixed"
               else review.Resolution(b.id, res, None, False))
        review.write_resolutions(run, 1, (row,))
        answer, _ = review.terminal_from_record(run, rounds_run=1, events=_bracket(run))
        assert answer != review.READY, res
        assert answer in (review.REVIEW_BLOCKED, review.DEGRADED), (res, answer)


def test_a_non_blocker_finding_does_not_block(tmp_path):
    run = _run_dir(tmp_path)
    minor = review.Finding(id=review.finding_id(1, "agy", "minor", "typo", "doc.md:1"),
                           round=1, seat="agy", severity="minor", claim="typo",
                           evidence="doc.md:1", resolution="open")
    review.write_round(run, review.Round(1, "a" * 40, (minor,), (),
                                         ("claude", "codex", "agy"), ()))
    assert review.terminal_from_record(
        run, rounds_run=1, events=_bracket(run))[0] == review.READY


def test_a_missing_round_record_refuses_rather_than_classifying(tmp_path):
    run = _run_dir(tmp_path)
    _clean_round(run, 1)
    with pytest.raises(review.ReviewError):
        review.terminal_from_record(run, rounds_run=2, events=_bracket(run, 2))


def test_an_orphaned_council_round_refuses_rather_than_classifying(tmp_path):
    """§14.1: a start with no receipt is `outcome_unknown` and is never silently retried."""
    run = _run_dir(tmp_path)
    _clean_round(run, 1)
    log = journal.Journal(storage.journal_path(run))
    log.record(journal.intent(review.COUNCIL_KIND), operation_id="review-round-1", round=1)
    with pytest.raises(review.ReviewError) as e:
        review.terminal_from_record(run, rounds_run=1, events=log.read())
    assert "review-round-1" in str(e.value)


def test_the_resolutions_record_is_written_once(tmp_path):
    run = _run_dir(tmp_path)
    rows = (review.Resolution("x" * 12, "fixed", "b" * 40, True),)
    review.write_resolutions(run, 1, rows)
    with pytest.raises(review.ReviewError):
        review.write_resolutions(run, 1, rows)


def test_a_resolution_naming_a_fixed_finding_with_no_checkpoint_is_refused():
    with pytest.raises(review.ReviewError):
        review.Resolution("x" * 12, "fixed", None, True)


def test_a_resolution_claiming_fixed_and_unverified_is_refused():
    with pytest.raises(review.ReviewError):
        review.Resolution("x" * 12, "fixed", "b" * 40, False)


def test_the_loop_stops_at_two_rounds_and_never_buys_a_third(tmp_path):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))
    rounds = []

    def fake_round(run_dir, *, round_, checkpoint, **kw):
        b = _blocker(round_=round_)
        r = review.Round(round_, checkpoint, (b,), (), ("claude", "codex", "agy"), ())
        review.write_round(run_dir, r)
        rounds.append(round_)
        return r

    def fix(findings, checkpoint):
        return checkpoint[:-1] + "f", True

    answer, why = review.loop(run, state=_reviewing(), checkout=co, checkpoint="a" * 40,
                              baseline_commit="b" * 40, baseline_tree="c" * 40,
                              artifact_manifest=None, other_clones=(), log=log,
                              manifest=_Cap(), fix=fix, run=fake_round)
    assert rounds == [1, 2]
    assert answer == review.REVIEW_BLOCKED


def test_the_loop_stops_when_the_synthesis_fix_cap_is_exhausted(tmp_path):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))
    calls = []

    def fake_round(run_dir, *, round_, checkpoint, **kw):
        b = _blocker(round_=round_)
        r = review.Round(round_, checkpoint, (b,), (), ("claude", "codex", "agy"), ())
        review.write_round(run_dir, r)
        return r

    def fix(findings, checkpoint):
        calls.append(checkpoint)
        return checkpoint[:-1] + "f", True

    answer, why = review.loop(run, state=_reviewing(), checkout=co, checkpoint="a" * 40,
                              baseline_commit="b" * 40, baseline_tree="c" * 40,
                              artifact_manifest=None, other_clones=(), log=log,
                              manifest=_Cap(cap=0), fix=fix, run=fake_round)
    assert calls == [], "a cap of zero funds no fix at all"
    assert answer == review.REVIEW_BLOCKED and "cap" in why


def test_a_fix_that_did_not_pass_verify_stops_the_loop_and_reports_unresolved(tmp_path):
    """§13: "a fix that breaks verify is reverted and the finding reported unresolved."
    Measured, this branch had NO test and its mutation SURVIVED — and without it the loop
    records a reverted fix as `fixed`, advances `current` to a checkpoint that does not
    exist, and buys round 2 on top of it."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))
    rounds = []

    def fake_round(run_dir, *, round_, checkpoint, **kw):
        b = _blocker(round_=round_)
        r = review.Round(round_, checkpoint, (b,), (), ("claude", "codex", "agy"), ())
        review.write_round(run_dir, r)
        rounds.append(round_)
        return r

    def fix(findings, checkpoint):
        return None, False              # reverted: no checkpoint, verify did not pass

    answer, why = review.loop(run, state=_reviewing(), checkout=co, checkpoint="a" * 40,
                              baseline_commit="b" * 40, baseline_tree="c" * 40,
                              artifact_manifest=None, other_clones=(), log=log,
                              manifest=_Cap(), fix=fix, run=fake_round)
    assert rounds == [1], "a fix that broke verify does not buy a second round"
    assert answer == review.REVIEW_BLOCKED and "verify" in why
    assert [r.resolution for r in review.read_resolutions(run, 1)] == ["unresolved"]


def test_the_loop_records_a_fix_pair_on_the_journal(tmp_path):
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))

    def fake_round(run_dir, *, round_, checkpoint, **kw):
        findings = (_blocker(round_=round_),) if round_ == 1 else ()
        r = review.Round(round_, checkpoint, findings, (), ("claude", "codex", "agy"), ())
        review.write_round(run_dir, r)
        return r

    head = subprocess.run(["git", "-C", str(co), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()

    def fix(findings, checkpoint):
        return head, True

    answer, _ = review.loop(run, state=_reviewing(), checkout=co, checkpoint=head,
                            baseline_commit="b" * 40,
                            baseline_tree="c" * 40, artifact_manifest=None,
                            other_clones=(), log=log, manifest=_Cap(), fix=fix,
                            run=fake_round)
    from forge import progress
    kinds = [e.event for e in log.read()]
    assert journal.intent(progress.FIX_KIND) in kinds
    assert journal.done(progress.FIX_KIND) in kinds
    assert journal.orphans(log.read()) == ()
    assert answer == review.READY


def test_the_loop_writes_down_the_phase_it_classified(tmp_path):
    """A run that classifies and never persists is a resume hazard of the bracket's own shape,
    one field over: `--collect` finds a position saying the review had not finished beside a
    findings record saying it had, and nothing says which is current."""
    from forge import runstate
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))

    def fake_round(run_dir, *, round_, checkpoint, **kw):
        r = review.Round(round_, checkpoint, (), (), ("claude", "codex", "agy"), ())
        review.write_round(run_dir, r)
        return r

    assert runstate.read_state(run) is None
    answer, _ = review.loop(run, state=_reviewing(), checkout=co, checkpoint="a" * 40,
                            baseline_commit="b" * 40, baseline_tree="c" * 40,
                            artifact_manifest=None, other_clones=(), log=log,
                            manifest=_Cap(), fix=lambda f, c: (c, True), run=fake_round)
    assert answer == review.READY
    assert runstate.read_state(run).phase == review.READY


def test_a_loop_stopped_by_the_fix_cap_records_its_phase_and_the_records_reason(tmp_path):
    """The two exits the loop reaches by its own control flow persist too — otherwise the
    cheapest way to leave a run unrecorded is the branch that spends nothing. The terminal is
    still the record's; the loop's sentence names the cap, which the record cannot."""
    from forge import runstate
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))

    def fake_round(run_dir, *, round_, checkpoint, **kw):
        r = review.Round(round_, checkpoint, (_blocker(round_=round_),), (),
                         ("claude", "codex", "agy"), ())
        review.write_round(run_dir, r)
        return r

    answer, why = review.loop(run, state=_reviewing(), checkout=co, checkpoint="a" * 40,
                              baseline_commit="b" * 40, baseline_tree="c" * 40,
                              artifact_manifest=None, other_clones=(), log=log,
                              manifest=_Cap(cap=0), fix=lambda f, c: (c, True),
                              run=fake_round)
    assert answer == review.REVIEW_BLOCKED
    assert runstate.read_state(run).phase == review.REVIEW_BLOCKED
    assert "cap" in why and "unresolved" in why, \
        "the record's classification and the loop's own reason, neither one standing in " \
        "for the other"


def test_a_refused_loop_leaves_the_run_where_it_was(tmp_path):
    """§14.1's `outcome_unknown` is not one of `reviewing`'s successors, so a run whose
    terminal could not be determined has no phase to move to — and `reviewing` is the honest
    position for a resume to find. A phase written on the way out of a refusal would be a
    position stated more confidently than the evidence for it."""
    from forge import runstate
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))

    def fake_round(run_dir, *, round_, checkpoint, **kw):
        _write_into(co, "sneaked.txt", "x\n")
        r = review.Round(round_, checkpoint, (), (), ("claude", "codex", "agy"), ())
        review.write_round(run_dir, r)
        return r

    with pytest.raises(review.ReviewError):
        review.loop(run, state=_reviewing(), checkout=co, checkpoint="a" * 40,
                    baseline_commit="b" * 40, baseline_tree="c" * 40,
                    artifact_manifest=None, other_clones=(), log=log, manifest=_Cap(),
                    fix=lambda f, c: (c, True), run=fake_round)
    assert runstate.read_state(run) is None


def test_settle_advances_the_run_state_through_a_declared_edge(tmp_path):
    from forge import runstate
    run = _run_dir(tmp_path)
    _clean_round(run, 1)
    state = runstate.State(phase="reviewing", round=1, attempt=0,
                           verified_checkpoint="a" * 40, deliverable_checkpoint="a" * 40)
    new, why = review.settle(run, state, rounds_run=1, events=_bracket(run))
    assert new.phase == review.READY
    assert runstate.read_state(run).phase == review.READY and why


# ------------------------------------------------- what the record says about the reviewers
def test_a_round_nobody_answered_does_not_read_as_a_clean_one(tmp_path):
    """THE FAIL-OPEN ONE READ LEVEL UP FROM `reviewer_specs`. That function refuses to convene
    an empty panel, but `terminal_from_record` reads a RECORD — and `Round`'s own docstring
    makes `Round(1, sha, (), (), (), ())` legal, "because a round whose panel could not be
    convened still has to be recordable". Zero findings and zero silent seats is precisely
    what three reviewers who found nothing leave behind."""
    run = _run_dir(tmp_path)
    review.write_round(run, review.Round(1, "a" * 40, (), (), (), ()))
    answer, why = review.terminal_from_record(run, rounds_run=1, events=_bracket(run))
    assert answer == review.DEGRADED
    assert review.VERIFIED_NOT_INDEPENDENTLY_REVIEWED in why


def test_two_resolutions_for_one_finding_are_never_written(tmp_path):
    """`terminal_from_record` keys the round's resolutions by finding id, so a second row for
    one finding silently replaces the first: `fixed` beside `open` for the same blocker reads
    as whichever the writer put last."""
    run = _run_dir(tmp_path)
    with pytest.raises(review.ReviewError) as e:
        review.write_resolutions(run, 1, (review.Resolution("x" * 12, "fixed", "b" * 40, True),
                                          review.Resolution("x" * 12, "open", None, False)))
    assert "x" * 12 in str(e.value)


def test_two_resolutions_for_one_finding_are_refused_on_the_way_back_in_too(tmp_path):
    """The write refusal covers this module's own writer; the read refusal covers the file,
    which is what a resumed run and `--collect` actually classify from."""
    run = _run_dir(tmp_path)
    path = review.resolutions_path(run, 1)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([
        {"finding_id": "x" * 12, "resolution": "fixed", "checkpoint": "b" * 40,
         "verified": True},
        {"finding_id": "x" * 12, "resolution": "open", "checkpoint": None,
         "verified": False}]))
    with pytest.raises(review.ReviewError):
        review.read_resolutions(run, 1)


# ------------------------------------------- the checkout the reviewers were told not to touch
def _write_into(co, name, text):
    (Path(co) / name).write_text(text)


def test_a_panel_that_wrote_into_the_checkout_makes_its_round_inadmissible(tmp_path):
    """§13's reviewers are write-capable and share the synthesis checkout. This DETECTS one
    that wrote; it does not prevent it — so the round's findings are refused rather than
    classified."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))

    def fake_round(run_dir, *, round_, checkpoint, **kw):
        _write_into(co, "src.py", "print('the reviewer fixed it')\n")
        r = review.Round(round_, checkpoint, (), (), ("claude", "codex", "agy"), ())
        review.write_round(run_dir, r)
        return r

    with pytest.raises(review.ReviewError) as e:
        review.loop(run, state=_reviewing(), checkout=co, checkpoint="a" * 40,
                    baseline_commit="b" * 40,
                    baseline_tree="c" * 40, artifact_manifest=None, other_clones=(),
                    log=log, manifest=_Cap(), fix=lambda f, c: (c, True), run=fake_round)
    assert "src.py" in str(e.value)


def test_a_disturbed_checkout_buys_no_fix_and_no_second_round(tmp_path):
    """The findings are inadmissible, so nothing may be spent acting on them."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))
    rounds, fixes = [], []

    def fake_round(run_dir, *, round_, checkpoint, **kw):
        rounds.append(round_)
        _write_into(co, "sneaked.txt", "x\n")
        r = review.Round(round_, checkpoint, (_blocker(round_=round_),), (),
                         ("claude", "codex", "agy"), ())
        review.write_round(run_dir, r)
        return r

    def fix(findings, checkpoint):
        fixes.append(checkpoint)
        return checkpoint, True

    with pytest.raises(review.ReviewError) as e:
        review.loop(run, state=_reviewing(), checkout=co, checkpoint="a" * 40,
                    baseline_commit="b" * 40,
                    baseline_tree="c" * 40, artifact_manifest=None, other_clones=(),
                    log=log, manifest=_Cap(), fix=fix, run=fake_round)
    assert "sneaked.txt" in str(e.value), "the refusal is the tree's, not something else's"
    assert rounds == [1] and fixes == []
    from forge import progress
    assert journal.intent(progress.FIX_KIND) not in [e.event for e in log.read()]


def test_the_disturbance_is_read_back_off_the_journal_and_not_out_of_memory(tmp_path):
    """A crash between the write and the verdict must not change the answer, which is the
    same rule §13 states for the findings themselves."""
    run = _run_dir(tmp_path)
    _clean_round(run, 1)
    log = journal.Journal(storage.journal_path(run))
    review.record_worktree_before(log, round_=1, digest="a" * 64, entries=3)
    review.record_worktree_after(log, round_=1, digest="b" * 64, entries=4,
                                 changed={"src.py": "modified"})
    with pytest.raises(review.ReviewError) as e:
        review.terminal_from_record(run, rounds_run=1, events=log.read())
    assert "src.py" in str(e.value)
    assert review.terminal_from_record(
        run, rounds_run=1, events=_bracket(run, name="clean"))[0] == review.READY, \
        "the same round over a journal whose bracket held classifies clean — the record is " \
        "what refuses, and nothing held in this process's memory is consulted either way"


def test_a_checkout_measured_before_the_panel_and_never_after_it_is_refused(tmp_path):
    """§14.1's orphan, applied to the tree rather than to the call: a round that started its
    measurement and never finished it cannot say whether a reviewer wrote."""
    run = _run_dir(tmp_path)
    _clean_round(run, 1)
    log = journal.Journal(storage.journal_path(run))
    review.record_worktree_before(log, round_=1, digest="a" * 64, entries=3)
    with pytest.raises(review.ReviewError) as e:
        review.terminal_from_record(run, rounds_run=1, events=log.read())
    assert "review-worktree-1" in str(e.value)


def test_two_unmeasured_digests_do_not_agree_that_nothing_changed(tmp_path):
    """THE FAIL-OPEN THIS PAIR IS SHAPED AGAINST: a missing digest on each side reads back as
    `None`, and `None == None` would report a tree nobody measured as an undisturbed one."""
    run = _run_dir(tmp_path)
    _clean_round(run, 1)
    log = journal.Journal(storage.journal_path(run))
    op = "review-worktree-1"
    log.record(journal.intent(review.WORKTREE_KIND), operation_id=op, round=1)
    log.record(journal.done(review.WORKTREE_KIND), operation_id=op, round=1)
    assert len(review.worktree_disturbances(log.read(), rounds_run=1)) == 1
    with pytest.raises(review.ReviewError):
        review.terminal_from_record(run, rounds_run=1, events=log.read())


def test_a_round_nobody_bracketed_is_refused_rather_than_read_as_a_clean_one(tmp_path):
    """DOES NOTHING LEAVE THE SAME RECORD AS NOBODY? It did. A round whose checkout was never
    measured left byte-for-byte the journal a round measured clean leaves — none of it — and
    the invariant held only because `loop` was the single writer of these records. A resume
    path that reads the record without having run the loop is a second writer."""
    run = _run_dir(tmp_path)
    _clean_round(run, 1)
    with pytest.raises(review.ReviewError) as e:
        review.terminal_from_record(run, rounds_run=1, events=())
    assert "review-worktree-1" in str(e.value)
    assert review.terminal_from_record(run, rounds_run=1, events=_bracket(run))[0] \
        == review.READY, "the same record with the bracket on it classifies clean"


def test_the_public_disturbance_check_answers_for_every_round_it_was_given():
    """WHOSE GUARANTEE IS IT. `worktree_disturbances` says "every round", and measured before
    it took the round count it answered `()` for an empty journal — a run with no bracket at
    all, every round unmeasured, with nothing said against it. Totality lived one level up, in
    `terminal_from_record`, which made this function's promise a fact about its caller. Plan J
    is the first outside consumer, and it reads the name and the docstring."""
    assert review.worktree_disturbances((), rounds_run=1) != ()
    assert len(review.worktree_disturbances((), rounds_run=3)) == 3
    for bad in (0, -1, True, "1", None):
        with pytest.raises(review.ReviewError):
            review.worktree_disturbances((), rounds_run=bad)
    with pytest.raises(TypeError):
        review.worktree_disturbances(())      # never defaulted: an omitted count is a claim
                                              # nobody made, and is wrong silently


def test_a_later_round_nobody_bracketed_is_refused_even_when_an_earlier_one_was(tmp_path):
    """The requirement ranges over every round the caller says ran, not over the first one:
    a run bracketed while somebody was watching and unbracketed afterwards is the shape a
    per-run check would miss."""
    run = _run_dir(tmp_path)
    _clean_round(run, 1)
    _clean_round(run, 2, checkpoint="b" * 40)
    with pytest.raises(review.ReviewError) as e:
        review.terminal_from_record(run, rounds_run=2, events=_bracket(run, 1))
    assert "review-worktree-2" in str(e.value) and "review-worktree-1" not in str(e.value)


def test_an_inventory_over_its_quota_refuses_rather_than_digesting_an_empty_tree(tmp_path):
    """`snapshot.take` answers a breach with an EMPTY inventory beside the breach line, so two
    breached measurements digest alike and the round would read as undisturbed on two scans
    that measured nothing."""
    co = _checkout(tmp_path)
    _write_into(co, "a.txt", "a\n")
    _write_into(co, "b.txt", "b\n")
    with pytest.raises(review.ReviewError) as e:
        review.worktree_identity(co, storage.Quota(max_files=1, max_file_bytes=1 << 20,
                                                   max_total_bytes=1 << 20))
    assert "quota" in str(e.value)


def test_the_identity_moves_on_content_alone(tmp_path):
    """Never mtime, never size: §7.3's predicate is content + mode + size, and a reviewer's
    edit is exactly the same-length rewrite an lstat-keyed check would miss."""
    co = _checkout(tmp_path)
    quota = storage.Quota.for_harvest()
    before, _ = review.worktree_identity(co, quota)
    _write_into(co, "src.py", "print('ho')\n")
    assert len((Path(co) / "src.py").read_text()) == len("print('hi')\n")
    after, _ = review.worktree_identity(co, quota)
    assert before != after


def test_git_doing_what_the_bundle_asks_does_not_disturb_the_checkout(tmp_path):
    """THE FALSE-POSITIVE CONTROL, and the documented gap in one assertion. The bundle tells
    every reviewer to run `git diff`, which refreshes and rewrites the index; the review
    inputs are laid down in the git directory too. Both are invisible here BECAUSE the git
    directory is not measured — which is the gap this check does not cover, stated as a test
    rather than only as a sentence."""
    co = _checkout(tmp_path)
    quota = storage.Quota.for_harvest()
    before, _ = review.worktree_identity(co, quota)
    for args in (["status"], ["diff", "HEAD"], ["log", "--oneline"]):
        subprocess.run(["git", "-C", str(co), *args], check=True, capture_output=True)
    gd = Path(subprocess.run(["git", "-C", str(co), "rev-parse", "--absolute-git-dir"],
                             check=True, capture_output=True, text=True).stdout.strip())
    (gd / "a-reviewer-wrote-here.txt").write_text("invisible to this check\n")
    after, _ = review.worktree_identity(co, quota)
    assert before == after


def test_a_planted_pyc_is_measured_rather_than_allow_listed(tmp_path):
    """WHAT THE `__pycache__` EXEMPTION ACTUALLY COST, and the reason it is gone. A `.pyc` is
    not inert output: Python executes it IN PREFERENCE TO the untouched source beside it, so
    an exempted `__pycache__` was an unmeasured location inside a tree three write-capable
    reviewers share from which the code under review could be replaced — and the act the
    exemption existed to permit, running the suite, is the act that loads it.

    Both halves are measured here: the planted bytecode really does run over unchanged source,
    and the bracket really does now see it. The writes themselves are prevented one step
    earlier by `reviewer_env`, which is the fix; this is what stands if one appears anyway."""
    co = _checkout(tmp_path)
    (Path(co) / "pkg").mkdir()
    _write_into(co, "pkg/mod.py", 'def who():\n    return "the real source"\n')
    quota = storage.Quota.for_harvest()
    before, _ = review.worktree_identity(co, quota)

    # An honest `.pyc` for `mod.py`, compiled from something else. The header keeps the real
    # source's mtime and size, which is what makes CPython accept it without reading `mod.py`.
    import importlib._bootstrap_external as bootstrap
    import importlib.util
    src = Path(co) / "pkg" / "mod.py"
    st = src.stat()
    code = compile('def who():\n    return "PLANTED BYTECODE"\n', str(src), "exec")
    cache = Path(importlib.util.cache_from_source(str(src)))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(bootstrap._code_to_timestamp_pyc(code, st.st_mtime, st.st_size))

    ran = subprocess.run([sys.executable, "-c", "import pkg.mod; print(pkg.mod.who())"],
                         cwd=str(co), capture_output=True, text=True)
    assert ran.stdout.strip() == "PLANTED BYTECODE", ran.stderr
    assert src.read_text() == 'def who():\n    return "the real source"\n', \
        "the source was never touched, which is the whole point"

    after, _ = review.worktree_identity(co, quota)
    assert before != after, \
        "the code under review was replaced; a bracket that reported this round undisturbed " \
        "would be a verdict reading cleaner than its evidence"


def test_the_reviewer_environment_stops_the_writes_the_exemption_used_to_excuse(tmp_path):
    """THE CAUSE, FIXED INSTEAD OF THE SYMPTOM — measured on a real Python child, no provider.

    The exemption existed because a reviewer running the suite writes `__pycache__` beside
    every module it imports, and burning a three-provider panel over that is too high a price.
    Removing the WRITE removes the need for the exemption, and leaves a cache directory that
    appears anyway as something worth refusing the round over."""
    co = _checkout(tmp_path)
    (Path(co) / "pkg").mkdir()
    _write_into(co, "pkg/mod.py", "x = 1\n")
    quota = storage.Quota.for_harvest()

    def import_it(env):
        return subprocess.run([sys.executable, "-c", "import pkg.mod"],
                              cwd=str(co), capture_output=True, text=True, env=env)

    plain = dict(os.environ)
    plain.pop("PYTHONDONTWRITEBYTECODE", None)
    before, _ = review.worktree_identity(co, quota)
    assert import_it(plain).returncode == 0
    assert review.worktree_identity(co, quota)[0] != before, \
        "the control: an ordinary child DOES write bytecode into the tree it imports from"

    for p in (Path(co) / "pkg" / "__pycache__").glob("*"):
        p.unlink()
    (Path(co) / "pkg" / "__pycache__").rmdir()
    before, _ = review.worktree_identity(co, quota)
    assert import_it(review.reviewer_env(plain)).returncode == 0
    assert review.worktree_identity(co, quota)[0] == before, \
        "under the reviewer's environment the same import leaves the tree byte-identical"


def test_the_reviewer_environment_drops_pythonpycacheprefix(tmp_path):
    """THE SAME ATTACK, RELOCATED OUTSIDE THE TREE. `PYTHONDONTWRITEBYTECODE` stops a NEW
    `.pyc` from being written; it says nothing about one already sitting wherever
    `PYTHONPYCACHEPREFIX` points, which need not be under the checkout `worktree_identity`
    measures. `reviewer_env` copies `os.environ` wholesale, so an ambient value here would
    reach all three reviewers alike — one shared, external location the source under review
    could be replaced from. This measures both halves: the control shows the mirrored
    forgery running under the plain environment, and the fix shows the same import falling
    back to real source once `reviewer_env` has dropped the variable."""
    co = _checkout(tmp_path)
    (Path(co) / "pkg").mkdir()
    _write_into(co, "pkg/mod.py", 'def who():\n    return "the real source"\n')
    mirror = tmp_path / "pycache-mirror"  # OUTSIDE the checkout entirely

    import importlib._bootstrap_external as bootstrap
    import importlib.util
    src = Path(co) / "pkg" / "mod.py"
    st = src.stat()
    code = compile('def who():\n    return "PLANTED BYTECODE"\n', str(src), "exec")
    old_prefix = sys.pycache_prefix
    sys.pycache_prefix = str(mirror)
    try:
        cache = Path(importlib.util.cache_from_source(str(src)))
    finally:
        sys.pycache_prefix = old_prefix
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(bootstrap._code_to_timestamp_pyc(code, st.st_mtime, st.st_size))

    plain = dict(os.environ)
    plain["PYTHONPYCACHEPREFIX"] = str(mirror)
    plain.pop("PYTHONDONTWRITEBYTECODE", None)

    def import_it(env):
        return subprocess.run([sys.executable, "-c", "import pkg.mod; print(pkg.mod.who())"],
                              cwd=str(co), capture_output=True, text=True, env=env)

    control = import_it(plain)
    assert control.stdout.strip() == "PLANTED BYTECODE", control.stderr

    fixed_env = review.reviewer_env(plain)
    assert "PYTHONPYCACHEPREFIX" not in fixed_env
    fixed = import_it(fixed_env)
    assert fixed.stdout.strip() == "the real source", fixed.stderr


def test_the_reviewer_environment_composes_and_keeps_what_it_was_given():
    """`PYTEST_ADDOPTS` is APPENDED to, never replaced: an operator's `--rootdir=…` is theirs,
    and dropping it would change what a reviewer's suite does as a side effect of a guard."""
    out = review.reviewer_env({"PYTEST_ADDOPTS": "--rootdir=/x", "A_MARKER": "kept"})
    assert out["PYTHONDONTWRITEBYTECODE"] == "1"
    assert out["PYTEST_ADDOPTS"] == "--rootdir=/x -p no:cacheprovider"
    assert out["A_MARKER"] == "kept"
    assert review.reviewer_env({})["PYTEST_ADDOPTS"] == "-p no:cacheprovider", \
        "no ambient value must not leave a leading space for pytest to parse"


def test_the_panel_is_launched_under_that_environment(tmp_path):
    """A SEAM CHECKED AT THE CALL, not by reading the function. `run_council` grew `env=` for
    exactly this, and an `env` composed here but never passed would leave every real reviewer
    writing bytecode into the tree while the record read identically."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    record = {}
    answer = ("I read it. TOK\n" + "y" * 500 + '\n```json\n{"findings": []}\n```')
    review.run_round(run, round_=1, checkout=co, checkpoint="a" * 40,
                     baseline_commit="b" * 40, baseline_tree="c" * 40,
                     artifact_manifest=None, other_clones=(),
                     log=journal.Journal(storage.journal_path(run)),
                     run_council=_fake_council(
                         tmp_path, answers={n: (answer, True, "ok")
                                            for n in ("claude", "codex", "agy")},
                         record=record),
                     probe=_probe, make_token=lambda: "TOK")
    assert record["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "-p no:cacheprovider" in record["env"]["PYTEST_ADDOPTS"]


def test_a_directory_git_ignores_is_measured_like_any_other(tmp_path):
    """THE MEASUREMENT RANGES OVER THE WHOLE WORKTREE, and "every path git ignores" is the
    rule it refuses. §7 does not trust `.gitignore` — agent output routinely lands in ignored
    paths, which is why harvest scans instead of asking git — so deriving the skip from a file
    the reviewed tree contains would let a write-capable reviewer choose where it may write
    unseen. `.git` is the one exclusion left, and it is a fixed name rather than a rule."""
    co = _checkout(tmp_path)
    write(co, ".gitignore", "build/\n")
    commit_all(co, "ignore build output")
    quota = storage.Quota.for_harvest()
    before, _ = review.worktree_identity(co, quota)
    (Path(co) / "build").mkdir()
    _write_into(co, "build/planted.py", "print('a reviewer wrote this')\n")
    assert subprocess.run(["git", "-C", str(co), "check-ignore", "-q", "build/planted.py"],
                          capture_output=True).returncode == 0, \
        "the path really is one git ignores, which is what makes this the right control"
    after, _ = review.worktree_identity(co, quota)
    assert before != after


def test_a_path_whose_type_changed_moves_the_identity(tmp_path):
    """`snapshot.diff` deliberately does NOT compare `kind`, so the digest has to — and this
    is the pair where every other field agrees. A symlink whose target TEXT is `special:4096`
    digests to `sha256(b"special:4096")` at mode 0 and size 0; `snapshot._special_entry` gives
    a FIFO (S_IFMT 4096) at mode 0 the same three. Without `kind` the swap is invisible."""
    co = _checkout(tmp_path)
    quota = storage.Quota.for_harvest()
    p = Path(co) / "swap"
    os.symlink("special:4096", p)
    before, before_entries = review.worktree_identity(co, quota)
    p.unlink()
    os.mkfifo(p)
    os.chmod(p, 0)
    after, after_entries = review.worktree_identity(co, quota)
    a, b = before_entries["swap"], after_entries["swap"]
    assert (a.digest, a.mode, a.size) == (b.digest, b.mode, b.size) and a.kind != b.kind
    assert snapshot.diff(before_entries, after_entries) == {}, \
        "the diff has nothing to say about this one, which is why the digest must"
    assert before != after


def test_the_bracket_does_not_fire_on_forges_own_writes(tmp_path):
    """WHAT EVERY OTHER LOOP TEST'S FAKE ROUND CANNOT REACH. `run_round` lays the reviewer
    inputs down inside the checkout's GIT DIRECTORY and the council's workdir under the run
    directory; if either landed in the worktree, every real round would report its own bundle
    as a reviewer that wrote and no run would ever reach a terminal. Driven through the real
    `run_round` with the council faked — NO PROVIDER IS INVOKED."""
    run = _run_dir(tmp_path)
    _ledger(run)
    co = _checkout(tmp_path)
    log = journal.Journal(storage.journal_path(run))
    clean = ("I read the bundle. TOK\n" + "y" * 500
             + '\n```json\n{"findings": []}\n```')
    answers = {n: (clean, True, "ok") for n in ("claude", "codex", "agy")}

    def runner(run_dir, **kw):
        return review.run_round(
            run_dir, run_council=_fake_council(tmp_path, answers=answers, record={}),
            probe=_probe, make_token=lambda: "TOK", **kw)

    answer, why = review.loop(run, state=_reviewing(), checkout=co, checkpoint="a" * 40,
                              baseline_commit="b" * 40, baseline_tree="c" * 40,
                              artifact_manifest=None, other_clones=(), log=log,
                              manifest=_Cap(), fix=lambda f, c: (c, True), run=runner)
    assert answer == review.READY, why
    assert journal.orphans(log.read()) == ()
    assert review.review_dir(co, 1).is_dir(), "the bundle really was written"


def test_a_checkout_that_cannot_be_inventoried_whole_refuses(tmp_path):
    """`snapshot.take` raises rather than returning a short inventory, and this module has to
    meet that in its own vocabulary — a partial answer here is a tree nobody measured reported
    as one nobody touched."""
    with pytest.raises(review.ReviewError) as e:
        review.worktree_identity(tmp_path / "not-there", storage.Quota.for_harvest())
    assert "inventoried whole" in str(e.value)
