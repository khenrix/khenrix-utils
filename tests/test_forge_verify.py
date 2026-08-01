"""The gate runs where the builder never was, and the engine runs it (spec §6)."""
import ast
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
    """A verify command that writes a file and stages it has authored a tracked delta
    outside the contract, and the gate is the party under suspicion here.

    BOTH halves of the delta see this one: content appeared under the path, and the index
    grew by it. The two cases that need one half only — an index promotion with no content
    delta, and a path the gate drops from the index — are pinned separately below.
    """
    repo = _generator_repo(
        tmp_path, "#!/bin/sh\necho x > snuck.txt\ngit add snuck.txt\n")
    v = _verifier(tmp_path, repo)
    fp = verify.fixed_point(v, BUILD, finspect.GeneratorContract())
    assert fp.run.exit_code == 0 and fp.admitted == ()
    assert fp.unexplained == ("snuck.txt",), \
        f"a file the gate staged for itself was read as untracked noise: {fp.unexplained}"


def test_an_index_promotion_with_no_content_delta_is_still_a_tracked_delta(tmp_path):
    """The gate can author a tracked change without moving a byte on disk.

    `git add` of a file the candidate carried as a sidecar promotes it into the verifier's
    index, and the bytes under that path are the bytes the inventory already saw — so a
    content diff reports nothing. Measured on the content-only form of this loop:
    `exit=0 admitted=() unexplained=()` with `git diff --cached` already naming
    `A  carried.txt`.
    """
    repo = _generator_repo(tmp_path, "#!/bin/sh\ngit add carried.txt\n")
    v = _verifier(tmp_path, repo, work=[("carried.txt", "sidecar\n")])

    fp = verify.fixed_point(v, BUILD, finspect.GeneratorContract())
    assert fp.run.exit_code == 0 and fp.admitted == ()
    assert fp.unexplained == ("carried.txt",), \
        f"the gate promoted a path into the index unseen: {fp.unexplained}"
    assert _staged(v) == {"carried.txt": "A"}, "the fixture did not stage anything"


def test_a_tracked_path_the_gate_drops_from_the_index_is_a_tracked_delta(tmp_path):
    """A path LEAVING the index is a transition too, and a content diff misses both shapes.

    `git rm --cached` moves no bytes at all. `git rm` moves bytes at a path that is no
    longer tracked when the run ends, so asking only whether a changed path is tracked
    AFTER the run reads a deleted tracked file as untracked noise — the fail-open
    direction, and the reason removals count here and are not filtered to additions.
    Measured on the content-only form: `unexplained=()` for both files.
    """
    repo = _generator_repo(
        tmp_path, "#!/bin/sh\ngit rm -q --cached kept.txt\ngit rm -q -f gone.txt\n",
        files=[("kept.txt", "still on disk\n"), ("gone.txt", "deleted too\n")])
    v = _verifier(tmp_path, repo)

    fp = verify.fixed_point(v, BUILD, finspect.GeneratorContract())
    assert fp.run.exit_code == 0 and fp.admitted == ()
    assert fp.unexplained == ("gone.txt", "kept.txt"), \
        f"the gate emptied the index of two tracked paths unseen: {fp.unexplained}"


def test_a_declared_output_the_gate_stages_itself_is_not_unexplained(tmp_path):
    """The subtraction is the contract's globs, the same predicate that decides admission —
    so both halves of the delta are explained by exactly the same rule and nothing falls
    into the gap between them.

    `gen/new.txt` is regenerated AND newly tracked in one pass, and it is the path the two
    candidate subtractions agree on. `gen/carried.txt` is the one they do not: the gate
    promotes it with no content delta at all, so a subtraction of what the CONTENT delta
    admitted would report it while a subtraction by the globs does not. The globs win
    because they are what this module means by explained — "admission is decided by the
    output glob alone, not by what changed" has to hold in the index channel too, or a
    declared path is treated one way when its bytes move and another way when they do not.
    The visible consequence, stated rather than hidden: a declared path whose index
    membership the gate moves without touching its bytes is in neither `admitted` (the
    engine never staged it) nor `unexplained` (the contract covers it).
    """
    repo = _generator_repo(
        tmp_path, "#!/bin/sh\nmkdir -p gen\necho fresh > gen/new.txt\n"
                  "git add gen/new.txt gen/carried.txt\n")
    v = _verifier(tmp_path, repo, work=[("gen/carried.txt", "sidecar\n")])

    fp = verify.fixed_point(v, BUILD, _contract(("src/*", "gen/*")))
    assert fp.run.exit_code == 0
    assert fp.admitted == ("gen/new.txt",) and fp.unexplained == (), \
        f"a declared path the gate staged itself was reported as unexplained: {fp}"
    assert _staged(v) == {"gen/carried.txt": "A", "gen/new.txt": "A"}, \
        f"the fixture did not promote both paths: {_staged(v)}"


