"""The gate runs where the builder never was, and the engine runs it (spec §6)."""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import pytest  # noqa: E402
from forge import baseline, bundle, fleet, harvest, inspect as finspect, verify  # noqa: E402
from forge_fixtures import git as _git, make_repo, write  # noqa: E402

IDENT = ("Forge Seat", "seat@forge.invalid")


def _candidate(tmp_path, repo, *, selected=(), setup=(), work=()):
    """Baseline -> seat -> four phases -> bundle, with `setup` written before Fsetup and
    `work` after it.

    The phase placement is the whole point and is why this helper takes two lists rather
    than one: `harvest.artifact_set` takes the artifact PATH set from Fsetup->Fwork, so
    setup's output is differenced out and only `work` has a channel into a verifier.
    """
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), list(selected), "r1")
    s = fleet.clone_seat(repo, b, tmp_path / "seat", name="claude", identity=IDENT)
    f0 = harvest.record(s.path)
    for rel, text in setup:
        write(s.path, rel, text)
    fsetup = harvest.record(s.path)
    for rel, text in work:
        write(s.path, rel, text)
    fwork = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=fwork), s.path, b.commit)
    return b, s, bundle.build(s.path, a, b)


def test_parse_rejects_shell_metacharacters():
    with pytest.raises(verify.VerifyError, match="shell") as e:
        verify.Command.parse(["make verify && rm -rf /"])
    # Naming the character is the requirement, not merely refusing: "invalid step" sends
    # the author looking at the wrong half of their config.
    assert "'&'" in str(e.value), f"the refusal did not name the character: {e.value}"
    c = verify.Command.parse([["make", "verify"]])
    assert c.steps[0].argv == ("make", "verify")


def test_parse_refuses_a_program_name_that_is_really_a_command_line():
    """The contract, stated where it can fail: a step IS an argv sequence, the FIRST token
    is a program name, and the tokens after it are arguments passed to that program.

    Whitespace in the program name is refused for the same reason a metacharacter is —
    nothing here runs a shell, so `["make verify"]` would be exec'd as one program of that
    literal name and the gate would fail for an infrastructure reason. Arguments are NOT
    scanned: they never reach a shell, and `grep -E 'a|b'` is an ordinary gate step.
    """
    with pytest.raises(verify.VerifyError, match="shell"):
        verify.Command.parse([["make verify"]])
    with pytest.raises(verify.VerifyError, match="shell"):
        verify.Command.parse([["make", "verify"], ["./run;rm"]])
    assert verify.Command.parse([["grep", "-E", "a|b", "f"]]).steps[0].argv[2] == "a|b"
    # A bare string with no metacharacter is still not a step. Accepting `"make"` as a
    # one-token argv would make `"pytest -q"` — no metacharacter, one space — a silent
    # ENOENT instead of a named refusal, so there is exactly one accepted shape.
    with pytest.raises(verify.VerifyError, match="argv"):
        verify.Command.parse(["make"])
    # Every other way a step can fail to be an argv list reports in this module's
    # vocabulary too, naming the step, rather than as a raw TypeError from `tuple()` or an
    # IndexError from inside subprocess several seconds into a run.
    with pytest.raises(verify.VerifyError, match="not a sequence"):
        verify.Command.parse([42])
    with pytest.raises(verify.VerifyError, match=r"step 1 names no program"):
        verify.Command.parse([["true"], []])
    with pytest.raises(verify.VerifyError, match="non-string"):
        verify.Command.parse([["make", 1]])
    with pytest.raises(verify.VerifyError, match="no program"):
        verify.Step(argv=())


