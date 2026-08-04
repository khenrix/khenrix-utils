"""The front end. Nothing here spends money: every provider call is an injected fake.

TWO RULES MAKE THAT STRUCTURAL RATHER THAN REMEMBERED, and both are in `_drive_a_start`:
`monkeypatch` is a REQUIRED POSITIONAL argument of it, so a test that drives a start cannot
have skipped the fixture that points `XDG_STATE_HOME` at `tmp_path` and keeps the provider
fake in place; and the launcher reaches `cli` only through the `make_launcher=` seam, either
passed explicitly through `cli.main(...)` or monkeypatched onto `cli.launch.make_launcher`.
A helper that quietly did neither — driven from a test with no `monkeypatch` in its signature
— is the shape that spends money and writes to a real home directory while looking like an
ordinary unit test.
"""
import ast
import io
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from council import engine  # noqa: E402
from forge import (cli, fingerprint, handover, runstate, seat as seatmod,  # noqa: E402
                   storage, taskbundle, ultra)
from forge_fixtures import commit_all, git as _git, make_repo, write  # noqa: E402

# §19's window. `council.engine.MODE_TIMEOUT` now carries it, but these tests still SET it:
# injected into the ENGINE's own table rather than around `cli._resolve_seat_timeout`, because
# the property under test is that the CLI reads that table and no other source — a
# monkeypatched resolver would pass for a CLI that invented a number, and an unpatched test
# would pass for one that hard-coded the same 3600.
_FORGE_TIMEOUT = 3600

# Long enough to clear `seat._MIN_RATIONALE_CHARS`: §8 refuses a zero-diff seat whose
# rationale is decorative, and a fake that signed off in two words would be refused for a
# reason no test here is about.
ANSWER = "the retry already backs off; adding one would double-sleep"

# Which seat the fake provider answers as a seat that cannot be classified. Module state
# rather than a closure variable because `_a_fake_make_launcher` is `_drive_a_start`'s
# DEFAULT argument and is bound at import; `monkeypatch.setitem` is what sets it per test and
# what puts it back.
_FAKE = {"refuse": None}

_TOKEN = re.compile(r"SENTINEL-[0-9a-f]{12}")

# §13.1's real pass, bound at import — BEFORE the autouse fixture below replaces the module
# attribute. The non-vacuity test needs the function the guard displaces, and reading it back
# off `ultra.run_ultra` inside a test reads the guard instead.
_REAL_RUN_ULTRA = ultra.run_ultra


def _sentinel_of(spec) -> str:
    """The per-seat token out of the spec the launcher just built.

    `engine.apply_sentinel` puts it in the prompt, which is `spec.stdin` for codex and buried
    in `argv` for claude and agy — so both are searched rather than one being guessed at. A
    fake that could not find it would answer without citing it, and every seat would be
    `partial` for a reason about this file rather than about the CLI.
    """
    m = _TOKEN.search(" ".join([*spec.argv, spec.stdin or ""]))
    assert m, spec.argv
    return m.group(0)


def _fake_provider(spec, retries, timeout, backoff, workdir, *, env=None):
    """`engine.run_provider`'s signature, `env=` included, with no provider behind it.

    The signature MIRRORS the real one — a fake that omitted `env=` would let a launcher that
    drops the scrubbed environment pass this whole file.

    It WRITES INTO THE SEAT, because §7.1's change predicate is the `Fsetup -> Fwork` path set
    and a provider that left the tree alone would make every seat an argued zero-diff one.
    """
    token = _sentinel_of(spec)
    if _FAKE["refuse"] == spec.name:
        # Changed nothing AND signed off in two words, which is §8 rule 3's refusal: the seat
        # cannot be classified, `run_seat` records the attempt and raises, and `runner.run`
        # returns no `SeatResult` for it at all. That is the fleet shape `_seat_lines` must
        # count from the records rather than from the returned tuple.
        return {"status": "ok", "valid": True, "reason": "ok", "exit_code": 0,
                "duration_sec": 1.0, "attempts": 1, "structured": False,
                "result_text": f"ok\n{token}"}
    (Path(workdir) / "candidate.txt").write_text("fused\n")
    return {"status": "ok", "valid": True, "reason": "ok", "exit_code": 0,
            "duration_sec": 1.0, "attempts": 1, "structured": False,
            "result_text": f"{ANSWER}\n{token}"}


def _fake_probe(*, prompt, token, cli, bundle_sha256=None, model_requested=None,
                model_reported=None):
    """`fingerprint.build`'s signature with the two live measurements faked.

    The real one runs `<cli> --version` and reads the installed plugin closure; both are
    machine state, and one of them is a subprocess. `prompt_hashes` is NOT faked — it is what
    §11 actually compares, and `bundle_sha256` is carried through because it is the value this
    whole file exists to follow from `--start` to the seat's record.

    `cli_version` DIFFERS PER CLI, which is the real fleet's shape and not a convenience: three
    seats are three different CLIs, so §11's fleet-wide label for any run forge performs is
    `differently-prompted`. A fake answering one version for all three would make the label
    `not-comparable` — the same answer an enumeration that read no seat at all produces, which
    is exactly the collapse `cli._agreement` is written against.
    """
    return fingerprint.PromptIdentity(
        *fingerprint.prompt_hashes(prompt, token), bundle_sha256,
        "/usr/bin/" + cli, f"1.0-{cli}", model_requested, model_reported, "d" * 64)


def _a_fake_make_launcher(**kw):
    """`launch.make_launcher`'s keyword-only signature, with only the transport faked.

    DELEGATES TO THE REAL LAUNCHER rather than standing in for it: §8.1's validator, the
    sentinel composition and §11's fingerprint are all built there, and a stand-in would leave
    every assertion in this file about this function instead.
    """
    return cli.launch.make_launcher(**kw, run_provider=_fake_provider, probe=_fake_probe)


def _never(**kw):
    raise AssertionError(f"a launcher was built for a run that must have been refused: {kw}")


# --------------------------------------------------------------------------- fixtures

def _a_clean_repo(tmp_path, name="repo"):
    return make_repo(tmp_path, name)


