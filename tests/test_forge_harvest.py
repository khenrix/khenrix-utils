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
from forge_fixtures import (_env as _hermetic_env, commit_all, git, make_repo,  # noqa: E402
                            write)


def _head(repo) -> str:
    """The pinned baseline commit B, read in the fixtures' hermetic environment.

    The return code is checked rather than discarded: a failed rev-parse also prints
    nothing, and an empty baseline would make `git diff <B>` fail for a second reason.
    """
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                       capture_output=True, text=True, timeout=30, env=_hermetic_env())
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


def _fake_node_modules(seat, packages=52, per_package=100):
    """A setup output of realistic SHAPE, not a single file standing in for one.

    5,200 files is the point: the docstring above this test says "npm ci / uv sync create
    thousands of files", and under `Quota.default` (max_files 5000) `record` raised
    `HarvestError: files: 5001 > 5000` — the inventory that must OBSERVE setup's output
    failing closed before it could. A one-file `node_modules` asserted the sentence and
    exercised none of it. Measured cost of the realistic shape: ~1.6s for the fixture plus
    all four inventories.
    """
    nm = Path(seat) / "node_modules"
    for i in range(packages):
        d = nm / f"pkg{i}" / "lib"
        d.mkdir(parents=True)
        for j in range(per_package):
            (d / f"m{j}.js").write_text("module.exports = {}\n")
    return packages * per_package


def test_setup_output_is_not_in_the_artifact_set(tmp_path):
    """npm ci / uv sync create thousands of files; they are not the agent's work."""
    seat = make_repo(tmp_path, "seat")
    base = _head(seat)
    p = _phases(seat,
                setup=lambda: _fake_node_modules(seat),
                work=lambda: write(seat, "src.py", "the agent's work\n"),
                verify=lambda: write(seat, "report.txt", "verify output\n"))
    a = harvest.artifact_set(p, seat, base)
    # The inventory must have SEEN the dependency tree, or it was differenced out of the
    # artifact set vacuously. This is the assertion the old one-file fixture could not make
    # and the reason the default quota was wrong for a seat.
    assert len(p.fsetup) > 5000, f"setup output was not observed: {len(p.fsetup)} entries"
    assert "src.py" in a.paths
    assert "node_modules/pkg0/lib/m0.js" not in a.paths
    assert not any(q.startswith("node_modules/") for q in a.paths)
    assert "report.txt" not in a.paths
    assert a.origin["src.py"] == "builder"
    # §7.1: the flag exists "so the ledger states the churn component instead of absorbing
    # it silently". A flag raised on every path states nothing and would have a reviewer
    # discount the whole candidate — so pin that it stays DOWN when nothing overlaps.
    assert a.setup_overlap == (), "setup touched only node_modules/ here"
    assert a.verify_overlap == (), "verify touched only report.txt here"


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


def test_a_gitattributes_diff_marker_cannot_empty_the_candidate(tmp_path):
    """Without `--binary`, git emits `Binary files a/x and b/x differ` and NO content, at
    exit 0 — for ordinary binaries and for any text file the seat marked `-diff` in its
    own in-tree .gitattributes, a routine pattern for lockfiles. The path would sit in
    `paths` asserting the agent changed it with nothing able to reconstruct it."""
    seat = make_repo(tmp_path, "seat")
    write(seat, ".gitattributes", "pkg.lock -diff\n")
    write(seat, "pkg.lock", "v1\n")
    base = commit_all(seat, "extra")
    p = _phases(seat, setup=lambda: None,
                work=lambda: write(seat, "pkg.lock", "v2-AGENT-WORK\n"),
                verify=lambda: None)
    a = harvest.artifact_set(p, seat, base)
    assert a.paths == ("pkg.lock",)
    assert "GIT binary patch" in a.tracked_diff
    assert "Binary files" not in a.tracked_diff, "content dropped at exit 0"