def test_a_sabotaged_test_runner_does_not_cross_into_the_verifier(tmp_path):
    """The headline property: a check the builder could rig is not a check.

    The rig is SETUP's output — written before Fsetup — because that is the phase
    `harvest` differences out (its docstring names `.venv` by name). A rig the agent places
    in its OWN work crosses BY DESIGN: the candidate is the agent's edits, and a candidate
    that edits the gate is a thing to FLAG, which is `gate_delta`'s job — and `gate_delta`
    is None ("nobody looked"), not (), precisely so nothing reads it as a clean gate.
    """
    repo = make_repo(tmp_path)
    write(repo, "check.sh", "#!/bin/sh\nexit 1\n")
    (repo / "check.sh").chmod(0o755)
    b, _s, cb = _candidate(
        tmp_path, repo, selected=["check.sh"],
        setup=[(".venv/bin/pytest", "#!/bin/sh\nexit 0\n")],
        work=[("src.py", "the actual work\n")])

    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT)
    assert (v / "src.py").exists(), "the agent's work must cross"
    assert not (v / ".venv").exists(), "seat-only state must not cross"
    r = verify.run_command(v, verify.Command.parse([["./check.sh"]]))
    assert r.exit_code == 1, "the ORIGINAL gate ran, not whatever the seat left behind"