def test_a_rename_the_gate_stages_surfaces_as_both_of_its_halves(tmp_path):
    """`git mv` is a removal plus an addition in a name-keyed set, and both are reported.

    Two entries for one logical change is the honest answer: nothing in this module detects
    renames, both statements are true of the index, and §6.2's PASS row asks for "no
    unexplained tracked delta" — a question about the set being empty, not about its shape.
    Pairing them up would need a similarity heuristic the engine has no way to state
    truthfully.
    """
    repo = _generator_repo(tmp_path, "#!/bin/sh\ngit mv old.txt new.txt\n",
                           files=[("old.txt", "content\n")])
    v = _verifier(tmp_path, repo)

    fp = verify.fixed_point(v, BUILD, finspect.GeneratorContract())
    assert fp.run.exit_code == 0
    assert fp.unexplained == ("new.txt", "old.txt"), \
        f"a staged rename lost one of its halves: {fp.unexplained}"


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


def _run(code, out=""):
    return verify.Run(exit_code=code, stdout=out, stderr="", duration_sec=0.1, step_index=0)


def test_exit_zero_alone_is_not_a_pass_when_the_bundle_omitted_an_input():
    b = bundle.CandidateBundle(version=1, baseline_ref="r", baseline_commit="c",
                               tracked_patch=b"", sidecars=(), gate_delta=(),
                               generator_contract_id="x", omitted=("fixtures/data.bin",))
    outcome, reason = verify.classify(_run(1, "FileNotFoundError: fixtures/data.bin"),
                                      _run(0), b)
    assert outcome == verify.HARVEST_INCOMPLETE
    assert "fixtures/data.bin" in reason


def test_a_baseline_that_was_already_red_is_not_reported_as_a_pass():
    b = bundle.CandidateBundle(version=1, baseline_ref="r", baseline_commit="c",
                               tracked_patch=b"", sidecars=(), gate_delta=(),
                               generator_contract_id="x", omitted=())
    outcome, _ = verify.classify(_run(1, "1 failed"), _run(1, "1 failed"), b)
    assert outcome == verify.BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE


def test_a_fail_then_pass_rerun_is_flaky_not_a_pass():
    b = bundle.CandidateBundle(version=1, baseline_ref="r", baseline_commit="c",
                               tracked_patch=b"", sidecars=(), gate_delta=(),
                               generator_contract_id="x", omitted=())
    outcome, _ = verify.classify(_run(1), _run(0), b, rerun=_run(0))
    assert outcome == verify.FLAKY


def test_a_candidate_that_edited_the_gate_is_marked_not_silently_passed():
    b = bundle.CandidateBundle(version=1, baseline_ref="r", baseline_commit="c",
                               tracked_patch=b"", sidecars=(), gate_delta=("Makefile",),
                               generator_contract_id="x", omitted=())
    outcome, reason = verify.classify(_run(0), _run(0), b)
    assert outcome == verify.GATE_CHANGED and "Makefile" in reason


def test_a_clean_pass_is_a_pass():
    b = bundle.CandidateBundle(version=1, baseline_ref="r", baseline_commit="c",
                               tracked_patch=b"", sidecars=(), gate_delta=(),
                               generator_contract_id="x", omitted=())
    assert verify.classify(_run(0), _run(0), b)[0] == verify.PASS


def _bundle(**kw):
    """A bundle that carries nothing, so a test names only the field it is about.

    `gate_delta` is spelled out in every call rather than defaulted, because its default is
    `None` — "nobody looked" — and a test that meant "clean gate" and got the default would
    be asserting the opposite of what it says.
    """
    kw.setdefault("gate_delta", ())
    return bundle.CandidateBundle(version=1, baseline_ref="r", baseline_commit="c",
                                  tracked_patch=b"", sidecars=(),
                                  generator_contract_id="x", **kw)


def test_a_gate_surface_nobody_measured_is_not_treated_as_a_clean_one():
    """`gate_delta is None` is the fail-OPEN reading of `()`, and it is the DEFAULT.

    `bundle.build` does not populate the field yet, so this is the shape every real bundle
    has today: a clean pass on an unmeasured gate surface must come back as GATE_CHANGED,
    not PASS, or the whole gate-surface defence is off by default.
    """
    outcome, reason = verify.classify(_run(0), _run(0), _bundle(gate_delta=None))
    assert outcome == verify.GATE_CHANGED
    assert "gate_delta is None" in reason
    assert "PASS" in reason, "the displaced verdict is not stated"