def _a_repo_with_a_secret(tmp_path, name="secretrepo"):
    """A repository whose SELECTED untracked path holds a credential. §3 screens the
    selection, so the refusal needs both halves — the file and the `--select` naming it."""
    repo = make_repo(tmp_path, name)
    write(repo, "scratch/.env", "AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLEKEY\n")
    return repo


def _a_task(tmp_path, body="Rename the helper and keep its tests green.\n", name="task"):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "TASK.md").write_text(body)
    return root


def _answers(tmp_path, name="answers.json", **kw):
    """§5 step 2's sheet. No `ultrareview` key: that decision is `--no-ultra`'s, and the CLI
    refuses a sheet that carries a second spelling of it."""
    path = tmp_path / name
    path.write_text(json.dumps({
        "setup": [["true"]], "verify": [["true"]],
        "on_calibration_failure": "abort", "strategy": "size-gated",
        "author": ["Ada Lovelace", "ada@example.invalid"], **kw}))
    return path


def _skipped_ultra():
    return ultra.Ultra(ultra.SKIPPED, None, None, None, False,
                       "a test stood in for §13.1 and requested nothing")


@pytest.fixture(autouse=True)
def _no_cloud_review(monkeypatch):
    """§13.1's ONE PAID CALL, neutralised for every test in this file whether it remembers or
    not.

    MEASURED, AND IT WAS SPENDING. `ultra.run_ultra` shells out to `claude ultrareview` through
    its `run=subprocess.run` default, and `claude` resolves on this machine; two `--collect`
    tests here reached that call with a one-file diff — comfortably inside §13.1's limits —
    because the run they collected was confirmed with §13.1 ON, which is the default. A cloud
    review of a fixture repository was requested against the user's own credits by a suite whose
    first line says nothing here spends money.

    AUTOUSE RATHER THAN A LINE IN `_drive_a_start`, because the spend is on the `--collect`
    side: a test that drives no start at all can still reach it, so the guard cannot live in the
    start helper. A test that wants to OBSERVE the call patches this same attribute again and
    wins, since its `setattr` runs after the fixture's.
    """
    monkeypatch.setattr(cli.ultra, "run_ultra", lambda *a, **kw: _skipped_ultra())


def test_the_cloud_review_this_file_neutralises_is_a_real_subprocess():
    """NON-VACUITY for the fixture above: it is only a guard if the thing it replaces spends.
    Read off the signature, because the alternative is running the review to find out."""
    import inspect as pyinspect  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    default = pyinspect.signature(_REAL_RUN_ULTRA).parameters["run"].default
    assert default is subprocess.run, \
        "§13.1's pass no longer shells out, so this file's autouse guard guards nothing"
    assert ultra.argv(timeout_minutes=30, target=None)[0] == "claude", \
        "and the program it shells out to is a provider CLI"


def _drive_a_start(tmp_path, monkeypatch, *, no_ultra=False, refuse_seat=None,
                   make_launcher=_a_fake_make_launcher):
    """§5's gate and §7's fleet, with nothing paid and nothing written outside tmp_path.

    THE NEUTRALISATIONS ARE NOT OPTIONAL AND THIS IS WHY THE FIXTURE IS REQUIRED RATHER THAN
    DEFAULTED. Without the first, `storage.run_root` writes run directories under the
    developer's real `~/.local/state` and every test run leaks one. Without the second, three
    real CLIs are launched and the suite spends money. Without the third, the result depends on
    which plugins happen to be installed on the machine running it.

    THE FOURTH IS TASK 9's ENTRY AND NOT A NEUTRALISATION OF THIS CODE. `council.engine`'s
    table has no `forge` window in this build, so `--start` refuses — deliberately, since §19
    forbids inventing one. `test_no_seat_window_is_invented_when_the_engine_names_none` is that
    refusal; every test that needs a run past it supplies the entry the engine will carry.

    `--attempts 1` because §8.1's retry budget is priced per seat and every attempt is a real
    clone: the refused seat below would otherwise cost three of them for a property none of
    them measures.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_closures",
                        lambda: {"claude": "a", "codex": "a", "agy": "a"})
    monkeypatch.setitem(engine.MODE_TIMEOUT, "forge", _FORGE_TIMEOUT)
    monkeypatch.setitem(_FAKE, "refuse", refuse_seat)

    repo = _a_clean_repo(tmp_path)
    argv = ["--start", "--repo", str(repo), "--task", str(_a_task(tmp_path)),
            "--answers", str(_answers(tmp_path)), "--attempts", "1"]
    if no_ultra:
        argv.append("--no-ultra")
    buf = io.StringIO()
    rc = cli.main(argv, out=buf, make_launcher=make_launcher)
    assert rc == 0, buf.getvalue()
    m = re.search(r"^run: (\S+)$", buf.getvalue(), re.M)
    assert m, buf.getvalue()
    return storage.run_root(repo, m.group(1), must_be_new=False)


def _run_id(run_dir) -> str:
    return runstate.read_manifest(run_dir).run_id


def _repo_of(run_dir) -> str:
    return runstate.read_manifest(run_dir).repo_path


def _fuse_something(run_dir):
    """A commit in the synthesis worktree, which is what the orchestrator does between
    `--start` and `--collect`. Without one the worktree's tree IS B1's, and `mergeability`
    refuses to describe a delivery nobody made."""
    synth = Path(run_dir) / handover.SYNTHESIS
    (synth / "fused.txt").write_text("the fusion\n")
    _git(synth, "add", "-A")
    _git(synth, "commit", "-q", "-m", "fuse")
    return synth


def _collect_text(tmp_path, run_dir, *argv) -> str:
    _fuse_something(run_dir)
    buf = io.StringIO()
    rc = cli.main(["--collect", _run_id(run_dir), "--repo", _repo_of(run_dir), "--accept",
                   *argv], out=buf)
    assert rc == 0, buf.getvalue()
    return buf.getvalue()


# --------------------------------------------------------------------------- the parser

def test_the_parser_offers_the_four_verbs_the_spec_names(capsys):
    """--start / --collect / --gc / --no-ultra. An absence here would be found by an operator
    rather than by this suite."""
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for flag in ("--start", "--collect", "--gc", "--no-ultra"):
        assert flag in out, flag


def test_gc_refuses_a_run_it_cannot_identify_as_a_sentence_rather_than_a_traceback(
        tmp_path, capsys, monkeypatch):
    """§15's refusals are `gc.GcError`, and `main`'s narrow except tuple is what turns one into
    the line an operator reads. A class missing from that tuple leaves a traceback where a
    sentence belongs — which is the state this flag was in when it was advertised and unbuilt.

    THE STATE DIRECTORY IS ASSERTED because `storage.run_root` CREATES: a `--gc` on an id
    nothing ever opened must not leave the directory the next `--gc all` reports as a run.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = _a_clean_repo(tmp_path)
    rc = cli.main(["--gc", "abc123", "--repo", str(repo)])
    assert rc != 0
    assert "no manifest" in capsys.readouterr().out
    assert storage.run_dirs(repo) == ()


