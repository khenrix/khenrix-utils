"""The gate runs where the builder never was, and the engine runs it (spec §6)."""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import pytest  # noqa: E402
from forge import baseline, bundle, fleet, harvest, inspect as finspect, verify  # noqa: E402
from forge_fixtures import commit_all, git as _git, make_repo, write  # noqa: E402

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
    # A LEADING `~` is a shell's expansion, not execve's: `["~/bin/tool"]` parsed clean and
    # then ENOENTed at gate time, which is exactly what the refusals here exist to prevent.
    # It is not a membership test, because `~` is an ordinary character everywhere else —
    # a trailing one is a real filename and stays legal.
    with pytest.raises(verify.VerifyError, match="only a shell expands"):
        verify.Command.parse([["~/bin/tool"]])
    assert verify.Command.parse([["./build.sh~", "~/x"]]).steps[0].argv == \
        ("./build.sh~", "~/x")
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
    # `Step` is public and `parse` is not the only way to reach one — the timeout cases in
    # this file build them directly — so the program-name rule has to live on `Step` too.
    # Before this, `Step(argv=("make verify",))` was accepted and ENOENTed at gate time,
    # which is the outcome the bare-string refusal exists to prevent.
    for bad in (("make verify",), ("./run;rm",), ("~/bin/tool",)):
        with pytest.raises(verify.VerifyError, match="cannot exist"):
            verify.Step(argv=bad)
    with pytest.raises(verify.VerifyError, match="non-string"):
        verify.Step(argv=("make", 1))
    # A whole spec that is one string iterates as CHARACTERS. It was already refused, but
    # by a message naming "step 0" and the value 'm' — neither of which the caller wrote.
    with pytest.raises(verify.VerifyError, match="LIST of steps"):
        verify.Command.parse("make")


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


def test_a_timed_out_step_reports_what_it_had_already_printed(tmp_path):
    """The output is the only thing that says WHICH test hung, and it is already sitting in
    the pipe by the time the step is killed. A message carrying the argv alone sends the
    reader back to reproduce a run that takes `timeout` seconds to fail again.

    Both streams, because a gate is `make` (everything on stdout) as often as it is a test
    runner (progress on stderr).

    The markers live in a SCRIPT, not in the argv. Written as `sh -c "echo OUT-MARKER; …"`
    this test passed with `_tail` dropping a whole stream, because the message also carries
    `list(step.argv)` — so both markers were in it whether or not any output was read.
    """
    d = tmp_path / "w"
    d.mkdir()
    (d / "noisy.sh").write_text(
        "#!/bin/sh\necho OUT-MARKER\necho ERR-MARKER >&2\nsleep 30\n")
    c = verify.Command(steps=(verify.Step(argv=("sh", "noisy.sh"), timeout=1),))
    with pytest.raises(verify.VerifyError) as e:
        verify.run_command(d, c)
    assert "ERR-MARKER" in str(e.value), \
        f"the timeout discarded the stderr that names the hang: {e.value}"
    assert "OUT-MARKER" in str(e.value), \
        f"the timeout discarded the stdout a `make` gate writes everything to: {e.value}"


def test_a_silent_timed_out_step_says_so_rather_than_showing_nothing(tmp_path):
    """An empty tail and a tail nobody could read are different states, and a message that
    renders both as blank space invites the reader to conclude the step was silent."""
    d = tmp_path / "w"
    d.mkdir()
    c = verify.Command(steps=(verify.Step(argv=("sleep", "30"), timeout=1),))
    with pytest.raises(verify.VerifyError, match="printed nothing before it was killed"):
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
    # It OUTRANKS the clone's local core.hooksPath pin (measured, git 2.53), and it was
    # missing from every list this package strips until the fix wave. Not exotic either:
    # git exports this into every child whenever anything up the tree ran `git -c …`.
    {"GIT_CONFIG_PARAMETERS": "'core.hooksPath'='{hooks}'"},
    # Its partner, which was in the list all along. Kept as its own case so the two names
    # are mutated apart: one loop covering both is one loop that can lose either.
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
    _assert_gate_commit_ran_no_hook(v)


def _assert_gate_commit_ran_no_hook(v, step_env=None):
    r = verify.run_command(v, verify.Command(steps=tuple(
        verify.Step(argv=a, env=dict(step_env or {}))
        for a in (("git", "add", "later.txt"), ("git", "commit", "-m", "gate")))))
    assert not (v / "HOOK-RAN").exists(), "a hook ran inside the verifier"
    assert r.exit_code == 0, f"the gate's own commit failed: {r.stderr}"


