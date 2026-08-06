"""One seat, end to end: the chain from a clone to a candidate, what it records, and the
verdict taken afterwards in a tree the seat never had.

NO TEST HERE INVOKES A REAL PROVIDER. `launch` is injected and every test passes `_fake`,
which writes into the seat and hands back a `council.engine.run_provider`-shaped record — so
the clone, the setup, all three inventories, the artifact set and the bundle are real, and
only the provider is not. §5.2 prices a real fleet in provider calls; a suite that spends
them is one nobody runs. `test_the_launch_is_injected_with_no_default` is what keeps that
structural rather than conventional.

The `verify_candidate` cases go one further: the seat, the harvest, the verifier clone, the
setup, the gate and the calibration are all real, and every gate here is a `sh` script this
file wrote, so the whole of §6 runs and nothing is stubbed except the provider.
"""
import inspect as pyinspect
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import pytest  # noqa: E402
from council import engine  # noqa: E402
from forge import (baseline, bundle, fleet, gate, harvest, inspect as finspect,  # noqa: E402
                   journal, runner, runstate, seat as seatmod, storage, taskbundle, verify)
from forge_fixtures import commit_all, make_repo, write  # noqa: E402

IDENT = ("Forge Seat", "seat@forge.invalid")

# Long enough to clear `seat._MIN_RATIONALE_CHARS`, because a seat that changed nothing is
# classified through §8's rationale rule and a two-word sign-off is refused there by design.
ANSWER = "the retry already backs off; adding one would double-sleep"


def _fake(fn=None, *, answer=ANSWER, valid=True, quote_token=True, quote=None):
    """A launch callable: run `fn(seat_path)`, then hand back a provider-shaped record.

    The record's keys are `run_provider`'s own — `valid` is what §8's `process` dimension
    reads and `result_text` is what `seat.read_proof` and the `no_change` rationale read.
    `quote_token` decides whether the answer cites the sentinel, which is the only input to
    `proven_read`.

    `answer` may be a callable of the token, for the cases whose subject is text the ENGINE
    supplied rather than text the seat wrote. `quote` transforms the token before the answer
    cites it — `str.lower` is a differently-cased echo, which `read_proof` accepts as proof
    and which the rationale therefore has to strip on the same terms.
    """
    calls = []

    def launch(*, name, seat_path, token, env):
        calls.append({"name": name, "seat_path": Path(seat_path), "token": token, "env": env})
        if fn is not None:
            fn(Path(seat_path))
        text = answer(token) if callable(answer) else answer
        if quote_token:
            text = f"{text}\n{(quote or str)(token)}"
        return {"name": name, "status": "ok" if valid else "failed", "valid": valid,
                "reason": "ok" if valid else "empty", "exit_code": 0 if valid else 1,
                "duration_sec": 1.5, "structured": False, "attempts": 1,
                "result_text": text}

    launch.calls = calls
    return launch


def _manifest(repo, b, setup, gate, seats, attempts):
    refs, digest = runstate.snapshot_refs(repo, (), forge_refs={b.ref: b.commit})
    return runstate.Manifest(
        run_id="r1", repo_path=str(repo), base_commit=b.base_commit,
        baseline_ref=b.ref, baseline_commit=b.commit, tracked_tree_oid=b.tracked_tree_oid,
        selected_paths=(), generator_contract=finspect.GeneratorContract(),
        setup=setup, verify=gate,
        protected_refs=refs, forge_refs={b.ref: b.commit}, status_digest=digest,
        index_digest=runstate.snapshot_index(repo), created_at="2026-08-03T00:00:00Z",
        seats=seats, attempts=attempts, review_rounds=2, synthesis_fix_cap=3)


def _open(tmp_path, *, setup=(verify.Step(argv=("true",)),),
          gate=(verify.Step(argv=("true",)),), name="repo", seed=None,
          seats=3, attempts=3):
    """A repository, a run directory, B, and the manifest that agreed to them.

    `seed` runs against the repository before B is taken, so a fixture that needs a gate
    script in the BASELINE — the only tree §6 lets a gate come from — writes and commits it
    there. `name` keeps two whole runs apart inside one `tmp_path`, which is what a case
    that differs only in what its calibration measured needs.

    `seats` and `attempts` are the run's SHAPE, which Plan G's fix wave put on the manifest
    and `runner.run` reads rather than defaulting: a case about the fleet is a case about
    those two numbers, so they are arguments here and not constants.
    """
    repo = make_repo(tmp_path, name)
    if seed is not None:
        seed(repo)
    run = tmp_path / f"run-{name}"
    run.mkdir(exist_ok=True)
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    m = _manifest(repo, b, setup, gate, seats, attempts)
    runstate.write_manifest(run, m)
    return repo, run, b, m


GATE = (verify.Step(argv=("./gate.sh",)),)


def _gate(body):
    """A `seed` for `_open`: commit `gate.sh` running `body` into the baseline.

    Executable and TRACKED, so it is in B1 and in the gate surface `_command_paths` reads
    off the confirmed argv — a gate the candidate did not touch, which is what leaves
    `gate_delta` empty and lets an outcome other than `GATE_CHANGED` be reached at all.
    """
    def seed(repo):
        script = write(repo, "gate.sh", f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
        commit_all(repo, "gate")
    return seed


def _calibrate(tmp_path, repo, b, m, *, dest="calib"):
    """§5 step 3's own run, in its own clone off the same B1 — never a hand-built `Run`.

    What makes `BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE` an honest outcome is the TREE the
    baseline run happened in, and a `Run` a test constructed would pass every type check
    while establishing nothing.

    `env` is passed for the reason `calibrate` states: the calibration and the candidates it
    will be compared against have to run in ONE environment, or `classify` is differencing
    two machines. It is the caller that holds both, and here that caller is this helper.
    """
    return verify.calibrate(repo, b, tmp_path / dest, identity=IDENT,
                            contract=m.generator_contract,
                            setup=verify.Command(steps=m.setup),
                            command=verify.Command(steps=m.verify),
                            env=fleet.forge_child_env(repo))


def _attempt(run, name, attempt):
    """The record of ONE attempt, read back out of the seat's own file.

    Spelled once here because every assertion about what reached disk goes through it, and
    the shape — a seat file holding every attempt it has made — is the thing §8.1's
    "preserved as partial input" turns on. A helper that quietly returned the LAST attempt
    would make a test for attempt 1 pass on attempt 2's record.
    """
    row = runstate.read_seat(run, name)
    assert row is not None, f"seat {name!r} recorded nothing at all"
    assert row["name"] == name
    (entry,) = [a for a in row["attempts"] if a["attempt"] == attempt]
    return entry


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
    assert (r.status.builder_setup, r.status.verify) == ("pass", "not-run"), \
        "§6 verifies elsewhere, so a seat's own verify dimension is never anything else"
    assert r.candidate.tracked_patch == b"" and r.candidate.sidecars == ()
    assert r.status.artifacts == "unusable", "nothing was produced, and that is recorded"
    assert ANSWER in r.launch_result["result_text"], \
        "the rationale survives on the result, which is what a later verify promotes from"


def test_the_seats_own_argument_survives_on_disk_and_not_only_in_memory(tmp_path):
    """§8: a correct conclusion that the task needs no edit must not be discarded — and a
    `SeatResult` nobody wrote down is discarded by the next process to look.

    The seat above is `partial` rather than `no_change` PRECISELY because its argument is
    kept for a later verification to promote from. The record is what fusion and a
    `--collect` reconstruction read, so an argument that reaches the dataclass and not the
    file has been discarded on disk instead of in memory — the same rule failing one layer
    down. Reproduced before the fix: a zero-diff seat's record carried no trace of it.

    The sentinel is asserted beside it because the two are only meaningful together: the
    rationale §8 measures is the answer MINUS the token, so a reader holding the answer and
    not the token cannot recompute what was measured.
    """
    repo, run, b, m = _open(tmp_path)
    r = runner.run_seat(m, run, b, name="codex", attempt=1, identity=IDENT, launch=_fake())

    row = _attempt(run, "codex", 1)
    assert ANSWER in row["launch"]["result_text"], \
        "the seat argued that nothing needed changing, and the record has to carry it"
    assert row["sentinel"] and row["sentinel"] in row["launch"]["result_text"]
    assert row["launch"]["result_text_chars"] == len(r.launch_result["result_text"]), \
        "and the record says how long the answer really was, not only what it kept"


def test_a_setup_failures_own_output_is_recorded_and_not_only_its_exit_code(tmp_path):
    """The same question as the test above, asked of the other field a later phase needs.

    `setup == "fail"` is one of the two ways §8 lands a seat on `failed`, and the record used
    to say so with a bare integer: an operator reading it learned that step 0 exited 3 and
    nothing about why. The `Run` holds the output, the clone does not — a command's stderr is
    not a file it left behind — so a record without it sends the reader to a tree that never
    had the answer.
    """
    setup = (verify.Step(argv=("sh", "-c", "echo why >&2; exit 3")),)
    repo, run, b, m = _open(tmp_path, setup=setup)
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT, launch=_fake())

    assert r.status.forge == "failed" and r.status.builder_setup == "fail"
    row = _attempt(run, "claude", 1)
    assert row["setup_run"]["exit_code"] == 3
    assert "why" in row["setup_run"]["stderr"]
    assert row["setup_run"]["stderr_chars"] == len(r.run.stderr)


def test_a_second_attempts_record_does_not_erase_the_first_attempts(tmp_path):
    """§8.1: *"The failed attempt is preserved as partial input."* The CLONE survived a retry
    already; its RECORD did not.

    Reproduced before the fix: the seat file was keyed by name alone, so attempt 2 replaced
    attempt 1 and attempt 1's path set and patch size were gone. A clone nobody can interpret
    without those is not the partial input §8.1 asked for — F0 and Fsetup are not persisted,
    so the `Fsetup -> Fwork` difference cannot be recomputed from the surviving tree either.

    Read back AFTER attempt 2 has been written, which is the only order that can fail.
    """
    repo, run, b, m = _open(tmp_path)
    first = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                            launch=_fake(lambda p: write(p, "half.py", "half\n")))
    second = runner.run_seat(m, run, b, name="claude", attempt=2, identity=IDENT,
                             launch=_fake(lambda p: write(p, "done.py", "done\n")))

    one, two = _attempt(run, "claude", 1), _attempt(run, "claude", 2)
    assert one["artifacts"]["paths"] == ["half.py"]
    assert two["artifacts"]["paths"] == ["done.py"]
    assert one["candidate"]["sidecars"] == ["half.py"] != two["candidate"]["sidecars"]
    assert one["path"] == str(first.path) != two["path"] == str(second.path)
    assert [a["attempt"] for a in runstate.read_seat(run, "claude")["attempts"]] == [1, 2], \
        "in attempt order, so a reader gets the run's history rather than a set"


def test_an_attempt_already_in_the_record_is_refused_even_with_no_clone_beside_it(tmp_path):
    """The clone refusal's own rule, one layer over — which is where this project keeps
    finding the second half of a fix missing.

    `run_seat` refuses attempt N when N's directory exists. That check answers the tree and
    says nothing about the record, so an attempt whose clone was moved away out of band would
    walk straight past it and overwrite its own recorded predecessor. Refused BEFORE the
    clone, like every other argument this function will not act on.
    """
    repo, run, b, m = _open(tmp_path)
    runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                    launch=_fake(lambda p: write(p, "half.py", "half\n")))
    runner.seat_dir(run, "claude", 1).rename(tmp_path / "moved-away")

    with pytest.raises(runner.RunnerError, match="already recorded"):
        runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT, launch=_fake())
    assert _attempt(run, "claude", 1)["artifacts"]["paths"] == ["half.py"], \
        "and the refused call left attempt 1's record exactly as it found it"


def test_a_confirmation_that_named_no_setup_never_records_a_passing_setup(tmp_path):
    """Fail closed: no steps ran, so nothing passed.

    `gate.Confirmation` admits an empty setup — it refuses only an empty VERIFY — so this is
    an ordinary repository that needs no toolchain, not a misconfiguration. `run_command`
    declines to report exit 0 for a gate that ran nothing and this is the other side of that
    refusal, which the adjacent comment claims in as many words ("Never a fabricated pass").

    UNPINNED UNTIL NOW, and that is the finding: no fixture used an empty `manifest.setup`,
    so the mutant that writes `"pass"` here SURVIVED beneath the comment denying it.

    `"none"` AND NOT `"not-run"`, and the difference is the whole of C1: `"not-run"` is §8's
    value for a confirmed command whose measurement was WITHHELD, and reading a run that
    never had one as though it had withheld something is what made the zero-diff intersection
    a crash. Neither is a pass, which is what this case exists to hold: `_PHASE` is checked
    for both spellings so a fabricated `"pass"` still dies here.
    """
    repo, run, b, m = _open(tmp_path, setup=())
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the agent's edit\n")))

    assert r.run is None, "the premise: no setup command was run in this seat"
    assert (r.status.process, r.status.artifacts, r.status.proven_read) \
        == ("valid", "usable", True), "every other dimension is at its strongest reading"
    assert r.status.builder_setup == "none"
    assert _attempt(run, "claude", 1)["status"]["builder_setup"] == "none"
    assert r.status.builder_setup not in ("pass", "not-run"), \
        "never a fabricated pass, and never a withheld measurement either"


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
    row = _attempt(run, "agy", 1)
    assert row["status"] == {"process": "valid", "artifacts": "usable", "proven_read": True,
                             "forge": "completed", "builder_setup": "pass", "verify": "not-run"}
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
    assert r.status.forge == "failed" and r.status.builder_setup == "fail"
    assert r.launch_result is None and r.run.exit_code != 0
    assert not (r.path / "never.py").exists()
    assert _attempt(run, "claude", 1)["launch"] is None, \
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

    argued = runner.run_seat(m, run, b, name="claude", attempt=2, identity=IDENT,
                             launch=_fake(answer=ANSWER))
    assert (argued.status.forge, argued.status.proven_read) == ("partial", True)


