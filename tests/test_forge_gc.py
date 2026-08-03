"""§15 — cleanup. Every refusal here protects a deliverable that cannot be rebuilt.

NOTHING HERE SPENDS ANYTHING: no provider is invoked, no clone is made, and every git command
runs in a fixture repository under `tmp_path`. What it does do is DELETE, which no other suite
in this project does, so every fixture points `XDG_STATE_HOME` at `tmp_path` before
`storage.run_root` is reached — a test that forgot would have `gc.collect` remove a directory
under the developer's real `~/.local/state`.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from forge import gc, handover, storage  # noqa: E402
from forge_fixtures import git as _git, make_repo  # noqa: E402


def _run_id(run_dir) -> str:
    """The run id out of the directory name, by `storage`'s own arithmetic rather than by a
    second reading of it."""
    return Path(run_dir).name.split("-", 1)[1]


def _show_ref(repo) -> str:
    """Every ref in `repo` as one blob of text, so a test can ask whether ANY of forge's are
    left rather than naming the three it happens to know about."""
    return subprocess.run(["git", "-C", str(repo), "show-ref"],
                          capture_output=True, text=True).stdout


def _a_run_with_a_synthesis_tree(tmp_path, monkeypatch, *, run_id="a1b2c3"):
    """A repository, a run directory with a manifest, and a registered synthesis worktree —
    the state `cli.start` leaves behind, minus everything that costs money.

    THE FORGE REFS ARE PART OF THE SHAPE AND NOT DECORATION. A real run has B1's base ref and
    one transported ref per seat under `refs/khenrix-forge/<run-id>/`, and those pin every
    object the seats produced in the USER's repository. A fixture that planted only the branch
    would let a `gc` that reclaims only the branch look complete.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = make_repo(tmp_path)
    run_dir = storage.run_root(repo, run_id)
    storage.manifest_path(run_dir).write_bytes(b'{"run_id": "%s"}' % run_id.encode())
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", f"refs/khenrix-forge/{run_id}/base", head)
    _git(repo, "update-ref", f"refs/khenrix-forge/{run_id}/claude", head)
    synth = run_dir / handover.SYNTHESIS
    _git(repo, "worktree", "add", "-q", "-b", handover.branch(run_id, handover.SYNTHESIS),
         str(synth), head)
    return repo, run_dir, synth


def _hand_over(run_dir, *, target="refs/heads/main"):
    handover.write_handover(run_dir, handover.Handover(
        run_id=_run_id(run_dir), branch=handover.branch(_run_id(run_dir), handover.SYNTHESIS),
        kind=handover.MERGE_READY, handover_target=target, accepted=False,
        out_of_band=(), baseline_owned=(), b1_files=(), why="merged by the user"))


# --------------------------------------------------------------------------- the fixture

def test_the_fixture_is_the_state_a_start_leaves(tmp_path, monkeypatch):
    """NON-VACUITY for every refusal below: they are only refusals if the thing they refuse to
    delete is really there. A fixture whose worktree was never registered would make
    `test_a_synthesis_directory_git_does_not_know_about_is_refused_not_rmtreed` pass over the
    ordinary case, and the two would be indistinguishable."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    assert synth.is_dir() and not synth.is_symlink()
    assert os.path.realpath(synth) in {os.path.realpath(t["worktree"])
                                       for t in gc.worktrees(repo)}
    refs = _show_ref(repo)
    assert "refs/heads/forge/" in refs and "refs/khenrix-forge/" in refs
    assert storage.run_dirs(repo) == (run_dir,), "the walk does not see the directory it walks"


# --------------------------------------------------------------------------- the licence

def test_a_synthesis_worktree_that_was_never_handed_over_is_refused(tmp_path, monkeypatch):
    """§15: 'it refuses to delete a synthesis worktree/branch not marked handed over.'"""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    assert handover.read_handover(run_dir) is None
    with pytest.raises(gc.GcError) as e:
        gc.collect(repo, _run_id(run_dir))
    assert "handed over" in str(e.value)
    assert synth.exists()
    assert "forge/" in _show_ref(repo)
    assert run_dir.exists()


def test_force_waives_the_handover_decision_because_it_is_the_operators_to_make(tmp_path,
                                                                                monkeypatch):
    """The discrimination check for the refusal above, and the shape it exists for: a `--start`
    whose bundle would not materialize leaves a registered worktree and its branch with no
    handover record, and that orphan is exactly what the operator has to be able to reclaim."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    removed = gc.collect(repo, _run_id(run_dir), force=True)
    assert not synth.exists() and not run_dir.exists()
    assert "forge/" not in _show_ref(repo), removed


