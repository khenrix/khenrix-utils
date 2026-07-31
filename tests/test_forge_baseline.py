"""B is composite; construction never touches the user's index (spec §2)."""
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import baseline, gitcmd, inspect as finspect  # noqa: E402
from forge_fixtures import make_repo, write  # noqa: E402


def _idx(repo):
    """sha256 of the index git itself would use for `repo`.

    The git dir is asked for rather than assumed to be `<repo>/.git`, so this works in a
    linked worktree too — but via a plain subprocess, so the guard stays independent of the
    resolution the module under test performs.
    """
    gd = subprocess.run(["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
                        capture_output=True, text=True, check=True).stdout.strip()
    return hashlib.sha256((Path(gd) / "index").read_bytes()).hexdigest()


def _loose_objects(repo):
    out = subprocess.run(["git", "-C", str(repo), "count-objects", "-v"],
                         capture_output=True, text=True, check=True).stdout
    return int(dict(line.split(": ") for line in out.splitlines())["count"])


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
    got = subprocess.run(["git", "-C", str(repo), "rev-parse", b.ref],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert got == b.base_commit, "the clean path must still publish a reachable ref"


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
    who = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%s",
                          b.commit], capture_output=True, text=True, check=True).stdout
    assert "Fixture <fixture@example.invalid>" in who
    assert "uncommitted working tree" in who
    # The split is the point of §2.1: the work is the user's, the act of committing is
    # forge's. Asserting only the author passes even with the committer left unset.
    author, committer, _subject = who.splitlines()
    assert author == "Fixture <fixture@example.invalid>"
    assert committer == "llm-forge <forge@khenrix.invalid>"


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


def test_ambient_literal_pathspecs_does_not_break_the_repo_wide_add(monkeypatch, tmp_path):
    # `:/` is magic, so a caller that hardened its own pathspecs by exporting
    # GIT_LITERAL_PATHSPECS=1 would turn it into a directory name and kill `add -u`. The
    # engine pins the variable OFF for that call rather than relying on it being unset.
    repo = make_repo(tmp_path)
    write(repo, "seed.txt", "modified\n")
    write(repo, "chosen.txt", "picked\n")
    run = tmp_path / "run"; run.mkdir()
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "1")
    b = _mk(repo, run, selected=["chosen.txt"])
    assert _tree_paths(repo, b.tracked_tree_oid) == {"seed.txt", "chosen.txt"}


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
    before = _idx(wt)
    b = _mk(wt, run)
    assert b.dirty is True
    assert "seed.txt" in _tree_paths(wt, b.tracked_tree_oid), "tracked content lost"
    blob = subprocess.run(["git", "-C", str(wt), "show", f"{b.tracked_tree_oid}:seed.txt"],
                          capture_output=True, text=True, check=True).stdout
    assert blob == "modified\n"
    # The guarantee has to hold on the one path where the index location is newly computed:
    # a wrong git dir could just as easily resolve to a real index that is not this one.
    assert _idx(wt) == before, "the worktree's own index was written"


