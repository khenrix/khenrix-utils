"""§13's reviewer input set, the in-process council call, and the durable findings record."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from council import engine  # noqa: E402
from forge import journal, ledger, review, storage  # noqa: E402

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
    rows, why = review.parse_findings(
        'prose\n```json\n{"findings": [{"severity": "blocker", "claim": "c"}]}\n```\n')
    assert rows == [{"severity": "blocker", "claim": "c"}] and why == ""


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
        ' "claim": "c"}]}\n```')
    # NOT `rows is None and "two" in why or "more than one" in why` — `and` binds tighter, so
    # that reads `(A and B) or C`, and C is true of the message whatever `rows` is.
    assert rows is None and "more than one" in why


def test_a_severity_outside_the_declared_set_is_unreadable():
    rows, why = review.parse_findings(
        '```json\n{"findings": [{"severity": "catastrophic", "claim": "c"}]}\n```')
    assert rows is None and "severity" in why


def test_a_finding_with_no_claim_is_unreadable():
    rows, why = review.parse_findings(
        '```json\n{"findings": [{"severity": "blocker"}]}\n```')
    assert rows is None


def test_malformed_json_is_unreadable():
    rows, why = review.parse_findings('```json\n{"findings": [\n```')
    assert rows is None and "json" in why.lower()


# --------------------------------------------------------------------------- the record
def _finding(round_=1, seat="claude", severity="blocker", claim="c", resolution="open"):
    return review.Finding(id=review.finding_id(round_, seat, severity, claim), round=round_,
                          seat=seat, severity=severity, claim=claim, resolution=resolution)


def test_a_findings_id_is_content_derived_and_stable():
    a = review.finding_id(1, "claude", "blocker", "the cache is unbounded")
    b = review.finding_id(1, "claude", "blocker", "the cache is unbounded")
    c = review.finding_id(2, "claude", "blocker", "the cache is unbounded")
    assert a == b and a != c and len(a) == 12


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
                       claim="c", resolution="open")


def test_a_finding_with_an_undeclared_resolution_cannot_be_recorded():
    with pytest.raises(review.ReviewError):
        review.Finding(id="x" * 12, round=1, seat="claude", severity="blocker",
                       claim="c", resolution="probably-fine")


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


# --------------------------------------------------------------------------- the round
ANSWER = ('I reviewed the diff.\n' + 'x' * 500 + '\nToken: {tok}\n'
          '```json\n{{"findings": [{{"severity": "blocker", "claim": "unbounded cache"}}]}}\n```')


def _fake_council(tmp_path, *, answers, record):
    """A `run_council` stand-in. NO PROVIDER IS INVOKED ANYWHERE IN THIS SUITE."""
    def run_council(specs, *, retries, timeout, backoff, workdir, prompt=None,
                    requested=None, mode=None, read_only=None, install_signal_handler=True):
        record.update(retries=retries, timeout=timeout, workdir=Path(workdir), prompt=prompt,
                      mode=mode, read_only=read_only,
                      install_signal_handler=install_signal_handler,
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
