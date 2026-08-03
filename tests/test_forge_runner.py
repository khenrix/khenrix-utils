"""One seat, end to end: the chain from a clone to a candidate, and what it records.

NO TEST HERE INVOKES A REAL PROVIDER. `launch` is injected and every test passes `_fake`,
which writes into the seat and hands back a `council.engine.run_provider`-shaped record — so
the clone, the setup, all three inventories, the artifact set and the bundle are real, and
only the provider is not. §5.2 prices a real fleet in provider calls; a suite that spends
them is one nobody runs. `test_the_launch_is_injected_with_no_default` is what keeps that
structural rather than conventional.
"""
import inspect as pyinspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import pytest  # noqa: E402
from forge import (baseline, inspect as finspect, runner,  # noqa: E402
                   runstate, seat as seatmod, storage, verify)
from forge_fixtures import make_repo, write  # noqa: E402

IDENT = ("Forge Seat", "seat@forge.invalid")

# Long enough to clear `seat._MIN_RATIONALE_CHARS`, because a seat that changed nothing is
# classified through §8's rationale rule and a two-word sign-off is refused there by design.
ANSWER = "the retry already backs off; adding one would double-sleep"


def _fake(fn=None, *, answer=ANSWER, valid=True, quote_token=True):
    """A launch callable: run `fn(seat_path)`, then hand back a provider-shaped record.

    The record's keys are `run_provider`'s own — `valid` is what §8's `process` dimension
    reads and `result_text` is what `seat.read_proof` and the `no_change` rationale read.
    `quote_token` decides whether the answer cites the sentinel, which is the only input to
    `proven_read`.
    """
    calls = []

    def launch(*, name, seat_path, token, env):
        calls.append({"name": name, "seat_path": Path(seat_path), "token": token, "env": env})
        if fn is not None:
            fn(Path(seat_path))
        text = f"{answer}\n{token}" if quote_token else answer
        return {"name": name, "status": "ok" if valid else "failed", "valid": valid,
                "reason": "ok" if valid else "empty", "exit_code": 0 if valid else 1,
                "duration_sec": 1.5, "structured": False, "attempts": 1,
                "result_text": text}

    launch.calls = calls
    return launch


def _manifest(repo, b, setup):
    refs, digest = runstate.snapshot_refs(repo, (), forge_refs={b.ref: b.commit})
    return runstate.Manifest(
        run_id="r1", repo_path=str(repo), base_commit=b.base_commit,
        baseline_ref=b.ref, baseline_commit=b.commit, tracked_tree_oid=b.tracked_tree_oid,
        selected_paths=(), generator_contract=finspect.GeneratorContract(),
        setup=setup, verify=(verify.Step(argv=("true",)),),
        protected_refs=refs, forge_refs={b.ref: b.commit}, status_digest=digest,
        index_digest=runstate.snapshot_index(repo), created_at="2026-08-03T00:00:00Z",
        seats=3, attempts=3)