@pytest.mark.parametrize("missing", ["user.name", "user.email"])
def test_missing_identity_refuses_to_fabricate_an_author(tmp_path, missing):
    # B1 is history the user is asked to merge, and forge's own commits are authored
    # `llm-forge` — so a placeholder author reads as a real third party in `git log`,
    # `git blame` and `--author` filters, permanently and with no signal at the point it was
    # chosen. Half an identity is no better than none: either half missing must refuse.
    repo = make_repo(tmp_path)
    gitcmd.git(repo, "config", "--unset", missing)
    write(repo, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    with pytest.raises(baseline.BaselineError, match="author="):
        _mk(repo, run, selected=["d.txt"])


def test_refusing_an_author_writes_nothing_at_all(tmp_path):
    # Not merely "nothing reachable". Probing identity after write-tree still leaves the
    # tree and its blobs loose in the user's object store — unreachable but present until
    # git's two-week gc grace expires. The object count is what makes fail-closed literal.
    repo = make_repo(tmp_path)
    gitcmd.git(repo, "config", "--unset", "user.name")
    write(repo, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    before_idx, before_objs = _idx(repo), _loose_objects(repo)
    with pytest.raises(baseline.BaselineError):
        _mk(repo, run, selected=["d.txt"])
    assert gitcmd.git(repo, "rev-parse", "--verify", "refs/khenrix-forge/r1/base",
                      check=False).returncode != 0, "a refused run published a ref anyway"
    assert _idx(repo) == before_idx
    assert _loose_objects(repo) == before_objs, "a refused run left objects in the store"
    assert not gitcmd.git(repo, "fsck", "--unreachable", "--no-progress").stdout.strip()


def test_repo_and_facts_naming_different_repositories_is_refused(tmp_path):
    # facts.root wins over the argument, so a mismatch would otherwise build A's baseline
    # from a call that named B and return normally — the loud pathspec error that used to
    # surface this went away with the root fix.
    a = make_repo(tmp_path, name="A")
    b = make_repo(tmp_path, name="B")
    write(a, "x.txt", "x\n")
    run = tmp_path / "run"; run.mkdir()
    with pytest.raises(baseline.BaselineError, match="different repositories"):
        baseline.materialize(b, run, finspect.repo_facts(a), ["x.txt"], "r1",
                             author=("U", "u@e.invalid"))


def test_a_subdirectory_of_the_same_repository_is_still_accepted(tmp_path):
    # The guard must not turn the legitimate subdirectory call into an error.
    repo = make_repo(tmp_path)
    write(repo, "nest/n.txt", "n\n")
    subprocess.run(["git", "-C", str(repo), "add", "nest/n.txt"], check=True)
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo / "nest", run)
    assert set(b.filesystem_manifest) == {"nest/n.txt", "seed.txt"}


def test_ambient_git_dir_cannot_redirect_b1(monkeypatch, tmp_path):
    # commit-tree must receive ONLY the four identity variables. Splatting os.environ into
    # env_extra re-injects the redirectors gitcmd scrubs, and env_extra is applied LAST — so
    # an inherited GIT_DIR (a hook, `rebase --exec`, `bisect run`) wins and B1 is written
    # into a different repository, leaving materialize returning an OID this repo lacks.
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    # The decoy is a CLONE, then given B1's exact tree, so under the bug commit-tree
    # SUCCEEDS there and the assertions below are what fires. A decoy lacking the parent or
    # the tree only ever produces `fatal: not a valid object`, which would let the test pass
    # for a reason its own assertion text does not describe.
    decoy = tmp_path / "decoy"
    gitcmd.git(repo, "clone", "-q", "--no-hardlinks", str(repo), str(decoy))
    write(decoy, "d.txt", "d\n")
    gitcmd.git(decoy, "add", "d.txt")
    decoy_tree = gitcmd.git(decoy, "write-tree").stdout.strip()
    base = finspect.repo_facts(repo).head
    # By construction decoy_tree IS the tree materialize will build, and the clone carries
    # its parent — so the decoy holds both objects commit-tree needs and genuinely accepts
    # B1 under the bug, instead of erroring on a missing object for an unrelated reason.
    assert gitcmd.git(decoy, "cat-file", "-e", decoy_tree, check=False).returncode == 0
    assert gitcmd.git(decoy, "cat-file", "-e", base, check=False).returncode == 0

    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    try:
        b = _mk(repo, run, selected=["d.txt"])
    except gitcmd.GitError as exc:
        # Same violation, seen one step later: B1 went elsewhere, so update-ref cannot find
        # the object it was handed. Named here so the failure reads as the invariant it is.
        pytest.fail(f"B1 was not written into the user's repository: {exc}")
    assert b.tracked_tree_oid == decoy_tree, "fixture drifted: decoy no longer holds B1's tree"
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


def test_manifest_is_root_relative_when_invoked_from_a_subdirectory(tmp_path):
    # `ls-files` reports relative to cwd; `add -u -- :/` is root-relative magic. Run from a
    # subdirectory those two disagree and the tree is root-scoped while the manifest is
    # keyed on subdirectory-relative names — returned as success. Downstream validates
    # materialization against the manifest, so a wrongly-keyed one is silent corruption.
    # No selected_untracked here on purpose: that is the branch that fails SILENTLY, since a
    # literal pathspec would otherwise resolve against cwd and die loudly first.
    repo = make_repo(tmp_path)
    write(repo, "nest/n.txt", "n\n")
    subprocess.run(["git", "-C", str(repo), "add", "nest/n.txt"], check=True)
    write(repo, "seed.txt", "modified\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo / "nest", run)
    assert _tree_paths(repo, b.tracked_tree_oid) == {"nest/n.txt", "seed.txt"}
    assert set(b.filesystem_manifest) == {"nest/n.txt", "seed.txt"}, \
        "manifest keys must match the tree the baseline actually captured"


def test_filesystem_manifest_covers_selected_and_tracked(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "chosen.txt", "c\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["chosen.txt"])
    assert "seed.txt" in b.filesystem_manifest and "chosen.txt" in b.filesystem_manifest
    assert len(b.filesystem_manifest["chosen.txt"]) == 64


def test_a_selected_directory_reaches_the_manifest_as_well_as_the_tree(tmp_path):
    """Spec §2.2 contemplates a directory pathspec, so this is supported input.

    The literal pathspec sweeps the directory's whole contents into the tree; an
    `is_file()`-only manifest guard describes none of it. §2.2 validates the materialized
    tree AGAINST the manifest and §4 asserts the full manifest before setup, so the
    disagreement is not caught downstream — both pass vacuously over the missing content,
    and the run returns success.
    """
    repo = make_repo(tmp_path)
    write(repo, "scratch/a.txt", "a\n")
    write(repo, "scratch/sub/b.txt", "b\n")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["scratch"])
    tree = _tree_paths(repo, b.tracked_tree_oid)
    assert tree == {"seed.txt", "scratch/a.txt", "scratch/sub/b.txt"}
    assert set(b.filesystem_manifest) == tree, \
        "the tree and the manifest must describe the same content"
    assert b.filesystem_manifest["scratch/sub/b.txt"] == hashlib.sha256(b"b\n").hexdigest()


def test_a_symlink_inside_a_selected_directory_is_not_hashed_through(tmp_path):
    """Walking a selection must not read what the tree never contained. git commits a
    symlink as a link; hashing it means open() FOLLOWING it, which puts content from
    outside the tree into a manifest that claims to describe the tree."""
    repo = make_repo(tmp_path)
    outside = tmp_path / "host-secret.txt"
    outside.write_text("host secret\n")
    write(repo, "scratch/a.txt", "a\n")
    (repo / "scratch" / "link.txt").symlink_to(outside)
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["scratch"])
    assert "scratch/link.txt" not in b.filesystem_manifest
    assert hashlib.sha256(b"host secret\n").hexdigest() not in b.filesystem_manifest.values()


def test_materialize_aborts_when_the_index_moves_mid_snapshot(tmp_path):
    """§2.2: "abort if the source moved mid-snapshot". index_sha had no consumer.

    The abort is asserted as WRITES-NOTHING, not merely as an exception: a check placed
    after `add`/`write-tree` would leave the selected file's blob and the tree loose in the
    user's object store — unreachable but present until git's two-week gc grace expires,
    the same defect the identity refusal already fixed. `d.txt` is untracked and selected,
    so its blob is an object the store does not already hold: if the guard ran late, the
    count would move.
    """
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    f = finspect.repo_facts(repo)
    f = finspect.replace(f, index_sha="0" * 64)      # pretend the index differed at describe time
    before_objs = _loose_objects(repo)
    with pytest.raises(baseline.BaselineError, match="moved"):
        baseline.materialize(repo, run, f, ["d.txt"], "r1")
    assert _loose_objects(repo) == before_objs, "the abort left objects in the user's store"
    assert gitcmd.git(repo, "rev-parse", "--verify", "refs/khenrix-forge/r1/base",
                      check=False).returncode != 0, "the abort published a ref anyway"


def test_the_drift_check_finds_the_real_index_in_a_linked_worktree(tmp_path):
    """The guard skips on an empty hash, so a wrong git dir makes it fail OPEN.

    A linked worktree's `.git` is a FILE, so a joined `<repo>/.git/index` does not exist,
    `is_file()` answers False without raising, and the hash comes back "" — at which point
    the drift guard silently does nothing on the one layout this module has already been
    caught assuming twice. The location must be asked for, not built.
    """
    repo = make_repo(tmp_path)
    wt = tmp_path / "wt"
    gitcmd.git(repo, "worktree", "add", "-q", str(wt), "-b", "feat")
    assert (wt / ".git").is_file(), "fixture precondition: a linked worktree uses a .git file"
    write(wt, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    f = finspect.replace(finspect.repo_facts(wt), index_sha="0" * 64)
    with pytest.raises(baseline.BaselineError, match="moved"):
        baseline.materialize(wt, run, f, ["d.txt"], "r1")


def test_sidecars_is_none_until_a_producer_exists(tmp_path):
    """An empty list reads as "there are none"; None reads as "nobody looked".

    Both construction paths, because only one of them takes the field's default — the
    dirty path passes it explicitly. Spec §2's sidecar manifest has no producer yet and a
    later plan consumes this field as authoritative.
    """
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    clean = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "clean")
    write(repo, "d.txt", "d\n")
    dirty = baseline.materialize(repo, run, finspect.repo_facts(repo), ["d.txt"], "dirty")
    assert clean.dirty is False and dirty.dirty is True, \
        "fixture: the two runs must cover both constructors"
    assert clean.sidecars is None and dirty.sidecars is None