def test_gc_all_over_a_repository_with_no_runs_says_so(tmp_path, capsys, monkeypatch):
    """"No runs" and "this walk could not look" are different answers; `gc.usage` raises for
    the second, so the reassurance below is only ever printed over a state directory that was
    read."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = _a_clean_repo(tmp_path)
    assert cli.main(["--gc", "all", "--repo", str(repo)]) == 0
    assert "no forge runs are on disk" in capsys.readouterr().out


def test_gc_all_prints_three_marks_because_there_are_three_licence_states(tmp_path, capsys,
                                                                          monkeypatch):
    """`handed_over is None` is a record this engine could not read. Printing it as "NOT handed
    over" tells an operator that a delivery they made is unfinished work, which is the one
    sentence that gets a deliverable deleted — and the total says in words that it excludes
    every run whose size is unknown."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = _a_clean_repo(tmp_path)
    for run_id, record in (("aaaaaa", None), ("bbbbbb", "handed"), ("cccccc", b"{ not json")):
        run_dir = storage.run_root(repo, run_id)
        storage.manifest_path(run_dir).write_bytes(b"{}")
        if record == "handed":
            handover.write_handover(run_dir, handover.Handover(
                run_id=run_id, branch=handover.branch(run_id, handover.SYNTHESIS),
                kind=handover.PATCH_ONLY, handover_target=None, accepted=True,
                out_of_band=(), baseline_owned=(), b1_files=(), why="took the patch"))
        elif record is not None:
            storage.handover_path(run_dir).write_bytes(record)

    assert cli.main(["--gc", "all", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "aaaaaa" in out and "NOT handed over" in out
    assert re.search(r"bbbbbb.*  handed over", out), out
    assert re.search(r"cccccc.*UNREADABLE", out), out
    assert "total:" in out


# --------------------------------------------------------------------------- refusals

def test_a_repository_preflight_refuses_costs_nothing_and_exits_non_zero(tmp_path, capsys,
                                                                         monkeypatch):
    """A refusal must not read as a run. Four assertions, because 'it printed something' is
    not one of them: the exit code, the absence of a run directory, that no launcher was ever
    built — structurally, through a `make_launcher` that raises — and that the operator was
    never shown a PRICE for a run that cannot happen.

    THE LAST ONE IS WHAT MAKES THIS A TEST OF `start`'s OWN GUARD. Measured: with `if blocked:`
    replaced by `if False:` the first three assertions all still hold, because `gate.open_run`
    re-reads `preflight.refusals` and raises before it creates anything — so the run still
    exits non-zero, still writes no manifest and still builds no launcher, and this test went
    green over a deleted guard by passing against a different one. What the guard here actually
    buys is everything BEFORE that raise: §20's bundle scan, the three plugin closures,
    `must_show`'s gate-surface resolution and the whole §5.2 quote, printed to an operator who
    is then told the run cannot happen. So the quote's own first line is the discriminator.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_closures", lambda: {"claude": "a", "codex": "a", "agy": "a"})
    monkeypatch.setitem(engine.MODE_TIMEOUT, "forge", _FORGE_TIMEOUT)
    repo = _a_repo_with_a_secret(tmp_path)
    rc = cli.main(["--start", "--repo", str(repo), "--task", str(_a_task(tmp_path)),
                   "--answers", str(_answers(tmp_path)), "--select", "scratch"],
                  make_launcher=_never)
    assert rc != 0
    err = capsys.readouterr().out
    assert "refuse" in err.lower()
    assert "scratch/.env" in err, "the refusal names the path it read"
    assert not list((tmp_path / "state").rglob("manifest.json"))
    assert "provider calls:" not in err, \
        "the operator was quoted a price for a run preflight had already refused"


def test_a_task_naming_provider_specific_machinery_is_refused_before_the_gate(tmp_path,
                                                                              capsys,
                                                                              monkeypatch):
    """§20's refusal has a home and this is its first caller."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_closures", lambda: {"claude": "a", "codex": "a", "agy": "a"})
    monkeypatch.setitem(engine.MODE_TIMEOUT, "forge", _FORGE_TIMEOUT)
    repo = _a_clean_repo(tmp_path)
    task = _a_task(tmp_path, body="Dispatch a subagent with subagent_type: Explore.")
    rc = cli.main(["--start", "--repo", str(repo), "--task", str(task),
                   "--answers", str(_answers(tmp_path))], make_launcher=_never)
    assert rc != 0
    assert "not portable" in capsys.readouterr().out
    assert not list((tmp_path / "state").rglob("manifest.json"))


def test_no_seat_window_is_invented_when_the_engine_names_none(tmp_path, capsys, monkeypatch):
    """§19 forbids a second timeout mechanism, so a missing `MODE_TIMEOUT["forge"]` is a
    REFUSAL rather than a fallback to `deep`'s 1200s review window — which would put a
    forty-minute builder seat under a twenty-minute cap.

    `delitem(..., raising=False)` rather than a plain `delitem`: the engine carries the entry
    now, but this test is about what happens without one, and it must go on saying that in a
    build where the entry was never added or has been rolled back.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_closures", lambda: {"claude": "a", "codex": "a", "agy": "a"})
    monkeypatch.delitem(engine.MODE_TIMEOUT, "forge", raising=False)
    rc = cli.main(["--start", "--repo", str(_a_clean_repo(tmp_path)),
                   "--task", str(_a_task(tmp_path)),
                   "--answers", str(_answers(tmp_path))], make_launcher=_never)
    assert rc != 0
    out = capsys.readouterr().out
    assert "MODE_TIMEOUT" in out and "forge" in out
    assert not (tmp_path / "state").exists(), "a run that cannot start left something behind"


def test_the_answer_sheet_may_not_carry_a_second_spelling_of_no_ultra(tmp_path, capsys,
                                                                      monkeypatch):
    """The decision moves three scalars on the quote AND is spent an hour later in another
    process. A sheet carrying its own copy beside the flag is the two readings §5 step 5
    forbids, arriving before `gate.confirm` can compare them."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_closures", lambda: {"claude": "a", "codex": "a", "agy": "a"})
    monkeypatch.setitem(engine.MODE_TIMEOUT, "forge", _FORGE_TIMEOUT)
    rc = cli.main(["--start", "--repo", str(_a_clean_repo(tmp_path)),
                   "--task", str(_a_task(tmp_path)),
                   "--answers", str(_answers(tmp_path, ultrareview=True))],
                  make_launcher=_never)
    assert rc != 0
    assert "ultrareview" in capsys.readouterr().out


# --------------------------------------------------------------------------- the seam

def test_start_records_the_task_bundle_and_hands_the_launcher_its_hash(tmp_path, monkeypatch):
    """The debt three plans deferred: make_launcher/forge_spec had no production caller, so
    §8.1's validator never ran outside the suite. This asserts the CLI builds the launcher and
    that the hash it passes is taskbundle.bundle_hash over the run's OWN recorded bundle.

    The spy wraps the REAL make_launcher with fake transport, so the seam under test is the
    CLI's argument list and not a stand-in for it."""
    seen = {}
    real = cli.launch.make_launcher

    def spy(**kw):
        seen.update(kw)
        return real(**kw, run_provider=_fake_provider, probe=_fake_probe)
    monkeypatch.setattr(cli.launch, "make_launcher", spy)
    run_dir = _drive_a_start(tmp_path, monkeypatch, make_launcher=None)   # resolve via module
    b = taskbundle.read_task_bundle(run_dir)
    assert seen["bundle_sha256"] == taskbundle.bundle_hash(b)
    assert seen["bundle_sha256"] is not None
    assert seen["timeout"] == _FORGE_TIMEOUT, \
        "§19's window comes off the engine's own table and from nowhere else"


def test_every_seats_recorded_fingerprint_carries_the_bundle_hash(tmp_path, monkeypatch):
    """§11's bundle_sha256 was None for every real seat until §20 had a producer."""
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    b = taskbundle.bundle_hash(taskbundle.read_task_bundle(run_dir))
    names = storage.seat_names(run_dir)
    assert len(names) == 3, names
    for name in names:
        rec = runstate.read_seat(run_dir, name)
        for attempt in rec["attempts"]:
            pi = attempt.get("prompt_identity") or {}
            assert pi.get("bundle_sha256") == b, (name, pi)


def test_the_production_launcher_installs_section_8_1s_validator():
    """Three plans deferred this: make_launcher/forge_spec had no production caller, so the
    validator that stops a seat being re-run in the same cwd on top of its own half-finished
    work had never run outside the test suite. The CLI is that caller — asserted through the
    real `make_launcher`, with only the transport faked."""
    seen = {}

    def fake_run_provider(spec, retries, timeout, backoff, workdir, *, env=None):
        seen["validator"] = spec.validator
        seen["min_chars"] = spec.min_chars
        seen["cwd"] = spec.cwd
        return {"valid": True, "result_text": "ok", "reason": "ok", "exit_code": 0}

    fn = cli.launch.make_launcher(prompt="do it", timeout=3600, bundle_sha256="a" * 64,
                                  run_provider=fake_run_provider, probe=_fake_probe)
    fn(name="claude", seat_path=".", token="SENTINEL-000000000000", env={})
    assert seen["validator"] is seatmod._forge_validator
    assert seen["min_chars"] == 0
    assert seen["cwd"] == "."


def test_the_cli_reaches_the_launcher_only_through_the_injected_seam():
    """NON-VACUITY for every test above: they all drive `--start` without paying a provider,
    and that is only a property of this code if the code has one door. Read off the source,
    because the alternative is launching three real CLIs to find out."""
    src = (ROOT / "shared" / "lib" / "forge" / "cli.py").read_text(encoding="utf-8")
    # PARSED, not grepped: the seam is argued at length in `start`'s own docstring, and a text
    # count reads that prose as a second call site — which would make this test fail for the
    # documentation and pass for a module that had removed it.
    reached = [n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.Attribute) and n.attr == "make_launcher"
               and isinstance(n.value, ast.Name) and n.value.id == "launch"]
    assert len(reached) == 1, \
        f"a second reference is a second door, and a test can only hold one shut: {reached}"
    assert "mk = make_launcher or launch.make_launcher" in src, \
        "resolved at CALL time: a def-time default ignores a monkeypatched module attribute"