def test_the_reason_separates_an_unmeasured_gate_from_an_edited_one():
    """One outcome, two very different facts. A reviewer who cannot tell them apart cannot
    act: one is a missing measurement to go and take, the other is a diff to read."""
    unmeasured = verify.classify(_run(0), _run(0), _bundle(gate_delta=None))[1]
    edited = verify.classify(_run(0), _run(0), _bundle(gate_delta=("Makefile",)))[1]
    assert "nobody measured" in unmeasured and "Makefile" not in unmeasured
    assert "Makefile" in edited and "nobody measured" not in edited


def test_a_bundle_that_omitted_a_path_is_incomplete_even_when_the_gate_passed():
    """`CandidateBundle.omitted`'s own consumer contract: before the exit code, not after.

    The measured case behind it is a nested repo whose gitlink is omitted while its content
    crosses as ordinary sidecars — `git -C sub rev-parse HEAD` then exits 0 in the verifier
    and answers the VERIFIER's HEAD, so the gate passes BECAUSE something is missing and
    nothing at the gate can see it.
    """
    outcome, reason = verify.classify(_run(0), _run(0), _bundle(omitted=("sub",)))
    assert outcome == verify.HARVEST_INCOMPLETE
    assert "sub" in reason and "settles nothing" in reason


def test_a_changed_gate_does_not_launder_a_failing_gate_into_a_review_mark():
    """GATE_CHANGED displaces PASS, never FAIL.

    A candidate that both weakened the gate and still failed it has failed; reporting
    "changed, please review" instead would be the verdict reading cleaner than its
    evidence. The gate edit is not lost — it is in the reason.
    """
    outcome, reason = verify.classify(_run(1), _run(0), _bundle(gate_delta=("Makefile",)))
    assert outcome == verify.FAIL
    assert "Makefile" in reason


def test_two_red_runs_are_not_comparable_when_the_gate_moved_between_them():
    """§6.2 names changed test discovery as a way two nonzero runs stop being equivalent,
    and "no new failure identified" is exactly that equivalence claim — so a moved gate
    surface has to displace it the same way it displaces PASS."""
    outcome, reason = verify.classify(_run(1), _run(1), _bundle(gate_delta=("Makefile",)))
    assert outcome == verify.GATE_CHANGED
    assert verify.BASELINE_RED_NO_NEW_IDENTIFIED_FAILURE in reason


def test_an_unexplained_tracked_rewrite_is_not_a_pass():
    """§6.2's PASS is exit 0 AND no unexplained tracked delta, and only the `FixedPoint`
    carries the second half. §7.2 routes an undeclared generator through §6.1's
    gate_changed rather than inventing a seventh outcome, so that is what it comes back as.
    """
    fp = verify.FixedPoint(_run(0), (), ("evals/chunk-map/receipt.json",))
    outcome, reason = verify.classify(fp, _run(0), _bundle())
    assert outcome == verify.GATE_CHANGED
    assert "evals/chunk-map/receipt.json" in reason


def test_a_failing_runs_stale_unexplained_set_is_not_read_as_a_gate_change():
    """`FixedPoint.unexplained` is measured only for a pass whose gate exited 0. On a
    failing run it holds whatever EARLIER passes measured, so reading it there would pin a
    gate change on evidence that is not about this verdict."""
    fp = verify.FixedPoint(_run(1), (), ("from/an/earlier/pass.txt",))
    outcome, reason = verify.classify(fp, _run(0), _bundle())
    assert outcome == verify.FAIL
    assert "from/an/earlier/pass.txt" not in reason


def test_a_pass_states_whether_the_tracked_delta_was_measured_at_all():
    """A bare `Run` cannot answer §7.2's half of PASS. That is allowed — `run_command` is a
    legitimate way to run a gate — but the PASS has to say so, or a weaker claim is
    indistinguishable from §6.2's."""
    bare = verify.classify(_run(0), _run(0), _bundle())
    measured = verify.classify(verify.FixedPoint(_run(0), (), ()), _run(0), _bundle())
    assert bare[0] == measured[0] == verify.PASS
    assert "nothing here measured" in bare[1]
    assert "nothing here measured" not in measured[1]
    assert "rewrote no tracked path outside the generator contract" in measured[1]


def test_a_pass_then_fail_rerun_is_flaky_in_that_direction_too():
    """§6.2 spells out fail->pass. Leaving pass->fail as a PASS would be the conversion it
    forbids, decided by which run happened to be first."""
    assert verify.classify(_run(0), _run(0), _bundle(), rerun=_run(1))[0] == verify.FLAKY


def test_classify_refuses_a_verdict_read_off_something_that_is_not_a_run():
    with pytest.raises(verify.VerifyError, match="Run or a FixedPoint"):
        verify.classify(0, _run(0), _bundle())


# --- gate_surface -----------------------------------------------------------------