def test_a_patch_handover_the_user_accepted_is_deletable_though_nothing_merged(tmp_path,
                                                                              monkeypatch):
    """§15's own sentence: 'a patch-based handover may intentionally never merge the internal
    branch.' A gc that read the absent merge as unfinished work would make every patch
    delivery permanent."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    handover.write_handover(run_dir, handover.Handover(
        run_id=_run_id(run_dir), branch=handover.branch(_run_id(run_dir), "synthesis"),
        kind=handover.PATCH_ONLY, handover_target=None, accepted=True,
        out_of_band=(), baseline_owned=(), b1_files=(), why="user took the patch"))
    removed = gc.collect(repo, _run_id(run_dir))
    assert not synth.exists()
    assert any("worktree" in r for r in removed)
    assert any("branch" in r for r in removed)
    assert "forge/" not in _show_ref(repo)
    assert not run_dir.exists()


def test_the_refs_a_run_pinned_in_the_users_repository_go_with_it(tmp_path, monkeypatch):
    """§9 names two forge namespaces and a run writes into both. `refs/heads/forge/<id>/…` is
    the deliverable branch; `refs/khenrix-forge/<id>/…` holds B1's base commit and every seat's
    transported work, in the USER's repository, pinning every object those seats produced. A
    cleanup that took the tree and the branch and left those would report the run reclaimed
    while the bulk of what it cost stayed — the same reading `worktree remove` invites one
    namespace over.

    THE OTHER RUN IS THE DISCRIMINATOR: a walk that deleted by namespace rather than by
    namespace AND run id would take it too.
    """
    repo, run_dir, _ = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/khenrix-forge/ffffff/base", head)
    _git(repo, "update-ref", "refs/heads/forgery-experiments", head)
    _hand_over(run_dir)
    removed = gc.collect(repo, _run_id(run_dir))
    refs = _show_ref(repo)
    assert f"refs/khenrix-forge/{_run_id(run_dir)}/" not in refs, removed
    assert "refs/khenrix-forge/ffffff/base" in refs, "another run's refs are not this run's"
    assert "refs/heads/forgery-experiments" in refs, "§9's prefixes are prefixes, not substrings"


def test_a_ref_a_live_worktree_is_on_is_refused_because_update_ref_would_not_refuse(
        tmp_path, monkeypatch):
    """Measured on git 2.53.0: `git branch -D` refuses a branch a linked worktree has checked
    out, and `update-ref -d` deletes it and leaves that worktree on an all-zero HEAD. So the
    guard git supplies for its own porcelain does not cover the plumbing this walk uses, and
    the refusal has to be here. Nothing is removed: this is checked before the first deletion.
    """
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    elsewhere = tmp_path / "operators-own-tree"
    # `--force` because git refuses a second checkout of one branch; the state it refuses to
    # create by accident is one an operator reaches deliberately, and `update-ref -d` does not
    # care how it got there.
    _git(repo, "worktree", "add", "-q", "--force", str(elsewhere),
         handover.branch(_run_id(run_dir), handover.SYNTHESIS))
    with pytest.raises(gc.GcError) as e:
        gc.collect(repo, _run_id(run_dir))
    assert "checked out" in str(e.value)
    assert synth.exists() and run_dir.exists() and elsewhere.exists()
    assert "forge/" in _show_ref(repo)


# --------------------------------------------------------------------------- §9's limits

def test_gc_never_prunes_the_whole_repository():
    """§9 forbids a repo-wide prune. Read off the source, because a behavioural test can only
    show that this fixture's other worktrees survived."""
    src = (ROOT / "shared" / "lib" / "forge" / "gc.py").read_text()
    assert '"prune"' not in src


