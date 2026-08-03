"""§16 — handover. Every git call in this module runs against the USER's own repository."""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import (bundle, gitcmd, handover, inspect as finspect,  # noqa: E402
                   runstate, storage, taskbundle, verify)

_SRC = (ROOT / "shared" / "lib" / "forge" / "handover.py").read_text(encoding="utf-8")


def _repo(tmp_path) -> Path:
    r = tmp_path / "user-repo"
    r.mkdir()
    for argv in (["init", "-q", "-b", "main"], ["config", "user.email", "u@e"],
                 ["config", "user.name", "U"]):
        subprocess.run(["git", "-C", str(r), *argv], check=True, capture_output=True)
    (r / "f.txt").write_text("hi\n")
    subprocess.run(["git", "-C", str(r), "add", "f.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "base"], check=True,
                   capture_output=True)
    return r


def _rev(r: Path, spec: str) -> str:
    return subprocess.run(["git", "-C", str(r), "rev-parse", spec], check=True,
                          capture_output=True, text=True).stdout.strip()


def _run_dir(tmp_path, name="run") -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


def _string_constants(src: str) -> set:
    return {n.value for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _with_a_task_bundle(run_dir) -> taskbundle.TaskBundle:
    """Record a bundle for `run_dir` the way `cli.start` will: the bytes beside the manifest,
    then the manifest. Small on purpose — this file is about where it LANDS."""
    src = storage.task_source_path(run_dir)
    src.mkdir(parents=True)
    (src / "TASK.md").write_text("Refactor the thing.\n")
    b = taskbundle.scan(src, entrypoint="TASK.md")
    taskbundle.write_task_bundle(run_dir, b)
    return b


def test_the_synthesis_worktree_is_created_on_a_branch_and_never_detached(tmp_path):
    """§16: 'never --detach: a detached HEAD leaves commits unreachable and the next git gc
    deletes them.' Asserted twice — the tree really is on a branch, and the FORBIDDEN FLAG
    appears nowhere in the module, because a caller could otherwise add it later."""
    r = _repo(tmp_path)
    dest = tmp_path / "synth"
    name = handover.create_synthesis_worktree(r, dest, run_id="abc123", at=_rev(r, "HEAD"),
                                              run_dir=_run_dir(tmp_path))
    assert name == "forge/abc123/synthesis"
    out = subprocess.run(["git", "-C", str(dest), "symbolic-ref", "HEAD"],
                         check=True, capture_output=True, text=True).stdout.strip()
    assert out == "refs/heads/forge/abc123/synthesis", out
    assert "--detach" not in _SRC


def test_the_worktree_call_carries_the_two_presets_that_were_measured_to_silence_it(tmp_path):
    """Measured (git 2.53.0): `worktree add -b` fires core.fsmonitor, post-checkout,
    post-index-change and reference-transaction; NO_DAEMON_CACHE + NO_HOOKS silence all four.
    This runs it against a repo with all of them armed and asserts nothing fired."""
    r = _repo(tmp_path)
    log = tmp_path / "fired.log"
    hooks = r / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for h in ("post-checkout", "post-index-change", "reference-transaction"):
        (hooks / h).write_text(f'#!/bin/sh\necho {h} >> {log}\n')
        (hooks / h).chmod(0o755)
    fsm = tmp_path / "fsm.sh"
    fsm.write_text(f'#!/bin/sh\necho fsmonitor >> {log}\nexit 0\n')
    fsm.chmod(0o755)
    subprocess.run(["git", "-C", str(r), "config", "core.fsmonitor", str(fsm)],
                   check=True, capture_output=True)
    # NON-VACUITY: the same command without the presets must fire, or this fixture arms
    # nothing and the assertion below is about a repository with no hooks in it.
    subprocess.run(["git", "-C", str(r), "worktree", "add", "-q", "-b", "control",
                    str(tmp_path / "control"), "HEAD"], check=True, capture_output=True)
    assert log.exists() and log.read_text().strip(), "the fixture armed nothing"
    log.unlink()

    handover.create_synthesis_worktree(r, tmp_path / "synth", run_id="abc123",
                                       at=_rev(r, "HEAD"), run_dir=_run_dir(tmp_path))
    assert not log.exists() or log.read_text() == "", log.read_text()


def test_the_run_task_bundle_is_in_the_tree_section_13s_reviewers_are_pointed_at(tmp_path):
    """THE GAP THIS CLOSES. `review.run_round` decides `task_bundle_present` by asking the
    checkout for `taskbundle.task_dir`, and until this call materialized one, three seats had
    the run's task and the synthesis tree — the ONE tree §13 sets every reviewer's cwd to —
    did not. Reviewers were told "there is no task bundle in this checkout" while judging a
    candidate built from it.

    The predicate asserted is `run_round`'s own expression, and the bytes are re-derived from
    the tree by `verify_materialized` rather than trusted from the copier.
    """
    r = _repo(tmp_path)
    run = _run_dir(tmp_path)
    b = _with_a_task_bundle(run)
    dest = tmp_path / "synth"
    handover.create_synthesis_worktree(r, dest, run_id="abc123", at=_rev(r, "HEAD"),
                                       run_dir=run)
    laid = taskbundle.task_dir(dest)
    assert Path(laid).is_dir(), "the expression `review.run_round` evaluates"
    assert (laid / "TASK.md").read_text() == "Refactor the thing.\n"
    taskbundle.verify_materialized(b, dest)          # raises if a byte moved
    # A LINKED WORKTREE'S GIT DIR IS NOT ITS `.git`, which is the whole reason
    # `taskbundle.task_dir` asks git instead of joining. If this were a join the bundle would
    # be sitting beside a FILE and this assertion would read the wrong tree.
    assert not (dest / ".git").is_dir()
    assert Path(laid).is_relative_to(r / ".git" / "worktrees")


def test_a_run_that_recorded_no_task_bundle_still_gets_a_synthesis_worktree(tmp_path):
    """Runs predating §20 record none, and `read_task_bundle_if_recorded` answers `None` for
    that and nothing else — so the worktree is built and no bundle is claimed for it."""
    r = _repo(tmp_path)
    dest = tmp_path / "synth"
    handover.create_synthesis_worktree(r, dest, run_id="abc123", at=_rev(r, "HEAD"),
                                       run_dir=_run_dir(tmp_path))
    assert not Path(taskbundle.task_dir(dest)).exists()


def test_a_task_bundle_nobody_can_read_stops_before_the_worktree_is_created(tmp_path):
    """The likely failure costs no tree: the bundle is decoded first, on `clone_seat`'s
    precedent of asking git to validate the branch name before paying for the clone."""
    r = _repo(tmp_path)
    run = _run_dir(tmp_path)
    storage.task_bundle_path(run).write_bytes(b"{ not json")
    dest = tmp_path / "synth"
    with pytest.raises(taskbundle.TaskBundleError):
        handover.create_synthesis_worktree(r, dest, run_id="abc123", at=_rev(r, "HEAD"),
                                           run_dir=run)
    assert not dest.exists()
    assert "forge/abc123/synthesis" not in subprocess.run(
        ["git", "-C", str(r), "show-ref"], capture_output=True, text=True).stdout


def test_a_bundle_that_did_not_arrive_refuses_and_names_the_tree_it_left(tmp_path):
    """The LATE failure — the manifest read clean and the bytes it describes moved. §20's
    `verify_materialized` is what turns that into a refusal, and the refusal has to say the
    worktree is still there, because nothing here removes one."""
    r = _repo(tmp_path)
    run = _run_dir(tmp_path)
    _with_a_task_bundle(run)
    (storage.task_source_path(run) / "TASK.md").write_text("Something else entirely.\n")
    dest = tmp_path / "synth"
    with pytest.raises(handover.HandoverError) as e:
        handover.create_synthesis_worktree(r, dest, run_id="abc123", at=_rev(r, "HEAD"),
                                           run_dir=run)
    assert str(dest) in str(e.value) and "left standing" in str(e.value)
    assert dest.exists(), "and it really is still there, so the message is not a guess"


def test_seat_work_is_fetched_with_an_explicit_refspec_and_nothing_else_crosses(tmp_path):
    """§16: never a bare `git fetch <path>` — default refspecs would pull whatever refs the
    seat created into the user's repository, reintroducing the write path §4 closed.

    THE BRANCH IS NOT THE ONLY CHANNEL, and the tags below are the measurement that says so:
    with the explicit refspec and nothing else, git 2.53.0 auto-followed both a lightweight
    and an annotated tag the seat authored into the user's repository. `--no-tags` is what
    closes that, and a fixture carrying only `sneaky` would never have seen it.
    """
    r = _repo(tmp_path)
    clone = tmp_path / "seat"
    subprocess.run(["git", "clone", "-q", str(r), str(clone)], check=True, capture_output=True)
    for argv in (["config", "user.email", "s@e"], ["config", "user.name", "S"],
                 ["checkout", "-q", "-b", "forge/abc123/claude"]):
        subprocess.run(["git", "-C", str(clone), *argv], check=True, capture_output=True)
    (clone / "w.txt").write_text("work\n")
    subprocess.run(["git", "-C", str(clone), "add", "w.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-qm", "w"], check=True,
                   capture_output=True)
    # Three refs the seat created that MUST NOT cross: a branch, and the two tag shapes the
    # refspec does not reach.
    for argv in (["update-ref", "refs/heads/sneaky", "HEAD"],
                 ["tag", "seat-tag", "HEAD"],
                 ["tag", "-a", "annot", "-m", "a", "HEAD"]):
        subprocess.run(["git", "-C", str(clone), *argv], check=True, capture_output=True)

    oid = handover.transport_seat(r, clone, run_id="abc123", seat="claude")
    refs = subprocess.run(["git", "-C", str(r), "show-ref"], capture_output=True,
                          text=True).stdout
    assert "refs/khenrix-forge/abc123/claude" in refs
    assert "sneaky" not in refs, refs
    assert "refs/tags/" not in refs, refs
    assert oid == _rev(clone, "HEAD")


def test_the_users_own_fetch_head_is_not_overwritten_by_the_transport(tmp_path):
    """A fetch rewrites `.git/FETCH_HEAD`, which is the record of the last fetch the USER ran.
    Measured: `--no-write-fetch-head` leaves it alone. Forge's bookkeeping does not get to
    destroy the operator's."""
    r = _repo(tmp_path)
    (r / ".git" / "FETCH_HEAD").write_text("USER-FETCH-HEAD\n")
    clone = tmp_path / "seat"
    subprocess.run(["git", "clone", "-q", str(r), str(clone)], check=True, capture_output=True)
    for argv in (["config", "user.email", "s@e"], ["config", "user.name", "S"],
                 ["checkout", "-q", "-b", "forge/abc123/claude"]):
        subprocess.run(["git", "-C", str(clone), *argv], check=True, capture_output=True)
    (clone / "w.txt").write_text("work\n")
    subprocess.run(["git", "-C", str(clone), "add", "w.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-qm", "w"], check=True,
                   capture_output=True)
    handover.transport_seat(r, clone, run_id="abc123", seat="claude")
    assert (r / ".git" / "FETCH_HEAD").read_text() == "USER-FETCH-HEAD\n"


def test_a_seat_branch_that_is_not_there_refuses_rather_than_reporting_nothing(tmp_path):
    r = _repo(tmp_path)
    clone = tmp_path / "seat"
    subprocess.run(["git", "clone", "-q", str(r), str(clone)], check=True, capture_output=True)
    with pytest.raises(gitcmd.GitError):
        handover.transport_seat(r, clone, run_id="abc123", seat="claude")


def _manifest(repo, base_oid, tree_oid):
    """A Manifest carrying only the fields mergeability reads, built through the REAL
    constructor so a field rename fails here rather than silently reading None."""
    return runstate.Manifest(
        run_id="abc123", repo_path=str(repo), base_commit=base_oid,
        baseline_ref="refs/khenrix-forge/abc123/base", baseline_commit=base_oid,
        tracked_tree_oid=tree_oid, selected_paths=(),
        generator_contract=finspect.GeneratorContract(id="", relations=()),
        setup=(), verify=(verify.Step(argv=("true",), cwd="", env={}, timeout=600),),
        protected_refs={}, forge_refs={}, status_digest="d", index_digest="i",
        created_at="2026-08-03T00:00:00Z", seats=3, attempts=3, review_rounds=2,
        synthesis_fix_cap=3)


# A synthesis tree DISTINCT from B1's own. `mergeability` reads the two against each other —
# an equal pair is a worktree nobody fused into — so every fixture below that means "the
# orchestrator did fuse something" has to say so with a different oid.
_FUSED = "f" * 40


def test_mergeability_separates_a_clean_baseline_from_a_dirty_one(tmp_path):
    r = _repo(tmp_path)
    base = _rev(r, "HEAD")
    tree = _rev(r, f"{base}^{{tree}}")
    ready = handover.mergeability(_manifest(r, base, tree), synthesis_tree_oid=_FUSED,
                                  sidecars=())
    assert ready.kind == handover.MERGE_READY

    out = handover.mergeability(_manifest(r, base, "0" * len(tree)),
                                synthesis_tree_oid=_FUSED, sidecars=())
    assert out.kind == handover.PATCH_ONLY
    assert "baseline" in out.why
    assert out.integration, "a patch handover with no integration command is a dead end"


def test_a_tree_nobody_measured_is_not_a_tree_that_matched(tmp_path):
    """`synthesis_tree_oid=None` compares unequal to every real oid, so a naive `!=` would
    answer PATCH_ONLY and read as a measured mismatch. Unmeasured is its own refusal.

    THE EMPTY STRING IS THE SAME FACT AND IT IS THE ONE THAT ACTUALLY ARRIVES: every oid this
    module compares comes off `gitcmd.git(...).stdout.strip()`, whose failure shape is `""`
    and never `None`. A guard written against `None` alone lets the real one through.
    """
    r = _repo(tmp_path)
    base = _rev(r, "HEAD")
    for unmeasured in (None, "", "   "):
        with pytest.raises(handover.HandoverError) as e:
            handover.mergeability(_manifest(r, base, "a" * 40),
                                  synthesis_tree_oid=unmeasured, sidecars=())
        assert "not measured" in str(e.value)
    # And the manifest's own oids are read the same way, or reading (2) would report a dirty
    # baseline about a baseline nobody measured.
    with pytest.raises(handover.HandoverError, match="not measured"):
        handover.mergeability(_manifest(r, base, ""), synthesis_tree_oid=_FUSED, sidecars=())


def test_an_unenumerated_sidecar_set_is_refused_by_mergeability_too(tmp_path):
    """`out_of_band` is not the only reader of this distinction: §16's second merge-ready
    condition is "no sidecars", and `None` answers it in the merge-ready direction."""
    r = _repo(tmp_path)
    base = _rev(r, "HEAD")
    tree = _rev(r, f"{base}^{{tree}}")
    with pytest.raises(handover.HandoverError) as e:
        handover.mergeability(_manifest(r, base, tree), synthesis_tree_oid=_FUSED,
                              sidecars=None)
    assert "not enumerated" in str(e.value)


def test_a_synthesis_worktree_nobody_fused_into_is_not_a_merge_ready_delivery(tmp_path):
    """THE PARAMETER IS READ, and this is the input that proves it. A run whose orchestrator
    fused nothing leaves the synthesis worktree at B1: its tree IS `tracked_tree_oid`, there
    are no sidecars, and a `mergeability` that only liveness-checked the oid would report
    'the branch merges as it stands' over an empty delivery — nothing and nobody, one record,
    in the artifact this whole task exists to produce."""
    r = _repo(tmp_path)
    base = _rev(r, "HEAD")
    tree = _rev(r, f"{base}^{{tree}}")
    with pytest.raises(handover.HandoverError) as e:
        handover.mergeability(_manifest(r, base, tree), synthesis_tree_oid=tree, sidecars=())
    assert "nothing was fused" in str(e.value)
    # And it is refused on a DIRTY baseline too: the comparison is against B1's tracked tree,
    # not against `base^{tree}`, so it does not accidentally depend on the baseline being clean.
    with pytest.raises(handover.HandoverError):
        handover.mergeability(_manifest(r, base, "0" * len(tree)),
                              synthesis_tree_oid="0" * len(tree), sidecars=())


def test_one_changed_sidecar_keeps_a_matching_tree_out_of_merge_ready(tmp_path):
    """§16's table: merge-ready needs `tracked_tree_oid == base^{tree}` AND NO SIDECARS. A
    tree that matches while carrying an out-of-band artifact is the input that makes those two
    conditions look like one."""
    r = _repo(tmp_path)
    base = _rev(r, "HEAD")
    tree = _rev(r, f"{base}^{{tree}}")
    side = (bundle.SidecarEntry(path="dist/app.js", kind="file", mode=0o644, payload=b"x"),)
    out = handover.mergeability(_manifest(r, base, tree), synthesis_tree_oid=_FUSED,
                                sidecars=side)
    assert out.kind == handover.PATCH_ONLY
    assert "dist/app.js" in out.why


def test_a_truncated_artifact_list_says_it_was_truncated(tmp_path):
    """Six artifacts and five names with nothing between them reads as a complete list to a
    reader who does not count. A verdict may not describe less evidence than it was given."""
    r = _repo(tmp_path)
    base = _rev(r, "HEAD")
    tree = _rev(r, f"{base}^{{tree}}")
    side = tuple(bundle.SidecarEntry(path=f"dist/{i}.js", kind="file", mode=0o644,
                                     payload=b"x") for i in range(6))
    out = handover.mergeability(_manifest(r, base, tree), synthesis_tree_oid=_FUSED,
                                sidecars=side)
    assert "6 out-of-band" in out.why and "…" in out.why


def test_out_of_band_never_force_adds_and_carries_a_size_beside_every_hash(tmp_path):
    """An empty payload hashes to a real, stable, valid-looking digest. `size` beside it is
    what keeps 'a file with no bytes' and 'a file nobody read' from comparing equal.

    THE FORCE-FLAG HALF IS PARSED, NOT GREPPED FOR ONE QUOTE STYLE. `'"-f"' not in src` is
    satisfied by `'-f'`, so it tests the module's quoting habits rather than the property its
    name claims. Every string constant in the module is read instead.
    """
    side = (bundle.SidecarEntry(path="dist/empty.js", kind="file", mode=0o644, payload=b""),
            bundle.SidecarEntry(path="dist/app.js", kind="file", mode=0o644, payload=b"abc"))
    out = handover.out_of_band(side, synthesis_path=tmp_path / "synth")
    assert [o.size for o in out] == [3, 0]        # sorted by path: app.js, empty.js
    assert len({o.sha256 for o in out}) == 2
    for o in out:
        assert o.copy_command and o.copy_command[0] == "cp"
        assert "-f" not in o.copy_command and "--force" not in o.copy_command
    forcing = {"-f", "--force", "--force-with-lease"} & _string_constants(_SRC)
    assert forcing == set(), forcing


def test_a_sidecar_payload_that_is_not_bytes_is_refused_rather_than_stringified(tmp_path):
    """`str(None).encode()` is four bytes with a real digest and a plausible size — an
    artifact nobody read, wearing the record of one that was."""
    side = (bundle.SidecarEntry(path="dist/app.js", kind="file", mode=0o644, payload=None),)
    with pytest.raises(handover.HandoverError, match="declares bytes"):
        handover.out_of_band(side, synthesis_path=tmp_path)


def test_an_unenumerated_out_of_band_set_is_not_an_empty_one(tmp_path):
    with pytest.raises(handover.HandoverError):
        handover.out_of_band(None, synthesis_path=tmp_path)
    assert handover.out_of_band((), synthesis_path=tmp_path) == ()


def test_a_handover_naming_neither_a_target_nor_an_acceptance_is_refused():
    """§15 makes 'unmerged' well-defined off this record, and a record carrying neither field
    makes it undefined again — with `--gc` then holding a synthesis tree it may never delete."""
    with pytest.raises(handover.HandoverError):
        handover.Handover(run_id="r", branch="forge/r/synthesis", kind=handover.PATCH_ONLY,
                          handover_target=None, accepted=False, out_of_band=(),
                          baseline_owned=(), b1_files=(), why="x")
    ok = handover.Handover(run_id="r", branch="forge/r/synthesis", kind=handover.PATCH_ONLY,
                           handover_target=None, accepted=True, out_of_band=(),
                           baseline_owned=(), b1_files=(), why="user accepted the patch")
    assert ok.accepted is True


def test_neither_half_of_the_delete_licence_is_read_for_truthiness():
    """`accepted="no"` is TRUE in Python and `handover_target=""` names no ref while passing
    an `is None` test. Both are how a run nobody accepted gets `--gc`'s licence to delete."""
    def h(**kw):
        return handover.Handover(run_id="r", branch="forge/r/synthesis",
                                 kind=handover.PATCH_ONLY, out_of_band=(), baseline_owned=(),
                                 b1_files=(), why="x", **kw)

    with pytest.raises(handover.HandoverError, match="accepted is a bool"):
        h(handover_target=None, accepted="no")
    with pytest.raises(handover.HandoverError, match="handover_target"):
        h(handover_target="", accepted=False)
    with pytest.raises(handover.HandoverError, match="states the condition"):
        handover.Handover(run_id="r", branch="forge/r/synthesis", kind=handover.PATCH_ONLY,
                          handover_target="refs/heads/main", accepted=False, out_of_band=(),
                          baseline_owned=(), b1_files=(), why="  ")


def test_a_handover_record_round_trips_and_a_damaged_one_refuses(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert handover.read_handover(run_dir) is None
    h = handover.Handover(run_id="r", branch="forge/r/synthesis", kind=handover.MERGE_READY,
                          handover_target="refs/heads/main", accepted=False,
                          out_of_band=(handover.OutOfBand("d/a.js", "0" * 64, 3,
                                                          ("cp", "-p", "/x/d/a.js", "d/a.js")),),
                          baseline_owned=("scratch/notes.md",), b1_files=("f.txt",),
                          why="clean baseline")
    handover.write_handover(run_dir, h)
    assert handover.read_handover(run_dir) == h
    from forge import storage as st
    st.handover_path(run_dir).write_bytes(b"{ not json")
    with pytest.raises(handover.HandoverError):
        handover.read_handover(run_dir)


def test_a_record_that_would_not_survive_its_own_round_trip_is_not_written(tmp_path):
    """JSON has one sequence type. A `baseline_owned` handed in as a list reads back as one
    and compares unequal to the record in memory, with nothing saying so until `--gc` asks."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    h = handover.Handover(run_id="r", branch="forge/r/synthesis", kind=handover.MERGE_READY,
                          handover_target="refs/heads/main", accepted=False, out_of_band=(),
                          baseline_owned=["scratch/notes.md"], b1_files=(), why="clean")
    with pytest.raises(handover.HandoverError, match="round trip"):
        handover.write_handover(run_dir, h)
    assert not storage.handover_path(run_dir).exists()