def test_the_gate_does_not_inherit_the_users_git_template(tmp_path, monkeypatch):
    """GIT_TEMPLATE_DIR is the same class as the injection above, one layer out: it decides
    what `git init` and `git clone` copy into a NEW repository, hooks included.

    `fleet` spends an explicit `--template=` on it for the seat clone, and nothing covered a
    gate step that creates a repository of its own — an ordinary thing for a build to do.
    Measured on git 2.53 with both /dev/null config pins set: `GIT_TEMPLATE_DIR=<dir> git
    init inner` installed <dir>/hooks/pre-commit and the next commit in `inner` ran it, exit
    1. So the gate is asserted by EFFECT: the repository the step creates has no hook.
    """
    tmpl = tmp_path / "tmpl"
    (tmpl / "hooks").mkdir(parents=True)
    (tmpl / "hooks" / "pre-commit").write_text("#!/bin/sh\ntouch HOOK-RAN\nexit 1\n")
    (tmpl / "hooks" / "pre-commit").chmod(0o755)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tmpl))
    repo = make_repo(tmp_path)
    b, _s, cb = _candidate(tmp_path, repo)
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT)
    r = verify.run_command(v, verify.Command.parse([["git", "init", "-q", "inner"]]))
    assert r.exit_code == 0, f"the gate's own git init failed: {r.stderr}"
    assert not (v / "inner" / ".git" / "hooks" / "pre-commit").exists(), \
        "the user's template installed a hook into a repository the gate created"


def test_a_steps_own_env_cannot_re_admit_what_the_base_dropped(tmp_path):
    """`Step.env` is merged over the hardened base, so before the merged result was
    re-hardened a step could hand back the very name `_gate_env` had just dropped —
    `run_command(env=…)` guarded and `Step.env` not, which is one rule with two answers."""
    repo = make_repo(tmp_path)
    hooks = tmp_path / "step-hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\ntouch HOOK-RAN\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    b, _s, cb = _candidate(tmp_path, repo, work=[("src.py", "x\n")])
    v = verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT)
    write(v, "later.txt", "later\n")
    _assert_gate_commit_ran_no_hook(
        v, {"GIT_CONFIG_PARAMETERS": f"'core.hooksPath'='{hooks}'"})


def test_a_bundle_that_would_rewrite_the_clone_config_never_reaches_the_gate(tmp_path):
    """MEASURED against `bundle.materialize` at 4545bb6: a sidecar named `.git/config` was
    written straight over the clone's config, taking the hooks pin, the clone's identity and
    admitting `core.fsmonitor` — a program git EXECUTES on an ordinary `git status`.

    Closed where it lives: `bundle._safe_rel` now refuses a `.git` component, so the refusal
    arrives as a `BundleError` BEFORE the config is touched. It propagates unwrapped, on the
    module's stated precedent — the class that knows what failed is the one that names it.
    """
    repo = make_repo(tmp_path)
    b, _s, cb = _candidate(tmp_path, repo)
    hostile = bundle.CandidateBundle(
        version=cb.version, baseline_ref=cb.baseline_ref, baseline_commit=cb.baseline_commit,
        sidecars=(bundle.SidecarEntry(".git/config", "file", 0o644,
                                      b"[core]\n\thooksPath = /tmp/rigged\n"),))
    dest = tmp_path / "verifier"
    with pytest.raises(bundle.BundleError, match="git's own directory"):
        verify.build_verifier(repo, b, hostile, dest, identity=IDENT)
    assert _git(dest, "config", "--local", "--get", "core.hooksPath").stdout.strip() \
        == os.devnull, "the pin was already gone by the time the refusal landed"