def _open(tmp_path, *, setup=(verify.Step(argv=("true",)),)):
    """A repository, a run directory, B, and the manifest that agreed to them."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    m = _manifest(repo, b, setup)
    runstate.write_manifest(run, m)
    return repo, run, b, m


def test_a_retry_gets_a_fresh_clone_and_the_failed_attempt_survives(tmp_path):
    """§8.1: never a reset-and-rerun in place. The failed attempt is partial INPUT, so
    deleting it is losing evidence, not tidying."""
    repo, run, b, m = _open(tmp_path)
    first = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                            launch=_fake(lambda p: write(p, "half.py", "half\n")))
    second = runner.run_seat(m, run, b, name="claude", attempt=2, identity=IDENT,
                             launch=_fake(lambda p: write(p, "done.py", "done\n")))
    assert first.path != second.path
    assert first.path.exists(), "the failed attempt is preserved as partial input"
    assert not (second.path / "half.py").exists(), "attempt 2 did not start on attempt 1's work"
    # The other half of "preserved": attempt 1's own work is still THERE, not merely its
    # directory. A retention policy that emptied the clone would pass every line above.
    assert (first.path / "half.py").read_text() == "half\n"
    assert first.artifacts.paths == ("half.py",)
    assert second.artifacts.paths == ("done.py",), \
        "and attempt 2's harvest is its own, not the union of both attempts"


def test_running_the_same_attempt_twice_is_refused_rather_than_reusing_the_clone(tmp_path):
    """The refusal IS the no-delete rule. Nothing in `runner` clears a seat directory, so a
    repeated attempt number has to stop rather than quietly overwrite the evidence §8.1
    requires kept."""
    repo, run, b, m = _open(tmp_path)
    first = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                            launch=_fake(lambda p: write(p, "half.py", "half\n")))
    with pytest.raises(runner.RunnerError):
        runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "other.py", "other\n")))
    assert (first.path / "half.py").read_text() == "half\n", \
        "the refused call left the earlier attempt exactly as it found it"


def test_a_launch_that_writes_nothing_is_not_discarded_as_failed(tmp_path):
    """§8: a correct conclusion that the task needs no edit must not be discarded.

    It is not reported as `no_change` either, and that is not a hedge. §8 makes `no_change`
    require independent verification, and §6 puts every verification in a clone the builder
    never had — "running the confirmed command in the seat's own clone therefore measures
    nothing", priced at the gate, where `verify_runs` counts the verifier clones and not the
    builders. So at harvest time the check that would confirm the claim has not happened,
    `verify` is `not-run`, and the honest verdict is `partial`: the argument is recorded and
    the promotion is withheld, rather than the seat being thrown away as `failed`.
    """
    repo, run, b, m = _open(tmp_path)
    r = runner.run_seat(m, run, b, name="codex", attempt=1, identity=IDENT, launch=_fake())

    assert r.artifacts.paths == (), "the premise: the launch left the tree as setup handed it"
    assert r.status.forge != "failed"
    assert r.status.forge == "partial"
    assert (r.status.setup, r.status.verify) == ("pass", "not-run"), \
        "§6 verifies elsewhere, so a seat's own verify dimension is never anything else"
    assert r.candidate.tracked_patch == b"" and r.candidate.sidecars == ()
    assert r.status.artifacts == "unusable", "nothing was produced, and that is recorded"
    assert ANSWER in r.launch_result["result_text"], \
        "the rationale survives on the result, which is what a later verify promotes from"


def test_the_seats_status_is_written_where_a_resume_will_read_it(tmp_path):
    """§14.2 lists per-seat atomic files among `--collect`'s inputs. A status held only in
    memory is one a crash erases.

    The assertions are all on facts that DO NOT EXIST until the harvest has run — the path
    set, the patch size, the forge verdict — so a record written before the inventories
    cannot satisfy them. That is the ordering, pinned by content rather than by a spy.
    """
    repo, run, b, m = _open(tmp_path)
    r = runner.run_seat(m, run, b, name="agy", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "seed.txt", "the agent's edit\n")))

    assert storage.seat_state_path(run, "agy").is_file()
    row = runstate.read_seat(run, "agy")
    assert row["status"] == {"process": "valid", "artifacts": "usable", "proven_read": True,
                             "forge": "completed", "setup": "pass", "verify": "not-run"}
    assert row["artifacts"]["paths"] == ["seed.txt"]
    assert row["candidate"]["tracked_patch_bytes"] == len(r.candidate.tracked_patch) > 0
    assert row["attempt"] == 1 and row["path"] == str(r.path)
    assert row["setup_run"]["exit_code"] == 0
    assert row["launch"]["status"] == "ok"


def test_the_setup_inventory_is_taken_between_setup_and_the_launch(tmp_path):
    """§7.1: the artifact path set is `Fsetup -> Fwork`, so setup's own output is
    DIFFERENCED out rather than handed over as the agent's work.

    That difference only exists if Fsetup is a third inventory. Reuse F0 for it — the
    obvious collapse, one line — and `deps.txt` joins the path set, the candidate and the
    ledger as something the agent never touched.
    """
    setup = (verify.Step(argv=("touch", "deps.txt")),)
    repo, run, b, m = _open(tmp_path, setup=setup)
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "done.py", "done\n")))

    assert (r.path / "deps.txt").is_file(), "the premise: setup really did produce a file"
    assert r.artifacts.paths == ("done.py",)
    assert "deps.txt" not in r.candidate.tracked_patch.decode()
    assert [e.path for e in r.candidate.sidecars] == ["done.py"]


def test_a_setup_failure_is_failed_and_never_spends_a_provider_call(tmp_path):
    """§8: "a setup failure does not proceed merely because it produced files" — so the
    verdict is fixed before the agent could change it, and §5.2 counts the call it would
    have cost. `Fwork` is still taken: with no launch it equals `Fsetup`, and taking it is
    what makes that an observation rather than an assumption written into the record."""
    setup = (verify.Step(argv=("false",)),)
    repo, run, b, m = _open(tmp_path, setup=setup)
    launch = _fake(lambda p: write(p, "never.py", "never\n"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT, launch=launch)

    assert launch.calls == [], "a seat whose verdict is already `failed` buys nothing more"
    assert r.status.forge == "failed" and r.status.setup == "fail"
    assert r.launch_result is None and r.run.exit_code != 0
    assert not (r.path / "never.py").exists()
    assert runstate.read_seat(run, "claude")["launch"] is None, \
        "and the record says the provider was never invoked, not that it returned nothing"


def test_the_proof_token_is_read_from_the_answer_and_decides_completed_or_partial(tmp_path):
    """§8.1: the sentinel is RECORDED and stripped of its power to invalidate. It feeds
    `proven_read`, which is the whole difference between a `completed` seat and a `partial`
    one — §8's first load-bearing rule, reached here through the real chain."""
    repo, run, b, m = _open(tmp_path)
    edit = lambda p: write(p, "seed.txt", "the agent's edit\n")   # noqa: E731

    proved = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                             launch=_fake(edit))
    unproved = runner.run_seat(m, run, b, name="claude", attempt=2, identity=IDENT,
                               launch=_fake(edit, quote_token=False))

    assert (proved.status.proven_read, proved.status.forge) == (True, "completed")
    assert (unproved.status.proven_read, unproved.status.forge) == (False, "partial")
    assert unproved.candidate.tracked_patch, \
        "the unproven seat still produced real work — `partial`, not discarded"


