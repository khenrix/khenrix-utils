"""B is composite; construction never touches the user's index (spec §2)."""
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import baseline, gitcmd, inspect as finspect  # noqa: E402
from forge_fixtures import make_repo, write  # noqa: E402


def _idx(repo):
    return hashlib.sha256((Path(repo) / ".git" / "index").read_bytes()).hexdigest()


def _tree_paths(repo, tree):
    out = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", tree],
                         capture_output=True, text=True, check=True).stdout
    return set(out.split())


def _mk(repo, run, selected=()):
    f = finspect.repo_facts(repo)
    return baseline.materialize(repo, run, f, list(selected), "r1")


def test_clean_tree_creates_no_commit(tmp_path):
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run)
    assert b.dirty is False
    assert b.commit == b.base_commit, "a clean baseline must not invent history"


def test_dirty_tree_captures_staged_unstaged_and_selected_untracked(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "s.txt", "staged\n")
    subprocess.run(["git", "-C", str(repo), "add", "s.txt"], check=True)
    write(repo, "seed.txt", "modified\n")          # unstaged change to a tracked file
    write(repo, "chosen.txt", "picked\n")          # untracked, selected
    write(repo, "ignored_by_user.txt", "no\n")     # untracked, NOT selected
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["chosen.txt"])
    assert b.dirty is True and b.commit != b.base_commit
    paths = _tree_paths(repo, b.tracked_tree_oid)
    assert {"s.txt", "seed.txt", "chosen.txt"} <= paths
    assert "ignored_by_user.txt" not in paths, "add -A leaked an unselected path"
    blob = subprocess.run(["git", "-C", str(repo), "show", f"{b.tracked_tree_oid}:seed.txt"],
                          capture_output=True, text=True, check=True).stdout
    assert blob == "modified\n", "unstaged content missing from the baseline"


def test_materialize_never_writes_the_user_index(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "s.txt", "x\n")
    subprocess.run(["git", "-C", str(repo), "add", "s.txt"], check=True)
    write(repo, "s.txt", "y\n")                    # stale cache-tree
    run = tmp_path / "run"; run.mkdir()
    before = _idx(repo)
    _mk(repo, run)
    assert _idx(repo) == before, "write-tree ran against the real index"


def test_ref_is_created_and_reachable(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["d.txt"])
    assert b.ref == "refs/khenrix-forge/r1/base"
    got = subprocess.run(["git", "-C", str(repo), "rev-parse", b.ref],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert got == b.commit


def test_b1_carries_user_authorship(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["d.txt"])
    who = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>%n%s",
                          b.commit], capture_output=True, text=True, check=True).stdout
    assert "Fixture <fixture@example.invalid>" in who
    assert "uncommitted working tree" in who


def test_path_with_glob_characters_survives_literally(tmp_path):
    # The sibling is what makes this discriminating. Selecting only "weird[1].txt", git 2.53
    # adds that file either way — so presence alone passes with or without literal mode. What
    # non-literal mode also does is expand `[1]` as a character class, whose one real match is
    # `weird1.txt`: a file the user did NOT select, silently swept into the baseline.
    repo = make_repo(tmp_path)
    write(repo, "weird[1].txt", "w\n")
    write(repo, "weird1.txt", "not selected\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["weird[1].txt"])
    paths = _tree_paths(repo, b.tracked_tree_oid)
    assert "weird[1].txt" in paths
    assert "weird1.txt" not in paths, "the pathspec was globbed instead of taken literally"


def test_alternate_index_is_seeded_from_the_real_git_dir_in_a_linked_worktree(tmp_path):
    # A linked worktree's `.git` is a FILE, so `<repo>/.git/index` does not exist and the
    # alternate index would start EMPTY — `add -u` then has no tracked entry to update and
    # the baseline would drop every tracked file. The git dir must be asked for, not assumed.
    repo = make_repo(tmp_path)
    wt = tmp_path / "wt"
    gitcmd.git(repo, "worktree", "add", "-q", str(wt), "-b", "feat")
    assert (wt / ".git").is_file(), "fixture precondition: a linked worktree uses a .git file"
    write(wt, "seed.txt", "modified\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(wt, run)
    assert b.dirty is True
    assert "seed.txt" in _tree_paths(wt, b.tracked_tree_oid), "tracked content lost"
    blob = subprocess.run(["git", "-C", str(wt), "show", f"{b.tracked_tree_oid}:seed.txt"],
                          capture_output=True, text=True, check=True).stdout
    assert blob == "modified\n"


def test_missing_identity_falls_back_to_an_explicit_sentinel(tmp_path):
    # Documents a LIMITATION, not a desired outcome. gitcmd pins GIT_CONFIG_GLOBAL to
    # /dev/null on every call, so `config --get user.name` sees repo-local config only; a
    # user whose identity lives in ~/.gitconfig (the common case) authors B1 as this
    # sentinel. Callers that know the user's identity must pass `author=`. If a later task
    # resolves identity properly, this test SHOULD fail and be updated deliberately.
    repo = make_repo(tmp_path)
    gitcmd.git(repo, "config", "--unset", "user.name")
    gitcmd.git(repo, "config", "--unset", "user.email")
    write(repo, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["d.txt"])
    who = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>", b.commit],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert who == "unknown <unknown@invalid>"


def test_ambient_git_dir_cannot_redirect_b1(monkeypatch, tmp_path):
    # commit-tree must receive ONLY the four identity variables. Splatting os.environ into
    # env_extra re-injects the redirectors gitcmd scrubs, and env_extra is applied LAST — so
    # an inherited GIT_DIR (a hook, `rebase --exec`, `bisect run`) wins and B1 is written
    # into a different repository, leaving materialize returning an OID this repo lacks.
    repo = make_repo(tmp_path)
    decoy = make_repo(tmp_path, name="decoy")
    write(repo, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    b = _mk(repo, run, selected=["d.txt"])
    assert gitcmd.git(repo, "cat-file", "-e", b.commit, check=False).returncode == 0, \
        "B1 was not written into the user's repository"
    assert gitcmd.git(decoy, "cat-file", "-e", b.commit, check=False).returncode != 0, \
        "B1 leaked into the repository named by an ambient GIT_DIR"


def test_explicit_author_overrides_the_config_probe(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    f = finspect.repo_facts(repo)
    b = baseline.materialize(repo, run, f, ["d.txt"], "r1",
                             author=("Real User", "real@example.invalid"))
    who = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>", b.commit],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert who == "Real User <real@example.invalid>"


def test_filesystem_manifest_covers_selected_and_tracked(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "chosen.txt", "c\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["chosen.txt"])
    assert "seed.txt" in b.filesystem_manifest and "chosen.txt" in b.filesystem_manifest
    assert len(b.filesystem_manifest["chosen.txt"]) == 64