def test_a_differently_cased_echo_of_the_sentinel_is_not_the_seats_rationale_either(tmp_path):
    """One spelling cannot be PROOF and ARGUMENT at once, and only case-folding stops it.

    `read_proof` folds case deliberately — "a model that reflows or backticks the token still
    counts as having read" — so a lowercased echo IS proof. If the rationale then failed to
    strip that same spelling, the seat would be credited twice for one obeyed instruction:
    once as proof of reading, once as 21 characters of argument. Measured as the gap it is —
    an `IGNORECASE` mutant SURVIVED here, because every fixture quoted the token verbatim.

    `proven_read` is asserted on the way past, so the case really did fold on the other side.
    """
    repo, run, b, m = _open(tmp_path)
    with pytest.raises(runner.RunnerError, match="cannot be classified"):
        runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(answer="ok", quote=str.lower))

    row = _attempt(run, "claude", 1)
    assert row["sentinel"].lower() in row["launch"]["result_text"]
    assert row["sentinel"] not in row["launch"]["result_text"], \
        "the premise: the echo is not the token's own spelling"
    assert seatmod.read_proof(row["launch"]["result_text"], row["sentinel"]), \
        "and it still counts as proof, which is exactly why it cannot also count as argument"


def test_the_engines_own_proof_instruction_is_not_the_seats_rationale(tmp_path):
    """The token was never the only engine-authored text in the answer.

    `apply_sentinel` puts `SENTINEL_NOTE` — 280-odd characters — in front of the task, and a
    seat that quotes the instruction back clears a 10-character floor on nothing it wrote.
    That is the sentinel finding again at the paragraph the sentinel came wrapped in: text
    forge itself supplied is not the seat's argument, whichever half of the wrapper it is.

    WHAT THIS DOES NOT CLAIM: it strips the note's EXACT text as `apply_sentinel` writes it.
    A reflowed paraphrase survives, and no 10-character floor is a plagiarism detector. It
    closes the one echo the engine's own instruction makes free.
    """
    repo, run, b, m = _open(tmp_path)
    note = lambda t: engine.SENTINEL_NOTE.format(token=t)   # noqa: E731
    assert len(note("SENTINEL-0123456789ab")) > seatmod._MIN_RATIONALE_CHARS, \
        "the premise: the note alone clears the substantive floor"

    with pytest.raises(runner.RunnerError, match="cannot be classified"):
        runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(answer=note, quote_token=False))
    assert _attempt(run, "claude", 1)["status"] is None

    argued = runner.run_seat(m, run, b, name="claude", attempt=2, identity=IDENT,
                             launch=_fake(answer=lambda t: f"{note(t)}\n{ANSWER}",
                                          quote_token=False))
    assert argued.status.forge == "partial", \
        "and a seat that echoed the note AND argued its case is still admitted on the second"


def test_the_directory_helpers_refuse_a_name_that_would_leave_the_run_directory(tmp_path):
    """Public helpers, so `run_seat` having already validated is not the question.

    Both build a path from an unvalidated `name`, and `seat_dir` validated its ATTEMPT while
    letting the component that can contain a separator through — reproduced at the helper:
    `../escape` put a seat's clone outside the run directory entirely. `run_seat` and
    `verify_candidate` are safe today because each calls `_named` first, which makes this a
    latent hole rather than a live one; a resume enumerating attempt directories is the
    second caller that closes it.
    """
    repo, run, b, m = _open(tmp_path)
    for bad in ("../escape", "seats/x", "CLAUDE", ""):
        with pytest.raises(runner.RunnerError):
            runner.seat_dir(run, bad, 1)
        with pytest.raises(runner.RunnerError):
            runner.verifier_dir(run, bad)
    assert runner.seat_dir(run, "claude", 1) == run / "seats" / "claude" / "attempt-1"
    assert runner.verifier_dir(run, "claude") == run / "verifiers" / "claude"


def test_a_seat_that_cannot_be_classified_records_what_it_was_refused_on(tmp_path):
    """A refusal is exactly when someone needs the evidence, and this one used to write none.

    `RunnerError`'s own docstring says it is raised "when there is nothing truthful to
    return", and the message sends the reader to the clone because "the evidence is still
    there". For an unclassifiable seat that was false in the half that matters: the seat is
    refused over what it SAID, and what it said is the provider's answer, which is nowhere in
    the tree. The reader was told to go look at something that had never been written — the
    same defect as an unpersisted rationale, wearing a refusal's hat.

    `status` is None rather than a fabricated verdict: no classification was reached, and a
    record that invented one would be the fabricated pass this module refuses everywhere
    else. Everything the refusal was taken ON is there beside it.
    """
    repo, run, b, m = _open(tmp_path)
    with pytest.raises(runner.RunnerError, match="cannot be classified"):
        runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(answer="ok"))

    row = _attempt(run, "claude", 1)
    assert row["status"] is None, "no verdict was reached, and none is invented"
    assert row["launch"]["result_text"].startswith("ok"), "the answer it was refused over"
    assert row["sentinel"] in row["launch"]["result_text"], \
        "and the token, without which nobody can recompute the rationale that was measured"
    assert row["path"] == str(runner.seat_dir(run, "claude", 1))


def test_a_refused_seat_does_not_erase_the_attempt_before_it(tmp_path):
    """The two fixes meeting: a record written on the refusal path must still be an APPEND.

    Writing the refusal as the seat's whole record would close T4 by reopening T2 on the one
    path where the evidence is least replaceable.
    """
    repo, run, b, m = _open(tmp_path)
    runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                    launch=_fake(lambda p: write(p, "half.py", "half\n")))
    with pytest.raises(runner.RunnerError):
        runner.run_seat(m, run, b, name="claude", attempt=2, identity=IDENT,
                        launch=_fake(answer="ok"))

    assert _attempt(run, "claude", 1)["artifacts"]["paths"] == ["half.py"]
    assert _attempt(run, "claude", 2)["status"] is None


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


# --------------------------------------------------------------------------- #
# §6: verification runs where the builder never was.
# --------------------------------------------------------------------------- #


def _spy(monkeypatch, calls, setup_steps):
    """Record §6's steps as the REAL chain takes them, by wrapping three functions rather
    than replacing them.

    `run_command` and not `run_setup`, because the ordering §6 states is about the setup
    COMMAND: `run_setup` hash-validates as its first statement, so a spy on the wrapper
    would report the validation after the step it precedes. Setup and gate are told apart by
    the steps they were handed, which is the manifest's own distinction.
    """
    real_materialize, real_validate = bundle.materialize, verify.validate_materialized
    real_run_command = verify.run_command

    def materialize(bnd, dest):
        calls.append("materialize")
        return real_materialize(bnd, dest)

    def validate(v):
        calls.append("hash-validate")
        return real_validate(v)

    def run_command(cwd, command, *, env=None):
        calls.append("setup" if command.steps == setup_steps else "gate")
        return real_run_command(cwd, command, env=env)

    monkeypatch.setattr(bundle, "materialize", materialize)
    monkeypatch.setattr(verify, "validate_materialized", validate)
    monkeypatch.setattr(verify, "run_command", run_command)


def test_the_five_steps_run_in_the_order_section_6_lists(tmp_path, monkeypatch):
    """The order is the argument, not an implementation detail: hash-validating after setup
    validates a tree setup has already moved, and §6 says so.

    Step 1 — "harvest the seat, BEFORE verification" — is the one line here that is not in
    the recorded sequence, because it is structural: the candidate handed over is
    `run_seat`'s, and a caller that has not harvested has nothing to pass.

    The spies are installed AFTER the seat and the calibration have run, so the four entries
    are §6's steps and not the run's whole history of git and gate calls.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the agent's edit\n")))
    cal = _calibrate(tmp_path, repo, b, m)

    calls = []
    _spy(monkeypatch, calls, m.setup)
    outcome, _reason, v, _s = runner.verify_candidate(
        m, run, b, r.candidate, name="claude", identity=IDENT, calibration=cal)

    assert calls == ["materialize", "hash-validate", "setup", "gate"]
    assert outcome == verify.PASS, "the premise: this chain really did reach a verdict"
    # And the tree it ran in is not the tree the builder wrote, which is the whole of §6.
    assert v.path != r.path and v.path == runner.verifier_dir(run, "claude")
    assert (v.path / "work.py").read_text() == "the agent's edit\n"


def test_a_candidate_that_fails_hash_validation_never_reaches_setup(tmp_path, monkeypatch):
    """Step 2 before step 3, enforced rather than documented.

    The mismatch is a MODE, not bytes: §6 applies a candidate as a patch rather than as blob
    writes precisely because a blob write drops the executable bit, so a materialization that
    lost `tool.sh`'s +x is the failure the check exists for and the one a digest alone misses.

    `SETUP-RAN` is the assertion. A spy could only say `run_setup` was not called; this says
    the confirmed setup command never ran in that tree, which is the property §6 states.
    """
    setup = (verify.Step(argv=("touch", "SETUP-RAN")),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=_gate("exit 0"))

    def executable(p):
        write(p, "tool.sh", "#!/bin/sh\nexit 0\n").chmod(0o755)

    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(executable))
    assert [(e.path, oct(e.mode)) for e in r.candidate.sidecars] == [("tool.sh", "0o755")], \
        "the premise: the bundle records a mode a materialization can lose"
    cal = _calibrate(tmp_path, repo, b, m)

    real = bundle.materialize

    def drops_the_bit(bnd, dest):
        out = real(bnd, dest)
        (Path(dest) / "tool.sh").chmod(0o644)
        return out

    monkeypatch.setattr(bundle, "materialize", drops_the_bit)
    with pytest.raises(verify.VerifyError):
        runner.verify_candidate(m, run, b, r.candidate, name="claude", identity=IDENT,
                                calibration=cal)

    v = runner.verifier_dir(run, "claude")
    assert v.is_dir() and not (v / "SETUP-RAN").exists(), \
        "setup ran in a tree nobody had validated against the bundle"


def test_a_run_that_confirmed_no_setup_command_is_still_hash_validated(tmp_path, monkeypatch):
    """§6 hash-validates before setup, and `run_setup` is not the only path to that.

    An empty setup is an ordinary repository that needs none — `gate.Confirmation` says so in
    as many words, and refuses only an empty VERIFY — so `run_setup` is never called for one,
    and with it the only check that the verifier holds the candidate the bundle describes.
    Without the branch this pins, the one ordering §6 states outright is skipped entirely for
    exactly the repositories that need no toolchain.

    The calibration here is hand-built and never read: this call raises at step 2, long
    before `classify`, so a real one would be a clone paid for to be ignored. It USED to have
    to be hand-built — `verify.calibrate` ran the confirmed setup unconditionally and
    `run_command` refuses a command with no steps, so §5 step 3 could not be taken at all for
    a run whose confirmation named no setup. That defect is closed; a real calibration for
    this fixture is `test_a_run_that_confirmed_no_setup_returns_no_verifier_setup_run`.
    """
    repo, run, b, m = _open(tmp_path, setup=(), gate=GATE, seed=_gate("exit 0"))

    def executable(p):
        write(p, "tool.sh", "#!/bin/sh\nexit 0\n").chmod(0o755)

    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(executable))
    real = bundle.materialize

    def drops_the_bit(bnd, dest):
        out = real(bnd, dest)
        (Path(dest) / "tool.sh").chmod(0o644)
        return out

    monkeypatch.setattr(bundle, "materialize", drops_the_bit)
    unread = verify.Calibration(
        run=verify.Run(exit_code=0, stdout="", stderr="", duration_sec=0.0, step_index=0),
        path=run, setup=None, second_pass=None)
    with pytest.raises(verify.VerifyError):
        runner.verify_candidate(m, run, b, r.candidate, name="claude", identity=IDENT,
                                calibration=unread)


def test_the_verdict_carries_the_calibration_when_one_was_taken(tmp_path):
    """§6.2's BASELINE_RED_… is a claim ABOUT a calibration; a verdict that ignores it while
    one exists is a verdict reading cleaner than its evidence.

    NOT that the calibration changes a PASS — it cannot. `_run_verdict` returns PASS on the
    candidate's own exit code before `base` is read at all, so what this pins is that the
    calibration REACHES `classify`: the two runs below differ in nothing else, the candidate
    fails its gate in both, and the outcome is `BASELINE_RED_…` where the untouched baseline
    was already red and `FAIL` where it was green. Drop the calibration and both become one
    answer, and the wrong one in one of the two directions whichever answer is picked.
    """
    was_red = _open(tmp_path, gate=GATE, name="red", seed=_gate("exit 1"))
    was_green = _open(tmp_path, gate=GATE, name="green", seed=_gate("test ! -f work.py"))

    verdicts = {}
    for label, (repo, run, b, m) in (("red", was_red), ("green", was_green)):
        r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                            launch=_fake(lambda p: write(p, "work.py", "the edit\n")))
        cal = _calibrate(tmp_path, repo, b, m, dest=f"calib-{label}")
        verdicts[label] = (cal, *runner.verify_candidate(
            m, run, b, r.candidate, name="claude", identity=IDENT, calibration=cal))

    red_cal, red_outcome, red_reason, _v, _s = verdicts["red"]
    green_cal, green_outcome, _reason, _v2, _s2 = verdicts["green"]

    assert (red_cal.run.exit_code, green_cal.run.exit_code) == (1, 0), \
        "the premise: the two calibrations really did measure different baselines"
    assert red_outcome == verify.BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE
    assert green_outcome == verify.FAIL
    assert "calibration" in red_reason, \
        "and the reason says what the outcome rests on, not only what it is"


def test_the_verdict_is_taken_on_the_bundle_the_verifier_measured_not_the_one_handed_in(
        tmp_path):
    """`build_verifier` is the only place both trees exist, so it is the only place §6.1's
    gate delta can be taken — and the filled bundle it returns is what a verdict has to be
    read off. The bundle `run_seat` produced records `gate_delta is None`, which `classify`
    reads as UNKNOWN and answers `GATE_CHANGED`: correct, and useless.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the edit\n")))
    cal = _calibrate(tmp_path, repo, b, m)
    outcome, _reason, v, _s = runner.verify_candidate(
        m, run, b, r.candidate, name="claude", identity=IDENT, calibration=cal)

    assert r.candidate.gate_delta is None and r.candidate.gate_surface is None
    assert v.candidate.gate_delta == () and "gate.sh" in v.candidate.gate_surface
    assert outcome == verify.PASS
    # The contract the gate admitted under is the manifest's own, with no second argument for
    # a caller to disagree with it through.
    assert v.contract == m.generator_contract