# --------------------------------------------------------------------------- --collect

def test_collect_builds_its_seat_lines_from_the_records_and_not_from_a_results_tuple(
        tmp_path, monkeypatch):
    """`runner.run` returns one SeatResult per seat that produced one, which is not always
    `manifest.seats` of them — a seat every attempt of which was refused has no verdict to
    return. Counting the tuple reports '2 of 2 seats completed' for a three-seat run."""
    run_dir = _drive_a_start(tmp_path, monkeypatch, refuse_seat="agy")
    text = _collect_text(tmp_path, run_dir)
    assert "of 3 seats completed" in text
    assert "of 2 seats" not in text


def test_collect_refuses_a_run_directory_it_cannot_read_whole(tmp_path, monkeypatch, capsys):
    """MEASURED: `runstate.reconstruct` raises `runstate.ManifestError`, `runstate.StateError`,
    `journal.JournalError` or `storage.StorageError` — four direct RuntimeError subclasses, and
    not one of them was in `main`'s first draft of the narrow except tuple. As drafted this
    test could not pass: `cli.main` would propagate the raise and the test would ERROR rather
    than read a return code. The tuple carries all four; the narrow-except argument survives,
    because each is this package refusing a run whose state is unknown."""
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    run_id, repo = _run_id(run_dir), _repo_of(run_dir)
    storage.manifest_path(run_dir).write_bytes(b"{ not json")
    rc = cli.main(["--collect", run_id, "--repo", repo])
    assert rc != 0
    out = capsys.readouterr().out.lower()
    assert "collected" not in out
    assert "manifest" in out, "the refusal did not name what could not be read"