def test_the_unlock_is_conditional_because_unlocking_an_unlocked_tree_is_rc_128(tmp_path,
                                                                               monkeypatch):
    """Measured on git 2.53.0: `git worktree unlock` on an unlocked worktree exits 128. §9
    prescribes unlock-then-remove for forge-owned trees, and doing it unconditionally fails on
    the ordinary case — so the lock state is read first."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    subprocess.run(["git", "-C", str(repo), "worktree", "lock", str(synth)],
                   check=True, capture_output=True)
    removed = gc.collect(repo, _run_id(run_dir))       # must not raise
    assert not synth.exists()
    assert any("unlock" in r for r in removed)


def test_a_tree_nobody_locked_never_reaches_the_unlock(tmp_path, monkeypatch):
    """The other half of the measurement above, and the one an unconditional unlock breaks:
    the common case is a tree nobody locked, where the unlock is rc=128 and would fail the
    whole walk.

    `startswith` AND NOT `in`: every line here carries the run directory's absolute path, so a
    substring test answers about whatever the enclosing directory happens to be called.
    """
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    removed = gc.collect(repo, _run_id(run_dir))
    assert not any(r.startswith("unlock ") for r in removed), removed
    assert not synth.exists()


def test_a_lock_taken_with_no_reason_is_still_a_lock(tmp_path, monkeypatch):
    """Measured: `worktree lock` with no `--reason` prints the bare word `locked` in the
    porcelain, so the key is present and its VALUE is empty. A reader that tested the value
    would call this tree unlocked, skip the unlock, and hand `worktree remove` a tree git
    refuses to remove (measured rc=128, and `--force` does not clear it either)."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    subprocess.run(["git", "-C", str(repo), "worktree", "lock", str(synth)],
                   check=True, capture_output=True)
    entry = [t for t in gc.worktrees(repo) if t.get("worktree") == str(synth)]
    assert entry and entry[0].get("locked") == "", entry
    assert any("unlock" in r for r in gc.collect(repo, _run_id(run_dir)))


def test_removing_the_worktree_is_not_removing_the_branch(tmp_path, monkeypatch):
    """Measured: `git worktree remove` leaves the branch. A gc that stopped there would report
    a run cleaned while its branch — and every object under it — stayed."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    gc.collect(repo, _run_id(run_dir))
    assert "forge/" not in _show_ref(repo)


def test_a_branch_that_was_already_gone_is_not_reported_as_deleted(tmp_path, monkeypatch):
    """Measured on git 2.53.0: a bare `update-ref -d <ref>` on a ref that does not exist exits
    0. So a report built from exit codes would print `branch forge/<id>/synthesis` for a branch
    nobody deleted — a run in which NOTHING happened leaving the record of one in which
    something did.

    WHAT THIS PINS IS THE ENUMERATION, not the `<oid>` argument: the report is built from the
    refs `_refs_of` FOUND, so a ref that was already gone is never named. The oid argument
    guards the other direction — a ref that moved since — and
    `test_a_ref_that_moved_since_this_walk_read_it_is_refused_at_the_new_value` is what holds
    it, because this fixture would pass without it.
    """
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    b = handover.branch(_run_id(run_dir), handover.SYNTHESIS)
    _git(repo, "worktree", "remove", str(synth))
    _git(repo, "update-ref", "-d", f"refs/heads/{b}")
    removed = gc.collect(repo, _run_id(run_dir))
    assert f"branch {b}" not in removed, removed
    assert any("refs/khenrix-forge" in r for r in removed), \
        "the discrimination check: the refs that WERE there are still reported"


def test_a_run_with_no_refs_left_says_so_rather_than_reporting_only_the_directory(
        tmp_path, monkeypatch):
    """"There was nothing of this run left to delete" and "the deletion happened" are different
    facts about a run, and a report that can only name removals states neither of them for a
    run whose refs are already gone — which is the one an operator chasing a missing branch is
    reading it for."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    run_id = _run_id(run_dir)
    _git(repo, "worktree", "remove", str(synth))
    for ref in (f"refs/heads/{handover.branch(run_id, handover.SYNTHESIS)}",
                f"refs/khenrix-forge/{run_id}/base", f"refs/khenrix-forge/{run_id}/claude"):
        _git(repo, "update-ref", "-d", ref)
    removed = gc.collect(repo, run_id)
    assert any("no refs" in r for r in removed), removed
    assert not run_dir.exists()