def test_a_gate_that_rewrote_a_tracked_file_does_not_pass_the_candidate(tmp_path):
    """§6.2's PASS is "exit 0 AND no unexplained tracked delta (§7.2)", and only the
    `FixedPoint` carries the second half — a bare `run_command` here would report PASS on the
    exit code alone and `classify` would say so in its reason rather than refuse.

    §7.2 routes exactly this through §6.1's `gate_changed`: a gate that rewrites tracked
    files no generator relation declares is partly a generator nobody declared. The gate
    below only does it when the candidate's own file is present, because a gate that did it
    unconditionally would already have been refused at §5 step 3 — `_confirm_fixed_point`
    raises `GeneratorUnstable` on the untouched baseline, before any provider spends a token.
    """
    repo, run, b, m = _open(
        tmp_path, gate=GATE,
        seed=_gate("if [ -f work.py ]; then echo more >> seed.txt; fi\nexit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the edit\n")))
    cal = _calibrate(tmp_path, repo, b, m)
    assert cal.run.exit_code == 0 and cal.unexplained == (), \
        "the premise: the same gate is quiet on the untouched baseline"

    outcome, reason, _v, _s = runner.verify_candidate(
        m, run, b, r.candidate, name="claude", identity=IDENT, calibration=cal)
    assert outcome == verify.GATE_CHANGED
    assert "seed.txt" in reason and "On the runs alone this would have been PASS" in reason, \
        "and the reason keeps the run's own answer beside what displaced it"


def test_a_verifier_setup_that_failed_is_named_beside_the_verdict(tmp_path):
    """A gate that ran in a tree the confirmed setup never finished preparing is not the
    same evidence as one that ran in a prepared tree, and `run_setup` RETURNS a failing setup
    rather than raising — so without this the run exists nowhere in what a caller is handed.

    INVERTED 2026-08-04, and the half that was right is kept. §6.2 still has no outcome for
    this and ATTRIBUTION is still open — a candidate really can break the setup that passed
    in its own clone — so nothing here says whose fault it was. What changed is that the
    prose caveat was standing beside a structured `PASS`, and this test asserted it:
    "a PASS that never says the setup failed reads cleaner than its evidence" was the whole
    mitigation, and prose does not repair a field `_verify_dim`, `classify_seat` and the
    handover all branch on. `SETUP_REFUSED` is a RUNNER verdict saying only that §6 step 4
    did not run, which is a fact rather than a judgement about a side.
    """
    setup = (verify.Step(argv=("sh", "-c", "exit 3")),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the edit\n")))
    assert r.status.builder_setup == "fail", "the premise: the same setup fails in the seat too"
    cal = _calibrate(tmp_path, repo, b, m)

    outcome, reason, _v, _s = runner.verify_candidate(
        m, run, b, r.candidate, name="claude", identity=IDENT, calibration=cal)
    assert outcome == runner.SETUP_REFUSED, \
        "the gate never ran, so there is no green to report"
    assert "setup command exited 3" in reason, \
        "and the reason still names the exit code rather than only its own refusal"


def test_the_verifiers_setup_and_gate_run_in_the_calibrations_own_environment(tmp_path):
    """`classify` differences this run against the calibration, so a candidate measured in a
    different environment is differencing two machines rather than two trees.

    `fleet.forge_child_env` is what both are given, and `LLM_FORGE_DEPTH` is the half of it
    with teeth: a gate step that reaches for /llm-forge inside a verifier must not be able to
    spawn three more write-enabled seats. §5.2's `provider_invoking_verify` refuses such a
    command at the gate; this is the latch that still holds if the operator confirmed one
    anyway.

    Both steps assert it, because the setup and the gate are handed `env` by two separate
    calls and a caller can lose either one alone.
    """
    guard = 'test "$LLM_FORGE_DEPTH" = 1'
    setup = (verify.Step(argv=("sh", "-c", guard)),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=_gate(guard))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the edit\n")))
    assert r.status.builder_setup == "pass", "the premise: the seat's own setup sees the guard too"
    cal = _calibrate(tmp_path, repo, b, m)

    outcome, reason, _v, _s = runner.verify_candidate(
        m, run, b, r.candidate, name="claude", identity=IDENT, calibration=cal)
    assert outcome == verify.PASS, "the gate ran without the recursion guard"
    assert "setup command exited" not in reason, "the setup ran without the recursion guard"


def test_a_second_candidate_never_reuses_the_first_verifier_clone(tmp_path):
    """§6 step 2 says a BRAND-NEW clone. A candidate materialized over a tree the previous
    candidate already stood in is measured against a gate that party could have moved, which
    is the one premise §6 exists to establish — and `gate.quote` prices one verifier per
    seat, so a second is a run the operator was never shown.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the edit\n")))
    cal = _calibrate(tmp_path, repo, b, m)
    runner.verify_candidate(m, run, b, r.candidate, name="claude", identity=IDENT,
                            calibration=cal)

    with pytest.raises(runner.RunnerError):
        runner.verify_candidate(m, run, b, r.candidate, name="claude", identity=IDENT,
                                calibration=cal)
    assert (runner.verifier_dir(run, "claude") / "work.py").exists(), \
        "the refused call left the first verdict's tree exactly as it found it"


def test_a_baseline_the_manifest_did_not_record_is_refused_before_the_verifier_is_cloned(
        tmp_path):
    """`run_seat`'s seam, at the other end of the chain: the candidate is materialized onto
    whatever the ARGUMENT names while the run agreed to what the MANIFEST names, and
    `bundle.materialize` cannot see it — it checks the clone against the bundle's own
    baseline commit, which agrees with itself.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the edit\n")))
    cal = _calibrate(tmp_path, repo, b, m)
    other = runstate.Manifest(**{**m.__dict__, "baseline_commit": "0" * 40})

    with pytest.raises(runner.RunnerError):
        runner.verify_candidate(other, run, b, r.candidate, name="claude", identity=IDENT,
                                calibration=cal)
    assert not runner.verifier_dir(run, "claude").exists()


def test_a_seat_name_that_is_not_a_filename_is_refused_before_the_verifier_is_cloned(tmp_path):
    """`run_seat`'s refusal at the other entry point, and not redundant with `clone_seat`'s:
    that one refuses a name that cannot be a BRANCH, which is a different set and a different
    exception class. A caller catching `RunnerError` for its own bad arguments would
    otherwise meet a `SeatError` from two frames further in.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the edit\n")))
    cal = _calibrate(tmp_path, repo, b, m)
    with pytest.raises(runner.RunnerError):
        runner.verify_candidate(m, run, b, r.candidate, name="../escape", identity=IDENT,
                                calibration=cal)
    assert not (run / "verifiers").exists()


def test_a_candidate_that_repoints_the_hooks_path_through_setup_does_not_reach_the_gate(
        tmp_path):
    """§6 step 5: *"Repository hooks and any post-seat git configuration are disabled in
    verifier clones."* Asserted in prose, and until now not at gate time.

    `build_verifier` pins `core.hooksPath` before materialization and asserts it once
    afterwards, when the candidate is the only writer so far. THE CONFIRMED SETUP COMMAND
    RUNS AFTER THAT — and the candidate may legitimately own the script setup invokes. So a
    candidate that rewrites `./setup.sh` to repoint `core.hooksPath` has the gate run under
    hooks it supplied, through a file §7 harvests as ordinary work. `run_setup`'s overlap
    check cannot see it: `.git/config` is not a tracked path.

    THIS PROVES A CAPABILITY AND ASSERTS ONE CONSEQUENCE OF IT, which is worth saying out
    loud because the gap was found that way. A candidate that can rewrite the setup
    entrypoint can do more than repoint hooks: it can write the gate's own interpreter into
    a path §6.1 enumerated BEFORE setup ran, where no tracked file moves and this assertion
    stays green. That second consequence is
    `test_forge_verify.py::test_a_gate_binary_that_setup_creates_is_inside_the_measured_surface`
    and the re-read that closes it is `verify.remeasure_gate_surface`, called from
    `verify_candidate` between setup and `assert_hooks_pinned`. Whatever else this
    capability reaches belongs beside them rather than in a third file.

    Reproduced end to end before the fix, and the run came back PASS — the founding premise
    failing, since a check the builder could rig is not a check. The second read is an
    ASSERTION and not a re-pin: re-pinning would restore the property and destroy the
    evidence it had been lost, which is the loss `build_verifier`'s own note warns about.
    """
    def seed(repo):
        write(repo, "gate.sh", "#!/bin/sh\nexit 0\n").chmod(0o755)
        write(repo, "setup.sh", "#!/bin/sh\nexit 0\n").chmod(0o755)
        commit_all(repo, "gate and setup")

    setup = (verify.Step(argv=("./setup.sh",)),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=seed)

    def rig(p):
        write(p, "setup.sh", "#!/bin/sh\ngit config --local core.hooksPath .githooks\n"
                             "exit 0\n").chmod(0o755)

    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(rig))
    assert r.status.builder_setup == "pass" and "setup.sh" in r.candidate.tracked_patch.decode(), \
        "the premise: the rig is ordinary harvested work and the seat's own setup was green"
    cal = _calibrate(tmp_path, repo, b, m)

    with pytest.raises(verify.VerifyError, match="core.hooksPath"):
        runner.verify_candidate(m, run, b, r.candidate, name="claude", identity=IDENT,
                                calibration=cal)


def test_the_verifiers_own_setup_run_is_returned_and_not_only_described(tmp_path):
    """A `Run` that exists nowhere in the returned value is evidence a caller cannot record.

    The failed setup is named in the reason, which is prose for an operator; the exit code
    and the step a later phase would write into the seat's record were discarded entirely.
    """
    setup = (verify.Step(argv=("sh", "-c", "exit 3")),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the edit\n")))
    cal = _calibrate(tmp_path, repo, b, m)

    _outcome, _reason, _v, setup_result = runner.verify_candidate(
        m, run, b, r.candidate, name="claude", identity=IDENT, calibration=cal)
    assert setup_result.run.exit_code == 3 and setup_result.overlap == ()


def test_a_run_that_confirmed_no_setup_returns_no_verifier_setup_run(tmp_path):
    """`None` is a different fact from a setup that exited 0, and the value has to say so."""
    repo, run, b, m = _open(tmp_path, setup=(), gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the edit\n")))
    cal = _calibrate(tmp_path, repo, b, m)

    outcome, reason, _v, setup_result = runner.verify_candidate(
        m, run, b, r.candidate, name="claude", identity=IDENT, calibration=cal)
    assert setup_result is None
    assert outcome == verify.PASS and "setup command exited" not in reason


# --------------------------------------------------------------------------- #
# §8's `no_change`, which needs a verification taken somewhere the builder was not.
# --------------------------------------------------------------------------- #


def test_a_zero_diff_seat_reaches_no_change_once_its_claim_has_been_verified(tmp_path):
    """§8: *"a `no_change` requires a substantive rationale AND independent verification — a
    correct conclusion that the task needs no edit must not be discarded."*

    THE WHOLE CHAIN, because until now no seat this package could produce ever reached
    `no_change` at all: `run_seat` classifies with `verify="not-run"` by construction, §8's
    rule 3 requires `verify == "pass"`, and nothing fed §6's answer back. The dimension was
    decorative and every argued zero-diff seat sat at `partial` permanently.

    The verification is real: a verifier clone off B1 that this seat never touched, the
    confirmed setup and the confirmed gate run in it. `partial` on the way past is asserted
    so the promotion is visibly the verification's doing and not the fixture's.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT, launch=_fake())
    assert (r.artifacts.paths, r.status.forge) == ((), "partial"), \
        "the premise: an argued zero-diff seat, withheld rather than discarded"

    cal = _calibrate(tmp_path, repo, b, m)
    outcome, reason, v, _s = runner.verify_candidate(
        m, run, b, r.candidate, name="claude", identity=IDENT, calibration=cal)
    assert outcome == verify.PASS and v.path != r.path

    done = runner.reclassify_seat(run, r, outcome, reason)
    assert (done.status.verify, done.status.forge) == ("pass", "no_change")
    assert done.status.builder_setup == "pass", "§8 asks for BOTH halves and neither substitutes"

    row = _attempt(run, "claude", 1)
    assert row["status"]["forge"] == "no_change", "and the promotion reached the record"
    assert row["verification"] == {"outcome": verify.PASS, "reason": reason}
    assert ANSWER in row["launch"]["result_text"], \
        "with the argument still beside it — the promotion adds evidence, it never replaces it"