def test_the_hooks_pin_is_read_back_after_the_candidate_is_laid_down(
        tmp_path, monkeypatch):
    """The canary itself, with `bundle` stubbed out — the only route left to a verifier
    whose config changed between the pin and the gate, now that `_safe_rel` refuses the
    sidecar that used to do it. Defence in depth against a bundle assembled some other way,
    so it is exercised by simulating that materialization rather than by pretending the
    closed route is still open.

    The ambient GIT_CONFIG_PARAMETERS is what makes this an assertion about the CLONE.
    Measured, git 2.53, in a clone whose local file has no core.hooksPath:

        GIT_CONFIG_PARAMETERS="'core.hooksPath'='/dev/null'" git config --get  -> /dev/null
        ...the same environment,                             --local --get     -> unset

    So a readback that answered from the environment would certify a verifier whose pin had
    been removed. Two latches now stop that — `gitcmd` strips the variable, and the readback
    passes `--local` — and this test fails if BOTH are lost.
    """
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.hooksPath'='{os.devnull}'")
    repo = make_repo(tmp_path)
    b, _s, cb = _candidate(tmp_path, repo)

    def _rewrite(_bundle, dest):
        # What the refused sidecar used to achieve: the pin dropped, a builder-chosen
        # executable key kept.
        (Path(dest) / ".git" / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n\tfsmonitor = /tmp/rigged\n")
        return ()

    monkeypatch.setattr(verify.bundle, "materialize", _rewrite)
    with pytest.raises(verify.VerifyError, match="rewrote the verifier's git config"):
        verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT)


def test_the_readback_asks_the_clones_own_file_not_whatever_would_answer(
        tmp_path, monkeypatch):
    """`--local` on the readback, pinned by the one config scope that lives INSIDE the
    clone. `.git/config.worktree` — enabled by `extensions.worktreeConfig` — outranks the
    local file and is writable by exactly the party the canary exists to ask about, so
    unlike the global/system files and GIT_CONFIG_PARAMETERS nothing else in this module
    covers it.

    Measured, git 2.53, in a verifier clone whose LOCAL file names a hooks directory and
    whose worktree file names /dev/null:

        git config --get         core.hooksPath  -> /dev/null
        git config --local --get core.hooksPath  -> <the hooks directory>

    So a readback without `--local` certifies a clone whose own config file the candidate
    rewrote, answering from a file the candidate wrote to say so.
    """
    repo = make_repo(tmp_path)
    b, _s, cb = _candidate(tmp_path, repo)
    hooks = tmp_path / "rigged-hooks"
    hooks.mkdir()

    def _rewrite(_bundle, dest):
        # Not `forge.gitcmd`: this stands in for the candidate, which is not the engine.
        _git(dest, "config", "--local", "extensions.worktreeConfig", "true")
        _git(dest, "config", "--worktree", "core.hooksPath", os.devnull)
        _git(dest, "config", "--local", "core.hooksPath", str(hooks))
        return ()

    monkeypatch.setattr(verify.bundle, "materialize", _rewrite)
    dest = tmp_path / "verifier"
    with pytest.raises(verify.VerifyError, match="rewrote the verifier's git config"):
        verify.build_verifier(repo, b, cb, dest, identity=IDENT)
    assert _git(dest, "config", "--get", "core.hooksPath").stdout.strip() == os.devnull, \
        "the fixture no longer masks the rewrite, so it pins nothing"


def test_a_scope_above_the_clones_local_file_cannot_hide_a_live_hook(tmp_path, monkeypatch):
    """The same scope, the other way round: `--local` alone is blind in BOTH directions.

    `.git/config.worktree` outranks the local file, so a candidate that never touches the
    pin and names its hooks directory one scope up leaves the readback looking at a value
    that is no longer the one git obeys.

    Measured, git 2.53, in a clone whose local pin is untouched at /dev/null and whose
    worktree file names a hooks directory:

        git config --local --get core.hooksPath  -> /dev/null
        git config --get         core.hooksPath  -> <the hooks directory>
        ...and the next `git commit` in that clone RAN the hook, exit 1

    So the effective value is read back too, and a canary that can be walked past this way
    is the overclaim, not the hook.
    """
    repo = make_repo(tmp_path)
    b, _s, cb = _candidate(tmp_path, repo)
    hooks = tmp_path / "rigged-hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\ntouch HOOK-RAN\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)

    def _rewrite(_bundle, dest):
        # Not `forge.gitcmd`: this stands in for the candidate, which is not the engine.
        # The local pin is left alone — that is the whole of the case.
        _git(dest, "config", "--local", "extensions.worktreeConfig", "true")
        _git(dest, "config", "--worktree", "core.hooksPath", str(hooks))
        return ()

    monkeypatch.setattr(verify.bundle, "materialize", _rewrite)
    dest = tmp_path / "verifier"
    with pytest.raises(verify.VerifyError, match="rewrote the verifier's git config"):
        verify.build_verifier(repo, b, cb, dest, identity=IDENT)
    assert _git(dest, "config", "--local", "--get", "core.hooksPath").stdout.strip() \
        == os.devnull, "the fixture disturbed the pin, so a --local readback would catch it"
    # ...and the override was live, not merely present: the refused clone runs the hook.
    write(dest, "later.txt", "later\n")
    _git(dest, "add", "later.txt")
    with pytest.raises(RuntimeError):
        _git(dest, "commit", "-m", "gate")
    assert (dest / "HOOK-RAN").exists(), \
        "the worktree-scope override did not actually reach git, so it pins nothing"


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