def test_a_provider_that_did_not_answer_validly_is_an_invalid_process(tmp_path):
    """§8 rule 1: an invalid process taints every other signal, so nothing it left behind is
    read. Fail closed — `valid` is believed only when it is exactly True."""
    repo, run, b, m = _open(tmp_path)
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "half.py", "half\n"), valid=False))
    assert (r.status.process, r.status.forge) == ("invalid", "failed")
    assert r.artifacts.paths == ("half.py",), \
        "and the work is still harvested — the verdict is about trust, not about deletion"


def test_a_baseline_the_manifest_did_not_record_is_refused_before_anything_is_cloned(tmp_path):
    """SEAM: `runstate` and `baseline`. The harvest diffs against the ARGUMENT and the
    verifier is built from the MANIFEST, so a disagreement between them hands over a
    candidate reconstructed from a tree the run never agreed to — and nothing downstream can
    see it, because both halves are internally consistent."""
    repo, run, b, m = _open(tmp_path)
    other = runstate.Manifest(**{**m.__dict__, "baseline_commit": "0" * 40})
    with pytest.raises(runner.RunnerError):
        runner.run_seat(other, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake())
    assert not runner.seat_dir(run, "claude", 1).exists(), \
        "a refused seat left no clone behind"


def test_an_attempt_that_is_not_a_whole_count_is_refused(tmp_path):
    """`isinstance(True, int)`, and the attempt becomes a directory name: `attempt-True` is
    a seat nothing accounts for. `runstate.count` is the predicate, called rather than
    re-spelled."""
    repo, run, b, m = _open(tmp_path)
    for bad in (True, 0, "1", 1.0):
        with pytest.raises(runner.RunnerError):
            runner.run_seat(m, run, b, name="claude", attempt=bad, identity=IDENT,
                            launch=_fake())