def test_an_outcome_that_settles_nothing_about_the_candidate_does_not_promote_a_seat(
        tmp_path):
    """§6.2 has six outcomes and §8's `verify` has three values, and the map is not onto.

    Only `PASS` confirms and only `FAIL` refutes. `GATE_CHANGED`, `HARVEST_INCOMPLETE`,
    `FLAKY` and `BASELINE_RED_…` each say the measurement produced no verdict ABOUT THIS
    CANDIDATE — §6.2 calls the second a harvesting gap "not a candidate defect" and the
    fourth "degraded rather than an equivalence". Reading any of them as a pass would promote
    a seat on evidence nobody has; reading them as a fail would make `classify_seat` refuse a
    seat over a contradiction that was never measured. They read as `not-run`, which is the
    value §8 already has for a measurement not taken — and §6.2's own word for what happened
    is kept beside it in the record, because the two vocabularies do not translate.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 1"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT, launch=_fake())
    cal = _calibrate(tmp_path, repo, b, m)
    outcome, reason, _v, _s = runner.verify_candidate(
        m, run, b, r.candidate, name="claude", identity=IDENT, calibration=cal)
    assert outcome == verify.BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE, \
        "the premise: a real §6.2 outcome that is not a verdict about this candidate"

    done = runner.reclassify_seat(run, r, outcome, reason)
    assert (done.status.verify, done.status.forge) == ("not-run", "partial")
    assert _attempt(run, "claude", 1)["verification"]["outcome"] == outcome, \
        "§6.2's own answer is recorded, so `not-run` is never all the record says"


def test_a_verification_that_refuted_the_claim_is_a_refusal_not_a_quieter_verdict(tmp_path):
    """§8's rule 3 raises for a measurement that was TAKEN and contradicts the claim, and the
    re-classification is where that can finally happen — a `changed=False` seat whose gate
    came back FAIL is a contradiction in the caller's own measurements.

    `RunnerError`, not a bare `SeatStatusError`: this module's callers name one class for an
    argument it will not act on, and the record is left carrying the pre-verification verdict
    rather than being rewritten to something nothing decided.

    THE OUTCOME IS HANDED IN RATHER THAN MEASURED, and that is forced rather than lazy: a
    zero-diff candidate materializes to a tree byte-identical to the baseline, so its gate
    and the calibration's are the same run of the same command — `FAIL` needs them to
    disagree, and no fixture can make them. Which is the point of the refusal: the pair can
    only arrive from a caller that mixed two seats up, or from a gate that is not a function
    of its tree. Both are the contradiction §8 declines to classify, not a verdict to record.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT, launch=_fake())
    assert r.artifacts.paths == (), "the premise: a zero-diff seat, so §8's rule 3 applies"

    with pytest.raises(runner.RunnerError, match="cannot be re-classified"):
        runner.reclassify_seat(run, r, verify.FAIL, "the gate exited 1")
    assert _attempt(run, "claude", 1)["status"]["forge"] == "partial"
    assert _attempt(run, "claude", 1)["verification"] is None


def test_an_outcome_that_is_not_one_of_section_6_2s_is_refused(tmp_path):
    """Fail closed on the input side: an unrecognized outcome must not fall through to the
    `not-run` default, which would silently read a typo as "no verdict yet"."""
    repo, run, b, m = _open(tmp_path)
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT, launch=_fake())
    for bad in ("pass", "Pass", "", None):
        with pytest.raises(runner.RunnerError):
            runner.reclassify_seat(run, r, bad, "whatever")


def test_the_calibration_is_required_and_is_never_a_bare_run(tmp_path):
    """Structural, and against the obvious signature, which defaults it to `None`.

    There is no honest value to run with when it is missing: `classify` reads
    `baseline_run` only after the candidate's gate has already failed, so a fabricated green
    `Run` reports a NEW failure nothing measured and a fabricated red one reports §6.2's
    baseline-red outcome on the evidence of no calibration at all — a verdict reading cleaner
    than its evidence, in opposite directions.

    A bare `Run` is refused for what it cannot establish: §6.2's baseline-red outcome is a
    claim about the untouched baseline's own gate, and the only thing that supports it is the
    TREE the run happened in.
    """
    sig = pyinspect.signature(runner.verify_candidate)
    p = sig.parameters["calibration"]
    assert p.default is pyinspect.Parameter.empty
    assert p.kind is pyinspect.Parameter.KEYWORD_ONLY

    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p_: write(p_, "work.py", "the edit\n")))
    green = verify.Run(exit_code=0, stdout="", stderr="", duration_sec=0.0, step_index=0)
    with pytest.raises(runner.RunnerError):
        runner.verify_candidate(m, run, b, r.candidate, name="claude", identity=IDENT,
                                calibration=green)
    assert not runner.verifier_dir(run, "claude").exists(), \
        "and the refusal came before a clone was paid for"


# --------------------------------------------------------------------------- #
# The run loop: §5 step 3, then §7 for every seat, then §6 for every candidate.
# --------------------------------------------------------------------------- #


def _confirmed(run, *, policy="abort"):
    """The `confirm` pair `gate.open_run` writes, since these fixtures open a run by hand.

    BOTH HALVES, because a `_done` with no `_start` is a shape `journal.orphans` refuses
    outright — so a fixture that wrote only the record it cared about would make every
    reconstruction raise before the case under test was reached.

    The policy lives here and not in the manifest because that is where `open_run` puts it:
    §14.2's manifest holds the commands and the baseline, and §5 step 2's two policies are
    journalled beside the operation that agreed them.
    """
    log = journal.Journal(storage.journal_path(run))
    log.record(journal.intent("confirm"), operation_id="r1")
    log.record(journal.done("confirm"), operation_id="r1",
               on_calibration_failure=policy, strategy="size-gated", accepted_gaps=[])
    return log


def _edit(p):
    write(p, "work.py", "the agent's edit\n")


def _per_seat(fn):
    """A launch callable that runs `fn(name, attempt_number, seat_path)` and answers valid.

    `_fake` is one behaviour for every seat; the loop hands ONE callable to all of them, so a
    case about retries or about one seat failing needs the name and the call count that
    `_fake` closes over rather than exposes.
    """
    calls = []

    def launch(*, name, seat_path, token, env):
        n = 1 + sum(1 for c in calls if c["name"] == name)
        calls.append({"name": name, "attempt": n, "seat_path": Path(seat_path)})
        valid = fn(name, n, Path(seat_path))
        return {"name": name, "status": "ok" if valid else "failed", "valid": valid,
                "reason": "ok", "exit_code": 0, "duration_sec": 1.0, "structured": False,
                "attempts": 1, "result_text": f"{ANSWER}\n{token}"}

    launch.calls = calls
    return launch


def test_the_loop_runs_every_seat_the_gate_priced_and_verifies_each_one(tmp_path):
    """The whole of Plan H in one call: §5 step 3, §7 for three seats, §6 for three
    candidates, and §8's verdict revised with what §6 found.

    THREE, because that is what the manifest records — §5.2 priced the run by that number and
    §5 step 5 forbids asking again, so a loop reading a seat count from its own default would
    build a fleet the operator never agreed to. The names come from `council.engine`'s list
    because a count is not a fleet.

    Each seat is verified in a tree it never had, and the record says so: `verify: "pass"` on
    disk is the §6 outcome fed back through `reclassify_seat`, which is the only route by
    which a seat's verify dimension is ever anything but `not-run`.

    NOTHING CHOOSES BETWEEN THEM, and that is the plan's stated ending rather than an
    omission: the run stops at `comparing`, three verified candidates on disk.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"))
    launch = _per_seat(lambda name, n, p: bool(write(p, f"{name}.py", "work\n")))
    results = runner.run(run, repo, identity=IDENT, launch=launch)

    assert [r.name for r in results] == ["claude", "codex", "agy"], \
        "every seat the manifest priced, in the engine's own order"
    assert [c["name"] for c in launch.calls] == ["claude", "codex", "agy"], \
        "one provider call each — a retry is spent on `failed` and this fleet has none"
    assert {r.status.forge for r in results} == {"completed"}
    assert {r.verification[0] for r in results} == {verify.PASS}
    for r in results:
        assert r.seat.path == runner.seat_dir(run, r.name, 1)
        assert _attempt(run, r.name, 1)["status"]["verify"] == "pass", \
            "§6's answer reached the record, not only the returned value"
        assert (runner.verifier_dir(run, r.name) / f"{r.name}.py").is_file(), \
            "and the verdict was taken in a tree the builder never had"
    assert runstate.read_state(run).phase == "comparing", \
        "the loop ends where fusion would begin, and fusion has no implementation"


def test_a_run_that_dies_mid_seat_reconstructs_as_outcome_unknown(tmp_path):
    """§14.1: the engine cannot distinguish never-started from partly-ran from completed —
    so it records enough that a READER can, and never retries what it cannot classify.

    The death is inside `launch`, which is where a real one is: a provider that ran for forty
    minutes and a process killed before its receipt landed leave the same record. What is on
    disk afterwards is `seat_start` for codex with no `seat_done`, which is the one shape
    §14.1 names `outcome_unknown`.

    THE SECOND CALL IS THE ASSERTION. An orphan the loop retried would re-clone, re-launch and
    re-spend a provider call for an operation that may have completed — so the refusal is
    measured by the launch callable never being reached, not by the exception alone.

    Claude's `_done` is asserted on the way past so the orphan is visibly the pairing and not
    "the journal has records in it".
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=2)

    def die(name, n, p):
        if name == "codex":
            raise RuntimeError("SIGKILL stands in for itself")
        return bool(write(p, "work.py", "work\n"))

    with pytest.raises(RuntimeError, match="SIGKILL"):
        runner.run(run, repo, identity=IDENT, launch=_per_seat(die))

    recon = runstate.reconstruct(run, repo)
    (orphan,) = recon.orphans
    assert orphan.event == journal.intent("seat")
    assert orphan.operation_id == "r1/codex/attempt-1"
    assert orphan.data["pid"] and orphan.data["boot_id_source"], \
        "with the identity §14.1's liveness question needs, which nobody can re-derive later"
    events = [e.event for e in journal.Journal(storage.journal_path(run)).read()]
    assert events.count(journal.done("seat")) == 1, \
        "the premise: claude's own operation closed, so the orphan is codex's alone"

    retry = _per_seat(lambda name, n, p: True)
    with pytest.raises(runner.RunnerError, match=runstate.OUTCOME_UNKNOWN):
        runner.run(run, repo, identity=IDENT, launch=retry)
    assert retry.calls == [], "never silently retried — not one provider call was re-spent"
    assert not runner.seat_dir(run, "codex", 2).exists(), \
        "and nothing was re-cloned on the way to the refusal"


def test_a_run_refuses_to_start_when_the_users_repository_has_moved(tmp_path):
    """§9: transition to `source_diverged` and do not continue to handover automatically.

    THE TRANSITION IS THE HALF A `raise` ALONE WOULD LOSE. A loop that only refused would
    leave the run recorded at whatever phase it was in, which a later reader resumes from —
    into the very handover §9 stopped. So the phase on disk is asserted, not just the
    exception.

    The move is the user's own editor: `seed.txt` is tracked and clean at t0, so editing it
    puts it in the porcelain and moves the status digest. §9's worked example is exactly this
    — "you have since changed 4 of the files it touches" — and a clean merge over it can
    silently revert their work on any hunk forge also touched.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=1)
    write(repo, "seed.txt", "the user kept working while the run was out\n")
    launch = _per_seat(lambda name, n, p: True)

    with pytest.raises(runner.RunnerError, match="source_diverged"):
        runner.run(run, repo, identity=IDENT, launch=launch)

    assert runstate.read_state(run).phase == "source_diverged"
    assert launch.calls == [] and not (run / "seats").exists(), \
        "refused before a clone was made or a provider call spent"
    assert not (run / "calibration").exists(), \
        "§9 is asked before §5 step 3, so not even the calibration clone was paid for"


def test_every_phase_the_loop_enters_was_a_declared_transition(tmp_path, monkeypatch):
    """`advance` raises on an undeclared edge, so a loop that moved the state by assignment
    would silently hold a graph the spec does not.

    Two halves, and the second is the one with teeth. The sequence is pinned because §14's
    chain is what the plan says this loop drives; then every consecutive pair is replayed
    through `advance` itself, so the claim is that the graph admits the route rather than
    that this test agrees with the route. A loop that wrote `comparing` straight after
    `setting_up` would satisfy a spelled-out list nobody re-derived and fail here.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=1)
    seen = []
    real = runstate.write_state

    def spy(run_dir, state):
        seen.append(state.phase)
        return real(run_dir, state)

    monkeypatch.setattr(runstate, "write_state", spy)
    runner.run(run, repo, identity=IDENT, launch=_per_seat(lambda name, n, p: True))

    assert seen == ["setting_up", "building", "harvested", "comparing"]
    phase = "confirmed"
    for nxt in seen:
        runstate.advance(runstate.State(phase=phase, round=0, attempt=0,
                                        verified_checkpoint=None,
                                        deliverable_checkpoint=None), nxt)
        phase = nxt
    assert "synthesizing" in runstate.PHASES, \
        "the premise: the loop stopped one declared edge short of fusion, not at the graph's end"


def test_a_run_standing_at_a_phase_this_loop_does_not_drive_is_refused(tmp_path):
    """The other side of the walk: `reviewing` and the five terminals are phases §14 declares
    and this loop has no route through, so it refuses rather than picking the nearest edge.

    Measured on `ready`, which is terminal: `advance` would raise anyway, but the refusal has
    to come from the ROUTE rather than from the graph — a loop that asked `advance` first
    would report "no edge from ready" for a run whose real problem is that it is finished.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=1)
    for phase in ("reviewing", "ready"):
        runstate.write_state(run, runstate.State(
            phase=phase, round=1, attempt=1, verified_checkpoint=None,
            deliverable_checkpoint=None))
        launch = _per_seat(lambda name, n, p: True)
        with pytest.raises(runner.RunnerError, match="not on the route"):
            runner.run(run, repo, identity=IDENT, launch=launch)
        assert launch.calls == []


def test_a_second_pass_over_a_run_that_was_driven_is_refused_rather_than_re_spent(tmp_path):
    """Nothing in this package serializes a `Calibration` or a `CandidateBundle`, so a second
    pass cannot continue the first — it could only re-take the operations whose products it
    cannot carry forward, at §5.2's price per provider call.

    §14.1 promises DISTINGUISHABILITY, not exactly-once, and concedes the second in its own
    first sentence. `runstate.reconstruct` is what reads a run that has already been driven,
    and it still answers here — the refusal is the loop's, not the record's.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=1)
    runner.run(run, repo, identity=IDENT, launch=_per_seat(lambda name, n, p: True))

    again = _per_seat(lambda name, n, p: True)
    with pytest.raises(runner.RunnerError, match="previous pass"):
        runner.run(run, repo, identity=IDENT, launch=again)
    assert again.calls == []
    assert runstate.reconstruct(run, repo).seats["claude"]["attempts"][0]["attempt"] == 1, \
        "the first pass's record is still readable, which is what §14.1 actually promises"