def _generator_repo(tmp_path, script, *, files=()):
    """A repo whose tracked `build.sh` IS the verify command, plus a verifier over it.

    The generator is committed rather than carried in the candidate, because §7.2's subject
    is a verify command the REPOSITORY already has — this repo's `make verify` runs
    `render` — and a build script the seat introduced would be testing §6.1's gate_changed
    instead.
    """
    repo = make_repo(tmp_path)
    for rel, text in files:
        write(repo, rel, text)
    write(repo, "build.sh", script)
    (repo / "build.sh").chmod(0o755)
    commit_all(repo, "seed generator")
    return repo


def _verifier(tmp_path, repo, **kw):
    b, _s, cb = _candidate(tmp_path, repo, **kw)
    return verify.build_verifier(repo, b, cb, tmp_path / "verifier", identity=IDENT)


def _staged(v):
    """What the verifier's INDEX holds against its HEAD — the checkpoint, not the worktree."""
    return dict(
        (line[2:], line[0])
        for line in _git(v, "diff", "--cached", "--name-status").stdout.splitlines() if line)


def _contract(*relations):
    return finspect.GeneratorContract(id="test", relations=relations)


BUILD = verify.Command.parse([["./build.sh"]])


def test_a_verify_that_regenerates_tracked_files_converges_in_two_passes(tmp_path):
    """This repo's own shape: `make verify` runs `render`, which rewrites tracked output.

    Adapted from the plan's draft in one place: `fixed_point` returns a `FixedPoint`, not
    an `(r, admitted)` pair — see `FixedPoint`'s docstring and
    `test_an_unexplained_tracked_rewrite_is_not_hidden_behind_exit_zero` for what the pair
    could not express.

    The staging assertion is the plan's own §7.2 requirement and nothing else pins it:
    convergence alone does NOT, because an idempotent generator writes the same bytes on
    pass 2 whether or not pass 1's output was checkpointed. Measured — with `_checkpoint`
    removed entirely, every other assertion here still passed.
    """
    repo = _generator_repo(tmp_path, "#!/bin/sh\nmkdir -p gen\ncp src/a.txt gen/a.txt\n",
                           files=[("src/a.txt", "v1\n"), ("gen/a.txt", "v1\n")])
    v = _verifier(tmp_path, repo, work=[("src/a.txt", "v2\n")])   # the agent edits the SOURCE

    fp = verify.fixed_point(v, BUILD, _contract(("src/*", "gen/*")))
    assert fp.run.exit_code == 0
    assert "gen/a.txt" in fp.admitted, "the regenerated output must be admitted, not discarded"
    assert (v / "gen" / "a.txt").read_text() == "v2\n"
    assert _staged(v).get("gen/a.txt") == "M", \
        f"admitted output was recorded but never staged: {_staged(v)}"


def test_a_nondeterministic_generator_is_unstable_not_silently_accepted(tmp_path):
    repo = _generator_repo(tmp_path, "#!/bin/sh\ndate +%s%N > gen.txt\n",
                           files=[("gen.txt", "seed\n")])
    v = _verifier(tmp_path, repo)
    with pytest.raises(verify.GeneratorUnstable, match="no fixed point"):
        verify.fixed_point(v, BUILD, _contract((".", "gen.txt")))


def test_an_output_outside_the_contract_is_not_admitted(tmp_path):
    """A seat cannot widen the contract by writing somewhere new."""
    repo = _generator_repo(tmp_path, "#!/bin/sh\necho sneaky > receipt.json\n")
    v = _verifier(tmp_path, repo)
    fp = verify.fixed_point(v, BUILD, _contract(("src/*", "gen/*")))
    assert "receipt.json" not in fp.admitted
    assert _staged(v) == {}, "an unadmitted path was checkpointed anyway"


