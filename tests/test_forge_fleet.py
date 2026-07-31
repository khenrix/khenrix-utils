"""Independent clones, not worktrees — and the push vector actually closed (spec §4)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import baseline, fleet, gitcmd, inspect as finspect  # noqa: E402
# The fixtures' env builder is reused rather than restated: duplicating the nine
# location variables is the copy most likely to drift out of step with them.
from forge_fixtures import _env as _hermetic_env, make_repo, write  # noqa: E402


def _mk_baseline(repo, run, selected=()):
    f = finspect.repo_facts(repo)
    return baseline.materialize(repo, run, f, list(selected), "r1")


def _git(repo, *a):
    """stdout of a git command that MUST succeed, in a hermetic environment.

    The return code is checked rather than discarded: a failing `git remote` also prints
    nothing, so a swallowed error would read as "no remotes" and pass vacuously.

    The environment is the fixtures' own, because this helper also WRITES (a branch and a
    commit, below). Under the developer's real config a global `core.hooksPath` runs their
    hooks inside these throwaway repos and `commit.gpgsign = true` fails the commit — the
    hazard forge_fixtures' docstring exists to prevent. It can only manufacture a false
    RED, never a false green, but this suite is the gate and must not depend on the machine.
    """
    r = subprocess.run(["git", "-C", str(repo), *a],
                       capture_output=True, text=True, env=_hermetic_env())
    assert r.returncode == 0, f"git {' '.join(a)} failed: {r.stderr.strip()}"
    return r.stdout.strip()


def test_clone_checks_out_the_dirty_baseline_not_head(tmp_path):
    """--single-branch alone follows the source's HEAD and would silently drop the
    user's uncommitted work from every seat."""
    repo = make_repo(tmp_path)
    write(repo, "seed.txt", "modified\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk_baseline(repo, run)
    seat = fleet.clone_seat(repo, b, tmp_path / "seat1").path
    assert (seat / "seed.txt").read_text() == "modified\n"
    assert _git(seat, "rev-parse", "HEAD") == b.commit


def test_clone_has_no_origin(tmp_path):
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    seat = fleet.clone_seat(repo, _mk_baseline(repo, run), tmp_path / "seat1").path
    assert _git(seat, "remote") == "", "git clone always sets origin; it must be removed"


def test_clone_is_not_hardlinked_to_the_source_objects(tmp_path):
    """A rogue non-git write through a shared inode corrupts the USER's repository."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    seat = fleet.clone_seat(repo, _mk_baseline(repo, run), tmp_path / "seat1").path
    src = {p.stat().st_ino for p in (Path(repo) / ".git" / "objects").rglob("*") if p.is_file()}
    dst = {p.stat().st_ino for p in (seat / ".git" / "objects").rglob("*") if p.is_file()}
    assert src and dst, "nothing was compared"
    assert not (src & dst), "clone shares object inodes with the source"


def test_clone_transfers_no_object_unreachable_from_the_baseline_ref(tmp_path):
    """--no-local does work of its own, which the inode test cannot see.

    The local transport copies the WHOLE object store; --no-hardlinks only stops it being
    copied by LINK. So without --no-local a seat also receives every object the baseline
    ref does not reach — other branches, stashes, dangling history — none of which the
    pre-launch secret screen ever looked at, since it screens the baseline.
    """
    repo = make_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "unrelated")
    write(repo, "secret.txt", "token-on-a-branch-the-baseline-never-reaches\n")
    _git(repo, "add", "secret.txt")
    _git(repo, "commit", "-qm", "secret")
    planted = _git(repo, "rev-parse", "HEAD:secret.txt")
    _git(repo, "checkout", "-q", "main")

    run = tmp_path / "run"; run.mkdir()
    b = _mk_baseline(repo, run)
    seat = fleet.clone_seat(repo, b, tmp_path / "seat1").path

    present = subprocess.run(["git", "-C", str(seat), "cat-file", "-e", planted],
                             capture_output=True)
    assert present.returncode != 0, "an object unreachable from B1 was transferred"

    reachable = {ln.split()[0] for ln in
                 _git(seat, "rev-list", "--objects", b.commit).splitlines() if ln}
    stored = set(_git(seat, "cat-file", "--batch-all-objects",
                      "--batch-check=%(objectname)").split())
    assert stored, "nothing was compared"
    assert stored <= reachable, f"seat holds {len(stored - reachable)} unreachable object(s)"


def test_clone_refuses_a_seat_whose_remote_survives(tmp_path, monkeypatch):
    """The postcondition is what closes the push vector, not `remote remove`'s exit code.

    Also pins the failure-path cleanup: a refused seat must not leave its template dir
    behind for the next seat at that path to inherit.
    """
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    real = fleet.gitcmd.git

    def remove_is_a_no_op(target, *a, **kw):
        if a[:2] == ("remote", "remove"):
            return subprocess.CompletedProcess(a, 0, "", "")
        return real(target, *a, **kw)

    monkeypatch.setattr(fleet.gitcmd, "git", remove_is_a_no_op)
    dest = tmp_path / "seat1"
    with pytest.raises(fleet.FleetError, match="push target"):
        fleet.clone_seat(repo, _mk_baseline(repo, run), dest)
    assert not (dest.parent / f".{dest.name}.tmpl").exists(), "template dir leaked"


def test_clone_carries_no_hooks_from_a_global_template(tmp_path, monkeypatch):
    """No ambient template dir installs hooks into a seat.

    Both machine-wide forms are set here, because only one of them can still fire.
    Measured on git 2.53: `init.templateDir` read from GIT_CONFIG_GLOBAL never reaches the
    seat at all — gitcmd pins GIT_CONFIG_GLOBAL to /dev/null on every call — so a test that
    set only that would pass with `--template=` deleted and prove nothing about the empty
    template dir. GIT_TEMPLATE_DIR is the form that survives the pin: it is an environment
    variable rather than config and is not one of gitcmd's REDIRECTING_ENV names, so without
    `--template=<empty>` the hook is installed into the seat AND executes during checkout.
    """
    repo = make_repo(tmp_path)
    tmpl = tmp_path / "tmpl" / "hooks"; tmpl.mkdir(parents=True)
    ran = tmp_path / "forge-hook-ran"
    (tmpl / "post-checkout").write_text(f"#!/bin/sh\ntouch {ran}\n")
    (tmpl / "post-checkout").chmod(0o755)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f"[init]\n\ttemplateDir = {tmpl.parent}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tmpl.parent))
    run = tmp_path / "run"; run.mkdir()
    seat = fleet.clone_seat(repo, _mk_baseline(repo, run), tmp_path / "seat1").path
    assert not (seat / ".git" / "hooks" / "post-checkout").exists()
    assert not ran.exists(), "a template hook executed inside the seat"


def test_ignore_semantics_are_replayed_from_a_linked_worktree(tmp_path):
    """`<repo>/.git` is a FILE in a linked worktree, so the string-joined path resolves to
    nothing, `is_file()` answers False without raising, and the seat gets NO excludes —
    a clean clone at the right ref with a silently missing file. The git dir must be asked
    for, and specifically the COMMON one: `info/exclude` is shared, not per-worktree, so
    `--absolute-git-dir` would point at `.git/worktrees/<name>/` and find nothing either.
    """
    repo = make_repo(tmp_path)
    wt = tmp_path / "wt"
    gitcmd.git(repo, "worktree", "add", "-q", str(wt), "-b", "feat")
    assert (wt / ".git").is_file(), "fixture precondition: a linked worktree uses a .git file"
    excl = repo / ".git" / "info" / "exclude"
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text("# forge marker\nbuild-scratch/\n")
    run = tmp_path / "run"; run.mkdir()
    s = fleet.clone_seat(wt, _mk_baseline(wt, run), tmp_path / "seat1")
    assert (s.path / ".git" / "info" / "exclude").read_text() == excl.read_text()
    assert "info/exclude" in s.replayed


def test_the_replay_record_reports_nothing_when_there_was_nothing_to_replay(tmp_path):
    """The record has to DISCRIMINATE, or it is decoration: a constant `("info/exclude",)`
    would satisfy the test above while the copy silently did nothing."""
    repo = make_repo(tmp_path)
    (repo / ".git" / "info" / "exclude").unlink(missing_ok=True)
    run = tmp_path / "run"; run.mkdir()
    s = fleet.clone_seat(repo, _mk_baseline(repo, run), tmp_path / "seat1")
    assert s.replayed == ()


def test_forge_child_env_strips_gits_redirectors_and_pins_config(tmp_path, monkeypatch):
    """scrub_env cannot see these: they do damage by pointing AWAY from the checkout.

    GIT_CONFIG_COUNT re-enables the very hooks clone_seat's empty `--template=` excluded,
    and an ambient absolute GIT_DIR aims a write-enabled agent at a repository that is not
    its clone. Both look repo-external, so both survive a scrub that drops only what points
    inward. The seat gets what gitcmd.git gives an engine call instead.
    """
    repo = make_repo(tmp_path)
    other = make_repo(tmp_path, "other")
    evil = tmp_path / "evil"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(evil))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    out = fleet.forge_child_env(repo)

    for k in ("GIT_CONFIG_COUNT", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_DIR"):
        assert k not in out, k
    assert out["GIT_CONFIG_GLOBAL"] == os.devnull
    assert out["GIT_CONFIG_SYSTEM"] == os.devnull

    # GIT_CONFIG_KEY_0/VALUE_0 remain as inert strings — git reads them only under
    # GIT_CONFIG_COUNT, exactly as in gitcmd.git — so the fourth variable is pinned by
    # EFFECT rather than by absence: assert the injection is dead, not merely disarmed
    # in a way this test asserts by restating the implementation.
    def _git_under_child_env(*a):
        r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True,
                           env=out)
        return r.stdout.strip()

    assert _git_under_child_env("config", "--get", "core.hooksPath") == "", \
        "core.hooksPath was injected into every git the seat runs"
    assert Path(_git_under_child_env("rev-parse", "--absolute-git-dir")).resolve() \
        == (repo / ".git").resolve(), "an ambient GIT_DIR redirected the seat's git"


def test_scrub_env_removes_only_values_pointing_at_the_repo(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    # Run FROM the repo: that is what makes `EDITOR=vim` discriminating. Resolving a value
    # with os.path.realpath expands a relative string against the CWD, so from inside the
    # checkout every non-path scalar would classify as repo-internal and be deleted.
    monkeypatch.chdir(repo)
    env = {"VIRTUAL_ENV": f"{repo}/.venv", "PYTHONPATH": f"{repo}/src",
           "PATH": "/usr/bin:/home/u/.local/share/mise/shims", "HOME": "/home/u",
           "EDITOR": "vim"}
    out = fleet.scrub_env(env, repo)
    assert "VIRTUAL_ENV" not in out and "PYTHONPATH" not in out
    assert out["PATH"] == env["PATH"], "PATH must survive: mise shims live outside the repo"
    assert out["HOME"] == "/home/u" and out["EDITOR"] == "vim"


def test_scrub_env_keeps_the_path_entries_that_are_outside_the_repo(tmp_path):
    """One repo-internal entry must not cost the whole variable.

    On this machine PATH really does contain a repo-internal directory — a plugin `bin`
    installed by plugin loading — with no venv involved, so dropping PATH wholesale is
    today's behaviour on forge's own repository, not a hypothetical.
    """
    repo = make_repo(tmp_path)
    out = fleet.scrub_env({"PATH": f"{repo}/.venv/bin{os.pathsep}/usr/bin"}, repo)
    assert out["PATH"] == "/usr/bin"


def test_scrub_env_drops_a_value_that_embeds_the_repo_inside_a_larger_token(tmp_path):
    """The checkout usually arrives inside a flag, not as the whole value.

    A whole-value path test misses this entire class, and misses it incoherently: a
    colon-delimited embed (`-javaagent:<repo>/a.jar`) is caught by the os.pathsep split
    while the space- and `=`-delimited forms are not.
    """
    repo = make_repo(tmp_path)
    env = {"CFLAGS": f"-I{repo}/include",
           "LDFLAGS": f"-L{repo}/build/lib",
           "RUSTFLAGS": f"-L native={repo}/target/debug",
           "NODE_OPTIONS": f"--require {repo}/tools/hook.js",
           "PYTEST_ADDOPTS": f"--rootdir={repo}",
           "MAKEFLAGS": f"-I{repo}/mk",
           "JAVA_TOOL_OPTIONS": f"-javaagent:{repo}/a.jar",
           # ld.so takes spaces as well as colons, and the repo entry is not first — an
           # os.pathsep-only split cannot see it.
           "LD_PRELOAD": f"/usr/lib/other.so {repo}/shim.so"}
    assert fleet.scrub_env(env, repo) == {}, "a repo path survived inside a larger token"


def test_scrub_env_does_not_treat_a_sibling_prefix_as_inside_the_repo(tmp_path):
    """`<root>-scratch` starts with `<root>` but is not inside it."""
    repo = tmp_path / "khenrix-utils"
    repo.mkdir()
    sibling = f"{repo}-scratch/bin"
    out = fleet.scrub_env({"TOOLS": sibling, "CFLAGS": f"-I{sibling}",
                           "PATH": f"{sibling}{os.pathsep}/usr/bin"}, repo)
    assert out["TOOLS"] == sibling
    assert out["CFLAGS"] == f"-I{sibling}", "the right boundary must survive the embed test"
    assert out["PATH"] == f"{sibling}{os.pathsep}/usr/bin"


def test_scrub_env_normalises_a_value_before_calling_it_outside_the_repo(tmp_path):
    """`embeds` matches literal text, so a non-normalized spelling of the checkout is not
    a match for any root. The path-shaped branch got normalisation for free from its
    per-entry `_inside`; the wholesale branch had no normalising test at all, so these
    three escaped on that branch only."""
    repo = tmp_path / "git" / "repo"
    repo.mkdir(parents=True)
    env = {"A": f"{tmp_path}/git/./repo/src",
           "B": f"{tmp_path}/git/other/../repo/s",
           "C": f"{tmp_path}/git//repo/src"}
    assert fleet.scrub_env(env, repo) == {}


def test_forge_depth_guard_increments(tmp_path):
    repo = make_repo(tmp_path)
    e1 = fleet.forge_child_env(repo, {"PATH": "/usr/bin"})
    assert e1["LLM_FORGE_DEPTH"] == "1"
    assert fleet.forge_child_env(repo, e1)["LLM_FORGE_DEPTH"] == "2"


def test_forge_child_env_scrubs_the_process_environment_by_default(tmp_path, monkeypatch):
    """`env=None` is the path a seat actually gets, and the one where a repo-internal PATH
    entry shows up in practice — so it is the path that must not lose the toolchain."""
    repo = make_repo(tmp_path)
    monkeypatch.setenv("PATH", f"{repo}/.venv/bin{os.pathsep}/usr/bin")
    monkeypatch.setenv("VIRTUAL_ENV", f"{repo}/.venv")
    monkeypatch.delenv("LLM_FORGE_DEPTH", raising=False)
    out = fleet.forge_child_env(repo)
    assert out["PATH"] == "/usr/bin", "a seat must still have a usable toolchain"
    assert "VIRTUAL_ENV" not in out
    assert out["LLM_FORGE_DEPTH"] == "1"


def test_seats_are_independent_of_each_other(tmp_path):
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    b = _mk_baseline(repo, run)
    s1 = fleet.clone_seat(repo, b, tmp_path / "s1").path
    s2 = fleet.clone_seat(repo, b, tmp_path / "s2").path
    (s1 / "seed.txt").write_text("seat one only\n")
    # Identity is supplied per-invocation: a clone carries none of the source's LOCAL
    # config, and falling back to the developer's ~/.gitconfig is the non-hermeticity
    # forge_fixtures exists to prevent.
    subprocess.run(["git", "-C", str(s1), "-c", "user.name=Seat",
                    "-c", "user.email=seat@example.invalid", "commit", "-aqm", "s1"],
                   check=True, capture_output=True, text=True, env=_hermetic_env())
    assert (s2 / "seed.txt").read_text() == "seed\n"
    assert _git(repo, "rev-parse", "HEAD") == b.base_commit, "user repo HEAD moved"


def test_forge_child_env_raises_a_named_error_on_a_bad_depth(tmp_path):
    """A non-numeric ambient depth is a caller error, not an engine crash.

    `int()` raises a bare ValueError from inside a function that otherwise only ever
    returns a dict, so the one thing a caller could catch is indistinguishable from any
    other ValueError raised beneath it.
    """
    repo = make_repo(tmp_path)
    with pytest.raises(fleet.ForgeEnvError, match="LLM_FORGE_DEPTH"):
        fleet.forge_child_env(repo, {"LLM_FORGE_DEPTH": "not-a-number"})
    # And catchable as a seat-construction failure, which is what it is: a caller that wraps
    # `clone_seat` + `forge_child_env` in `except FleetError` must not miss half of them
    # because the two errors from one module happen to be siblings.
    with pytest.raises(fleet.FleetError):
        fleet.forge_child_env(repo, {"LLM_FORGE_DEPTH": "not-a-number"})