def test_a_ref_that_moved_since_this_walk_read_it_is_refused_at_the_new_value(tmp_path,
                                                                              monkeypatch):
    """`update-ref -d <ref>` with no expected value deletes whatever the ref points at NOW, so
    a walk that read a ref, decided it was this run's, and then deleted it by name would take
    with it whatever landed there in between — and this engine is not the only writer of its
    own namespace while a fleet is out. With the OID this walk read, the deletion is a
    compare-and-swap: it fails rather than removing something nobody looked at.

    The stale reading is injected rather than raced, because a race is not a test.
    """
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    (repo / "second.txt").write_text("a commit this run never saw\n")
    _git(repo, "add", "second.txt")
    _git(repo, "commit", "-q", "-m", "second")
    moved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    real = gc._refs_of
    monkeypatch.setattr(gc, "_refs_of",
                        lambda r, i: {name: moved for name in real(r, i)})
    with pytest.raises(gc.GcError) as e:
        gc.collect(repo, _run_id(run_dir))
    assert "moved it since" in str(e.value)
    assert f"refs/khenrix-forge/{_run_id(run_dir)}/base" in _show_ref(repo)
    assert run_dir.exists(), "the run directory went with a deletion that was refused"


# --------------------------------------------------------------------------- the disk report

def test_the_disk_report_names_a_run_it_could_not_measure_rather_than_summing_it_as_zero(
        tmp_path, monkeypatch):
    """A total silently missing a 40 GB run is §15's report inverted."""
    repo, run_dir, _ = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    rows = gc.usage(repo)
    assert rows and rows[0].bytes_ > 0 and rows[0].files > 0

    def boom(*a, **kw):
        raise PermissionError("nope")
    monkeypatch.setattr(gc, "_walk_bytes", boom)
    rows = gc.usage(repo)
    assert rows[0].bytes_ is None, "an unwalkable run was reported as holding 0 bytes"
    assert rows[0].files is None


def test_a_subdirectory_the_walk_cannot_list_is_not_summed_as_the_rest_of_the_run(
        tmp_path, monkeypatch):
    """`os.walk`'s DEFAULT `onerror` swallows the listing error and yields nothing for that
    subtree, so a run whose `seats/` directory cannot be read comes back as the sum of
    everything else — a clean number over content nobody read, and the failure the monkeypatch
    above cannot reach because it replaces the walker instead of breaking the tree.

    Skipped for root, which can list anything.
    """
    repo, run_dir, _ = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    if os.geteuid() == 0:
        pytest.skip("root can list an unreadable directory, so this measures nothing")
    closed = run_dir / "seats"
    closed.mkdir()
    (closed / "big.bin").write_bytes(b"x" * 4096)
    closed.chmod(0o000)
    try:
        rows = gc.usage(repo)
        assert rows[0].bytes_ is None, "an unlistable subtree was summed as the rest of the run"
        assert "could not be walked" in rows[0].why
    finally:
        closed.chmod(0o700)


