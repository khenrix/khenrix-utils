"""B is composite; construction never touches the user's index (spec §2)."""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import baseline, gitcmd, inspect as finspect  # noqa: E402
from forge_fixtures import git as _git, make_repo, write  # noqa: E402


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


def test_a_symlink_in_a_selection_is_manifested_as_its_target_text(tmp_path):
    """Walking a selection must not read what the tree never contained. git commits a
    symlink as a link; hashing it means open() FOLLOWING it, which puts content from
    outside the tree into a manifest that claims to describe the tree.

    The link is now DESCRIBED rather than dropped (Plan D, D-1): its manifest value is the
    sha256 of the target TEXT, which is `snapshot`'s and `bundle`'s identity for a link too,
    and which reads nothing outside the tree. Dropping it was the weaker answer — it left
    the tree carrying a path the manifest said nothing about, which is the third outcome
    `test_forge_seams.py` exists to rule out.

    The TREE half is asserted too, and the name used to say so, because "is not hashed
    through" read like a containment guarantee it never made. `git add -f` on the selection
    commits the link, so this module keeps the target's CONTENT out of the manifest while
    shipping a working path to it in B1. Both halves are true and only one of them is
    protective; a test asserting the manifest half alone stayed green while an escaping link
    reached a seat unscreened.

    What actually stops that link is upstream and belongs to other suites:
    `inspect.rejections` refuses an ESCAPING one and `screen` breaches on ANY one. The
    joint property is pinned in `test_forge_seams.py`.
    """
    repo = make_repo(tmp_path)
    outside = tmp_path / "host-secret.txt"
    outside.write_text("host secret\n")
    write(repo, "scratch/a.txt", "a\n")
    (repo / "scratch" / "link.txt").symlink_to(outside)
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["scratch"])
    assert b.filesystem_manifest["scratch/link.txt"] == \
        hashlib.sha256(str(outside).encode()).hexdigest(), \
        "the link is described by the text it holds, not by what that text reaches"
    assert hashlib.sha256(b"host secret\n").hexdigest() not in b.filesystem_manifest.values()
    assert "scratch/link.txt" in _tree_paths(repo, b.tracked_tree_oid), \
        "the manifest entry is not containment — the tree carries the link"
    assert (repo / "scratch" / "link.txt").is_symlink(), \
        "and it is still a link, so B1 hands a seat a working path out of the repository"


def test_every_link_the_tree_carries_reaches_the_manifest(tmp_path):
    """The two arrival routes the `is_file()` / `is_dir()` guards used to lose.

    Both shapes are committed by the same `git add` this function runs, so both are in the
    tree — and a path in the tree with nothing in the manifest describing it is the third
    outcome `test_forge_seams.py::test_everything_in_the_tree_is_in_the_manifest` exists to
    rule out.

    - a TRACKED link that dangles: `is_file()` follows it, finds nothing, and answers
      False, so the `ls-files` loop skipped it — a link is not required to resolve to
      anything to be a perfectly good link.
    - a SELECTED top-level link: the loop below `continue`d on one, because reading it once
      meant reading THROUGH it. Its target text is not a read of the target.
    """
    repo = make_repo(tmp_path)
    (repo / "dangling.txt").symlink_to("missing.txt")
    _git(repo, "add", "dangling.txt")
    _git(repo, "commit", "-qm", "dangling")
    (repo / "sel.link").symlink_to("seed.txt")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run, selected=["sel.link"])
    assert {"dangling.txt", "sel.link"} <= _tree_paths(repo, b.tracked_tree_oid), \
        "precondition: git committed both links, so the manifest owes an entry for each"
    assert b.filesystem_manifest["dangling.txt"] == \
        hashlib.sha256(b"missing.txt").hexdigest()
    assert b.filesystem_manifest["sel.link"] == hashlib.sha256(b"seed.txt").hexdigest()