def test_an_unexplained_tracked_rewrite_is_not_hidden_behind_exit_zero(tmp_path):
    """Why `FixedPoint` carries three fields and not the plan's `(Run, admitted)` pair.

    The contract here is the EMPTY one `detect_generators` returns, so this is also what
    forge's fail-closed default does end to end: it admits nothing, and the tracked rewrite
    it refuses to admit is still reported.

    §6.2 makes exit 0 a PASS only alongside "no unexplained tracked delta", and the two runs
    below differ in exactly that — yet their `run.exit_code` and `admitted` are equal, which
    is the measurement. `junk.log` is the other half: untracked churn is what every real
    gate produces, so it must NOT arrive as an unexplained delta.
    """
    repo = _generator_repo(
        tmp_path, "#!/bin/sh\necho rewritten > notes.txt\necho noise > junk.log\n",
        files=[("notes.txt", "original\n")])
    v = _verifier(tmp_path, repo)

    clean = verify.fixed_point(v, verify.Command.parse([["true"]]), finspect.GeneratorContract())
    dirty = verify.fixed_point(v, BUILD, finspect.GeneratorContract())

    assert (clean.run.exit_code, clean.admitted) == (dirty.run.exit_code, dirty.admitted) \
        == (0, ()), "the pair the plan specified cannot distinguish these two runs"
    assert clean.unexplained == ()
    assert dirty.unexplained == ("notes.txt",), \
        f"the tracked rewrite outside the contract was lost: {dirty.unexplained}"


def test_a_generated_file_that_is_new_or_deleted_is_checkpointed_too(tmp_path, monkeypatch):
    """Admission is decided by the contract's output globs ALONE.

    Not by tracked-ness: `gen/new.txt` has no index entry when the generator creates it, and
    leaving it unstaged hands over a required file that a drift check reads as untracked.
    And not by presence: a generator that DELETES one of its outputs has to have that
    deletion staged, or the same check sees a tracked file missing from the worktree.

    The batch size is driven down to 1 so the checkpoint's argv batching runs at all: at the
    default every admitted set in this suite is one batch, so a loop that staged only its
    first batch would look identical to a correct one.
    """
    monkeypatch.setattr(verify, "_CHECKPOINT_BATCH", 1)
    repo = _generator_repo(
        tmp_path, "#!/bin/sh\nmkdir -p gen\necho fresh > gen/new.txt\nrm -f gen/old.txt\n",
        files=[("gen/old.txt", "stale\n")])
    v = _verifier(tmp_path, repo)
    fp = verify.fixed_point(v, BUILD, _contract(("src/*", "gen/*")))
    assert fp.run.exit_code == 0
    assert fp.admitted == ("gen/new.txt", "gen/old.txt")
    assert _staged(v) == {"gen/new.txt": "A", "gen/old.txt": "D"}, \
        f"the generator's creation and deletion were not both checkpointed: {_staged(v)}"


def test_an_untracked_declared_path_the_generator_removes_is_not_a_failed_checkpoint(tmp_path):
    """`git add` on a path that is neither on disk nor in the index exits 128 — measured:
    `fatal: pathspec ':(literal)…' did not match any files` — which would turn an ordinary
    run into an engine error.

    Reachable without anything exotic: an untracked file the candidate carries as a sidecar,
    under a path the contract declares, that the generator then cleans up.
    """
    repo = _generator_repo(tmp_path, "#!/bin/sh\nrm -f gen/stale.txt\n")
    v = _verifier(tmp_path, repo, work=[("gen/stale.txt", "carried\n")])
    assert not (v / "gen" / "stale.txt").is_symlink() and (v / "gen" / "stale.txt").exists()

    fp = verify.fixed_point(v, BUILD, _contract(("src/*", "gen/*")))
    assert fp.run.exit_code == 0 and "gen/stale.txt" in fp.admitted
    assert _staged(v) == {}, "a path git never tracked was somehow checkpointed"


def test_a_failing_gate_is_returned_rather_than_checkpointed(tmp_path):
    """The gate has answered. Staging its output would checkpoint a failed run's leftovers,
    and re-running would spend the whole pass budget on a command that already lost."""
    repo = _generator_repo(
        tmp_path, "#!/bin/sh\nmkdir -p gen\necho out > gen/a.txt\nexit 3\n",
        files=[("gen/a.txt", "before\n")])
    v = _verifier(tmp_path, repo)
    fp = verify.fixed_point(v, BUILD, _contract(("src/*", "gen/*")))
    assert fp.run.exit_code == 3
    assert fp.admitted == () and fp.unexplained == ()
    assert _staged(v) == {}, "a failed gate's output was checkpointed"