def test_an_unreadable_handover_record_is_not_reported_as_not_handed_over(tmp_path,
                                                                         monkeypatch):
    """"This run was not handed over" is an answer; "this engine could not read the record" is
    the absence of one. Printing them the same way tells an operator that a delivery they made
    is unfinished work, which is the one sentence that gets a deliverable deleted. And both
    reasons survive when the walk ALSO fails — a row that dropped one would name a size problem
    and silently lose a record problem."""
    repo, run_dir, _ = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    storage.handover_path(run_dir).write_bytes(b"{ not json")
    rows = gc.usage(repo)
    assert rows[0].handed_over is None, "an unreadable record answered the licence question"
    assert "could not be read" in rows[0].why

    def boom(*a, **kw):
        raise PermissionError("nope")
    monkeypatch.setattr(gc, "_walk_bytes", boom)
    rows = gc.usage(repo)
    assert rows[0].handed_over is None
    assert "could not be walked" in rows[0].why and "could not be read" in rows[0].why


def test_the_three_licence_states_are_three_answers(tmp_path, monkeypatch):
    """The discrimination check for the row above: `True`, `False` and `None` are reached by
    three different runs, so a field hardcoded to any one of them fails here."""
    repo, run_dir, _ = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch, run_id="aaaaaa")
    assert gc.usage(repo)[0].handed_over is False
    _hand_over(run_dir)
    assert gc.usage(repo)[0].handed_over is True
    storage.handover_path(run_dir).write_bytes(b"{ not json")
    assert gc.usage(repo)[0].handed_over is None


def test_no_runs_and_a_state_directory_that_cannot_be_read_are_different_answers(tmp_path,
                                                                                 monkeypatch):
    """`()` is what `--gc all` prints as "no forge runs are on disk for this repository", which
    is a reassurance. A state directory that exists and cannot be listed is this walk failing,
    and answering the two alike would print that reassurance over a full disk."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = make_repo(tmp_path)
    assert gc.usage(repo) == ()

    base = storage.forge_root()
    base.mkdir(parents=True)
    if os.geteuid() == 0:
        pytest.skip("root can list an unreadable directory, so this measures nothing")
    base.chmod(0o000)
    try:
        with pytest.raises(gc.GcError) as e:
            gc.usage(repo)
        assert "could not be listed" in str(e.value)
    finally:
        base.chmod(0o700)


def test_a_run_directory_that_is_a_symlink_is_measured_as_unknown_and_never_walked(
        tmp_path, monkeypatch):
    """`os.walk` follows the TOP of the tree it is given whatever `followlinks` says, so a run
    directory that is a link would report the target's bytes as this run's. Neither number is
    this run's disk and deleting the link reclaims none of it."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = make_repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "huge.bin").write_bytes(b"x" * 8192)
    link = storage.forge_root() / f"{storage.run_digest(repo)}-deadbe"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(elsewhere, target_is_directory=True)

    rows = gc.usage(repo)
    assert len(rows) == 1 and rows[0].bytes_ is None and rows[0].files is None
    assert rows[0].handed_over is None, \
        "the licence question was answered about whatever the link points at"
    assert "symbolic link" in rows[0].why
    assert (elsewhere / "huge.bin").exists()


# --------------------------------------------------------------------------- what it owns

