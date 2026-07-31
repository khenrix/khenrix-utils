"""Origin is provenance, not eligibility (spec §7.1)."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import gitcmd, harvest, snapshot, storage  # noqa: E402
# The fixtures' env builder is reused rather than restated, for the reason its own
# docstring gives: under an exported GIT_DIR + GIT_WORK_TREE (a hook, `git rebase --exec`,
# `git bisect run`) a bare `git -C <seat>` reads and WRITES the developer's real repository.
# Measured: `git -C seat rev-parse HEAD` returned the outer repo's HEAD, and
# `git -C seat commit -a` committed the outer repo's staged file.
from forge_fixtures import _env as _hermetic_env, commit_all, make_repo, write  # noqa: E402


def _head(repo) -> str:
    """The pinned baseline commit B, read in the fixtures' hermetic environment.

    The return code is checked rather than discarded: a failed rev-parse also prints
    nothing, and an empty baseline would make `git diff <B>` fail for a second reason.
    """
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                       capture_output=True, text=True, env=_hermetic_env())
    assert r.returncode == 0, f"rev-parse failed: {r.stderr.strip()}"
    return r.stdout.strip()


def _phases(seat, setup, work, verify):
    """Run three stub 'commands' around the three inventory points."""
    f0 = harvest.record(seat)
    setup()
    fsetup = harvest.record(seat)
    work()
    fwork = harvest.record(seat)
    verify()
    fverify = harvest.record(seat)
    return harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=fverify)


def test_setup_output_is_not_in_the_artifact_set(tmp_path):
    """npm ci / uv sync create thousands of files; they are not the agent's work."""
    seat = make_repo(tmp_path, "seat")
    base = _head(seat)
    p = _phases(seat,
                setup=lambda: (Path(seat) / "node_modules").mkdir() or
                              (Path(seat) / "node_modules" / "x.js").write_text("dep\n"),
                work=lambda: write(seat, "src.py", "the agent's work\n"),
                verify=lambda: write(seat, "report.txt", "verify output\n"))
    a = harvest.artifact_set(p, seat, base)
    assert "src.py" in a.paths
    assert "node_modules/x.js" not in a.paths
    assert "report.txt" not in a.paths
    assert a.origin["src.py"] == "builder"


def test_a_path_touched_by_setup_and_the_agent_is_flagged_overlap(tmp_path):
    seat = make_repo(tmp_path, "seat")
    base = _head(seat)
    p = _phases(seat,
                setup=lambda: write(seat, "lock.txt", "v1\n"),
                work=lambda: write(seat, "lock.txt", "v2\n"),
                verify=lambda: None)
    a = harvest.artifact_set(p, seat, base)
    assert "lock.txt" in a.paths
    assert "lock.txt" in a.setup_overlap


def test_verify_origin_is_recorded_not_discarded(tmp_path):
    """§7.2: a verify-origin change can be a REQUIRED deliverable, so label it."""
    seat = make_repo(tmp_path, "seat")
    base = _head(seat)
    p = _phases(seat, setup=lambda: None,
                work=lambda: write(seat, "src.py", "work\n"),
                verify=lambda: write(seat, "generated.txt", "rendered\n"))
    a = harvest.artifact_set(p, seat, base)
    assert a.origin.get("generated.txt") == "verify"
    assert "generated.txt" not in a.paths


def test_tracked_content_comes_from_the_baseline_not_the_seat_head(tmp_path):
    """A seat that commits would show an empty `git diff` against its own HEAD."""
    seat = make_repo(tmp_path, "seat")
    base = _head(seat)
    p = _phases(seat, setup=lambda: None,
                work=lambda: (write(seat, "seed.txt", "changed by agent\n"),
                              commit_all(seat, "w")),
                verify=lambda: None)
    a = harvest.artifact_set(p, seat, base)
    assert "seed.txt" in a.paths
    assert "changed by agent" in a.tracked_diff


def test_builder_origin_wins_over_setup_and_setup_wins_over_verify(tmp_path):
    """One precedence rule, not two idioms: the path set is defined by the work phase, so
    a path the agent touched is builder-origin whichever other phase also touched it."""
    seat = make_repo(tmp_path, "seat")
    base = _head(seat)
    p = _phases(seat,
                setup=lambda: (write(seat, "lock.txt", "v1\n"),
                               write(seat, "shared.txt", "s1\n")),
                work=lambda: write(seat, "lock.txt", "v2\n"),
                verify=lambda: write(seat, "shared.txt", "s2\n"))
    a = harvest.artifact_set(p, seat, base)
    assert a.origin["lock.txt"] == "builder", "setup must not overwrite the agent's claim"
    assert a.origin["shared.txt"] == "setup", "verify must not overwrite setup's claim"


def test_a_path_touched_by_the_agent_and_verify_is_flagged_overlap(tmp_path):
    """§7.2: "if builder and verifier both touch a path: label work+verify overlap".
    Labelling it "builder" and stopping there is exactly the discard §7.2 forbids — this
    repo's own case, where the agent runs `make render` and verify re-runs it."""
    seat = make_repo(tmp_path, "seat")
    base = _head(seat)
    p = _phases(seat, setup=lambda: None,
                work=lambda: write(seat, "gen.txt", "v1\n"),
                verify=lambda: write(seat, "gen.txt", "v2\n"))
    a = harvest.artifact_set(p, seat, base)
    assert "gen.txt" in a.paths
    assert "gen.txt" in a.verify_overlap
    assert a.origin["gen.txt"] == "builder"