def test_a_link_target_that_is_not_utf8_does_not_crash_the_baseline(tmp_path):
    """A link target is a filesystem NAME, and `os.readlink` returns surrogates for one
    that is not valid UTF-8 — on which a plain `.encode()` raises UnicodeEncodeError.

    That crash arrived with the manifest's link support: while the `ls-files` loop guarded
    on `is_file()` this function never met a link at all. It surfaced out of
    `baseline.materialize` as neither `BaselineError` nor anything `harvest` enumerates,
    which is the class of failure a caller cannot catch on purpose. All three digest
    functions — here, `fleet` and `snapshot` — take surrogateescape, so they stay
    comparable; for a valid-UTF-8 target the two encodings are byte-identical.
    """
    repo = make_repo(tmp_path)
    os.symlink(b"caf\xe9.txt", repo / "link.txt")
    _git(repo, "add", "link.txt")
    _git(repo, "commit", "-qm", "latin-1 target")
    run = tmp_path / "run"; run.mkdir()
    b = _mk(repo, run)
    assert b.filesystem_manifest["link.txt"] == \
        hashlib.sha256(b"caf\xe9.txt").hexdigest(), \
        "the digest is over the raw bytes of the name, whatever encoding they are in"


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


def test_building_b_never_runs_the_repositorys_fsmonitor_program(tmp_path):
    """`core.fsmonitor` is a PROGRAM the repository names in its own config, and git runs it
    for whoever loads an index. B is built out of the user's repository, so every index-loading
    call here is one that would run it.

    BOTH sites, and the second is the one a narrower rule loses. `ls-files -z` is cached-only —
    no `--others` — and runs the monitor anyway, because loading the index is what triggers it.
    `write-tree` runs under GIT_INDEX_FILE pointed at B's private copy, and runs the monitor
    anyway, because the PROGRAM comes from the repository's config and not from the index. Only
    a dirty repository reaches the second, so the fixture is dirty and the clean case is checked
    beside it.

    The control fires the same monitor under an ordinary `git status`, and the equality below
    is the second half of the claim: the flags suppress a cache, so B must come out identical.
    """
    repo = make_repo(tmp_path)
    hook = write(repo, "fsmonitor.sh", f"#!/bin/sh\n: > {repo}/HOOK-RAN\nprintf ''\n")
    hook.chmod(0o755)
    write(repo, "tracked.txt", "committed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tracked")
    _git(repo, "config", "core.fsmonitor", str(hook))

    clean = tmp_path / "clean"; clean.mkdir()
    baseline.materialize(repo, clean, finspect.repo_facts(repo), [], "r-clean")
    assert not (repo / "HOOK-RAN").exists(), "a clean baseline ran the repository's fsmonitor"

    write(repo, "tracked.txt", "the user's uncommitted work\n")
    write(repo, "new.txt", "selected and untracked\n")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), ["new.txt"], "r1")
    assert b.dirty, "the premise: this run reaches write-tree"
    assert not (repo / "HOOK-RAN").exists(), "a dirty baseline ran the repository's fsmonitor"
    assert _tree_paths(repo, b.tracked_tree_oid) == {"seed.txt", "tracked.txt", "new.txt",
                                                     "fsmonitor.sh"}, \
        "the flags changed what the tree holds, which is not what suppressing a cache may do"

    _git(repo, "status", "--porcelain")
    assert (repo / "HOOK-RAN").exists(), \
        "the control failed: this fsmonitor does nothing even when an ordinary git runs it"