def test_a_failed_seat_is_retried_in_a_fresh_clone_and_its_predecessor_survives(tmp_path):
    """§8.1: *"Every retry attempt gets a fresh clone. The failed attempt is preserved as
    partial input. Never a reset-and-rerun in place."*

    `run_seat` owns the fresh clone and the no-delete; the loop owns only the BUDGET, which
    is `manifest.attempts` — the number §5.2 priced as `builders = seats * attempts` and the
    operator agreed to. So what is measured here is that the budget is read from the record
    and spent on the one state §8 says is worth respending: an invalid process is a seat that
    produced nothing trustworthy.

    Attempt 1's work is asserted present, not merely its directory: a retention policy that
    emptied the clone would pass a bare `exists()`.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=1, attempts=2)

    def flaky(name, n, p):
        write(p, f"attempt-{n}.py", "work\n")
        return n > 1

    (result,) = runner.run(run, repo, identity=IDENT, launch=_per_seat(flaky))

    assert result.attempt == 2 and result.status.forge == "completed"
    assert _attempt(run, "claude", 1)["status"]["forge"] == "failed"
    assert (runner.seat_dir(run, "claude", 1) / "attempt-1.py").read_text() == "work\n", \
        "the failed attempt is preserved as partial input, contents and all"
    assert not (runner.seat_dir(run, "claude", 2) / "attempt-1.py").exists(), \
        "and attempt 2 did not start on attempt 1's work"


def test_a_seat_that_fails_every_attempt_is_not_verified_and_does_not_stop_the_fleet(tmp_path):
    """§8's rules 1 and 2 fix `failed` on the process and the setup, so `classify_seat`
    answers `failed` for every value of `verify` — a verifier clone for one would be a run
    §5.2 priced to change an answer that cannot move.

    The other seat is what makes this a division of labour rather than a swallowed failure:
    one seat's verdict is not the fleet's, which is the whole reason §8 has four dimensions.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=2, attempts=2)

    def invalid_but_productive(name, n, p):
        # Both seats leave real work behind. §8 rule 1 is about TRUST, not about deletion, so
        # the failing seat's files are harvested and its verdict is still `failed`.
        write(p, "work.py", f"{name} attempt {n}\n")
        return name != "claude"

    results = runner.run(run, repo, identity=IDENT, launch=_per_seat(invalid_but_productive))

    forge = {r.name: r.status.forge for r in results}
    assert forge == {"claude": "failed", "codex": "completed"}
    assert not runner.verifier_dir(run, "claude").exists(), \
        "no verifier clone was bought for a verdict that cannot move"
    assert runner.verifier_dir(run, "codex").is_dir()
    assert _attempt(run, "claude", 2)["status"]["forge"] == "failed", \
        "and the budget was spent — both attempts are on the record"


def test_a_zero_diff_fleet_reaches_no_change_through_the_loops_own_sequencing(tmp_path):
    """§8's `no_change` end to end, with nothing hand-driven.

    Every step is the loop's: it harvests a seat that argued the task needs no edit, verifies
    the claim in a clone the seat never had, and feeds §6's answer back through
    `reclassify_seat`. §8 says such a conclusion "must not be discarded", and before the
    sequencing existed it was: `partial` was the ceiling for every argued zero-diff seat
    because nothing called the three in order.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=1)
    (result,) = runner.run(run, repo, identity=IDENT,
                           launch=_per_seat(lambda name, n, p: True))

    assert result.artifacts.paths == (), "the premise: the seat changed nothing"
    assert (result.status.verify, result.status.forge) == ("pass", "no_change")
    assert _attempt(run, "claude", 1)["verification"]["outcome"] == verify.PASS


def test_a_no_toolchain_fleet_that_changed_nothing_is_verified_rather_than_lost(tmp_path):
    """THE INTERSECTION of two gate-legal inputs, each of which already had a case alone: an
    empty confirmed setup and a zero-diff seat, across MORE THAN ONE seat.

    Measured on the pair before this, the run did not degrade — it died. Seat 1's
    re-classification raised *"no_change requires independent verification (setup='not-run',
    verify='pass')"* out of `run`; seats 2 and 3 were never verified though all three
    providers had already been paid; the phase stuck at `comparing`; `orphans` came back `()`
    so `reconstruct` read CLEAN; and `_refuse_a_second_pass` then made the run unrecoverable.
    Three paid provider calls, one verdict, and a recovery path reporting nothing wrong.

    `"none"` is what closed it — §8 reads it as a setup with nothing to measure rather than a
    measurement withheld — and the ceiling this branch recorded as accepted goes with it: a
    repository needing no toolchain reaches `no_change` here, and `completed` one rule down.
    """
    repo, run, b, m = _open(tmp_path, setup=(), gate=GATE, seed=_gate("exit 0"), seats=3)
    results = runner.run(run, repo, identity=IDENT,
                         launch=_per_seat(lambda name, n, p: True))

    assert [r.name for r in results] == ["claude", "codex", "agy"]
    assert {r.artifacts.paths for r in results} == {()}, \
        "the premise's first half: no seat changed anything"
    assert {r.run for r in results} == {None}, \
        "and its second: no setup command ran in any of them"
    assert {(r.status.builder_setup, r.status.verify, r.status.forge) for r in results} \
        == {("none", "pass", "no_change")}
    for r in results:
        assert runner.verifier_dir(run, r.name).is_dir(), \
            "every seat was verified, not only the one that used to raise on its way out"
        assert _attempt(run, r.name, 1)["status"]["forge"] == "no_change"
    assert runstate.read_state(run).phase == "comparing"
    assert runstate.reconstruct(run, repo).orphans == (), \
        "and a clean-reading reconstruction is the truth now rather than the disguise"


def test_a_verification_this_loop_refuses_costs_that_seat_and_not_the_fleet(tmp_path):
    """§6 measures each candidate in its own clone against one calibration, so nothing about
    seat A's verdict is evidence about seat B's — and the loop's two halves now agree about
    that. The build half already contained a refusal per seat; this half re-raised, so ONE
    seat's failure ended the run with every provider paid and the seats behind it unverified.

    The refusal is a real one, not a stub: §6 step 2 says a BRAND-NEW clone, and a directory
    already standing where codex's verifier goes is what `verify_candidate` refuses by name.

    CONTAINED IS NOT QUIET. The refused seat keeps the verdict it held before verification —
    never a promotion — and `verification_refused` on its own record is what stops it reading
    exactly like a seat the loop had not reached yet, for a verifier clone bought and spent.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=3)
    runner.verifier_dir(run, "codex").mkdir(parents=True)

    results = runner.run(run, repo, identity=IDENT, launch=_per_seat(
        lambda name, n, p: bool(write(p, f"{name}.py", "work\n"))))

    seen = {r.name: (r.status.verify, r.status.forge) for r in results}
    assert seen["claude"] == seen["agy"] == ("pass", "completed"), \
        "the seats either side of the refusal were verified; the run used to die at the first"
    assert seen["codex"] == ("not-run", "completed"), \
        "and the refused one keeps its pre-verification verdict rather than gaining one"

    refused = _attempt(run, "codex", 1)
    assert "already has a verifier clone" in refused["verification_refused"], \
        "the seat's own record says §6 was asked and would not answer"
    assert refused["verification"] is None
    assert _attempt(run, "claude", 1)["verification_refused"] is None, \
        "and the field is not merely always set"

    done = [e for e in journal.Journal(storage.journal_path(run)).read()
            if e.event == journal.done("verification")]
    assert len(done) == 3 and sum("refused" in e.data for e in done) == 1, \
        "three operations, all closed — a contained refusal is not an orphan"
    assert runstate.reconstruct(run, repo).orphans == ()
    assert runstate.read_state(run).phase == "comparing"


def test_the_gate_measurement_and_the_verifiers_setup_reach_the_seats_record(tmp_path):
    """`verify_candidate` returns four values and its only call site read two.

    §6.1's `gate_delta`/`gate_surface` are filled by `build_verifier` onto `Verifier.candidate`
    — never onto the bundle handed in, which is `run_seat`'s pre-verification one — so a
    caller that keeps the input bundle holds `gate_delta is None`. The measurement was taken
    over the two trees only `build_verifier` has, and then dropped at the seam.

    The verifier's own `SetupResult` was the other half: `run_setup` RETURNS a failing setup
    rather than raising, so the exit code reached an operator as prose inside `reason` and no
    record at all. The setup below passes in the BUILDER (it runs before the agent writes
    `w.py`) and fails in the VERIFIER (the candidate is materialized first), which is the one
    shape that separates the two runs without failing the seat.
    """
    setup = (verify.Step(argv=("sh", "-c", "! test -f w.py")),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=_gate("exit 0"), seats=1)
    (result,) = runner.run(run, repo, identity=IDENT, launch=_per_seat(
        lambda name, n, p: bool(write(p, "w.py", "the edit\n"))))

    assert result.status.builder_setup == "pass", "the premise: the same command passed in the seat"
    assert result.verification[0] == runner.SETUP_REFUSED and "setup command exited 1" in \
        result.verification[1], "and failed in the verifier, where §6 takes the verdict"
    # THE POINT OF THIS TEST SURVIVES THE OUTCOME CHANGE, and that is why the refusal is
    # placed AFTER `on_measurement` rather than before it: the §6.1 reading and the verifier's
    # `SetupResult` were both bought, and a refusal is exactly when someone needs them. The
    # row assertions below are the evidence that neither was dropped at the seam.

    row = _attempt(run, "claude", 1)
    assert row["verifier_setup"] == {"exit_code": 1, "step_index": 0, "overlap": []}, \
        "the exit code is a fact a later phase can branch on, not a phrase inside a reason"
    assert row["candidate"]["gate_surface"] == ["gate.sh"], \
        "§6.1 ranged over the gate the candidate did not touch, and the record says which"
    assert row["candidate"]["gate_delta"] == [], \
        "empty BESIDE the surface it was measured over — `[]` with no surface cannot say "\
        "whether anything was looked at, which is why the two are written together"


def test_a_verdict_section_8_refuses_is_still_a_verdict_that_reaches_the_record(tmp_path):
    """The case before this one, on the path where §8 says no — the same measurement, the
    same requirement that it reach disk.

    THE VERIFIER WAS BOUGHT AND SPENT EITHER WAY. §6.1's gate surface is measured over the
    two trees only `build_verifier` has, the verifier's own setup ran, and §6.2 returned a
    verdict; §8 then refusing to carry that verdict into a `Status` says nothing about
    whether any of the three happened. The containment path used to write the
    PRE-verification result whole, so all four came back null for a seat whose clone was
    built and whose gate was run — I2's finding reopened one seam over.

    THE GATE IS PATH-DEPENDENT, and that is the smallest input that reaches §8's refusal
    without a stub: it exits 0 on the untouched baseline, so §5 step 3 calibrates green and
    the run carries on, and 1 in the verifier, so a seat that changed NOTHING draws §6.2's
    `FAIL`. §8 rule 3 will not classify a `changed=False` claim the gate positively refuted,
    which is the refusal — and the verdict that provoked it is exactly what a reader needs.
    """
    repo, run, b, m = _open(
        tmp_path, gate=GATE, seats=1,
        seed=_gate('case "$(pwd)" in */verifiers/*) exit 1 ;; esac'))
    (result,) = runner.run(run, repo, identity=IDENT,
                           launch=_per_seat(lambda name, n, p: True))

    assert result.artifacts.paths == (), "the premise: the seat changed nothing"
    assert result.verification is not None and result.verification[0] == verify.FAIL, \
        "§6 was asked and ANSWERED; only §8 refused"
    assert "cannot be re-classified under verify='fail'" in result.verification_refused
    assert (result.status.verify, result.status.forge) == ("not-run", "partial"), \
        "the pre-verification verdict is kept, never promoted and never worsened"

    row = _attempt(run, "claude", 1)
    assert row["verification"]["outcome"] == verify.FAIL, \
        "the verdict is on disk, which is where a later phase and `--collect` read it"
    assert row["status"]["verify"] == "not-run", \
        "beside the status §8 would not revise — the two disagree, and truthfully"
    assert row["candidate"]["gate_surface"] == ["gate.sh"] and \
        row["candidate"]["gate_delta"] == [], "§6.1's measurement survived the refusal"
    assert row["verifier_setup"] == {"exit_code": 0, "step_index": 0, "overlap": []}, \
        "and so did the verifier's own setup run, which exists nowhere else"

    done = [e for e in journal.Journal(storage.journal_path(run)).read()
            if e.event == journal.done("verification")]
    assert [(e.data.get("outcome"), "refused" in e.data) for e in done] \
        == [(verify.FAIL, True)], \
        "and the run's own log says §6 answered rather than reporting a bare refusal"
    assert runstate.reconstruct(run, repo).orphans == (), \
        "a contained refusal closes its operation; it does not orphan it"