def _surface_repo(tmp_path, files, *, commit=True):
    repo = make_repo(tmp_path)
    for rel, text in files:
        write(repo, rel, text)
    if commit:
        commit_all(repo, "surface")
    return repo


def test_the_gate_surface_holds_the_build_definition_and_the_discovered_tests_only(tmp_path):
    """§6.1's list, against a tree that actually has each thing."""
    repo = _surface_repo(tmp_path, [
        ("Makefile", "verify:\n\t./run\n"),
        ("tests/test_a.py", "def test_a(): pass\n"),
        ("tests/helpers.py", "X = 1\n"),
        ("src/app.py", "print(1)\n"),
        ("README.md", "docs\n"),
    ])
    s = verify.gate_surface(repo, finspect.GeneratorContract())
    assert "Makefile" in s and "tests/test_a.py" in s
    assert "src/app.py" not in s and "README.md" not in s and "seed.txt" not in s
    assert "tests/helpers.py" not in s, \
        ("an ACCEPTED gap, not a design property: no discovery rule matches this name, so a "
         "test that imports it can be weakened through it without touching the surface. "
         "Stated in gate_surface's uncovered list under INDIRECTION.")


def test_a_conftest_is_gate_surface_even_though_it_is_not_a_test(tmp_path):
    """pytest's per-directory hook file can drop a failing test from the collection
    entirely, which is the quietest way there is to weaken a gate."""
    repo = _surface_repo(tmp_path, [("tests/conftest.py", "def pytest_configure(c): pass\n")])
    assert "tests/conftest.py" in verify.gate_surface(repo, finspect.GeneratorContract())


def test_the_surface_never_names_a_file_the_tree_does_not_have(tmp_path):
    """The rules are engine-owned; the ANSWER is derived from the tree. A repo with no
    Makefile and no CI has neither in its surface — a hardcoded name set would return both.
    """
    repo = _surface_repo(tmp_path, [("src/app.py", "print(1)\n")])
    assert verify.gate_surface(repo, finspect.GeneratorContract()) == ()


def test_the_repos_own_pytest_config_widens_the_test_globs_and_cannot_narrow_them(tmp_path):
    """The one discovery rule a repository legitimately redefines — read as DATA, and
    UNIONed with the defaults.

    Replacement would be the fail-open form: `python_files` is read out of a file the
    candidate may have written, so honouring it as a replacement lets the candidate drop
    every real test file out of the gate's own definition.
    """
    repo = _surface_repo(tmp_path, [
        ("pytest.ini", "[pytest]\npython_files = check_*.py\n"),
        ("check_thing.py", "def test(): pass\n"),
        ("tests/test_a.py", "def test_a(): pass\n"),
    ])
    s = verify.gate_surface(repo, finspect.GeneratorContract())
    assert "check_thing.py" in s, "the tree's own declared discovery was not read"
    assert "tests/test_a.py" in s, "a declared narrowing removed a default-discovered test"
    assert "pytest.ini" in s


def test_a_gate_file_the_candidate_added_without_committing_is_still_surface(tmp_path):
    """A candidate's new file arrives in a verifier as a SIDECAR — written to disk, not to
    the index — so an index-only enumeration would miss the test file it just added."""
    repo = _surface_repo(tmp_path, [("Makefile", "verify:\n\t./run\n")])
    write(repo, "tests/test_new.py", "def test_new(): assert True\n")
    assert "tests/test_new.py" in verify.gate_surface(repo, finspect.GeneratorContract())


def test_a_ci_workflow_is_gate_surface(tmp_path):
    repo = _surface_repo(tmp_path, [
        (".github/workflows/ci.yml", "on: push\n"),
        ("docs/workflows/ci.yml", "not ci\n"),
    ])
    s = verify.gate_surface(repo, finspect.GeneratorContract())
    assert ".github/workflows/ci.yml" in s
    assert "docs/workflows/ci.yml" not in s


def test_the_command_names_the_gate_files_no_rule_can(tmp_path):
    """Two holes closed by the same derivation, and both are stated as holes without it.

    `check.sh` defines the gate completely and its name says nothing; `.venv/bin/pytest` is
    gitignored, so `_enumerate` cannot see it at all — and this module's own docstring
    opens on a seat replacing exactly that file.
    """
    repo = _surface_repo(tmp_path, [
        (".gitignore", ".venv/\n"),
        ("check.sh", "#!/bin/sh\nexit 0\n"),
        (".venv/bin/pytest", "#!/bin/sh\nexit 0\n"),
    ])
    empty = finspect.GeneratorContract()
    assert verify.gate_surface(repo, empty) == ()
    s = verify.gate_surface(repo, empty, command=verify.Command.parse(
        [["./check.sh"], [".venv/bin/pytest", "-q"]]))
    assert "check.sh" in s and ".venv/bin/pytest" in s