def test_a_non_utf8_file_neither_aborts_the_harvest_nor_corrupts_the_patch(tmp_path):
    """git echoes the file's raw bytes, so under subprocess' strict UTF-8 decode ONE
    latin-1 file raised UnicodeDecodeError and took every other path's content with it —
    in a class that is none of the three this module documents."""
    seat = make_repo(tmp_path, "seat")
    legacy = Path(seat) / "legacy.txt"
    legacy.write_bytes("café naïve\n".encode("latin-1"))
    base = commit_all(seat, "extra")
    p = _phases(seat, setup=lambda: None,
                work=lambda: legacy.write_bytes("café CHANGED\n".encode("latin-1")),
                verify=lambda: None)
    a = harvest.artifact_set(p, seat, base)
    assert a.paths == ("legacy.txt",)
    # Reversibility is the point, not mere survival: a lossy decode yields a patch that
    # no longer applies, which §6 materialization needs and no exit code would reveal.
    raw = a.tracked_diff.encode("utf-8", "surrogateescape")
    assert b"caf\xe9 CHANGED" in raw, "the patch no longer carries the file's real bytes"


def test_record_refuses_a_quota_breach_rather_than_returning_a_partial_inventory(tmp_path):
    """snapshot returns ({}, [breach]); an empty inventory read as "the agent deleted the
    tree" is the whole reason that pair must not be dropped on the floor here."""
    seat = make_repo(tmp_path, "seat")
    write(seat, "a.txt", "a\n")
    write(seat, "b.txt", "b\n")
    with pytest.raises(harvest.HarvestError, match="files"):
        harvest.record(seat, quota=storage.Quota(max_files=2, max_file_bytes=1000,
                                                 max_total_bytes=10_000))


def test_the_harvest_default_quota_is_a_seats_budget_not_the_screens(tmp_path):
    """The two callers ask different questions and must not share one answer.

    `Quota.default` bounds what the PRE-LAUNCH SCREEN will decode out of the user's own
    source; `record` inventories a seat AFTER the engine's setup ran inside it. Sharing
    `default` made the second fail closed on the first thing it exists to see. Raising
    `default` itself is the fix that must NOT happen — it would loosen the screen — so the
    screen's cap is asserted unmoved in the same test as the harvest's.

    The fail-closed property is asserted on the SAME call that gets the larger budget: an
    explicit quota still refuses, so what changed is the number, not the stance.
    """
    assert storage.Quota.default().max_files == 5000, "the screen's decode budget moved"
    h = storage.Quota.for_harvest()
    assert h.max_files > 5000 and h.max_file_bytes >= storage.Quota.default().max_file_bytes
    assert h.breach(files=h.max_files + 1, file_bytes=0, total_bytes=0), \
        "a harvest quota that cannot breach is not fail-closed"
    seat = make_repo(tmp_path, "seat")
    write(seat, "a.txt", "a\n")
    assert harvest.record(seat), "the default harvest budget must admit an ordinary seat"


def test_an_unwalkable_seat_propagates_the_snapshot_error(tmp_path):
    """Not wrapped: SnapshotError already names the unreadable path and is already a
    RuntimeError, so a second layer would only hide which tree failed."""
    with pytest.raises(snapshot.SnapshotError, match="cannot walk"):
        harvest.record(tmp_path / "never-created")