def test_a_seat_name_that_is_not_a_filename_is_refused_before_the_clone(tmp_path):
    """The name reaches `storage.seat_state_path`, which puts the run's record inside the
    run directory. Refused before the clone is paid for, not after."""
    repo, run, b, m = _open(tmp_path)
    with pytest.raises(runner.RunnerError):
        runner.run_seat(m, run, b, name="../escape", attempt=1, identity=IDENT,
                        launch=_fake())
    assert not (run / "seats").exists()


def test_the_launch_is_injected_with_no_default(tmp_path):
    """Structural, not conventional: a `launch` with a default is one a test can forget to
    pass, and the thing it would fall back to spends real provider calls. §5.2 is why the
    suite is not allowed to."""
    sig = pyinspect.signature(runner.run_seat)
    p = sig.parameters["launch"]
    assert p.default is pyinspect.Parameter.empty
    assert p.kind is pyinspect.Parameter.KEYWORD_ONLY


def test_the_sentinel_a_seat_was_ordered_to_quote_is_not_counted_as_its_rationale(tmp_path):
    """§8: an unargued `no_change` is indistinguishable from a seat that did nothing, and the
    rationale is the only evidence that separates them.

    MEASURED, and the rule is defeated outright without `_rationale`: `make_sentinel` returns
    `SENTINEL-` plus twelve hex characters — 21, against `seat._MIN_RATIONALE_CHARS` of 10 —
    and the proof-of-reading instruction ORDERS the seat to print it. So "ok" plus an obeyed
    instruction is a 24-character "substantive rationale", and every zero-diff seat in every
    real run clears §8's bar on text the engine supplied. The token is stripped before the
    rationale is measured, and only then; `proven_read` still reads the answer whole.

    Both directions, because a refusal that fires on everything is not the rule: the same
    seat with a real argument is admitted, and the ONLY difference between the two calls is
    what the agent itself said.
    """
    repo, run, b, m = _open(tmp_path)
    with pytest.raises(runner.RunnerError):
        runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(answer="ok"))
    assert runner.seat_dir(run, "claude", 1).is_dir(), \
        "nothing here deletes a seat, including on the way out of a refusal"
    assert runstate.read_seat(run, "claude") is None, \
        "and no status was recorded for a seat that could not be classified"

    argued = runner.run_seat(m, run, b, name="claude", attempt=2, identity=IDENT,
                             launch=_fake(answer=ANSWER))
    assert (argued.status.forge, argued.status.proven_read) == ("partial", True)


def test_the_launch_gets_the_seat_and_a_scrubbed_environment(tmp_path):
    """What the injected adapter is handed is the whole of the contract a real one has to
    satisfy. The environment is `fleet.forge_child_env`'s: the recursion guard is set, so a
    seat that reaches for /llm-forge cannot spawn three more write-enabled seats."""
    repo, run, b, m = _open(tmp_path)
    launch = _fake()
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT, launch=launch)
    (call,) = launch.calls
    assert call["seat_path"] == r.path == r.seat.path
    assert call["name"] == "claude"
    assert call["env"]["LLM_FORGE_DEPTH"] == "1"
    assert call["token"] and seatmod.read_proof(r.launch_result["result_text"], call["token"])