def test_a_run_directory_that_is_a_symlink_is_refused_rather_than_deleted_through(
        tmp_path, monkeypatch):
    """The deletion half of the row above. A link named like a run directory can point ANYWHERE
    — including at another run's directory inside forge's own store, which is the shape a
    resolved-parent check alone clears. So the link is refused before the parent is compared,
    and the target keeps every byte."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch, run_id="aaaaaa")
    _hand_over(run_dir)
    link = storage.forge_root() / f"{storage.run_digest(repo)}-bbbbbb"
    link.symlink_to(run_dir, target_is_directory=True)

    with pytest.raises(gc.GcError) as e:
        gc.collect(repo, "bbbbbb")
    assert "symbolic link" in str(e.value)
    assert link.is_symlink(), "the link itself was removed by a walk that refused it"
    assert run_dir.exists() and synth.exists(), "a refusal deleted another run's directory"
    assert "forge/" in _show_ref(repo)


def test_a_run_directory_outside_forges_own_storage_is_refused(tmp_path, monkeypatch):
    """The ownership rule stated the other way round: what makes a path deletable is that this
    walk can place it inside `storage.forge_root()`, not that it was passed to `--gc`."""
    repo, run_dir, _ = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    outside = tmp_path / "not-forges"
    outside.mkdir()
    (outside / "manifest.json").write_bytes(b"{}")
    monkeypatch.setattr(gc.storage, "run_root", lambda *a, **kw: outside)
    with pytest.raises(gc.GcError) as e:
        gc.collect(repo, _run_id(run_dir))
    assert "forge" in str(e.value)
    assert outside.exists() and (outside / "manifest.json").exists()


def test_a_synthesis_path_that_is_a_symlink_is_refused(tmp_path, monkeypatch):
    """Resolving it would match whatever registered worktree it points at — the operator's own
    checkout, for one — and hand THAT path to `git worktree remove`."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    _git(repo, "worktree", "remove", str(synth))
    theirs = tmp_path / "their-own-worktree"
    _git(repo, "worktree", "add", "-q", "-b", "their-branch", str(theirs))
    synth.symlink_to(theirs, target_is_directory=True)
    with pytest.raises(gc.GcError) as e:
        gc.collect(repo, _run_id(run_dir))
    assert "symbolic link" in str(e.value)
    assert theirs.exists() and (theirs / "seed.txt").exists()
    assert str(theirs) in {t.get("worktree") for t in gc.worktrees(repo)}


def test_a_run_directory_reached_through_a_symlinked_state_root_is_still_collectable(
        tmp_path, monkeypatch):
    """Measured on git 2.53.0: `worktree add` records the RESOLVED path and `worktree list
    --porcelain` prints that one, while `worktree remove` accepts either spelling. So a
    comparison of unresolved strings reads "git does not have this registered" for every
    ordinary run on a machine whose state directory — or home — goes through a link, and that
    refusal has no remedy the operator can act on.
    """
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    linked = tmp_path / "linked-state"
    linked.symlink_to(real_state, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(linked))

    repo = make_repo(tmp_path)
    run_dir = storage.run_root(repo, "c0ffee")
    storage.manifest_path(run_dir).write_bytes(b"{}")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    synth = run_dir / handover.SYNTHESIS
    _git(repo, "worktree", "add", "-q", "-b", handover.branch("c0ffee", handover.SYNTHESIS),
         str(synth), head)
    assert str(synth) not in {t.get("worktree") for t in gc.worktrees(repo)}, \
        "the fixture is vacuous: git recorded the link spelling after all"
    _hand_over(run_dir)

    removed = gc.collect(repo, "c0ffee")
    assert not synth.exists() and not run_dir.exists(), removed
    assert "forge/" not in _show_ref(repo)


def test_a_corrupt_handover_record_reaches_the_operator(tmp_path, monkeypatch):
    repo, run_dir, _ = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    storage.handover_path(run_dir).write_bytes(b"{ not json")
    with pytest.raises(handover.HandoverError):
        gc.collect(repo, _run_id(run_dir))