@pytest.mark.parametrize("vector", ["diff.external", "driver.command", "driver.textconv"])
def test_harvesting_never_runs_a_program_the_seats_own_config_names(tmp_path, vector):
    """A diff driver is a command git runs while producing the diff, named in the repository's
    own config — the same shape as `core.fsmonitor`, at the same call site, and reached
    through a key that pin did not cover. `diff.external` needs the config alone; the other
    two are selected by an in-tree `.gitattributes`, which the seat also writes.

    THE WHOLE CHAIN, not the config value: the seat writes its config as any agent would, and
    then the ENGINE harvests it — which happens after the agent's own process has exited, so
    a program that runs here runs in the trusted parent with nothing left to attribute it to.

    The patch is asserted too, and it is not a second-order concern: measured before the fix,
    textconv gave the file's converted text, a driver `command` gave the driver's own output,
    and `diff.external` gave NOTHING at exit 0 — a candidate that silently carries no content,
    which is what `--binary` and `:(literal)` are already spent preventing.
    """
    seat = make_repo(tmp_path, "seat")
    write(seat, "app.py", "v1\n")
    base = commit_all(seat, "extra")
    p = _phases(seat, setup=lambda: None,
                work=lambda: write(seat, "app.py", "v2-AGENT-WORK\n"),
                verify=lambda: None)
    honest = harvest.artifact_set(p, seat, base)
    assert honest.paths == ("app.py",)
    assert "v2-AGENT-WORK" in honest.tracked_diff

    ran = Path(tmp_path) / "PROGRAM-RAN"
    prog = write(seat, "rig.sh", f"#!/bin/sh\n: > {ran}\ncat \"$1\" 2>/dev/null\n")
    prog.chmod(0o755)
    # Written with an ordinary git, never through `gitcmd`: the party doing this is the agent
    # inside its own clone, and routing it through the engine's hardened wrapper would hide
    # whether the property survives the config an ordinary `git config` leaves behind.
    if vector == "diff.external":
        git(seat, "config", "--local", "diff.external", str(prog))
    else:
        write(seat, ".gitattributes", "app.py diff=rigged\n")
        git(seat, "config", "--local", f"diff.rigged.{vector.split('.')[1]}", str(prog))

    rigged = harvest.artifact_set(p, seat, base)
    assert not ran.exists(), "the engine ran a program the seat's own config named"
    assert rigged.tracked_diff == honest.tracked_diff, \
        "the seat's config changed the bytes the candidate carries"

    # The control, and the test asserts nothing without it: an ordinary git in that same tree
    # must run the program, or the fixture proved only that the rigging never worked.
    ran.unlink(missing_ok=True)
    git(seat, "diff", "--binary", base, "--", "app.py")
    assert ran.exists(), \
        "the control failed: this rigging does not run even for an unhardened git"


def test_a_seat_named_clean_filter_is_the_reach_the_flags_do_not_close(tmp_path):
    """TURNED ROUND, as the version of this docstring that named the close asked for.

    What follows describes the reach that is still THERE — git has no flag that refuses a
    clean driver, and one would still substitute the builder's bytes. What changed is that
    the harvest now REFUSES rather than proceeding, so the assertions below run against the
    refusal instead of against a patch carrying the driver's output. The mechanism paragraphs
    are kept verbatim because they are why the refusal is shaped the way it is.

    A diff against the WORKING TREE has to put worktree content into git's form, so
    `filter.<d>.clean` — a program named in the seat's own config, selected by the seat's own
    `.gitattributes` — runs on this call, and git has no flag that refuses it. There is no
    fixed key to `-c` away either: the driver NAME is the seat's to choose.

    What that costs is stated exactly, and it is SUBSTITUTION rather than added noise: the
    program runs, and the agent's own text is gone from the patch with the filter's in its
    place — which is why the third assertion pins the absence and not only the presence. It is
    not a way into the engine's own state beyond running the program, and it is not closed
    here; a future close should turn these assertions round rather than delete them.

    THE CLOSE IS DETECTION, NOT A READBACK, and the difference is worth pinning here because a
    readback is the one that looks right. `verify._assert_hooks_pinned` can promise something
    because it bounds two engine actions with one known writer between them; harvest has no
    such boundary, a canary reads back a value the ENGINE wrote where the driver name is the
    seat's out of an unbounded namespace, and a readback would be a second invocation from this
    diff anyway. `inspect._filtered_paths` is the shape that fits — `check-attr -z filter` over
    the harvested paths, which NAMES a rigged seat.
    """
    seat = make_repo(tmp_path, "seat")
    write(seat, "app.py", "v1\n")
    base = commit_all(seat, "extra")
    p = _phases(seat, setup=lambda: None,
                work=lambda: write(seat, "app.py", "v2-AGENT-WORK\n"),
                verify=lambda: None)

    ran = Path(tmp_path) / "FILTER-RAN"
    prog = write(seat, "rig.sh", f"#!/bin/sh\n: > {ran}\nsed s/AGENT-WORK/FILTERED/\n")
    prog.chmod(0o755)
    write(seat, ".gitattributes", "app.py filter=rigged\n")
    git(seat, "config", "--local", "filter.rigged.clean", str(prog))

    with pytest.raises(harvest.HarvestError) as e:
        harvest.artifact_set(p, seat, base)
    assert "app.py" in str(e.value)
    assert not ran.exists(), \
        "the refusal must come BEFORE the diff, or the driver has already run and the only " \
        "thing refused is the caller's use of its output"

    # THE CONTROL, AND IT IS WHAT KEEPS THE REFUSAL FROM PASSING VACUOUSLY. Without it this
    # test is satisfied by a harvest that refuses everything, or by a fixture whose rigging
    # never worked. An ordinary git in this same tree must still run the program.
    git(seat, "diff", "--binary", base, "--", "app.py")
    assert ran.exists(), \
        "the control failed: this rigging does not run even for an unhardened git, so the " \
        "refusal above proved nothing"