def test_an_uninventoriable_verifier_is_refused_rather_than_read_as_converged(
        tmp_path, monkeypatch):
    """A quota breach answers `({}, [breach])`, and `diff({}, {})` is empty — which is
    exactly what this loop reads as a clean fixed point. Dropping the breach line would not
    lose a warning, it would manufacture a PASS."""
    repo = _generator_repo(tmp_path, "#!/bin/sh\ntrue\n")
    v = _verifier(tmp_path, repo)
    monkeypatch.setattr(verify.snapshot, "take", lambda *a, **kw: ({}, ["files: 9 > 8"]))
    with pytest.raises(verify.VerifyError, match="could not be inventoried"):
        verify.fixed_point(v, BUILD, _contract(("src/*", "gen/*")))


def test_fixed_point_refuses_a_pass_budget_that_would_run_nothing(tmp_path):
    """`range(0)` falls straight through to the unstable raise, which would report a
    generator unstable on the evidence of zero runs. The refusal also comes BEFORE the first
    inventory, so it is the budget that is named rather than the tree."""
    d = tmp_path / "not-even-a-tree"
    with pytest.raises(verify.VerifyError, match="run the gate zero times"):
        verify.fixed_point(d, BUILD, finspect.GeneratorContract(), max_passes=0)


def test_a_declared_output_that_is_gitignored_is_still_checkpointed(tmp_path):
    """`git add` refuses an ignored path and exits 1, so without `-f` a repository whose
    generator writes into an ignored directory could never reach a fixed point.

    §7.4's own example is `.chunkmap/map.md`. The contract is the engine's declaration,
    confirmed at the §5 gate; the repo's ignore rules must not overrule it — the same
    argument `baseline.materialize` makes for selected untracked paths.
    """
    repo = _generator_repo(tmp_path, "#!/bin/sh\nmkdir -p dist\necho built > dist/out.txt\n",
                           files=[(".gitignore", "dist/\n")])
    v = _verifier(tmp_path, repo)
    fp = verify.fixed_point(v, BUILD, _contract(("src/*", "dist/*")))
    assert fp.run.exit_code == 0 and fp.admitted == ("dist/out.txt",)
    assert _staged(v) == {"dist/out.txt": "A"}, \
        f"git's ignore rules vetoed a declared generator output: {_staged(v)}"


def test_a_path_the_gate_itself_stages_is_a_tracked_delta(tmp_path):
    """Tracked-ness is read AFTER the run, not before it.

    A verify command that stages a file has authored a tracked delta outside the contract,
    and it is invisible to a set of tracked paths captured before the command ran — which is
    the fail-open direction, since the gate is the party under suspicion.
    """
    repo = _generator_repo(
        tmp_path, "#!/bin/sh\necho x > snuck.txt\ngit add snuck.txt\n")
    v = _verifier(tmp_path, repo)
    fp = verify.fixed_point(v, BUILD, finspect.GeneratorContract())
    assert fp.run.exit_code == 0 and fp.admitted == ()
    assert fp.unexplained == ("snuck.txt",), \
        f"a file the gate staged for itself was read as untracked noise: {fp.unexplained}"


def test_the_tracked_set_is_keyed_the_way_snapshot_keys_a_non_utf8_filename(tmp_path):
    """These two sets are compared path by path, so they have to agree byte for byte on a
    filename that is not valid UTF-8 — a filesystem name is bytes, not text.

    `harvest` already paid for the strict-decode half of this: one latin-1 byte raised
    UnicodeDecodeError out of subprocess and took the whole call with it. The other half is
    worse here, because it is silent: under `errors="replace"` git's answer would come back
    with U+FFFD where `os.walk` put a surrogate, so the path would be in the index and
    absent from this set — a tracked file reported as untracked, which is the direction that
    turns an unexplained delta into ordinary build noise.
    """
    repo = make_repo(tmp_path)
    raw = os.path.join(os.fsencode(repo), b"caf\xe9.txt")
    with open(raw, "wb") as fh:
        fh.write(b"latin-1 name\n")
    commit_all(repo, "a filename that is not utf-8")

    name = os.fsdecode(raw).rsplit("/", 1)[1]
    entries, breaches = verify.snapshot.take(repo)
    assert breaches == []
    assert name in entries, "the fixture did not produce the filename this test is about"
    assert name in verify._tracked(Path(repo)), \
        "git's view of the index and snapshot's view of the tree disagree on this name"