def test_a_synthesis_directory_git_does_not_know_about_is_refused_not_rmtreed(tmp_path,
                                                                             monkeypatch):
    """The `rmtree` at the end of `collect` takes the whole run directory, `synthesis/`
    included. If `worktree list` never named that path, the directory goes and
    `.git/worktrees/<name>` stays registered — and §9 forbids the repo-wide `worktree prune`
    that is the only thing that reclaims it. Permanent, and silent."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    # Deregister the tree without removing the directory, which is exactly the state a killed
    # `--gc` or a hand-edited `.git` leaves behind.
    monkeypatch.setattr(gc, "worktrees", lambda repo_: ())
    with pytest.raises(gc.GcError) as e:
        gc.collect(repo, _run_id(run_dir))
    assert "worktree list" in str(e.value)
    assert synth.exists(), "an unregistered synthesis worktree was deleted anyway"
    assert run_dir.exists()
    # And --force does not clear THIS refusal: it waives the handover decision and nothing else.
    with pytest.raises(gc.GcError):
        gc.collect(repo, _run_id(run_dir), force=True)


def test_a_registered_tree_whose_directory_is_gone_is_reclaimed_not_refused(tmp_path,
                                                                           monkeypatch):
    """The other side of the refusal above, measured: `worktree remove` on a registered tree
    whose directory no longer exists is rc=0 and clears the admin entry. Refusing it would
    leave the one leak §9's ban on `prune` makes permanent."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    shutil.rmtree(synth)
    removed = gc.collect(repo, _run_id(run_dir))
    assert any("worktree" in r for r in removed), removed
    assert not any(t.get("worktree", "").endswith(handover.SYNTHESIS)
                   for t in gc.worktrees(repo))