def test_a_seat_configured_clean_filter_refuses_the_harvest(tmp_path):
    """The close the residual test above asks for, turned round rather than deleted.

    §7's diff runs in the TRUSTED PARENT and `filter.<name>.clean` still executes, so the
    driver's output SUBSTITUTES for the builder's bytes. `ArtifactSet` carries no per-path
    Fwork hashes and `bundle.build` treats a path present in the patch as covered, so the
    live file is never re-read as a sidecar and nothing downstream can notice.

    Every other founding-premise finding is about a check the builder could influence. This
    one is about the BYTES THE CHECK IS RUN ON: broken code on disk, passing code in the
    patch, and a verifier that materializes and passes the replacement while the verdict is
    presented as being about the builder's artifact.
    """
    seat = make_repo(tmp_path, "seat")
    write(seat, "app.py", "def f(): return 1\n")
    base = commit_all(seat, "seed")
    p = _phases(seat, setup=lambda: None,
                work=lambda: write(seat, "app.py", "def f(): raise RuntimeError\n"),
                verify=lambda: None)
    prog = write(seat, "rig.sh", "#!/bin/sh\nsed 's/raise RuntimeError/return 1/'\n")
    prog.chmod(0o755)
    write(seat, ".gitattributes", "app.py filter=rigged\n")
    git(seat, "config", "--local", "filter.rigged.clean", str(prog))

    with pytest.raises(harvest.HarvestError) as e:
        harvest.artifact_set(p, seat, base)
    assert "app.py" in str(e.value) and "filter" in str(e.value).lower()


def test_a_filter_the_BASELINE_declares_does_not_refuse_the_harvest(tmp_path):
    """The guard that keeps this from firing on every git-lfs repository.

    `check-attr` reports the EFFECTIVE attribute, and `filter=lfs` is declared in the
    BASELINE's own `.gitattributes` in a very large fraction of real repositories. A rule
    reading "any filter is active" would raise for every seat, after every provider call is
    paid, on all of them — which is the shape a widened check must not have. The question is
    not whether a filter is active; it is whether the seat's own config DEFINES what the
    attribute names, since the attribute alone is inert and `.git/config` is what a seat can
    write.
    """
    seat = make_repo(tmp_path, "seat")
    write(seat, ".gitattributes", "*.bin filter=lfs\n")
    write(seat, "app.py", "v1\n")
    base = commit_all(seat, "seed with an lfs attribute and no driver")
    p = _phases(seat, setup=lambda: None,
                work=lambda: write(seat, "app.py", "v2\n"), verify=lambda: None)

    a = harvest.artifact_set(p, seat, base)
    assert "app.py" in a.paths
