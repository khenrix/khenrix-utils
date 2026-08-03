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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import pytest  # noqa: E402
from council import engine  # noqa: E402
from forge import (baseline, bundle, fleet, inspect as finspect,  # noqa: E402
                   runner, runstate, seat as seatmod, storage, verify)
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


def _manifest(repo, b, setup, gate):
    refs, digest = runstate.snapshot_refs(repo, (), forge_refs={b.ref: b.commit})
    return runstate.Manifest(
        run_id="r1", repo_path=str(repo), base_commit=b.base_commit,
        baseline_ref=b.ref, baseline_commit=b.commit, tracked_tree_oid=b.tracked_tree_oid,
        selected_paths=(), generator_contract=finspect.GeneratorContract(),
        setup=setup, verify=gate,
        protected_refs=refs, forge_refs={b.ref: b.commit}, status_digest=digest,
        index_digest=runstate.snapshot_index(repo), created_at="2026-08-03T00:00:00Z",
        seats=3, attempts=3)


def _open(tmp_path, *, setup=(verify.Step(argv=("true",)),),
          gate=(verify.Step(argv=("true",)),), name="repo", seed=None):
    """A repository, a run directory, B, and the manifest that agreed to them.

    `seed` runs against the repository before B is taken, so a fixture that needs a gate
    script in the BASELINE — the only tree §6 lets a gate come from — writes and commits it
    there. `name` keeps two whole runs apart inside one `tmp_path`, which is what a case
    that differs only in what its calibration measured needs.
    """
    repo = make_repo(tmp_path, name)
    if seed is not None:
        seed(repo)
    run = tmp_path / f"run-{name}"
    run.mkdir(exist_ok=True)
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    m = _manifest(repo, b, setup, gate)
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
    assert (r.status.setup, r.status.verify) == ("pass", "not-run"), \
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

    assert r.status.forge == "failed" and r.status.setup == "fail"
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
    so the mutant that writes `"pass"` here SURVIVED beneath the comment denying it. The seat
    below is otherwise strong enough to be `completed` — valid process, usable artifacts,
    proof token quoted — so `partial` is §8 rule 6 firing on the one dimension left, and
    nothing else in the fixture can produce it.
    """
    repo, run, b, m = _open(tmp_path, setup=())
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the agent's edit\n")))

    assert r.run is None, "the premise: no setup command was run in this seat"
    assert (r.status.process, r.status.artifacts, r.status.proven_read) \
        == ("valid", "usable", True), "every other dimension is at its strongest reading"
    assert r.status.setup == "not-run"
    assert r.status.forge == "partial", "§8 rule 6: an unmeasured setup cannot promote a seat"
    assert _attempt(run, "claude", 1)["status"]["setup"] == "not-run"


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

    Deliberately not an outcome: §6.2 has none for it, and which side it belongs to is open —
    a candidate really can break the setup that passed in its own clone.
    """
    setup = (verify.Step(argv=("sh", "-c", "exit 3")),)
    repo, run, b, m = _open(tmp_path, setup=setup, gate=GATE, seed=_gate("exit 0"))
    r = runner.run_seat(m, run, b, name="claude", attempt=1, identity=IDENT,
                        launch=_fake(lambda p: write(p, "work.py", "the edit\n")))
    assert r.status.setup == "fail", "the premise: the same setup fails in the seat too"
    cal = _calibrate(tmp_path, repo, b, m)

    outcome, reason, _v, _s = runner.verify_candidate(
        m, run, b, r.candidate, name="claude", identity=IDENT, calibration=cal)
    assert outcome == verify.PASS, "the gate itself was green, and that is reported"
    assert "setup command exited 3" in reason, \
        "but a PASS that never says the setup failed reads cleaner than its evidence"


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
    assert r.status.setup == "pass", "the premise: the seat's own setup sees the guard too"
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
    assert r.status.setup == "pass" and "setup.sh" in r.candidate.tracked_patch.decode(), \
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
    assert done.status.setup == "pass", "§8 asks for BOTH halves and neither substitutes"

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