def test_building_b_never_fires_the_repositorys_own_hooks(tmp_path):
    """The user's decision: forge does not fire the user's hooks for its own bookkeeping.

    A hook is the repository's OTHER program, and unlike `core.fsmonitor` it is not one git
    runs behind the caller's back — it is the user's own policy, invoked because forge really
    did write something. Suppressed anyway, on the ref's namespace: `refs/khenrix-forge/…` and
    `run_dir/baseline.index` are forge's bookkeeping by name, and a hook told the user's index
    moved when a private copy was written is being told something false about their repository.

    BOTH branches, because they fire different hooks and only one of them is reachable from
    either fixture: a clean tree returns after a single `update-ref` and never reaches
    `_build_tree`, which is exactly how a call inside the dirty branch hid behind a clean
    fixture for two waves. Dirty adds two `add`s and a `write-tree`, all of which write the
    private index.

    The control fires the same hooks under an ordinary git, so a suite that armed hooks git
    would never have run cannot read an unreachable claim as a kept promise.
    """
    repo = make_repo(tmp_path)
    fired = tmp_path / "fired"
    fired.mkdir()
    for name in ("reference-transaction", "post-index-change", "pre-commit", "post-checkout"):
        h = repo / ".git" / "hooks" / name
        h.write_text(f"#!/bin/sh\n: > {fired}/{name}\nexit 0\n")
        h.chmod(0o755)

    clean = tmp_path / "clean"; clean.mkdir()
    b0 = baseline.materialize(repo, clean, finspect.repo_facts(repo), [], "r-clean")
    assert not b0.dirty, "the premise: this run takes the clean branch"
    assert not list(fired.iterdir()), "a clean baseline fired the repository's hooks"

    write(repo, "seed.txt", "the user's uncommitted work\n")
    write(repo, "new.txt", "selected and untracked\n")
    run = tmp_path / "run"; run.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), ["new.txt"], "r1")
    assert b.dirty, "the premise: this run reaches the two adds and write-tree"
    assert not list(fired.iterdir()), "a dirty baseline fired the repository's hooks"
    assert _tree_paths(repo, b.tracked_tree_oid) == {"seed.txt", "new.txt"}, \
        "suppressing hooks changed what the tree holds, which is not what a hooks pin may do"

    _git(repo, "update-ref", "refs/heads/control", b.base_commit)
    _git(repo, "add", "-A")
    assert {p.name for p in fired.iterdir()} == {"reference-transaction", "post-index-change"}, \
        "the control failed: these hooks do not fire even when an ordinary git runs them"


def test_the_drift_check_finds_the_real_index_in_a_linked_worktree(tmp_path):
    """Drift is CAUGHT here, and caught by reading the index this worktree actually uses.

    A linked worktree's `.git` is a FILE, so a joined `<repo>/.git/index` does not exist,
    `is_file()` answers False without raising, and the hash comes back "" for a repository
    with a perfectly good index — the layout this module has already been caught assuming
    twice. The strict guard turns that into a raise, which is why the two ordinary worktree
    tests now fail on it too; but a raise alone cannot tell "found the index and it moved"
    apart from "never found the index", and the second is what this test is named for.

    So the assertion is the index's OWN hash, computed by `_idx` through a plain subprocess
    that resolves the location independently of the module under test. Matching on the word
    "moved" passed while the message read `(000000000000 -> <no index>)` — a raise for the
    wrong reason, the same name-outruns-assertion defect this task exists to close.
    """
    repo = make_repo(tmp_path)
    wt = tmp_path / "wt"
    gitcmd.git(repo, "worktree", "add", "-q", str(wt), "-b", "feat")
    assert (wt / ".git").is_file(), "fixture precondition: a linked worktree uses a .git file"
    write(wt, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    f = finspect.replace(finspect.repo_facts(wt), index_sha="0" * 64)
    with pytest.raises(baseline.BaselineError, match=_idx(wt)[:12]):
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


def test_an_index_that_appeared_after_preflight_is_drift_not_a_skip(tmp_path):
    """"" is MEASURED-and-absent, so it must COMPARE rather than disable the check.

    The guard's earlier shape skipped whenever either side was empty. That reads as
    defensiveness and is the opposite: `idx_now == ""` is the exact signature of a git dir
    resolved wrongly — the bug this package has shipped twice — so tolerating it turns a
    path slip into a baseline nobody guarded, detectable only by a bespoke regression test.
    Strict comparison makes the whole class fail loudly instead.
    """
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    f = finspect.replace(finspect.repo_facts(repo), index_sha="")
    with pytest.raises(baseline.BaselineError, match="moved"):
        baseline.materialize(repo, run, f, ["d.txt"], "r1")


def test_index_sha_none_is_the_explicit_opt_out(tmp_path):
    """None is "not measured" — the one value that disables the check.

    Also the proof that the strict guard did not simply become "always raise": with the
    check opted out, an ordinary dirty baseline still builds.
    """
    repo = make_repo(tmp_path)
    write(repo, "d.txt", "d\n")
    run = tmp_path / "run"; run.mkdir()
    f = finspect.replace(finspect.repo_facts(repo), index_sha=None)
    b = baseline.materialize(repo, run, f, ["d.txt"], "r1")
    assert b.dirty is True and "d.txt" in _tree_paths(repo, b.tracked_tree_oid)