def test_a_seat_verified_before_the_gate_was_measured_records_neither_half(tmp_path):
    """`gate_delta` and `gate_surface` are `None` together or written together, on
    `bundle.with_gate_measurement`'s own rule: `[]` alone cannot say whether the gate was
    measured and nothing moved, or whether nobody looked. A caller that took no §6.1
    measurement therefore records the third state rather than the flattering one.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the edit\n")))
    cal = _calibrate(tmp_path, repo, b, m)
    outcome, reason, v, s = runner.verify_candidate(
        m, run, b, r.candidate, name="claude", identity=IDENT, calibration=cal)

    runner.reclassify_seat(run, r, outcome, reason)
    row = _attempt(run, "claude", 1)
    assert (row["candidate"]["gate_delta"], row["candidate"]["gate_surface"]) == (None, None)
    assert row["verifier_setup"] is None, "and a setup nobody handed over is not reported"

    with pytest.raises(runner.RunnerError, match="not the one this seat harvested"):
        runner.reclassify_seat(run, r, outcome, reason,
                               candidate=replace(v.candidate, tracked_patch=b"not mine"))
    with pytest.raises(runner.RunnerError, match="verify.SetupResult is required"):
        runner.reclassify_seat(run, r, outcome, reason, verifier_setup=s.run)


def test_a_baseline_that_fails_its_own_gate_stops_a_run_whose_operator_said_abort(tmp_path):
    """§5 step 2's first policy, obeyed rather than recorded and ignored.

    IT IS IN THE JOURNAL AND NOT THE MANIFEST — `gate.open_run` puts it there — so a loop that
    only read the manifest would carry on past a red baseline in every run, silently making
    the operator's decision for them in the direction that spends money.

    BEFORE ANY PROVIDER CALL, which is why §5 step 3 calibrates where it does: the abort costs
    one clone, not three provider calls. And the ending is `failed`, not `degraded` — §14's
    `degraded` is the ending of a run that continued as far as a review, and `reviewing` is
    the only phase declaring an edge to it.

    Both directions, because a policy that fired whichever way it was answered is not a
    policy: the same red baseline under `degraded` runs the fleet and reports §6.2's own word
    for what the gate established.
    """
    aborting = _open(tmp_path, gate=GATE, seed=_gate("exit 1"), name="abort", seats=1)
    repo, run, b, m = aborting
    _confirmed(run, policy="abort")
    launch = _per_seat(lambda name, n, p: True)
    with pytest.raises(runner.RunnerError, match="abort"):
        runner.run(run, repo, identity=IDENT, launch=launch)
    assert launch.calls == [], "nothing was spent on a provider"
    assert runstate.read_state(run).phase == "failed"
    assert (run / "calibration").is_dir(), "the premise: the calibration really was taken"

    repo2, run2, b2, m2 = _open(tmp_path, gate=GATE, seed=_gate("exit 1"),
                                name="carry", seats=1)
    _confirmed(run2, policy="degraded")
    (carried,) = runner.run(run2, repo2, identity=IDENT,
                            launch=_per_seat(lambda name, n, p: True))
    assert carried.verification[0] == verify.BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE
    assert carried.status.verify == "not-run", \
        "§6.2's baseline-red outcome settles nothing about this candidate, and §8 says so"


def test_a_red_baseline_with_no_recorded_policy_is_refused_rather_than_carried(tmp_path):
    """Fail closed on the operator's own answer. §5 step 2 has no safe default —
    `gate.confirm` refuses to invent one in as many words — so a loop that could not find the
    answer must not pick the branch that keeps spending.

    The fixture opens its run by hand and journals no `confirm`, which is exactly what a
    resume against a run directory written by an older gate would find.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 1"), seats=1)
    launch = _per_seat(lambda name, n, p: True)
    with pytest.raises(runner.RunnerError, match="no confirm_done record"):
        runner.run(run, repo, identity=IDENT, launch=launch)
    assert launch.calls == []


def test_a_run_priced_for_more_seats_than_there_are_providers_is_refused(tmp_path):
    """A seat COUNT is what §5.2 prices and what `gate.confirm` records; it is not a fleet.

    Refused rather than truncated: running fewer seats than were agreed is a run the operator
    never confirmed, and giving one provider two seat names would put two records where
    §14.2 has one file per seat.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"),
                            seats=len(runner._SEATS) + 1)
    launch = _per_seat(lambda name, n, p: True)
    with pytest.raises(runner.RunnerError, match="providers to fill them"):
        runner.run(run, repo, identity=IDENT, launch=launch)
    assert launch.calls == [] and not (run / "calibration").exists()


def test_the_seat_names_are_the_engines_own_and_are_legal_records(tmp_path):
    """Structural: the loop's fleet comes from `council.engine`, because the adapter `launch`
    wraps is `engine.run_provider` and a name invented in `runner` is one no provider answers
    to. Each one also has to survive `storage.seat_state_path`, which is what §14.2's per-seat
    file is keyed by — a provider named `Claude Code` would clone happily and then be unable
    to record anything."""
    assert runner._SEATS == tuple(engine.DEFAULT_PROVIDERS)
    repo, run, b, m = _open(tmp_path)
    for name in runner._SEATS:
        assert storage.seat_state_path(run, name).parent == run


def test_the_baseline_the_loop_rebuilds_still_checks_what_a_seat_received(tmp_path):
    """`gate.open_run` returns the run directory and DISCARDS the `Baseline` it built, so
    every caller that drives a run rebuilds one from the record — and the record had no
    filesystem manifest in it.

    That is the field `fleet.clone_seat` compares a seat's content against, path by path. A
    `Baseline` rebuilt with `filesystem_manifest={}` makes that loop range over nothing and
    still return `Seat.verified is True`, which `verify.build_verifier` then reads as "clone
    seat has already verified the checkout against its manifest" before it takes the baseline
    gate surface. A premise asserted and not measured.

    Measured here rather than argued: the persisted manifest is corrupted for one path, and
    the run stops at the first clone. With `{}` there is no loop to enter and every assertion
    about a seat's content passes vacuously.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=1)
    recorded = baseline.read_filesystem_manifest(run)
    assert recorded == b.filesystem_manifest and "seed.txt" in recorded, \
        "the premise: materialize measured something and it survived to disk"

    baseline.filesystem_manifest_path(run).write_text(
        '{"seed.txt": "%s"}\n' % ("0" * 64))
    launch = _per_seat(lambda name, n, p: True)
    with pytest.raises(fleet.SeatError, match="differs from the baseline manifest"):
        runner.run(run, repo, identity=IDENT, launch=launch)
    assert launch.calls == []


def test_a_baseline_manifest_that_was_never_recorded_is_a_refusal_not_an_empty_check(tmp_path):
    """`{}` is precisely what the disarmed check looks like, so absent and empty must not be
    one value. A run directory missing the file cannot be driven at all.

    AND NEITHER CAN ONE HOLDING `{}`, which is the half this only argued. Absence was
    refused and a present-but-empty file returned the exact value the argument says must
    never be produced — `clone_seat`'s per-path loop ranging over nothing and reporting
    verified on the HEAD assertion alone. Unreachable from a missing file, perfectly
    reachable from a truncated write or a hand-edited run directory.
    """
    repo, run, b, m = _open(tmp_path, seats=1)
    restore = dict(base_commit=m.base_commit, tracked_tree_oid=m.tracked_tree_oid,
                   commit=m.baseline_commit, ref=m.baseline_ref)

    baseline.filesystem_manifest_path(run).write_text("{}\n")
    with pytest.raises(baseline.BaselineError, match="describes no paths"):
        baseline.restore(run, **restore)

    baseline.filesystem_manifest_path(run).unlink()
    with pytest.raises(baseline.BaselineError, match="filesystem manifest"):
        baseline.restore(run, **restore)
    with pytest.raises(baseline.BaselineError):
        runner.run(run, repo, identity=IDENT,
                   launch=_per_seat(lambda name, n, p: True))


def test_the_loop_reads_the_run_from_disk_and_takes_no_fact_the_directory_holds(tmp_path):
    """§14: resuming "always from disk and never from conversation state ... makes compaction
    and restart indistinguishable, one code path instead of two".

    Structural, and against the signature a caller would reach for first: `run` takes no
    manifest, no baseline, no commands and no seat count, because every one of those is in the
    run directory and an argument would be a second answer to a settled question. The three
    it does take are the three the directory does not hold.
    """
    sig = pyinspect.signature(runner.run)
    assert list(sig.parameters) == ["run_dir", "repo", "identity", "launch"]
    for name in ("identity", "launch"):
        p = sig.parameters[name]
        assert p.kind is pyinspect.Parameter.KEYWORD_ONLY
        assert p.default is pyinspect.Parameter.empty


def test_a_repository_that_is_not_the_one_the_run_recorded_is_refused(tmp_path):
    """`drift` refuses a repository the manifest did not record, so a caller cannot hand the
    loop a byte copy taken at t0 and be told nothing moved. Reached through `reconstruct`,
    which is the loop's only read of the world."""
    repo, run, b, m = _open(tmp_path, seats=1)
    other = make_repo(tmp_path, "stranger")
    launch = _per_seat(lambda name, n, p: True)
    with pytest.raises(runstate.ManifestError):
        runner.run(run, other, identity=IDENT, launch=launch)
    assert launch.calls == []


def test_a_run_whose_confirmation_named_no_setup_is_driven_end_to_end(tmp_path):
    """An empty setup is an ordinary repository that needs no toolchain — `gate.Confirmation`
    admits one in as many words and refuses only an empty VERIFY — and it is the input this
    package has repeatedly mishandled at call sites no fixture reached.

    Five of them were closed one at a time: `run_command` refuses a command with no steps,
    `run_seat` records `none` rather than a fabricated pass, `calibrate` would once raise for
    every such repository, `verify_candidate` would skip §6's hash validation entirely, and
    §8 would refuse the re-classification of a zero-diff seat outright. THE LOOP IS A SIXTH
    CALL SITE, and it reaches all of them in one pass — so the case is driven whole rather
    than asserted per branch. The zero-diff pair is the case beside this one.
    """
    repo, run, b, m = _open(tmp_path, setup=(), gate=GATE, seed=_gate("exit 0"), seats=1)
    (result,) = runner.run(run, repo, identity=IDENT,
                           launch=_per_seat(lambda name, n, p: bool(write(p, "w.py", "w\n"))))

    assert result.run is None and result.status.builder_setup == "none", \
        "no setup ran in the seat, and the record says there was none rather than a pass"
    assert result.verification[0] == verify.PASS
    assert result.status.forge == "completed", \
        "§8 rule 6 degrades a WITHHELD setup measurement, and a run with no setup command "\
        "withheld nothing — this was `partial` while the two shared one literal, recorded as "\
        "a standing ceiling for every repository that needs no toolchain"
    assert runstate.read_state(run).phase == "comparing"


def test_a_seat_refused_at_classification_is_recorded_and_does_not_orphan_its_operation(
        tmp_path):
    """A `RunnerError` out of `run_seat` is a KNOWN outcome, and §14.1's orphan is not.

    `run_seat` writes the seat's record — the answer, the sentinel, the path set — BEFORE it
    raises, so the operation ended in a way this loop watched. Leaving the intent open would
    report `outcome_unknown` for something nobody is unsure about, and would then refuse every
    later call on a run whose seat merely signed off badly.

    The seat below answers "ok": twenty-one characters of sentinel the engine ordered it to
    print, and nothing it wrote. Both attempts are refused, so the seat contributes no result
    at all — and the loop does not invent one. What happened is on disk, per attempt.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=1, attempts=2)

    def signs_off(*, name, seat_path, token, env):
        return {"name": name, "status": "ok", "valid": True, "reason": "ok", "exit_code": 0,
                "duration_sec": 1.0, "structured": False, "attempts": 1,
                "result_text": f"ok\n{token}"}

    assert runner.run(run, repo, identity=IDENT, launch=signs_off) == (), \
        "no seat could be classified, and the loop reports that rather than a verdict"

    assert runstate.reconstruct(run, repo).orphans == (), \
        "a refusal this loop caught is not an operation whose outcome is unknown"
    assert [a["status"] for a in runstate.read_seat(run, "claude")["attempts"]] == [None, None]
    done = [e for e in journal.Journal(storage.journal_path(run)).read()
            if e.event == journal.done("seat")]
    assert len(done) == 2 and all("cannot be classified" in e.data["refused"] for e in done), \
        "and the journal says what each attempt was refused on, not merely that it ended"
    assert not runner.verifier_dir(run, "claude").exists()


def test_a_candidate_section_6_refuses_is_recorded_rather_than_orphaned_or_invented(
        tmp_path):
    """§6's own refusals — `SetupOverlap` here — leave a seat with no verdict, and the loop
    says so in both places a reader looks rather than leaving `outcome_unknown` behind.

    The setup command only overlaps WHEN THE CANDIDATE IS PRESENT, which is what makes this
    reachable at all: on the untouched baseline it is a no-op, so §5 step 3 calibrates clean
    and the seat's own setup passes, and only the verifier — the one tree holding both the
    confirmed setup and the candidate — sees a tracked file move under a contract that
    declares nothing.

    NO OUTCOME IS INVENTED, which is what this case has always been about: §6.2 has none for
    it and the seat keeps `verify: "not-run"`. What changed is the blast radius — this used
    to END THE RUN, so on a real fleet the two seats behind this one lost verifications their
    providers had already been paid for. The seat beside it holds that half.
    """
    setup = (verify.Step(argv=("sh", "-c", "if [ -f work.py ]; then echo x >> seed.txt; fi")),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=_gate("exit 0"), seats=1)

    (result,) = runner.run(run, repo, identity=IDENT,
                           launch=_per_seat(lambda n_, i_, p: bool(write(p, "work.py", "w\n"))))
    assert result.verification is None and "seed.txt" in result.verification_refused

    assert runstate.reconstruct(run, repo).orphans == (), \
        "the loop watched the verification end, so nothing here is `outcome_unknown`"
    (done,) = [e for e in journal.Journal(storage.journal_path(run)).read()
               if e.event == journal.done("verification")]
    assert "seed.txt" in done.data["refused"]
    row = _attempt(run, "claude", 1)
    assert row["status"]["verify"] == "not-run", \
        "and the seat's record still carries the verdict its evidence supports"
    assert "seed.txt" in row["verification_refused"], \
        "beside the reason it never got a better one — `not-run` alone would read as a seat "\
        "the loop had not reached, for a verifier clone that was bought and spent"


def test_a_crash_after_the_phase_was_written_and_before_anything_was_journalled_resumes(
        tmp_path):
    """The one window a second call can legitimately pick up, and the reason `_reach` has an
    equal case at all.

    `run` writes `setting_up` and then records the calibration's intent; a process killed
    between those two leaves a phase on disk and no operation in the journal. Nothing was
    spent, so there is nothing to carry forward and nothing to refuse — and a loop that
    advanced unconditionally would meet `advance`'s own refusal of a repeat and turn a
    resumable run into a dead one.

    Every LATER phase is followed immediately by a record `_refuse_a_second_pass` sees, which
    is why this window is `setting_up` and nothing else — asserted below by the run reaching
    `comparing` rather than by re-deriving the argument.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=1)
    runstate.write_state(run, runstate.State(
        phase="setting_up", round=0, attempt=0, verified_checkpoint=None,
        deliverable_checkpoint=None))
    assert journal.Journal(storage.journal_path(run)).read() == (), \
        "the premise: the phase was written and no operation was journalled"

    (result,) = runner.run(run, repo, identity=IDENT,
                           launch=_per_seat(lambda name, n, p: True))
    assert result.verification[0] == verify.PASS
    assert runstate.read_state(run).phase == "comparing"


