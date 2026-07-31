"""Run-directory layout and quotas (spec §15)."""
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import gitcmd, storage  # noqa: E402
from forge_fixtures import commit_all, make_repo, write  # noqa: E402


def test_run_root_is_under_xdg_state_and_hashed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    p = storage.run_root(Path("/home/u/git/utils"), "abc123")
    assert p.parent.parent == tmp_path / "state"
    assert p.parent.name == "khenrix-forge"
    # hashed repo path, not basename: two repos with the same basename must not collide
    q = storage.run_root(Path("/home/u/work/utils"), "abc123")
    assert p != q
    assert p.name.endswith("-abc123") and len(p.name) == 12 + 1 + 6


def test_run_root_creates_0700(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    p = storage.run_root(Path("/home/u/git/utils"), "abc123")
    assert p.is_dir()
    assert stat.S_IMODE(p.stat().st_mode) == 0o700


def test_run_root_rejects_a_colliding_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    p = storage.run_root(Path("/home/u/git/utils"), "abc123")
    # same repo, same run id: two runs must never share the directory holding their work
    with pytest.raises(FileExistsError):
        storage.run_root(Path("/home/u/git/utils"), "abc123")
    # ...unless the caller is deliberately reattaching to a run that already exists
    assert storage.run_root(Path("/home/u/git/utils"), "abc123", must_be_new=False) == p


def test_run_root_defaults_without_xdg(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    p = storage.run_root(Path("/x/y"), "r1")
    assert p.parent.parent == tmp_path / "home" / ".local" / "state"


def test_new_run_id_is_short_and_unique():
    a, b = storage.new_run_id(), storage.new_run_id()
    assert a != b and len(a) == 6 and a.isalnum()


def test_quota_default_and_breach():
    q = storage.Quota.default()
    assert q.max_files > 0 and q.max_file_bytes > 0 and q.max_total_bytes > 0
    small = storage.Quota(max_files=2, max_file_bytes=10, max_total_bytes=100)
    assert small.breach(files=1, file_bytes=5, total_bytes=50) is None
    assert "files" in small.breach(files=3, file_bytes=5, total_bytes=50)
    assert "file_bytes" in small.breach(files=1, file_bytes=99, total_bytes=50)
    assert "total_bytes" in small.breach(files=1, file_bytes=5, total_bytes=999)


def test_git_readonly_preset_reaches_the_child(tmp_path):
    # A `!`-alias is the only way to see what git's own child process was handed; asserting
    # that rev-parse merely succeeded would pass just as well with env_extra ignored.
    repo = make_repo(tmp_path)
    gitcmd.git(repo, "config", "alias.envprobe", "!printenv GIT_OPTIONAL_LOCKS")
    assert gitcmd.git(repo, "envprobe", env_extra=gitcmd.READONLY).stdout.strip() == "0"
    # not vacuous: printenv exits 1 and prints nothing when the variable is unset
    bare = gitcmd.git(repo, "envprobe", check=False)
    assert bare.returncode != 0 and bare.stdout.strip() == ""


def test_git_ignores_an_ambient_git_dir(tmp_path, monkeypatch):
    # Under a hook, `git rebase --exec` or `git bisect run`, GIT_DIR is exported and beats
    # `-C`; inheriting it would aim the engine at the user's repository.
    a = make_repo(tmp_path, "a")
    b = make_repo(tmp_path, "b")
    write(b, "b-only.txt", "b\n")
    head_b = commit_all(b, "b-only")          # distinct history, so the OIDs differ
    head_a = gitcmd.git(a, "rev-parse", "HEAD").stdout.strip()
    assert head_a != head_b

    monkeypatch.setenv("GIT_DIR", str(b / ".git"))
    assert gitcmd.git(a, "rev-parse", "HEAD").stdout.strip() == head_a


def test_git_env_extra_wins_over_the_scrub(tmp_path):
    # The scrub runs BEFORE env_extra, so a deliberate GIT_INDEX_FILE still takes effect —
    # baseline construction depends on exactly this ordering.
    repo = make_repo(tmp_path)
    assert gitcmd.git(repo, "ls-files").stdout.split() == ["seed.txt"]
    alt = tmp_path / "alt.index"
    r = gitcmd.git(repo, "ls-files", env_extra={"GIT_INDEX_FILE": str(alt)})
    assert r.stdout.strip() == ""


def test_git_check_raises_on_failure(tmp_path):
    repo = make_repo(tmp_path)
    try:
        gitcmd.git(repo, "rev-parse", "--verify", "refs/heads/nope")
    except gitcmd.GitError as e:
        assert "nope" in str(e) or "fatal" in str(e).lower()
    else:
        raise AssertionError("expected GitError")
    r = gitcmd.git(repo, "rev-parse", "--verify", "refs/heads/nope", check=False)
    assert r.returncode != 0


def test_zero_oid_matches_repo_hash_width(tmp_path):
    repo = make_repo(tmp_path)
    z = gitcmd.zero_oid(repo)
    assert set(z) == {"0"}
    head = gitcmd.git(repo, "rev-parse", "HEAD").stdout.strip()
    assert len(z) == len(head)