def test_a_generated_gate_file_pulls_its_contract_source_into_the_surface(tmp_path):
    """A gate file that is GENERATED is defined by whatever generates it, so an edit to the
    source moves the gate on the next generator pass. Without this the edit is invisible."""
    repo = _surface_repo(tmp_path, [
        ("out/Makefile", "verify:\n\t./run\n"),
        ("src/render.py", "print(1)\n"),
        ("elsewhere/other.py", "print(2)\n"),
    ])
    assert "src/render.py" not in verify.gate_surface(repo, finspect.GeneratorContract())
    s = verify.gate_surface(repo, _contract(("src/*", "out/Makefile")))
    assert "src/render.py" in s and "elsewhere/other.py" not in s


def test_a_reason_that_would_carry_the_whole_candidate_is_bounded():
    """`omitted` and a gate delta are each as large as the candidate, and a reason travels
    into a report and a judge prompt."""
    reason = verify.classify(_run(0), _run(0),
                             _bundle(omitted=tuple(f"p{i}.txt" for i in range(9))))[1]
    assert "p4.txt" in reason and "p5.txt" not in reason and "(+4 more)" in reason


def test_a_config_that_cannot_be_parsed_leaves_the_defaults_standing(tmp_path):
    """A candidate can write the config this reads. Raising on a malformed one would let it
    stop the surface being computed at all; keeping the defaults is the wider answer."""
    repo = _surface_repo(tmp_path, [
        ("pytest.ini", "[pytest\nthis is not an ini file\n"),
        ("pyproject.toml", "[tool.pytest.ini_options\n"),
        ("tests/test_a.py", "def test_a(): pass\n"),
    ])
    assert "tests/test_a.py" in verify.gate_surface(repo, finspect.GeneratorContract())


def test_a_command_token_that_leaves_the_tree_is_not_surface(tmp_path):
    """A token naming something outside the tree cannot be a candidate path, so it cannot
    be in a delta computed against one — and `..` in a surface would be a path this module
    hands on to a caller that resolves it."""
    repo = _surface_repo(tmp_path, [("check.sh", "#!/bin/sh\nexit 0\n")])
    (tmp_path / "outside.sh").write_text("#!/bin/sh\nexit 1\n")
    s = verify.gate_surface(repo, finspect.GeneratorContract(),
                            command=verify.Command.parse(
                                [["/bin/sh", "check.sh"], ["../outside.sh"]]))
    assert s == ("check.sh",)


def test_a_directory_the_command_names_is_not_itself_surface(tmp_path):
    """A delta is over files. `pytest tests` is covered by the discovery globs underneath
    it, and adding the directory would put a name no delta can ever match into a surface."""
    repo = _surface_repo(tmp_path, [("tests/test_a.py", "def test_a(): pass\n")])
    s = verify.gate_surface(repo, finspect.GeneratorContract(),
                            command=verify.Command.parse([["pytest", "tests"]]))
    assert s == ("tests/test_a.py",)


def test_a_contract_whose_output_is_not_gate_surface_adds_nothing(tmp_path):
    """The relation only reaches the surface through its OUTPUT. Adding every source of
    every relation would put the whole of a generator's input into the gate surface."""
    repo = _surface_repo(tmp_path, [
        ("out/Makefile", "verify:\n\t./run\n"),
        ("src/render.py", "print(1)\n"),
    ])
    s = verify.gate_surface(repo, _contract(("src/*", "out/nothing.mk")))
    assert s == ("out/Makefile",)


def test_a_passing_gate_that_merely_mentions_an_omitted_path_is_not_a_named_failure():
    """§6.2's mechanical check is a FAILING command naming a missing path. A passing run
    whose output happens to print the same name has not identified anything, and reporting
    it in the failing-command form would put a diagnosis in front of a reviewer that no
    failure supports."""
    reason = verify.classify(_run(0, "wrote fixtures/data.bin\n"), _run(0),
                             _bundle(omitted=("fixtures/data.bin",)))[1]
    assert "settles nothing" in reason and "its output names" not in reason


def test_an_ignored_gate_shaped_file_is_outside_the_surface(tmp_path):
    """The boundary `_enumerate` accepts, pinned rather than left to be rediscovered.

    Dropping `--exclude-standard` would catch this file — and would also enumerate every
    `*.test.js` inside `node_modules` and every file in `.venv`, so a candidate that
    reinstalled a dependency would read as a gate change. The cost is stated in
    `gate_surface`'s uncovered list, and `command` is the route that reaches such a file.
    """
    repo = _surface_repo(tmp_path, [(".gitignore", "build/\n")])
    write(repo, "build/test_generated.py", "def test_g(): pass\n")
    assert verify.gate_surface(repo, finspect.GeneratorContract()) == ()