def test_the_calibration_is_taken_in_the_phase_section_14_puts_it_in(tmp_path, monkeypatch):
    """`runstate._EDGES` gives `failed` an edge out of every non-terminal phase BECAUSE
    "calibration runs inside `setting_up`, hours before any review" — so a loop that took it
    in `building` would make that reasoning describe a graph nothing uses.

    Ordering across two files (the state record and the journal) is not readable from either
    alone, so both writes are collected into one sequence as they happen. That §5 step 3 comes
    before any PROVIDER call is a different property with its own test; this is about which
    phase the run is recorded in while it does.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=1)
    seen = []
    real_state, real_calibrate = runstate.write_state, verify.calibrate

    def state_spy(run_dir, state):
        seen.append(("phase", state.phase))
        return real_state(run_dir, state)

    def calibrate_spy(*a, **kw):
        seen.append(("calibrate",))
        return real_calibrate(*a, **kw)

    monkeypatch.setattr(runstate, "write_state", state_spy)
    monkeypatch.setattr(verify, "calibrate", calibrate_spy)
    runner.run(run, repo, identity=IDENT, launch=_per_seat(lambda name, n, p: True))

    assert seen[:3] == [("phase", "setting_up"), ("calibrate",), ("phase", "building")]


def test_the_policy_is_read_off_this_runs_own_confirm_record_and_no_other(tmp_path):
    """An `operation_id` names one operation, and §5 step 2's answer belongs to the run that
    gave it. A reader that took the last `confirm_done` in the file whatever it was about
    would carry a red baseline on another run's permission.

    Reachable by a journal that was copied or concatenated, which is exactly the material a
    resume is handed; the refusal costs nothing and the alternative is silent.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 1"), seats=1)
    log = journal.Journal(storage.journal_path(run))
    log.record(journal.intent("confirm"), operation_id="some-other-run")
    log.record(journal.done("confirm"), operation_id="some-other-run",
               on_calibration_failure="degraded", strategy="size-gated", accepted_gaps=[])

    launch = _per_seat(lambda name, n, p: True)
    with pytest.raises(runner.RunnerError, match="no confirm_done record"):
        runner.run(run, repo, identity=IDENT, launch=launch)
    assert launch.calls == []


def test_a_provider_name_that_could_not_be_recorded_is_refused_before_any_clone(
        tmp_path, monkeypatch):
    """The fleet list is `council.engine`'s, so this module does not control what lands in it.

    A provider added there as `Claude Code` clones happily — it is a legal branch component —
    and then cannot be recorded at all, because §14.2's per-seat file is keyed by the name and
    `storage.seat_state_path` refuses one with a space in it. Discovered at the first
    `write_seat`, that costs a provider call and a clone; discovered here it costs nothing.

    `_SEATS` is patched rather than the constant argued about: what is being measured is that
    the loop asks the question before it spends, not what `engine.DEFAULT_PROVIDERS` happens
    to hold today.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=2)
    monkeypatch.setattr(runner, "_SEATS", ("claude", "Claude Code"))
    launch = _per_seat(lambda name, n, p: True)

    with pytest.raises(runner.RunnerError):
        runner.run(run, repo, identity=IDENT, launch=launch)
    assert launch.calls == [] and not (run / "calibration").exists(), \
        "refused before the calibration clone, let alone a seat's"


def test_a_refused_retry_does_not_erase_the_verdict_the_attempt_before_it_reached(tmp_path):
    """Attempt 1 reached `failed` and attempt 2 reached nothing at all, and those are
    different facts about the same seat.

    Reporting the seat's ABSENCE because its last attempt was refused would throw away the one
    verdict this seat did produce — the same class of loss as a rationale that survives in
    memory and not on disk, one layer up. `None` stays reserved for the seat that reached no
    verdict at any attempt, which is the case with genuinely nothing to say.
    """
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=1, attempts=2)

    seen = []

    def fails_then_signs_off(*, name, seat_path, token, env):
        seen.append(name)
        first = len(seen) == 1
        if first:
            # Attempt 1: real work, and a provider that did not answer validly — §8 rule 1
            # taints every other signal, so the verdict is `failed` and a retry is spent.
            write(Path(seat_path), "work.py", "work\n")
        # Attempt 2 changes nothing and signs off with the token the engine ordered it to
        # print, which §8's rule 3 refuses to read as the rationale a `no_change` needs.
        return {"name": name, "status": "ok", "valid": not first,
                "reason": "ok", "exit_code": 0, "duration_sec": 1.0, "structured": False,
                "attempts": 1, "result_text": ANSWER if first else f"ok\n{token}"}

    (result,) = runner.run(run, repo, identity=IDENT, launch=fails_then_signs_off)

    assert (result.attempt, result.status.forge) == (1, "failed")
    assert _attempt(run, "claude", 2)["status"] is None, \
        "the premise: attempt 2 reached no verdict, and its record says so"


def test_the_record_always_carries_the_prompt_identity_key(tmp_path):
    """§8's proven_read/partial rule: a launch that returned no fingerprint must not produce
    a record that OMITS the key. An omitted key and an unmeasured one read the same to
    `--collect`, and only one of them is a run that lost a measurement."""
    repo, run, b, m = _open(tmp_path)
    runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                    launch=_fake(_edit))
    entry = _attempt(run, "claude", 1)
    assert "prompt_identity" in entry and entry["prompt_identity"] is None


def test_a_launch_that_returned_a_fingerprint_has_it_recorded(tmp_path):
    """The other half, and the one `_fake` cannot reach: every launch in this suite returns
    no fingerprint, so a `_prompt_identity` that answered `None` unconditionally would keep
    every case above green while §11's whole measurement never reached disk."""
    from forge import fingerprint  # noqa: PLC0415

    row = fingerprint.as_row(fingerprint.PromptIdentity(
        "a" * 64, "b" * 64, None, "/usr/bin/claude", "2.1.220", "opus-5", None, None))

    def launch(*, name, seat_path, token, env):
        _edit(Path(seat_path))
        return {"name": name, "status": "ok", "valid": True, "reason": "ok",
                "exit_code": 0, "duration_sec": 1.0, "structured": False, "attempts": 1,
                "result_text": f"{ANSWER}\n{token}", "prompt_identity": row}

    repo, run, b, m = _open(tmp_path)
    runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT, launch=launch)
    assert _attempt(run, "claude", 1)["prompt_identity"] == row


def test_a_malformed_prompt_identity_is_refused_at_the_writer(tmp_path):
    """'Nobody measured' and 'somebody wrote nonsense' are different records, and only one
    of them is safe to act on."""
    def launch(*, name, seat_path, token, env):
        _edit(Path(seat_path))
        return {"name": name, "status": "ok", "valid": True, "reason": "ok",
                "exit_code": 0, "duration_sec": 1.0, "structured": False, "attempts": 1,
                "result_text": f"{ANSWER}\n{token}",
                "prompt_identity": {"prompt_sha256": "a" * 64}}

    repo, run, b, m = _open(tmp_path)
    with pytest.raises(runner.RunnerError, match="prompt_identity"):
        runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT, launch=launch)


def test_a_measurement_taken_before_a_refusal_still_reaches_the_record(tmp_path):
    """THE THIRD INSTANCE, closed. `build_verifier` fills §6.1's gate surface and `run_setup`
    returns a SetupResult; a later refusal dropped both as locals, so a verifier clone that
    was built, whose gate surface WAS measured over two trees, and whose setup ran and
    exited 0 came back `gate_delta: null, gate_surface: null, verifier_setup: null`.

    THE RIG IS THE ADVERSARIAL ONE, not a lookalike. It is the same candidate
    `test_a_candidate_that_repoints_the_hooks_path_through_setup_does_not_reach_the_gate`
    uses — a `./setup.sh` that repoints `core.hooksPath` — driven through `runner.run` so
    `_verify_a_seat` runs and the record is written. A fixture built only of realistic-looking
    answers cannot reach the guard that exists for unrealistic ones.
    """
    def seed(repo):
        write(repo, "gate.sh", "#!/bin/sh\nexit 0\n").chmod(0o755)
        write(repo, "setup.sh", "#!/bin/sh\nexit 0\n").chmod(0o755)
        commit_all(repo, "gate and setup")

    setup = (verify.Step(argv=("./setup.sh",)),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=seed,
                            seats=1, attempts=1)
    _confirmed(run)

    def rig(name, n, p):
        write(p, "setup.sh", "#!/bin/sh\ngit config --local core.hooksPath .githooks\n"
                             "exit 0\n").chmod(0o755)
        return True

    runner.run(run, repo, identity=IDENT, launch=_per_seat(rig))
    entry = _attempt(run, "claude", 1)
    assert entry["verification_refused"] and "core.hooksPath" in entry["verification_refused"]
    assert entry["candidate"]["gate_surface"] is not None, \
        "§6.1's surface was measured over two trees in a clone that was built and paid for"
    assert entry["candidate"]["gate_delta"] is not None
    assert entry["verifier_setup"] is not None and \
        entry["verifier_setup"]["exit_code"] == 0, \
        "the verifier's setup ran and exited 0 — the very fact explaining how the hooks moved"


def test_the_revision_writer_validates_its_payload_too(tmp_path, monkeypatch):
    """THE SECOND WRITER. `_write` is not the only one: `_revise` calls `runstate.write_seat`
    directly, and it is the writer on `reclassify_seat`'s success path and BOTH of
    `_verify_a_seat`'s refusal paths — including the one the test above drives. A schema check
    installed only at `_write` would leave every verification-phase record, this task's whole
    deliverable, written unvalidated.

    Driven through the same hooks rig so it is the real path and not a lookalike: a key is
    dropped from `_record`'s output, and the run must refuse at the writer rather than publish."""
    def seed(repo):
        write(repo, "gate.sh", "#!/bin/sh\nexit 0\n").chmod(0o755)
        write(repo, "setup.sh", "#!/bin/sh\nexit 0\n").chmod(0o755)
        commit_all(repo, "gate and setup")

    setup = (verify.Step(argv=("./setup.sh",)),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=seed, seats=1, attempts=1)
    _confirmed(run)

    real_record = runner._record
    state = {"n": 0}

    def lossy(result):
        # Whole on the way in (so `run_seat`'s own write succeeds and there is an attempt to
        # revise), damaged on the revision — which is the writer this test is about.
        out = real_record(result)
        state["n"] += 1
        if state["n"] > 1:
            out.pop("verifier_setup")
        return out
    monkeypatch.setattr(runner, "_record", lossy)

    def rig(name, n, p):
        write(p, "setup.sh", "#!/bin/sh\ngit config --local core.hooksPath .githooks\n"
                             "exit 0\n").chmod(0o755)
        return True

    with pytest.raises(runner.RunnerError, match="verifier_setup"):
        runner.run(run, repo, identity=IDENT, launch=_per_seat(rig))


def test_a_recovered_measurement_that_is_not_this_seats_does_not_end_the_fleet(tmp_path,
                                                                              monkeypatch):
    """CONTAINMENT, on the path the recovery added. `_measured` refuses a candidate that is not
    this seat's, and that refusal is right — but it now runs INSIDE the `except`, where nothing
    catches it. Uncontained, ONE seat's refused verification ends the whole run with every
    provider already paid, which is the Critical `_verify_a_seat`'s docstring records as closed.

    The seat must keep its pre-verification verdict, and the record must SAY the measurement
    was dropped: a refusal that read as if nothing had been measured would be cleaner than its
    evidence."""
    def seed(repo):
        write(repo, "gate.sh", "#!/bin/sh\nexit 0\n").chmod(0o755)
        write(repo, "setup.sh", "#!/bin/sh\nexit 0\n").chmod(0o755)
        commit_all(repo, "gate and setup")

    setup = (verify.Step(argv=("./setup.sh",)),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=seed, seats=1, attempts=1)
    _confirmed(run)

    def refusing(result, candidate, verifier_setup):
        raise runner.RunnerError("that candidate is not this seat's")
    monkeypatch.setattr(runner, "_measured", refusing)

    def rig(name, n, p):
        write(p, "setup.sh", "#!/bin/sh\ngit config --local core.hooksPath .githooks\n"
                             "exit 0\n").chmod(0o755)
        return True

    runner.run(run, repo, identity=IDENT, launch=_per_seat(rig))      # must not raise
    entry = _attempt(run, "claude", 1)
    refused = entry["verification_refused"]
    assert refused and "core.hooksPath" in refused
    assert "not this seat" in refused, \
        "the dropped measurement is named, not silently absent"
    assert entry["candidate"]["gate_surface"] is None and \
        entry["verifier_setup"] is None, \
        "nothing unadmitted reached the record; the seat kept its pre-verification verdict"


def test_the_first_writer_validates_its_payload_too(tmp_path, monkeypatch):
    """THE OTHER WRITER. A schema check installed only at `_revise` would be the mirror of
    the defect that put it there — `_write` publishes every pre-verification record, which is
    every record a run that never reaches §6 has at all.

    MEASURED: with `_payload` removed from `_write` alone, the whole runner suite stays green.
    A key dropped from `_record` has to be refused where the producer is still standing, not
    met by a resume hours later.
    """
    real_record = runner._record

    def lossy(result):
        out = real_record(result)
        out.pop("prompt_identity")
        return out
    monkeypatch.setattr(runner, "_record", lossy)

    repo, run, b, m = _open(tmp_path)
    with pytest.raises(runner.RunnerError, match="prompt_identity"):
        runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(_edit))