def test_collect_on_a_run_that_does_not_exist_refuses_and_leaves_no_directory(tmp_path,
                                                                             monkeypatch,
                                                                             capsys):
    """`storage.run_root(must_be_new=False)` CREATES, so a mistyped id would otherwise leave an
    empty run directory behind for §15's walk to find and report as a run."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = cli.main(["--collect", "beefed", "--repo", str(_a_clean_repo(tmp_path))])
    assert rc != 0
    assert "no run" in capsys.readouterr().out
    assert not list((tmp_path / "state").glob("khenrix-forge/*")), \
        "a collect that found nothing left a directory that looks like a run"


def test_collect_refuses_a_synthesis_worktree_nobody_fused_in(tmp_path, monkeypatch, capsys):
    """§16's first reading: a synthesis tree equal to B1's tracked tree is a worktree nobody
    fused into, and describing it would report a merge-ready branch over an empty delivery —
    a run in which NOTHING happened leaving the same record as one in which NOBODY did.

    AND IT REFUSES BEFORE §13.1 IS ASKED FOR. Measured: with the cloud review taken first — as
    this function was ordered when it was written — a run with nothing to hand over paid $5-25
    to be told so. Every refusal `--collect` can reach off the disk now comes before the one
    call that cannot be taken back, and this is the assertion that holds that order.
    """
    calls = []
    monkeypatch.setattr(cli.ultra, "run_ultra",
                        lambda *a, **kw: calls.append(kw) or _skipped_ultra())
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    rc = cli.main(["--collect", _run_id(run_dir), "--repo", _repo_of(run_dir), "--accept"])
    assert rc != 0
    assert "nothing was fused into it" in capsys.readouterr().out
    assert not calls, "§13.1's review was requested for a run with nothing to hand over"


def test_no_ultra_reaches_run_ultra_as_well_as_the_quote(tmp_path, monkeypatch):
    """Two spellings of one flag, in two processes. The answer is recorded at the gate and
    read back here; this asserts the READ, not the write."""
    calls = []
    monkeypatch.setattr(cli.ultra, "run_ultra",
                        lambda *a, **kw: calls.append(kw) or _skipped_ultra())
    run_dir = _drive_a_start(tmp_path, monkeypatch, no_ultra=True)
    _collect_text(tmp_path, run_dir)
    assert calls and calls[-1]["enabled"] is False


def test_the_ultrareview_answer_is_read_off_the_record_and_not_off_this_processs_default(
        tmp_path, monkeypatch):
    """The discrimination check for the test above: a `--collect` that hardcoded `enabled` —
    in either direction — would pass one of the two runs. `--collect` is never given
    `--no-ultra`, so the only source is the journalled answer."""
    calls = []
    monkeypatch.setattr(cli.ultra, "run_ultra",
                        lambda *a, **kw: calls.append(kw) or _skipped_ultra())
    run_dir = _drive_a_start(tmp_path, monkeypatch, no_ultra=False)
    _collect_text(tmp_path, run_dir)
    assert calls and calls[-1]["enabled"] is True


def test_a_run_that_cannot_say_what_it_priced_is_not_charged_for_a_cloud_review(
        tmp_path, monkeypatch, capsys):
    """`_confirmed_ultrareview` fails closed. §13.1 is priced in usage credits, so a run whose
    record cannot say ON from OFF may not be collected on this process's default.

    THE VALUE IS REWRITTEN RATHER THAN THE RECORD REMOVED, and the difference is what this
    test can see. Dropping the line renumbers the journal, so `journal.JournalError` refuses
    first and the refusal under test never runs — a green test about a different guard. `"yes"`
    is the shape a boolean-ish config parse or a hand-edited record leaves behind, and it is
    true in Python, so it is the value that would silently buy the review.
    """
    called = []
    monkeypatch.setattr(cli.ultra, "run_ultra",
                        lambda *a, **kw: called.append(kw) or _skipped_ultra())
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    _fuse_something(run_dir)
    log = storage.journal_path(run_dir)
    raw = log.read_bytes()
    assert b'"ultrareview": true' in raw, raw
    log.write_bytes(raw.replace(b'"ultrareview": true', b'"ultrareview": "yes"'))
    rc = cli.main(["--collect", _run_id(run_dir), "--repo", _repo_of(run_dir), "--accept"])
    assert rc != 0
    assert "ultrareview='yes'" in capsys.readouterr().out
    assert not called, "a review was requested for a run that cannot say it agreed to one"


@pytest.mark.parametrize("flag,value", [
    # §5 step 2's answer sheet takes `gate.STRATEGY_RULES` — `size-gated`, `fusion`,
    # `base-and-port` — and this flag reports what the fusion FOLLOWED, which is
    # `strategy.STRATEGIES` (`from_scratch`, `partition`, `base_and_port`). The two vocabularies
    # overlap in meaning and in NO spelling, so the words an operator most plausibly carries
    # over from the `--start` they ran an hour earlier are exactly the ones `--collect` refuses.
    ("--strategy", "fusion"),
    ("--strategy", "base-and-port"),
    ("--synthesis-outcome", "PASSED"),
])
def test_a_mistyped_collect_flag_is_refused_before_the_cloud_review_is_paid_for(
        flag, value, tmp_path, monkeypatch, capsys):
    """The sibling of the ordering two functions up, one validation over.

    `--strategy` and `--synthesis-outcome` are checked by `handover.Provenance.__post_init__`,
    and that record used to be built AFTER §13.1's pass — so an operator who mistyped either
    word paid $5-25 for a cloud review and was then told the word is not in the vocabulary. The
    assertion that matters is `not calls`: a test that only read the return code passes on the
    ordering this one exists to refuse.
    """
    calls = []
    monkeypatch.setattr(cli.ultra, "run_ultra",
                        lambda *a, **kw: calls.append(kw) or _skipped_ultra())
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    _fuse_something(run_dir)
    rc = cli.main(["--collect", _run_id(run_dir), "--repo", _repo_of(run_dir), "--accept",
                   flag, value])
    assert rc != 0
    out = capsys.readouterr().out
    assert value in out, f"the refusal did not quote the word it refused: {out!r}"
    assert not calls, "a mistyped flag bought a cloud review before it was refused"


def test_collect_validates_its_whole_provenance_record_before_the_call_that_spends(tmp_path):
    """THE GENERAL FORM OF THE TEST ABOVE, because the two flags are not two checks.

    Every rule `Provenance.__post_init__` states is over a value `collect` already holds when
    it has finished reading the disk — the two argv words, the seat rows, the confirmed verify
    command, §11's agreement label, §13's terminal and its counts — and the ONLY one that needs
    §13.1's result is the type check on `ultra`. Hoisting the two flag checks alone would leave
    the other dozen behind the one call that cannot be taken back, which is the same defect
    this test's parametrized sibling names, found again a week later.

    Read off the SOURCE, in this file's own precedent: a behavioural test can drive one refusal
    per fixture, and what is at issue is where the construction sits rather than which rule
    fires. The alternative is a fixture per rule and a paid review whenever one is missed.
    """
    src = (ROOT / "shared" / "lib" / "forge" / "cli.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "collect")
    spends = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and n.func.attr == "run_ultra"]
    records = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute) and n.func.attr == "Provenance"]
    assert len(spends) == 1, f"§13.1 is asked for once, not {len(spends)} time(s): {spends}"
    assert records, "collect no longer builds the record this test is about"
    assert max(records) < min(spends), (
        f"a Provenance is constructed at line(s) {records} and §13.1's paid pass is asked for "
        f"at line {spends[0]} — every refusal that record can make costs nothing, so all of "
        "them belong in front of the spend")


def test_a_refused_collect_leaves_no_handover_record_for_gc_to_delete_the_run_on(
        tmp_path, monkeypatch, capsys):
    """§15's refusal to reclaim an uncollected run, defeated by the collect that refused it.

    `handover.json` IS §15's LICENCE and `--collect` used to write it before it had validated
    the record, so a run refused for a mistyped word was left claiming a delivery nobody made.
    MEASURED against the code this test was written for: the collect below exits 1,
    `read_handover` then answers non-None, `gc.usage` reports the run handed over, and `--gc`
    with NO `--force` removes SEVEN paths — the synthesis worktree, its branch, all four
    `refs/khenrix-forge/` refs and the run directory holding every seat record. The one thing
    in this engine that deletes, acting on a claim a refusal wrote.

    THE `--gc` ASSERTION IS THE ONE THAT MATTERS. A test that stopped at `read_handover` says
    the file is absent; what an operator loses here is the run's evidence, and the only thing
    that can say it is safe is the walk that would have deleted it.
    """
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    _fuse_something(run_dir)
    run_id, repo = _run_id(run_dir), _repo_of(run_dir)
    assert cli.main(["--collect", run_id, "--repo", repo, "--accept",
                     "--strategy", "fusion"]) != 0
    assert handover.read_handover(run_dir) is None, \
        "a refused collect wrote the record §15 reads as its licence to delete this run"
    assert [r.handed_over for r in cli.gcmod.usage(Path(repo))] == [False]
    assert cli.main(["--gc", run_id, "--repo", repo]) != 0
    assert "not marked handed over" in capsys.readouterr().out
    assert (Path(run_dir) / handover.SYNTHESIS).exists() and run_dir.exists(), \
        "the run this collect refused was reclaimed without --force"


def test_collect_persists_the_handover_only_below_every_refusal_it_can_make():
    """THE GENERAL FORM of the two tests around it, read off the source on the precedent of
    `test_collect_validates_its_whole_provenance_record_before_the_call_that_spends`.

    A fixture can drive one refusal at a time, and what is at issue is where the WRITE sits:
    every statement above it can refuse, and the record is §15's licence to delete the run. The
    rendering is inside the ordering rather than beside it — `handover.text` refuses an
    `ultra.STATUSES` member that grew no line in the header — so a write hoisted one statement
    up would be back to persisting a claim this function has not finished making.
    """
    src = (ROOT / "shared" / "lib" / "forge" / "cli.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "collect")

    def called_at(attr):
        return [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == attr]

    writes = called_at("write_handover")
    assert len(writes) == 1, f"the handover is persisted once, not {len(writes)} time(s)"
    for attr in ("Provenance", "run_ultra", "text"):
        lines = called_at(attr)
        assert lines, f"collect no longer calls {attr}, so this ordering is about nothing"
        assert writes[0] > max(lines), (
            f"the handover record is written at line {writes[0]}, above `{attr}` at "
            f"{lines} — everything this function can refuse belongs above the one file §15 "
            "reads as its licence to delete the run")


def test_the_collect_that_succeeds_writes_the_record_gc_reads_as_its_licence(tmp_path,
                                                                             monkeypatch):
    """THE DISCRIMINATION CHECK for the test above, which a `--collect` that wrote no record at
    all would also pass. §15 then refuses every run the operator DID hand over, and `--force` —
    the flag that waives the licence question entirely — becomes the one they type routinely,
    over runs they have not read."""
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    _collect_text(tmp_path, run_dir)
    recorded = handover.read_handover(run_dir)
    assert recorded is not None and recorded.run_id == _run_id(run_dir)
    assert cli.main(["--gc", _run_id(run_dir), "--repo", _repo_of(run_dir)]) == 0
    assert not run_dir.exists()


def test_the_handover_reports_the_review_this_run_got_and_not_the_pre_spend_placeholder(
        tmp_path, monkeypatch):
    """The cost of validating the record before the spend: it is built carrying a placeholder
    `Ultra`, and `dataclasses.replace` is the only thing that puts §13.1's real result in its
    place. Without that call the handover says the cloud review was never requested over a run
    that paid for one — this package's usual overclaim printed backwards, and just as wrong.

    MEASURED with `scripts/mutate.py`: replacing `dataclasses.replace(p, ultra=u)` with `p`
    left `test_forge_cli.py` and `test_forge_handover.py` both green (79 passed, SURVIVED), so
    nothing else in the suite holds the placeholder in.
    """
    ran = ultra.Ultra(ultra.RAN, None, (), None, True, "a test stood in for §13.1 and ran")
    monkeypatch.setattr(cli.ultra, "run_ultra", lambda *a, **kw: ran)
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    text = _collect_text(tmp_path, run_dir)
    assert "Ultrareview: 0 finding(s) reported" in text
    assert cli._UNREQUESTED.detail not in text
    assert "not requested" not in text, \
        "the header describes a review nobody asked for over a run that got one"


def test_the_handover_a_collect_prints_does_not_call_an_asserted_verdict_verified(
        tmp_path, monkeypatch, capsys):
    """`--collect --synthesis-outcome PASS` renders a verdict THIS ENGINE DID NOT MEASURE. It
    builds no verifier clone for the fusion and runs no confirmed command over it, so the
    §16.1 "Verified here means" paragraph may not appear beside it."""
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    _fuse_something(run_dir)          # a commit in the synthesis worktree
    rc = cli.main(["--collect", _run_id(run_dir), "--repo", _repo_of(run_dir),
                   "--accept", "--synthesis-outcome", "PASS", "--strategy", "from_scratch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert handover._VERIFIED_MEANS not in out
    assert "the orchestrator reports PASS" in out
    assert "this engine did not run it" in out
    assert handover._ASSERTED_MEANS in out


def test_there_is_no_flag_that_relabels_an_asserted_verdict_as_a_measured_one(tmp_path,
                                                                             capsys):
    """The overclaim at its source. `Provenance.synthesis_measured` is what decides which
    provenance paragraph prints, and `--collect` passes the constant `False`; a flag would be
    exactly how a measured label came to sit over a command nobody ran. A duration is the same
    claim one field over — `Provenance` refuses one beside an unmeasured verdict — so there is
    no flag for that either."""
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "--synthesis-measured" not in out
    assert "--synthesis-seconds" not in out
    src = (ROOT / "shared" / "lib" / "forge" / "cli.py").read_text(encoding="utf-8")
    assert "synthesis_measured=False," in src
    assert "synthesis_measured=True" not in src


def test_the_out_of_band_set_is_enumerated_rather_than_assumed_empty(tmp_path, monkeypatch):
    """`mergeability` and `out_of_band` both refuse `None` and both accept `()`, and the two
    are different facts. A `--collect` that passed `()` without looking would tell the user
    there is nothing to copy out of a tree holding an ignored artifact — and would call the
    delivery merge-ready while half of it stayed in a directory."""
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    synth = _fuse_something(run_dir)
    (synth / "build.log").write_text("out of band\n")
    buf = io.StringIO()
    rc = cli.main(["--collect", _run_id(run_dir), "--repo", _repo_of(run_dir), "--accept"],
                  out=buf)
    assert rc == 0, buf.getvalue()
    text = buf.getvalue()
    assert "build.log" in text and "MERGING THE BRANCH ALONE DOES NOT INSTALL THESE" in text
    assert "(patch)" in text, "an out-of-band artifact is not a merge-ready delivery"


def test_the_seat_table_a_collect_prints_names_every_seat_the_run_recorded(tmp_path,
                                                                           monkeypatch):
    """§16.1's first line counts rows, and `Provenance` refuses a duplicated or empty one. The
    denominator is `storage.seat_names`, so a seat whose record exists is counted whether or
    not it returned a verdict."""
    run_dir = _drive_a_start(tmp_path, monkeypatch, refuse_seat="agy")
    lines = cli._seat_lines(run_dir)
    assert sorted(s.name for s in lines) == ["agy", "claude", "codex"]
    by_name = {s.name: s for s in lines}
    assert by_name["agy"].forge == "failed", \
        "a seat no attempt of which could be classified is not a completed one"
    assert by_name["claude"].forge == "completed"
    # THE KEY NAMES ARE `runner._record`'s AND THIS IS WHERE THAT IS PINNED. `verification` is
    # written as `{"outcome", "reason"}` — a MAPPING, not the pair it is on a `SeatResult` — so
    # a reader that indexed it positionally would raise, and one that guessed a key would get
    # `None` and report §6's verdict as a gate that never ran, for a seat that passed.
    assert by_name["claude"].verify_outcome == "PASS"
    assert by_name["agy"].verify_outcome is None, \
        "a seat §6 never verified is not a seventh verdict"


def test_the_agreement_label_is_computed_over_every_seat_that_ran(tmp_path, monkeypatch):
    """§11's label. A seat with no recorded fingerprint makes it `not-comparable` rather than
    being dropped: a label over a smaller fleet than the one that ran, reported as the
    fleet's, is the fail-open this function is written against.

    THE FIRST ASSERTION NAMES THE LABEL RATHER THAN CHECKING MEMBERSHIP, because `in LABELS` is
    true of every answer including the one an enumeration that read NO seat gives. Measured:
    with the loop replaced by an empty one this test went green on `in LABELS`, and the two
    readings the function exists to keep apart — the fleet's real label and the label of a
    comparison nobody made — were spelled the same way.
    """
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    assert cli._agreement(run_dir) == "differently-prompted", \
        "three seats are three different CLIs, which is §11's 'labelled weaker'"
    storage.seat_state_path(run_dir, "claude").write_text(
        json.dumps({"name": "claude", "attempts": []}))
    assert cli._agreement(run_dir) == "not-comparable"


def test_a_run_with_no_review_round_reports_no_terminal_and_no_rounds(tmp_path, monkeypatch):
    """`manifest.review_rounds` is the BUDGET the operator agreed to pay for; §16.1's header
    reports what happened. `handover.Provenance` refuses a `None` terminal beside a non-zero
    round count — "a review nobody convened" carrying rounds — so passing the budget would
    fail every run whose review has not been implemented."""
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    assert runstate.read_manifest(run_dir).review_rounds == 2, "the budget is on the manifest"
    assert cli._rounds_run(run_dir) == 0
    assert cli._review_terminal(run_dir, 0, ()) is None
    assert cli._unresolved(run_dir, 0) == 0


def test_no_strongest_seat_is_named_over_a_rubric_this_run_never_measured(tmp_path,
                                                                          monkeypatch):
    """§12.5's answer is a `(seat | None, why)` pair and `None` is the ordinary one: ranking
    the seats this engine could read would turn "the strongest seat we were able to measure"
    into "the strongest seat"."""
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    name, why = cli._strongest(run_dir)
    assert name is None
    assert "ledger" in why and why.strip()


def test_an_out_of_band_path_this_engine_cannot_read_is_not_one_the_tree_does_not_hold(
        tmp_path, monkeypatch, capsys):
    """`verify._materialized_sidecar` answers `None` for anything a `SidecarEntry` cannot
    describe — missing, unreadable, or a shape with no payload — and dropping those would
    report a delivery with nothing to copy over a tree git had just named a path in. §16's
    second mergeability condition turns on the difference, so the refusal names them.

    A MODE-000 FILE AND NOT A FIFO, which is what this reached for first. Measured on git
    2.53.0: `git status --porcelain -uall` does NOT list a FIFO at all, so the fake shape never
    reached the reader and the test passed over an untouched branch; it DOES list an unreadable
    regular file as `??`, which is the case that gets that far.
    """
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    synth = _fuse_something(run_dir)
    locked = synth / "locked.bin"
    locked.write_bytes(b"payload")
    locked.chmod(0o000)
    if os.access(locked, os.R_OK):
        pytest.skip("this process can read a mode-000 file, so there is nothing to refuse")
    try:
        rc = cli.main(["--collect", _run_id(run_dir), "--repo", _repo_of(run_dir), "--accept"])
        assert rc != 0
        out = capsys.readouterr().out
        assert "locked.bin" in out and "could not be read" in out
    finally:
        locked.chmod(0o600)


def test_an_out_of_band_directory_is_refused_rather_than_read_as_one_path(tmp_path,
                                                                          monkeypatch):
    """THE BACKSTOP FOR THE FLAG PAIR, driven directly because git will not produce the shape
    while both flags are in place. Measured on git 2.53.0: `--untracked-files=all` plus
    `--ignored=traditional` expands an ignored directory into its files, and
    `--ignored=matching` collapses the same tree into one `ignoredir/` record. This drives the
    collapsed answer past the flags so the guard is exercised by something rather than trusted
    — the fail-open it stops is a handover reporting nothing to copy over a hundred files."""
    class _Answer:
        stdout = b"!! ignoredir/\0?? a.txt\0"

    monkeypatch.setattr(cli.gitcmd, "git", lambda *a, **kw: _Answer())
    with pytest.raises(cli.CliError) as e:
        cli._sidecars_of(tmp_path)
    assert "ignoredir/" in str(e.value) and "DIRECTORY" in str(e.value)


def test_an_ignored_file_in_the_synthesis_tree_is_named_per_file(tmp_path, monkeypatch):
    """The measurement the flag pair rests on, taken against real git rather than assumed: an
    ignored file two directories deep comes back as its own record, so §16's copy command names
    a path with a payload."""
    run_dir = _drive_a_start(tmp_path, monkeypatch)
    synth = _fuse_something(run_dir)
    (synth / ".gitignore").write_text("artifacts/\n")
    _git(synth, "add", ".gitignore")
    _git(synth, "commit", "-q", "-m", "ignore")
    (synth / "artifacts" / "deep").mkdir(parents=True)
    (synth / "artifacts" / "deep" / "out.bin").write_bytes(b"payload")
    assert [s.path for s in cli._sidecars_of(synth)] == ["artifacts/deep/out.bin"]


def test_the_selected_untracked_files_are_named_as_the_baselines_in_the_handover(tmp_path,
                                                                                monkeypatch):
    """§16 requires the B1 file list in the handover text rather than only at a confirmation
    gate an hour earlier, and requires the selected untracked/ignored files to be named as the
    USER's. Both come off `manifest.selected_paths`, which is what the gate recorded."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_closures", lambda: {"claude": "a", "codex": "a", "agy": "a"})
    monkeypatch.setitem(engine.MODE_TIMEOUT, "forge", _FORGE_TIMEOUT)
    monkeypatch.setitem(_FAKE, "refuse", None)
    repo = _a_clean_repo(tmp_path)
    write(repo, "scratch/notes.txt", "the user's own untracked notes\n")
    buf = io.StringIO()
    rc = cli.main(["--start", "--repo", str(repo), "--task", str(_a_task(tmp_path)),
                   "--answers", str(_answers(tmp_path)), "--attempts", "1",
                   "--select", "scratch/notes.txt"],
                  out=buf, make_launcher=_a_fake_make_launcher)
    assert rc == 0, buf.getvalue()
    run_dir = storage.run_root(repo, re.search(r"^run: (\S+)$", buf.getvalue(), re.M).group(1),
                               must_be_new=False)
    assert runstate.read_manifest(run_dir).selected_paths == ("scratch/notes.txt",)
    text = _collect_text(tmp_path, run_dir)
    body = text.split("B1 — the baseline this run started from:")[1]
    b1, owned = body.split("Baseline-owned")
    # TWO LISTS, TWO SOURCES. B1 is the tracked tree PLUS the selection, off the filesystem
    # manifest `baseline.materialize` recorded; baseline-owned is the selection alone, off the
    # run manifest. Taking both from `selected_paths` renders the same list twice and reports
    # "(no files)" as the baseline of every ordinary clean repository.
    assert "seed.txt" in b1 and "scratch/notes.txt" in b1
    assert "seed.txt" not in owned and "scratch/notes.txt" in owned
    assert "yours, not forge's" in text