def test_a_pyproject_declared_discovery_widens_the_globs_too(tmp_path):
    """The same rule as `pytest.ini`, through the other parser this has to speak."""
    repo = _surface_repo(tmp_path, [
        ("pyproject.toml",
         '[tool.pytest.ini_options]\npython_files = ["check_*.py", "probe_*.py"]\n'),
        ("check_thing.py", "def test(): pass\n"),
        ("probe_thing.py", "def test(): pass\n"),
        ("src/app.py", "print(1)\n"),
    ])
    s = verify.gate_surface(repo, finspect.GeneratorContract())
    assert "check_thing.py" in s and "probe_thing.py" in s and "src/app.py" not in s
    # `python_files` is whitespace-separated, so TOML lets it be one string as well as a
    # list, and pytest reads both. Handling only the list drops the whole declaration.
    other = _surface_repo(tmp_path / "b", [
        ("pyproject.toml", '[tool.pytest.ini_options]\npython_files = "check_*.py"\n'),
        ("check_thing.py", "def test(): pass\n"),
    ])
    assert "check_thing.py" in verify.gate_surface(other, finspect.GeneratorContract())


def test_a_runner_config_whose_name_varies_by_extension_is_still_surface(tmp_path):
    """`jest.config.{js,ts,mjs,cjs,json}` is one config file with five spellings, which is
    why the runner-config rules have a glob half as well as a name half."""
    repo = _surface_repo(tmp_path, [
        ("jest.config.mjs", "export default {};\n"),
        ("vitest.config.ts", "export default {};\n"),
        ("jest.helpers.mjs", "export const x = 1;\n"),
    ])
    s = verify.gate_surface(repo, finspect.GeneratorContract())
    assert "jest.config.mjs" in s and "vitest.config.ts" in s
    assert "jest.helpers.mjs" not in s


def test_a_ci_directory_below_the_root_is_surface_too(tmp_path):
    """A CI system reads only its own checkout root, so this over-reports by one file in a
    monorepo — and anchoring at the root instead would MISS the gate in every tree this is
    handed a subdirectory of, which is the direction `gate_delta` fails open in."""
    repo = _surface_repo(tmp_path, [
        ("sub/.github/workflows/ci.yml", "on: push\n"),
        ("docs/workflows/ci.yml", "not ci\n"),
        # Two paths that contain the directory's NAME without containing the directory,
        # one on each side. The trailing `/` in the rule rejects the first; only a
        # SEGMENT-anchored match rejects the second, where the name is the tail of a longer
        # segment — a naive `d in path` admits it and widens the surface by a filename.
        ("docs/my.github/workflowsx/a.yml", "not ci\n"),
        ("docs/my.circleci/a.yml", "not ci\n"),
    ])
    s = verify.gate_surface(repo, finspect.GeneratorContract())
    assert "sub/.github/workflows/ci.yml" in s
    assert "docs/workflows/ci.yml" not in s
    assert "docs/my.github/workflowsx/a.yml" not in s
    assert "docs/my.circleci/a.yml" not in s


def test_a_ci_definition_matched_by_name_is_surface_wherever_it_sits(tmp_path):
    """The other half of the CI rule: `Jenkinsfile` and `.gitlab-ci.yml` are named files
    rather than a directory, and a `.gitlab-ci.yml` reached by `include:` sits anywhere."""
    repo = _surface_repo(tmp_path, [
        (".gitlab-ci.yml", "stages: [test]\n"),
        ("ci/Jenkinsfile", "pipeline {}\n"),
        ("ci/notes.md", "prose\n"),
    ])
    s = verify.gate_surface(repo, finspect.GeneratorContract())
    assert ".gitlab-ci.yml" in s and "ci/Jenkinsfile" in s and "ci/notes.md" not in s