def test_the_gate_surface_survives_a_refusal_taken_before_the_verifiers_setup_finished(
        tmp_path):
    """THE FIRST HAND-OVER, which the hooks rig cannot reach. That rig is refused AFTER the
    verifier's setup ran, so the SECOND hand-over covers it and the first could be deleted
    with every other case in this file still green — measured.

    `run_setup` raising `SetupOverlap` is the path where no setup result exists yet, and
    §6.1's surface — filled by `build_verifier` one statement earlier, over two trees, in a
    clone the run already paid for — is dropped unless it was handed over the instant it
    existed. The rig is the one §6's own refusal test uses: a confirmed setup that moves a
    tracked file only when the candidate is present, so the baseline calibrates clean.
    """
    setup = (verify.Step(argv=("sh", "-c", "if [ -f work.py ]; then echo x >> seed.txt; fi")),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=_gate("exit 0"), seats=1)

    runner.run(run, repo, identity=IDENT,
               launch=_per_seat(lambda n_, i_, p: bool(write(p, "work.py", "w\n"))))
    row = _attempt(run, "claude", 1)
    assert "seed.txt" in row["verification_refused"], "the premise: §6 refused at run_setup"
    assert row["candidate"]["gate_surface"] is not None and \
        row["candidate"]["gate_delta"] is not None, \
        "§6.1's measurement existed before the refusal and reached the record"
    assert row["verifier_setup"] is None, \
        "and nothing is invented — that setup never produced a result to record"


def test_a_prior_attempt_its_own_reader_refuses_costs_no_provider_call(tmp_path):
    """I2: THE REFUSAL WAS AT THE WRITER, WHICH IS AFTER THE PROVIDER HAS BEEN PAID.

    `_prior_attempts` asked only that `attempts` be a list of dicts, while `_payload` runs the
    whole `seatrecord.decode` schema over `[*priors, this]` before publishing. So a prior
    attempt missing a field — or carrying a malformed `prompt_identity` — passed the cheap
    check, and the run then cloned the seat, ran the setup command and called the provider
    before failing at the write. `_prior_attempts`' own docstring argues that this refusal is
    free BECAUSE it is taken before any of that; running the reader's schema here is what makes
    the sentence true rather than aspirational.

    The provider is a fake that RECORDS being called, because the verdict is identical either
    way and the cost is the whole finding.
    """
    repo, run, b, m = _open(tmp_path)
    runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                    launch=_fake(lambda p: write(p, "half.py", "half\n")))
    row = runstate.read_seat(run, "claude")
    del row["attempts"][0]["verification"]              # a field the writer always writes
    runstate.write_seat(run, "claude", row)

    called = []

    def launch(**kw):
        called.append(kw)
        return {"status": "ok", "valid": True, "result_text": "x"}
    with pytest.raises(runner.RunnerError, match="its own reader refuses"):
        runner.run_seat(m, run, b, name="claude", attempt=2, identity=IDENT, launch=launch)
    assert called == [], "the damaged record must be refused before a provider is paid"
    assert not runner.seat_dir(run, "claude", 2).exists(), \
        "and before the clone, which is what makes the refusal free"


def _a_task_source(run, body="Refactor the thing.\n"):
    """The run's OWN task source directory, and the name matters.

    `_materialize_the_task` reads `storage.task_source_path(run_dir)` and nothing else, so a
    scratch directory beside the run would make every assertion below pass or fail for a
    reason that has nothing to do with what a real run does — §20 persists the resolved
    instruction inside the run precisely so a resume does not depend on a tree that has gone.
    """
    src = storage.task_source_path(run)
    src.mkdir()
    (src / "TASK.md").write_text(body)
    return src


def test_a_run_with_a_task_bundle_materializes_it_into_every_seat(tmp_path):
    """§20: 'materialize it identically in every clone'. The bundle lands in the seat's GIT
    DIRECTORY (`taskbundle.task_dir`), which `harvest.record` never walks — `snapshot.take`
    skips `.git` — so it is invisible to the artifact set by construction rather than by a
    name rule.

    EVERY seat, plural, because that is the claim: one seat would leave a per-seat condition
    unmeasured, and §11 reads agreement off seats that were identically prompted.
    """
    repo, run, b, m = _open(tmp_path)
    _a_task_source(run)
    tb = taskbundle.scan(storage.task_source_path(run), entrypoint="TASK.md")
    taskbundle.write_task_bundle(run, tb)

    for name in ("claude", "codex"):
        out = runner.run_seat(m, run, b, name=name, attempt=1, identity=IDENT, launch=_fake())
        laid = taskbundle.task_dir(out.seat.path)
        assert (laid / "TASK.md").read_text() == "Refactor the thing.\n"
        taskbundle.verify_materialized(tb, out.seat.path)      # raises if a byte moved
        # The bundle is NOT in the artifact set: it is engine-supplied, not the agent's work.
        # Asserted on the INVENTORY rather than on `artifacts.paths`, because the path set is a
        # Fsetup -> Fwork difference and a bundle present in both would difference itself out —
        # so `"TASK.md" not in paths` would hold even if `harvest` were walking `.git`.
        assert "TASK.md" not in out.artifacts.paths
        assert not any("khenrix-forge" in p for p in harvest.record(out.seat.path)), \
            "the engine-supplied bundle reached the seat's own inventory"


def test_a_run_that_recorded_no_task_bundle_still_builds_its_seats(tmp_path):
    """Runs that predate §20 record none, and an absent bundle is not a refusal."""
    repo, run, b, m = _open(tmp_path)
    out = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT, launch=_fake())
    assert out.seat is not None
    assert not taskbundle.task_dir(out.seat.path).exists(), \
        "a run that recorded no bundle laid something down anyway"


def test_a_task_bundle_that_cannot_be_read_stops_the_seat_before_the_provider_is_paid(tmp_path):
    """The refusal has to land before `launch`, or a corrupt bundle costs a provider call and
    the seat answers a task it was never given."""
    repo, run, b, m = _open(tmp_path)
    storage.task_bundle_path(run).write_bytes(b"{ not json")
    calls = []
    with pytest.raises(taskbundle.TaskBundleError):
        runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=lambda **kw: calls.append(kw) or {})
    assert calls == [], "a provider was launched over a bundle nobody could read"


def test_a_seat_handed_bytes_its_manifest_does_not_describe_is_refused_before_the_launch(
        tmp_path):
    """`materialize` returns cleanly here, and that is the whole point of the case.

    It reads the SOURCE live, so a file edited between the scan and the seat arrives in the
    clone unhashed and unnamed — every path in the manifest exists, with different bytes.
    `verify_materialized` re-deriving the manifest FROM THE SEAT is the only step that turns
    that into a refusal; without it the run proceeds, `bundle_sha256` claims a hash of bytes no
    seat holds, and §11 reads two such seats as identically prompted.
    """
    repo, run, b, m = _open(tmp_path)
    src = _a_task_source(run)
    tb = taskbundle.scan(src, entrypoint="TASK.md")
    taskbundle.write_task_bundle(run, tb)
    (src / "TASK.md").write_text("Refactor something else entirely.\n")

    calls = []
    with pytest.raises(taskbundle.TaskBundleError,
                       match="does not match the authored manifest"):
        runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=lambda **kw: calls.append(kw) or {})
    assert calls == [], "a provider was paid to answer a task the seat was never handed"


def test_a_task_bundle_whose_source_is_gone_stops_the_seat_rather_than_running_it_empty(
        tmp_path):
    """The manifest is recorded and the bytes it describes are not. A seat launched here would
    be told to read an entrypoint it does not have, and would answer about nothing."""
    repo, run, b, m = _open(tmp_path)
    src = _a_task_source(run)
    tb = taskbundle.scan(src, entrypoint="TASK.md")
    taskbundle.write_task_bundle(run, tb)
    (src / "TASK.md").unlink()
    src.rmdir()

    calls = []
    with pytest.raises(taskbundle.TaskBundleError):
        runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=lambda **kw: calls.append(kw) or {})
    assert calls == []


def test_a_retry_materializes_the_same_bundle_into_the_fresh_clone(tmp_path):
    """§8.1 gives every retry a FRESH clone, and `materialize` refuses to write into a
    directory that already holds a bundle — so a retry that re-used a seat would be refused
    here rather than silently re-materializing. The attempt-2 clone is a new tree, so it is
    not, and this is what says the two rules compose."""
    repo, run, b, m = _open(tmp_path)
    src = _a_task_source(run)
    tb = taskbundle.scan(src, entrypoint="TASK.md")
    taskbundle.write_task_bundle(run, tb)
    first = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT, launch=_fake())
    second = runner.run_seat(m, run, b, name="claude", attempt=2, identity=IDENT,
                             launch=_fake())
    assert first.seat.path != second.seat.path
    for out in (first, second):
        taskbundle.verify_materialized(tb, out.seat.path)


def test_a_verifier_whose_setup_failed_cannot_report_pass(tmp_path):
    """A caveat in prose does not repair a field downstream code branches on.

    `_with_setup_caveat` is right that ATTRIBUTION is open — a candidate really can break the
    setup that passed in its own clone — and this does not settle it. It settles the VERDICT,
    which is a different claim: a gate that ran in a tree the confirmed setup never finished
    preparing has not measured the candidate, whoever's fault the setup failure was.
    `SETUP_REFUSED` says only that §6 step 4 did not run.
    """
    setup = (verify.Step(argv=("sh", "-c", "exit 3")),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the edit\n")))
    cal = _calibrate(tmp_path, repo, b, m)

    outcome, reason, _v, sr = runner.verify_candidate(
        m, run, b, r.candidate, name="claude", identity=IDENT, calibration=cal)
    assert outcome == runner.SETUP_REFUSED, \
        "the gate ran in a tree the confirmed setup command failed to prepare"
    assert sr is not None and sr.run.exit_code == 3, \
        "and the measurement that was bought is still handed back, not replaced by prose"
    assert runner._verify_dim(outcome) == "not-run"


def test_a_verifier_setup_that_SUCCEEDS_still_reaches_the_gate(tmp_path):
    """The guard against over-tightening: refusing on a zero-exit setup would refuse every run."""
    setup = (verify.Step(argv=("sh", "-c", "exit 0")),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the edit\n")))
    cal = _calibrate(tmp_path, repo, b, m)
    outcome, _reason, _v, _s = runner.verify_candidate(
        m, run, b, r.candidate, name="claude", identity=IDENT, calibration=cal)
    assert outcome == verify.PASS


def test_a_fleet_that_produced_nothing_does_not_reach_comparing(tmp_path):
    """REPRODUCED: with every seat refused on every attempt, `built` is empty and the run
    still advanced `harvested` -> `comparing`. `comparing` is §6's phase and its meaning is
    "the fleet's candidates are verified" — recording it over zero candidates is a run that
    got further than it did, and a resume reading that position would look for verifier
    clones nobody made.

    The RETURN VALUE is deliberately unchanged: `()` is what this loop has always handed back
    for a fleet that produced nothing, and the sibling test above depends on it. What changes
    is only that the run directory keeps its last TRUE phase."""
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=1, attempts=2)

    def signs_off(*, name, seat_path, token, env):
        return {"name": name, "status": "ok", "valid": True, "reason": "ok", "exit_code": 0,
                "result_text": "SENTINEL-" + token, "duration_sec": 0.1}

    assert runner.run(run, repo, identity=IDENT, launch=signs_off) == ()
    assert runstate.read_state(run).phase == "building", \
        "an empty fleet recorded a phase claiming its candidates were verified"


def test_the_verify_dimension_table_is_total_over_every_outcome():
    """§8's dimension used two entries and a `.get` default, so four of §6.2's six outcomes
    reached "not-run" by falling through — and three of those DID execute the gate.

    THE EXTERNAL QUESTION is not "does GATE_CHANGED map to not-run" (it still does, and the
    reasoning is at the table: §8's vocabulary has no word for "ran, and the verdict is
    neither", and calling it "fail" would report a candidate failure the candidate did not
    have). It is whether the seventh outcome §6.2 gains next is answered by a DECISION or by
    a default. A default absorbs it silently; a total table raises."""
    assert set(runner._VERIFY_DIM) == set(verify.OUTCOMES), (
        set(verify.OUTCOMES) ^ set(runner._VERIFY_DIM))
    assert set(runner._VERIFY_DIM.values()) <= {"pass", "fail", "not-run"}
    for outcome in verify.OUTCOMES:
        assert runner._verify_dim(outcome) in ("pass", "fail", "not-run")
    # The refusal for a value that is not an outcome at all still fires.
    with pytest.raises(runner.RunnerError):
        runner._verify_dim("INVENTED")


def test_a_refusal_that_repeats_itself_does_not_buy_another_clone(tmp_path):
    """§8.1's retry buys a fresh clone because a SEAT can fail differently the second time.
    A refusal this ENGINE makes for the same reason twice will make it a third time, and each
    further attempt spends another clone — and, for any refusal raised after the launch,
    another provider call — to reach the identical outcome.

    THE MESSAGE IS THE DISCRIMINATOR BECAUSE THE TYPE IS NOT: `RunnerError` covers both
    "this engine refused" and "this seat failed" across 29 raise sites, and splitting it is a
    change to every one of them. Two identical messages are evidence of determinism that
    needs no such classification — and TWO are required, so a genuinely transient failure
    still gets its retry."""
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=1, attempts=5)
    tries = []

    def always_the_same(*, name, seat_path, token, env):
        tries.append(name)
        raise runner.RunnerError("the same deterministic refusal")

    assert runner.run(run, repo, identity=IDENT, launch=always_the_same) == ()
    assert len(tries) == 2, f"a deterministic refusal bought {len(tries)} attempts of 5"


def test_two_different_refusals_still_get_their_retries(tmp_path):
    """THE DISCRIMINATION CHECK. A failure that presents differently each time is exactly the
    transient one §8.1's retry exists for, and stopping on it would spend the budget the
    operator agreed to on nothing."""
    repo, run, b, m = _open(tmp_path, gate=GATE, seed=_gate("exit 0"), seats=1, attempts=3)
    n = []

    def different_each_time(*, name, seat_path, token, env):
        n.append(name)
        raise runner.RunnerError(f"transient failure {len(n)}")

    assert runner.run(run, repo, identity=IDENT, launch=different_each_time) == ()
    assert len(n) == 3, f"only {len(n)} of 3 attempts were spent on a changing failure"