def test_run_command_reports_the_failing_step_index(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    c = verify.Command.parse([["true"], ["false"], ["true"]])
    r = verify.run_command(d, c)
    assert r.exit_code != 0 and r.step_index == 1
    # ...and a clean run points at the LAST step it ran, never at a step it skipped.
    ok = verify.run_command(d, verify.Command.parse([["true"], ["true"]]))
    assert ok.exit_code == 0 and ok.step_index == 1
    assert ok.duration_sec > 0, "duration_sec is a measurement, not a placeholder"


def test_a_program_that_cannot_be_started_is_a_verify_error(tmp_path):
    """A gate that cannot RUN is not a gate that FAILED, and the two must not arrive as the
    same value — `Run(exit_code=127)` would be read downstream as the candidate's verdict."""
    d = tmp_path / "w"
    d.mkdir()
    with pytest.raises(verify.VerifyError, match="could not be started"):
        verify.run_command(d, verify.Command.parse([["forge-no-such-program-xyz"]]))


def test_a_step_timeout_is_a_verify_error_not_a_hang(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    c = verify.Command(steps=(verify.Step(argv=("sleep", "30"), timeout=1),))
    with pytest.raises(verify.VerifyError, match="timeout"):
        verify.run_command(d, c)


def test_a_timeout_leaves_no_process_behind_it(tmp_path):
    """A leaked `sleep` is a leaked process, and `subprocess.run`'s own timeout handling
    kills only the DIRECT child — a gate is `make`, so the thing still running afterwards
    is the grandchild that was doing the work."""
    d = tmp_path / "w"
    d.mkdir()
    c = verify.Command(steps=(verify.Step(
        argv=("sh", "-c", "sleep 30 & echo $! > gc.pid; sleep 30"), timeout=1),))
    with pytest.raises(verify.VerifyError, match="timeout"):
        verify.run_command(d, c)
    pid = int((d / "gc.pid").read_text().strip())
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, 9)     # do not leak it out of the test either
    pytest.fail(f"grandchild {pid} outlived the step's timeout")


def test_a_gate_with_no_steps_is_refused_rather_than_passed(tmp_path):
    """exit_code 0 from a command that ran nothing is this module's cardinal sin."""
    d = tmp_path / "w"
    d.mkdir()
    with pytest.raises(verify.VerifyError, match="no steps"):
        verify.run_command(d, verify.Command.parse([]))


def test_the_verifier_clone_has_no_origin_and_its_own_identity(tmp_path):
    repo = make_repo(tmp_path)
    b, _s, cb = _candidate(tmp_path, repo)
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT)
    # Asked with an ORDINARY git, never `forge.gitcmd`: the property must survive a git
    # that has none of the engine's presets, which is the git a gate step would use.
    assert _git(v, "remote").stdout.strip() == "", "the verifier ships a push target"
    assert _git(v, "config", "--local", "--get", "user.email").stdout.strip() == IDENT[1]
    # A branch, not a detached HEAD: §7.2 has the engine CHECKPOINT admitted generator
    # output here, and work committed onto a detached HEAD is unreachable the moment
    # anything else moves.
    # The name is restated rather than read back from `verify.VERIFIER_NAME`, on
    # `test_forge_seams.py::_KNOWN_REDIRECTORS`' argument: an assertion phrased in terms of
    # the constant it is checking moves with the constant and pins nothing.
    assert _git(v, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "forge/r1/verify"


def test_the_gate_sees_none_of_the_users_global_git_config(tmp_path, monkeypatch):
    """A verdict must not depend on the developer's ~/.gitconfig — `core.autocrlf` alone
    changes what a gate's `git add` stores.

    `fetch.parallel` is the probe because the clone has no local value for it: a key the
    seat identity or the hooks pin already sets locally would be masked by the local win
    and the leak would go unseen.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[fetch]\n\tparallel = 7\n")
    monkeypatch.setenv("HOME", str(home))
    repo = make_repo(tmp_path)
    b, _s, cb = _candidate(tmp_path, repo)
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT)
    r = verify.run_command(v, verify.Command.parse(
        [["git", "config", "--get", "fetch.parallel"]]))
    assert r.stdout.strip() == "", \
        f"the user's global git config reached the gate: {r.stdout!r}"


def test_a_hook_in_the_verifiers_own_hooks_dir_does_not_run(tmp_path):
    repo = make_repo(tmp_path)
    b, _s, cb = _candidate(tmp_path, repo, work=[("src.py", "x\n")])
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT)
    hook = v / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\ntouch HOOK-RAN\nexit 1\n")
    hook.chmod(0o755)

    write(v, "later.txt", "later\n")
    r = verify.run_command(v, verify.Command.parse(
        [["git", "add", "later.txt"], ["git", "commit", "-m", "gate"]]))
    assert not (v / "HOOK-RAN").exists(), "a hook inside the verifier ran"
    assert r.exit_code == 0, f"the gate's own commit failed: {r.stderr}"


@pytest.mark.parametrize("injection", [
    # Not one of `gitcmd.REDIRECTING_ENV`, and it OUTRANKS the clone's local
    # core.hooksPath pin (measured, git 2.53). Not exotic either: git exports this into
    # every child whenever anything up the tree ran `git -c …`.
    {"GIT_CONFIG_PARAMETERS": "'core.hooksPath'='{hooks}'"},
    # This one IS in REDIRECTING_ENV. Kept as its own case so the two strips are
    # mutated apart: one loop covering both names is one loop that can lose either.
    {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
     "GIT_CONFIG_VALUE_0": "{hooks}"},
], ids=["git-config-parameters", "git-config-count"])
def test_an_ambient_git_config_injection_cannot_re_enable_hooks(
        tmp_path, monkeypatch, injection):
    repo = make_repo(tmp_path)
    hooks = tmp_path / "ambient-hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\ntouch HOOK-RAN\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    b, _s, cb = _candidate(tmp_path, repo, work=[("src.py", "x\n")])
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT)

    for k, tmpl in injection.items():
        monkeypatch.setenv(k, tmpl.format(hooks=hooks))
    write(v, "later.txt", "later\n")
    r = verify.run_command(v, verify.Command.parse(
        [["git", "add", "later.txt"], ["git", "commit", "-m", "gate"]]))
    assert not (v / "HOOK-RAN").exists(), "an ambient config injection re-enabled hooks"
    assert r.exit_code == 0, f"the gate's own commit failed: {r.stderr}"


def test_a_candidate_that_rewrites_the_clone_config_is_refused(tmp_path):
    """MEASURED against `bundle.materialize` at 3ac3784: `_safe_rel` refuses an absolute or
    `..` sidecar path but says nothing about `.git/…`, so a sidecar named `.git/config` is
    written straight over the clone's config — taking the hooks pin, `core.fsmonitor` and
    `core.sshCommand` (both of which name a command git EXECUTES) and the clone's identity
    with it.

    `bundle.build` cannot emit one today (`snapshot.take` skips `.git`), but bundle.py's
    own guards are written for a bundle DESERIALIZED from a ledger, where sidecars are
    input. So the pin is read back after materialization and a rewritten config is
    REFUSED rather than repaired: overwriting just `core.hooksPath` would leave every other
    builder-chosen key in place.
    """
    repo = make_repo(tmp_path)
    b, _s, cb = _candidate(tmp_path, repo)
    hostile = bundle.CandidateBundle(
        version=cb.version, baseline_ref=cb.baseline_ref, baseline_commit=cb.baseline_commit,
        sidecars=(bundle.SidecarEntry(".git/config", "file", 0o644,
                                      b"[core]\n\thooksPath = /tmp/rigged\n"),))
    with pytest.raises(verify.VerifyError, match="config"):
        verify.build_verifier(repo, b, hostile, tmp_path / "verifier", identity=IDENT)


def test_an_injected_hooks_path_cannot_answer_for_the_clones_own(tmp_path, monkeypatch):
    """The canary is read with `--local`, never a bare `--get`.

    MEASURED, git 2.53, in a clone whose local file has no core.hooksPath:

        GIT_CONFIG_PARAMETERS="'core.hooksPath'='/dev/null'" git config --get  -> /dev/null
        ...the same environment,                             --local --get     -> unset

    `gitcmd` pins GIT_CONFIG_GLOBAL/SYSTEM at /dev/null, so a ~/.gitconfig cannot reach the
    readback — but GIT_CONFIG_PARAMETERS is not one of its REDIRECTING_ENV names, and it
    enters at command-line precedence. Under a bare `--get`, a candidate that rewrote
    `.git/config` (dropping the pin, keeping its own `core.fsmonitor`, which names a command
    git EXECUTES) would be accepted because the check answered from the environment instead
    of from the clone.
    """
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.hooksPath'='{os.devnull}'")
    repo = make_repo(tmp_path)
    b, _s, cb = _candidate(tmp_path, repo)
    hostile = bundle.CandidateBundle(
        version=cb.version, baseline_ref=cb.baseline_ref, baseline_commit=cb.baseline_commit,
        sidecars=(bundle.SidecarEntry(".git/config", "file", 0o644,
                                      b"[core]\n\tfsmonitor = /tmp/rigged\n"),))
    with pytest.raises(verify.VerifyError, match="config"):
        verify.build_verifier(repo, b, hostile, tmp_path / "verifier", identity=IDENT)


def test_a_step_cwd_may_not_leave_the_verifier(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    for bad in ("/etc", "../..", "sub/../../elsewhere"):
        with pytest.raises(verify.VerifyError, match="leaves the verifier"):
            verify.run_command(d, verify.Command(
                steps=(verify.Step(argv=("true",), cwd=bad),)))
    (d / "sub").mkdir()
    r = verify.run_command(d, verify.Command(
        steps=(verify.Step(argv=("pwd",), cwd="sub"),)))
    assert Path(r.stdout.strip()).resolve() == (d / "sub").resolve()


def test_undecodable_gate_output_is_not_a_crash(tmp_path):
    """`harvest` already paid for this once: subprocess' strict UTF-8 decode raised
    UnicodeDecodeError on a single latin-1 byte and took the whole call with it. Gate
    output is human-facing and nothing re-encodes it, so it takes errors="replace" rather
    than harvest's reversible surrogateescape."""
    d = tmp_path / "w"
    d.mkdir()
    r = verify.run_command(d, verify.Command.parse([["printf", "caf\\351\\n"]]))
    assert r.exit_code == 0 and r.stdout.startswith("caf")