def test_outcomes_holds_exactly_what_classify_can_return():
    """`OUTCOMES` exists for a consumer validating a value read back out of a manifest, so
    an outcome missing from it is a value that consumer would reject.

    Read off the module's own `return` statements rather than off a hand-written list: a
    list written here is a second copy of the vocabulary, and a seventh outcome added to
    `classify` and forgotten in `OUTCOMES` would satisfy both copies of the mistake.

    What it cannot see is an outcome the module never NAMES at a return. `classify`'s own
    `return outcome, reason` is one such site and costs nothing, because the value it
    forwards was named in `_run_verdict`, which the walk does read; an outcome built by an
    expression instead would be invisible here.
    """
    returned = set()
    for node in ast.walk(ast.parse(Path(verify.__file__).read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)
                and node.value.elts):
            continue
        head = node.value.elts[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            returned.add(head.value)
        elif isinstance(head, ast.Name) and isinstance(getattr(verify, head.id, None), str):
            returned.add(getattr(verify, head.id))
    assert returned, "the AST walk matched no verdict return, so it pins nothing"
    assert returned == set(verify.OUTCOMES)
    assert len(verify.OUTCOMES) == len(set(verify.OUTCOMES))


def test_an_incomplete_harvest_still_reports_what_the_gate_surface_says(tmp_path):
    """`HARVEST_INCOMPLETE` wins the outcome, but it does not consume the gate facts.

    These are independent measurements: the bundle lost a path, AND nobody measured the
    gate surface (or the candidate moved it). A reviewer deciding whether to re-harvest is
    the same reviewer who has to decide whether the gate can be trusted afterwards, and
    dropping the second fact makes the verdict read cleaner than its evidence. `gate_delta
    is None` is the shape of every real bundle today, so this is not a corner case.
    """
    outcome, reason = verify.classify(
        _run(0), _run(0), _bundle(gate_delta=("Makefile",), omitted=("sub",)))
    assert outcome == verify.HARVEST_INCOMPLETE
    assert "sub" in reason and "Makefile" in reason

    outcome, reason = verify.classify(
        _run(0), _run(0), _bundle(gate_delta=None, omitted=("sub",)))
    assert outcome == verify.HARVEST_INCOMPLETE
    assert "nobody measured the gate surface" in reason


def test_a_step_cwd_that_leaves_the_tree_contributes_no_surface(tmp_path):
    """`Step` is public and directly constructible, and `__post_init__` validates argv
    only — so `cwd` is a caller-supplied path this function must contain itself.

    The token guard alone does not: it is applied to the token and the cwd is prepended
    afterwards, so a clean token under an escaping cwd produced `../outside/check.sh` and
    `/etc/passwd` — names no delta computed against this tree can ever match, and an
    absolute one this module would be handing on to a caller that resolves it.
    """
    repo = _surface_repo(tmp_path, [("check.sh", "#!/bin/sh\nexit 0\n")])
    (tmp_path / "outside").mkdir(exist_ok=True)
    (tmp_path / "outside" / "check.sh").write_text("#!/bin/sh\nexit 1\n")

    escaping = verify.Command(steps=(verify.Step(argv=("./check.sh",), cwd="../outside"),))
    assert verify.gate_surface(repo, finspect.GeneratorContract(), command=escaping) == ()

    absolute = verify.Command(steps=(verify.Step(argv=("passwd",), cwd="/etc"),))
    assert verify.gate_surface(repo, finspect.GeneratorContract(), command=absolute) == ()

    # The spelling that got through the first fix: the containment check said `.//etc` is
    # contained -- it is, it means `etc` -- and a leading-`./` STRIP applied afterwards
    # rewrote it to `/etc`. Vacuous on a host with no /etc/passwd; the discriminating form
    # is the tmp_path one in test_the_cwd_rule_is_one_rule_so_both_callers_read_the_same_path.
    slashed = verify.Command(steps=(verify.Step(argv=("passwd",), cwd=".//etc"),))
    assert verify.gate_surface(repo, finspect.GeneratorContract(), command=slashed) == ()

    # The same rule `_step_cwd` enforces, which is why both refuse the same two values.
    for step in (escaping.steps[0], absolute.steps[0]):
        with pytest.raises(verify.VerifyError):
            verify._step_cwd(Path(repo), step, 0)

    # A cwd that stays inside still contributes, or the fix would have closed the feature.
    inside = verify.Command(steps=(verify.Step(argv=("./check.sh",), cwd="."),))
    assert verify.gate_surface(repo, finspect.GeneratorContract(), command=inside) \
        == ("check.sh",)


def test_the_cwd_rule_is_one_rule_so_both_callers_read_the_same_path(tmp_path):
    """`_step_cwd` decides where a step RUNS, `_command_paths` decides which files DEFINE
    it, and a spelling the two read differently is a hole either way: a laxer surface names
    files outside the tree, a stricter one omits the files of a step that really runs.

    The first was in the code. Checking `.//etc` -- contained, it means `etc` -- and then
    STRIPPING the leading `./` off it produced `/etc`, so the surface named `/etc/passwd`
    while the step ran inside the tree. Moving the strip above the check closes that one
    and makes the mirror SYSTEMATIC: `.//sub` is then refused by the surface and still run,
    so a candidate hides `sub/check.sh` from `gate_delta` by writing one extra slash.
    Before the reorder that spelling is silent too, but only because the `/sub/check.sh` it
    joins happens not to exist on this host -- an accident, where the reorder makes it a
    rule. A
    leading-`./` strip is not a path normalization at all -- `.//x` is a RELATIVE path
    meaning `x`, which is where `chdir` goes with it -- so the two share the step that
    produces the path, not only the predicate that judges it.
    """
    repo = _surface_repo(tmp_path, [("sub/check.sh", "#!/bin/sh\nexit 0\n")])
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "check.sh").write_text("#!/bin/sh\nexit 1\n")

    def surface(cwd, argv=("check.sh",)):
        return verify.gate_surface(repo, finspect.GeneratorContract(), command=verify.Command(
            steps=(verify.Step(argv=argv, cwd=cwd),)))

    def runs_in(cwd):
        try:
            return verify._step_cwd(Path(repo), verify.Step(argv=("true",), cwd=cwd), 0)
        except verify.VerifyError:
            return None

    # Seven spellings of one directory. Each must contribute the path under the spelling
    # the TREE holds it under -- `.//sub/check.sh` names a file no `gate_delta` can match.
    for cwd in ("sub", "./sub", ".//sub", "sub/", "./sub/", "././/sub", "sub/."):
        assert surface(cwd) == ("sub/check.sh",), cwd
        assert runs_in(cwd) == Path(repo) / "sub", cwd
    for token in ("check.sh", "./check.sh", ".//check.sh", "././check.sh"):
        assert surface("sub", argv=(token,)) == ("sub/check.sh",), token

    # Refused by both. `a/../b` may well be inside; `..` is refused rather than collapsed,
    # because collapsing it disagrees with the kernel whenever `a` is a symlink.
    for cwd in ("/etc", "../outside", "a/../b", "sub/../../outside"):
        assert surface(cwd) == (), cwd
        assert runs_in(cwd) is None, cwd

    # `.//<absolute>` is the shape that leaked, and it is not an escape at all: the `.` and
    # the absolute path's own leading slash spell one RELATIVE path, so both callers must
    # read it as one -- inside the tree, naming nothing, never the out-of-tree file below.
    inside_looking_absolute = f"./{outside}"
    assert surface(inside_looking_absolute) == ()
    assert runs_in(inside_looking_absolute) == Path(repo) / str(outside).lstrip("/")
    assert (outside / "check.sh").is_file(), "the fixture stopped discriminating"