def test_uncommitted_work_in_the_synthesis_tree_stops_the_walk_before_anything_is_deleted(
        tmp_path, monkeypatch):
    """Measured on git 2.53.0: `worktree remove` is rc=128 for a tree holding a modified
    tracked file or an untracked one, and rc=0 when the only extra files are IGNORED — which is
    what §16's out-of-band artifacts are. Passing `--force` past that would delete a fusion
    nobody committed, out of the one tree it exists in, on a walk whose job is to reclaim disk.
    Nothing is removed, so the refusal costs the operator a re-run and no work."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    _hand_over(run_dir)
    (synth / "unsaved.txt").write_text("the fusion nobody committed\n")
    with pytest.raises(gc.GcError) as e:
        gc.collect(repo, _run_id(run_dir))
    assert "Nothing was deleted" in str(e.value)
    assert (synth / "unsaved.txt").read_text().startswith("the fusion")
    assert run_dir.exists() and "forge/" in _show_ref(repo)


def test_an_ignored_artifact_is_not_uncommitted_work(tmp_path, monkeypatch):
    """The discrimination check for the refusal above, and the ordinary case: §16's out-of-band
    artifacts are ignored files sitting in the synthesis tree at handover time. A walk that
    read those as unsaved work would refuse every real run."""
    repo, run_dir, synth = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    (synth / ".gitignore").write_text("build/\n")
    _git(synth, "add", ".gitignore")
    _git(synth, "commit", "-q", "-m", "ignore build")
    (synth / "build").mkdir()
    (synth / "build" / "artifact.bin").write_bytes(b"x" * 128)
    _hand_over(run_dir)
    assert gc.collect(repo, _run_id(run_dir))
    assert not synth.exists()


def test_a_run_id_nothing_ever_opened_leaves_no_directory_behind(tmp_path, monkeypatch):
    """`storage.run_root` CREATES, so a typo'd run id would otherwise leave an empty directory
    that the next `--gc all` reports as a run — a walk whose own refusals manufacture the runs
    it later names."""
    repo, run_dir, _ = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    with pytest.raises(gc.GcError) as e:
        gc.collect(repo, "beef01")
    assert "manifest" in str(e.value)
    assert [d.name for d in storage.run_dirs(repo)] == [run_dir.name]


@pytest.mark.parametrize("bad", ["", ".", "..", "x/../../elsewhere", "a/b", "../evil",
                                 ".hidden"])
def test_a_run_id_that_would_name_a_directory_elsewhere_is_refused_before_it_is_created(
        bad, tmp_path, monkeypatch):
    """`storage.run_root` joins the id into a directory name and creates with `parents=True`,
    so a separator or a `..` makes directories outside forge's own storage BEFORE any check
    below can refuse them. The ownership check catches the deletion; nothing can catch a
    creation after the fact, so this one is a refusal at the door."""
    repo, run_dir, _ = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    before = sorted(p.name for p in storage.forge_root().iterdir())
    with pytest.raises(gc.GcError) as e:
        gc.collect(repo, bad)
    assert "not a run id" in str(e.value)
    assert sorted(p.name for p in storage.forge_root().iterdir()) == before
    assert not (tmp_path / "elsewhere").exists() and not (tmp_path / "evil").exists()


def test_a_partial_run_directory_is_reported_rather_than_removed(tmp_path, monkeypatch):
    """The `rmdir` above only ever succeeds on an EMPTY directory, which is what keeps it from
    taking a half-written run's evidence — §8.1 keeps a failed attempt as partial input, and it
    is a file in this directory."""
    repo, run_dir, _ = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch)
    partial = storage.run_root(repo, "0ddba1")
    storage.journal_path(partial).write_bytes(b'{"event": "start"}\n')
    with pytest.raises(gc.GcError):
        gc.collect(repo, "0ddba1")
    assert partial.exists() and storage.journal_path(partial).exists()


# --------------------------------------------------------------------------- the seam

def test_the_worktree_parser_reads_the_shape_git_prints():
    """Parsed rather than assumed: the porcelain is one block per tree, keys and values split
    on the first space, and a bare attribute (`locked`, `detached`, `bare`) has no value."""
    trees = gc._parse_worktrees(
        "worktree /a\nHEAD 0123\nbranch refs/heads/main\n\n"
        "worktree /b\nHEAD 4567\ndetached\nlocked because\n\n"
        "worktree /c\nHEAD 89ab\nlocked\n")
    assert len(trees) == 3, trees
    assert trees[0] == {"worktree": "/a", "HEAD": "0123", "branch": "refs/heads/main"}
    assert trees[1]["detached"] == "" and trees[1]["locked"] == "because"
    assert "locked" in trees[2] and trees[2]["locked"] == "", \
        "a lock with no reason must not read as an unlocked tree"


def test_gc_asks_storage_where_a_run_lives_rather_than_deriving_it():
    """A second copy of `storage.run_root`'s arithmetic would be byte-identical today, which is
    exactly when a duplicate is most dangerous: nothing fails until one of them moves, and then
    `--gc all` reports an empty disk over a full one."""
    src = (ROOT / "shared" / "lib" / "forge" / "gc.py").read_text()
    assert "sha256" not in src, "gc re-derives the run-directory digest instead of asking"
    assert "XDG_STATE_HOME" not in src, "gc re-derives the state root instead of asking"
    # THE QUOTED LITERAL, because the bare name is not one string: `refs/khenrix-forge/` is
    # §9's REF namespace and `gc` is entitled to name that in prose. What it may not do is
    # spell the state subdirectory `storage.forge_root` builds, which is this literal.
    assert '"khenrix-forge"' not in src, "gc re-derives the state subdirectory instead of asking"
    assert "storage.run_dirs(" in src


def test_every_git_call_gc_makes_goes_through_the_audited_door():
    """`gitcmd` is the one audited way this package invokes git — argv lists, an explicit
    environment, and the presets the closures in `test_forge_seams.py` check. A `subprocess`
    here would be outside all of it, and this module is the one that DELETES."""
    src = (ROOT / "shared" / "lib" / "forge" / "gc.py").read_text()
    assert "subprocess" not in src and "os.system" not in src
    assert "shell=True" not in src


def test_the_row_is_reported_under_an_id_gc_can_resolve(tmp_path, monkeypatch):
    """`usage` takes the run id off the directory name and `--gc <run-id>` builds the name back
    from it. A row reported under an id that does not round-trip names a run the operator
    cannot then collect."""
    repo, run_dir, _ = _a_run_with_a_synthesis_tree(tmp_path, monkeypatch, run_id="0f0f0f")
    row = gc.usage(repo)[0]
    assert row.run_id == "0f0f0f"
    assert storage.run_root(repo, row.run_id, must_be_new=False) == run_dir
