"""Independent clones, not worktrees — and the push vector actually closed (spec §4)."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import baseline, fleet, inspect as finspect  # noqa: E402
from forge_fixtures import make_repo, write  # noqa: E402


def _mk_baseline(repo, run, selected=()):
    f = finspect.repo_facts(repo)
    return baseline.materialize(repo, run, f, list(selected), "r1")


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a],
                          capture_output=True, text=True).stdout.strip()


def test_clone_checks_out_the_dirty_baseline_not_head(tmp_path):
    """--single-branch alone follows the source's HEAD and would silently drop the
    user's uncommitted work from every seat."""
    repo = make_repo(tmp_path)
    write(repo, "seed.txt", "modified\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk_baseline(repo, run)
    seat = fleet.clone_seat(repo, b, tmp_path / "seat1")
    assert (seat / "seed.txt").read_text() == "modified\n"
    assert _git(seat, "rev-parse", "HEAD") == b.commit


def test_clone_has_no_origin(tmp_path):
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    seat = fleet.clone_seat(repo, _mk_baseline(repo, run), tmp_path / "seat1")
    assert _git(seat, "remote") == "", "git clone always sets origin; it must be removed"


def test_clone_is_not_hardlinked_to_the_source_objects(tmp_path):
    """A rogue non-git write through a shared inode corrupts the USER's repository."""
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    seat = fleet.clone_seat(repo, _mk_baseline(repo, run), tmp_path / "seat1")
    src = {p.stat().st_ino for p in (Path(repo) / ".git" / "objects").rglob("*") if p.is_file()}
    dst = {p.stat().st_ino for p in (seat / ".git" / "objects").rglob("*") if p.is_file()}
    assert src and dst, "nothing was compared"
    assert not (src & dst), "clone shares object inodes with the source"


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
    seat = fleet.clone_seat(repo, _mk_baseline(repo, run), tmp_path / "seat1")
    assert not (seat / ".git" / "hooks" / "post-checkout").exists()
    assert not ran.exists(), "a template hook executed inside the seat"


def test_scrub_env_removes_only_values_pointing_at_the_repo(tmp_path):
    repo = make_repo(tmp_path)
    env = {"VIRTUAL_ENV": f"{repo}/.venv", "PYTHONPATH": f"{repo}/src",
           "PATH": "/usr/bin:/home/u/.local/share/mise/shims", "HOME": "/home/u",
           "EDITOR": "vim"}
    out = fleet.scrub_env(env, repo)
    assert "VIRTUAL_ENV" not in out and "PYTHONPATH" not in out
    assert out["PATH"] == env["PATH"], "PATH must survive: mise shims live outside the repo"
    assert out["HOME"] == "/home/u" and out["EDITOR"] == "vim"


def test_forge_depth_guard_increments(tmp_path):
    repo = make_repo(tmp_path)
    e1 = fleet.forge_child_env(repo, {"PATH": "/usr/bin"})
    assert e1["LLM_FORGE_DEPTH"] == "1"
    assert fleet.forge_child_env(repo, e1)["LLM_FORGE_DEPTH"] == "2"


def test_seats_are_independent_of_each_other(tmp_path):
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    b = _mk_baseline(repo, run)
    s1 = fleet.clone_seat(repo, b, tmp_path / "s1")
    s2 = fleet.clone_seat(repo, b, tmp_path / "s2")
    (s1 / "seed.txt").write_text("seat one only\n")
    # Identity is supplied per-invocation: a clone carries none of the source's LOCAL
    # config, and falling back to the developer's ~/.gitconfig is the non-hermeticity
    # forge_fixtures exists to prevent.
    subprocess.run(["git", "-C", str(s1), "-c", "user.name=Seat",
                    "-c", "user.email=seat@example.invalid", "commit", "-aqm", "s1"],
                   check=True, capture_output=True, text=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                        "GIT_CONFIG_SYSTEM": os.devnull})
    assert (s2 / "seed.txt").read_text() == "seed\n"
    assert _git(repo, "rev-parse", "HEAD") == b.base_commit, "user repo HEAD moved"