def test_a_seat_with_no_work_changes_yields_no_content_at_all(tmp_path):
    """`git diff <B> --` with an EMPTY pathspec list diffs the whole tree, so a seat that
    changed nothing would hand back setup's tracked churn as the candidate's content."""
    seat = make_repo(tmp_path, "seat")
    base = _head(seat)
    p = _phases(seat,
                setup=lambda: write(seat, "seed.txt", "SETUP TOUCHED A TRACKED FILE\n"),
                work=lambda: None, verify=lambda: None)
    a = harvest.artifact_set(p, seat, base)
    assert a.paths == ()
    assert a.origin["seed.txt"] == "setup"
    assert a.tracked_diff == "", "an empty path set must mean no content, not all content"


def test_an_all_untracked_path_set_is_an_empty_diff_not_a_failure(tmp_path):
    """The precondition that makes `check=True` on the diff safe: git exits 0 for a
    pathspec that matches nothing tracked, so only a REAL failure is nonzero."""
    seat = make_repo(tmp_path, "seat")
    base = _head(seat)
    p = _phases(seat, setup=lambda: None,
                work=lambda: write(seat, "brand-new.txt", "untracked\n"),
                verify=lambda: None)
    a = harvest.artifact_set(p, seat, base)
    assert a.paths == ("brand-new.txt",)
    assert a.tracked_diff == ""


def test_a_baseline_the_seat_does_not_have_fails_loudly(tmp_path):
    """`check=False` made a git failure indistinguishable from an empty diff: a pinned B
    missing from the seat's object store would hand over an EMPTY candidate in silence."""
    seat = make_repo(tmp_path, "seat")
    p = _phases(seat, setup=lambda: None,
                work=lambda: write(seat, "seed.txt", "changed by agent\n"),
                verify=lambda: None)
    with pytest.raises(gitcmd.GitError):
        harvest.artifact_set(p, seat, "deadbeef" * 5)


def test_a_glob_in_a_filename_cannot_pull_another_file_into_the_content(tmp_path):
    """A git pathspec is a GLOB, not a filename. Measured: `git diff <B> -- 'a*.txt'`
    emitted ab.txt's diff too — setup-origin content laundered into the candidate."""
    seat = make_repo(tmp_path, "seat")
    write(seat, "ab.txt", "x\n")
    write(seat, "a*.txt", "y\n")
    base = commit_all(seat, "extra")
    p = _phases(seat,
                setup=lambda: write(seat, "ab.txt", "SETUP-ONLY\n"),
                work=lambda: write(seat, "a*.txt", "AGENT-WORK\n"),
                verify=lambda: None)
    a = harvest.artifact_set(p, seat, base)
    assert a.paths == ("a*.txt",)
    assert "AGENT-WORK" in a.tracked_diff
    assert "SETUP-ONLY" not in a.tracked_diff, "the glob matched a file the agent never touched"


def test_a_colon_prefixed_filename_is_not_silently_dropped_from_the_content(tmp_path):
    """The opposite failure of the same cause, and the worse one: `:x` parses as pathspec
    MAGIC, so `git diff <B> -- ':weird.txt'` exited 0 with no output — the file's content
    vanished from the candidate with nothing to notice."""
    seat = make_repo(tmp_path, "seat")
    write(seat, ":weird.txt", "z\n")
    base = commit_all(seat, "extra")
    p = _phases(seat, setup=lambda: None,
                work=lambda: write(seat, ":weird.txt", "AGENT-WORK\n"),
                verify=lambda: None)
    a = harvest.artifact_set(p, seat, base)
    assert a.paths == (":weird.txt",)
    assert "AGENT-WORK" in a.tracked_diff


def test_a_file_the_agent_deleted_is_part_of_the_candidate(tmp_path):
    """`snapshot.diff` reports removals, so they belong in the path set — dropping them
    would hand over a candidate that silently resurrects every file the agent removed."""
    seat = make_repo(tmp_path, "seat")
    base = _head(seat)
    p = _phases(seat, setup=lambda: None,
                work=lambda: (Path(seat) / "seed.txt").unlink(),
                verify=lambda: None)
    a = harvest.artifact_set(p, seat, base)
    assert a.paths == ("seed.txt",)
    assert "deleted file" in a.tracked_diff


def test_record_refuses_a_quota_breach_rather_than_returning_a_partial_inventory(tmp_path):
    """snapshot returns ({}, [breach]); an empty inventory read as "the agent deleted the
    tree" is the whole reason that pair must not be dropped on the floor here."""
    seat = make_repo(tmp_path, "seat")
    write(seat, "a.txt", "a\n")
    write(seat, "b.txt", "b\n")
    with pytest.raises(harvest.HarvestError, match="files"):
        harvest.record(seat, quota=storage.Quota(max_files=2, max_file_bytes=1000,
                                                 max_total_bytes=10_000))


def test_an_unwalkable_seat_propagates_the_snapshot_error(tmp_path):
    """Not wrapped: SnapshotError already names the unreadable path and is already a
    RuntimeError, so a second layer would only hide which tree failed."""
    with pytest.raises(snapshot.SnapshotError, match="cannot walk"):
        harvest.record(tmp_path / "never-created")