def test_the_js_test_globs_cover_both_runners_documented_extension_set(tmp_path):
    """Vitest's `include` is `**/*.{test,spec}.?(c|m)[jt]s?(x)` and Jest's `testMatch` is
    `**/?(*.)+(spec|test).?([mc])[jt]s?(x)`, so `.test.mts` is a test file in a TS-ESM repo
    and a bare `test.js` is one under Jest. A glob set narrower than the runner's is the
    fail-OPEN direction: the file is collected, the surface does not name it, and deleting
    it never reaches `gate_delta`.
    """
    repo = _surface_repo(tmp_path, [
        ("a.test.mts", "test('x', () => {});\n"),
        ("b.spec.cts", "test('x', () => {});\n"),
        ("c.test.tsx", "test('x', () => {});\n"),
        ("test.js", "test('x', () => {});\n"),
        ("spec.ts", "test('x', () => {});\n"),
        ("latest.ts", "export const x = 1;\n"),
        ("manifest.json", "{}\n"),
    ])
    s = verify.gate_surface(repo, finspect.GeneratorContract())
    for name in ("a.test.mts", "b.spec.cts", "c.test.tsx", "test.js", "spec.ts"):
        assert name in s, name
    # `*test.*` would sweep these in. Neither runner collects them.
    assert "latest.ts" not in s and "manifest.json" not in s


def test_a_candidate_written_ignore_rule_can_hide_a_gate_file_it_added(tmp_path):
    """The gap I3 names, pinned rather than left to be rediscovered.

    `.gitignore` carries no gate role, so editing it is not a `gate_delta` entry — while it
    decides what `_enumerate` can see at all. A candidate arriving as sidecars can ship a
    `conftest.py` that drops a failing test plus one ignore line, and the surface goes
    quiet. Bounded to files the candidate ADDED: `--cached` ignores excludes, so the same
    line over a TRACKED conftest changes nothing, which the second half asserts.
    """
    added = _surface_repo(tmp_path / "added", [], commit=False)
    write(added, "tests/test_a.py", "def test_a(): pass\n")
    write(added, "tests/conftest.py", "collect_ignore_glob = ['*']\n")
    assert "tests/conftest.py" in verify.gate_surface(added, finspect.GeneratorContract())
    write(added, ".gitignore", "tests/conftest.py\n")
    hidden = verify.gate_surface(added, finspect.GeneratorContract())
    assert "tests/conftest.py" not in hidden and ".gitignore" not in hidden

    tracked = _surface_repo(tmp_path / "tracked", [
        ("tests/test_a.py", "def test_a(): pass\n"),
        ("tests/conftest.py", "x = 1\n"),
    ])
    write(tracked, ".gitignore", "tests/conftest.py\n")
    assert "tests/conftest.py" in verify.gate_surface(tracked, finspect.GeneratorContract())
