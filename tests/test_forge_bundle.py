"""What crosses from a seat into a verifier — and what provably does not."""
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import pytest  # noqa: E402
from forge import (baseline, bundle, fleet, harvest,  # noqa: E402
                   inspect as finspect, snapshot)
from forge_fixtures import make_repo, write, git  # noqa: E402

IDENT = ("Forge Seat", "seat@forge.invalid")


def _seat(tmp_path, selected=(), name="claude"):
    repo = make_repo(tmp_path)
    run = tmp_path / "run"; run.mkdir()
    f = finspect.repo_facts(repo)
    b = baseline.materialize(repo, run, f, list(selected), "r1")
    s = fleet.clone_seat(repo, b, tmp_path / name, name=name, identity=IDENT)
    return repo, b, s


def _phases(seat, work):
    f0 = harvest.record(seat)
    fsetup = harvest.record(seat)
    work()
    fwork = harvest.record(seat)
    return harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=harvest.record(seat))


def test_a_tracked_edit_crosses_and_applies(tmp_path):
    repo, b, s = _seat(tmp_path)
    p = _phases(s.path, lambda: write(s.path, "seed.txt", "agent edit\n"))
    a = harvest.artifact_set(p, s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    written = bundle.materialize(cb, dest)
    assert "seed.txt" in written
    assert (dest / "seed.txt").read_text() == "agent edit\n"


def test_an_untracked_file_crosses_as_a_sidecar_not_a_patch(tmp_path):
    """`tracked_diff` is a partial view of `paths` — untracked is one of its three holes."""
    repo, b, s = _seat(tmp_path)
    p = _phases(s.path, lambda: write(s.path, "new.py", "print('hi')\n"))
    a = harvest.artifact_set(p, s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    assert "new.py" in [e.path for e in cb.sidecars]
    assert cb.omitted == ()
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    bundle.materialize(cb, dest)
    assert (dest / "new.py").read_text() == "print('hi')\n"


def test_an_executable_bit_survives_the_crossing(tmp_path):
    repo, b, s = _seat(tmp_path)
    def work():
        q = write(s.path, "run.sh", "#!/bin/sh\necho hi\n")
        q.chmod(0o755)
    p = _phases(s.path, work)
    a = harvest.artifact_set(p, s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    bundle.materialize(cb, dest)
    assert os.access(dest / "run.sh", os.X_OK), "mode dropped; a test runner would not run"


def test_a_binary_file_crosses_intact(tmp_path):
    """`git diff` without --binary drops content at exit 0; harvest passes --binary."""
    repo, b, s = _seat(tmp_path)
    blob = bytes(range(256)) * 4
    p = _phases(s.path, lambda: (s.path / "img.bin").write_bytes(blob))
    a = harvest.artifact_set(p, s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    bundle.materialize(cb, dest)
    assert (dest / "img.bin").read_bytes() == blob


def test_a_deletion_crosses_as_a_deletion(tmp_path):
    repo, b, s = _seat(tmp_path)
    p = _phases(s.path, lambda: (s.path / "seed.txt").unlink())
    a = harvest.artifact_set(p, s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    bundle.materialize(cb, dest)
    assert not (dest / "seed.txt").exists()


def test_setup_output_does_not_cross(tmp_path):
    """The bundle carries the AGENT's work. node_modules is not it."""
    repo, b, s = _seat(tmp_path)
    f0 = harvest.record(s.path)
    (s.path / "node_modules").mkdir()
    write(s.path, "node_modules/dep.js", "dep\n")
    fsetup = harvest.record(s.path)
    write(s.path, "src.py", "work\n")
    fwork = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=fwork), s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    carried = set(_carried(cb))
    assert "src.py" in carried
    assert not any(c.startswith("node_modules/") for c in carried)


def test_a_path_the_bundle_cannot_carry_is_named_in_omitted(tmp_path):
    """A verifier failure from a missing input must be distinguishable from a defect.

    The FIFO is made INSIDE the work phase, not before it. `paths` is `Fsetup -> Fwork`,
    and `snapshot` records a special file's TYPE as its digest — so a pipe that already
    existed at Fsetup is byte-identical at Fwork and never enters `paths` at all. Measured
    with the mkfifo hoisted above `_phases`: `artifact_set(...).paths == ('src.py',)`, and
    the assertion below then fails against a perfectly correct bundle.
    """
    repo, b, s = _seat(tmp_path)

    def work():
        os.mkfifo(s.path / "pipe")        # in paths; no honest payload
        write(s.path, "src.py", "work\n")
    p = _phases(s.path, work)
    a = harvest.artifact_set(p, s.path, b.commit)
    assert set(a.paths) == {"pipe", "src.py"}, "precondition: both reach the bundle"
    cb = bundle.build(s.path, a, b)
    assert "pipe" in cb.omitted
    assert "src.py" not in cb.omitted


def test_materialize_refuses_a_bundle_from_a_different_baseline(tmp_path):
    """Two `make_repo` fixtures are not two baselines.

    `make_repo` commits an identical tree with an identical message, author and committer,
    and git's timestamps have one-second granularity — so two of them built in the same
    second are the SAME commit OID. Measured: both baselines came back
    `3d93025854a6...`, the verifier clone's HEAD matched the bundle's, and a correct
    `materialize` had nothing to refuse. The extra commit gives `other` a tree of its own,
    and the precondition below fails loudly rather than vacuously if that ever stops
    holding.
    """
    repo, b, s = _seat(tmp_path)
    p = _phases(s.path, lambda: write(s.path, "src.py", "work\n"))
    cb = bundle.build(s.path, harvest.artifact_set(p, s.path, b.commit), b)
    other = make_repo(tmp_path, "other")
    write(other, "elsewhere.txt", "a tree of its own\n")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "other")
    run2 = tmp_path / "run2"; run2.mkdir()
    b2 = baseline.materialize(other, run2, finspect.repo_facts(other), [], "r2")
    assert b2.commit != b.commit, "precondition: the two baselines must actually differ"
    dest = tmp_path / "verifier"
    fleet.clone_seat(other, b2, dest, name="verifier", identity=IDENT)
    with pytest.raises(bundle.BundleError, match="baseline"):
        bundle.materialize(cb, dest)


def _carried(cb):
    """Every path the bundle actually carries, from both channels."""
    out = [e.path for e in cb.sidecars]
    for line in cb.tracked_patch.decode("utf-8", "surrogateescape").splitlines():
        if line.startswith("+++ b/"):
            out.append(line[6:])
    return out


# --- D-1: what a symlink is, and which ones cross -------------------------------------

def test_an_in_tree_symlink_crosses_as_a_link_not_as_its_targets_content(tmp_path):
    """D-1: a link is its TARGET TEXT, which is `snapshot`'s and `baseline`'s answer too.

    `docs/latest -> v2` is an ordinary artefact, so omitting every link would mutilate
    ordinary candidates. Carrying its target's CONTENT instead would put a file the
    candidate never named into the candidate — and would produce a regular file in the
    verifier where the seat had a link, which is a different tree.
    """
    repo, b, s = _seat(tmp_path)

    def work():
        write(s.path, "docs/v2.md", "second edition\n")
        (s.path / "docs" / "latest").symlink_to("v2.md")
    p = _phases(s.path, work)
    a = harvest.artifact_set(p, s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    link = [e for e in cb.sidecars if e.path == "docs/latest"]
    assert link and (link[0].kind, link[0].payload) == ("symlink", b"v2.md")
    assert cb.omitted == ()

    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    bundle.materialize(cb, dest)
    out = dest / "docs" / "latest"
    assert out.is_symlink(), "materialized as a regular file; the verifier tree differs"
    assert os.readlink(out) == "v2.md"
    assert out.read_text() == "second edition\n", "and it still resolves inside the clone"


def test_a_symlink_that_would_escape_the_verifier_is_omitted(tmp_path):
    """D-1's containment half: an escaping link never crosses.

    A verifier clone exists so the confirmed command runs where the builder could not reach.
    Materializing `out -> ../../elsewhere` and then running that command would write through
    it, straight out of the clone — so the link is refused and NAMED, which is the one
    outcome that keeps a resulting failure honest.

    Three spellings escape. A `..`-prefixed target and an absolute one — the absolute case
    matters even when it names a path inside the SEAT, because the verifier is a different
    directory, so the link would point back at the builder's own tree. And a BARE `..`,
    which normalizes to exactly `".."` rather than to anything with a separator in it, so a
    `startswith("../")` test alone lets the parent directory of the whole clone across.
    """
    repo, b, s = _seat(tmp_path)

    def work():
        (s.path / "up").symlink_to(Path("..") / ".." / "elsewhere")
        (s.path / "abs").symlink_to(s.path / "seed.txt")
        (s.path / "parent").symlink_to("..")
        write(s.path, "fine.txt", "carried\n")
    p = _phases(s.path, work)
    a = harvest.artifact_set(p, s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    assert set(cb.omitted) == {"up", "abs", "parent"}
    assert [e.path for e in cb.sidecars] == ["fine.txt"]

    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    bundle.materialize(cb, dest)
    for gone in ("up", "abs", "parent"):
        assert not os.path.lexists(dest / gone)
    assert (dest / "fine.txt").read_text() == "carried\n"


def test_an_escaping_symlink_the_PATCH_carries_is_refused_too(tmp_path):
    """The route the sidecar guard never saw: `git add` on a link puts it in the diff.

    A seat is given a branch and an identity precisely so it can stage and commit, so a
    mode-120000 entry in `tracked_patch` is an ordinary agent action, not an exotic one.
    Measured before the guard, on this exact fixture: `omitted ()`, `sidecars []`, the
    verifier carrying `esc -> ../outside`, and a write through it landing outside the clone
    (`PWNED`). The test above creates only UNTRACKED links, so it exercises the sidecar
    route exclusively and stayed green over this the whole time.

    The co-changed tracked file is the other half: refusing the link must narrow the patch,
    not discard the candidate.
    """
    repo, b, s = _seat(tmp_path)
    outside = tmp_path / "outside"; outside.mkdir()

    def work():
        os.symlink(Path("..") / "outside", s.path / "esc")
        write(s.path, "seed.txt", "real work\n")
        git(s.path, "add", "esc", "seed.txt")
    p = _phases(s.path, work)
    a = harvest.artifact_set(p, s.path, b.commit)
    assert "120000" in a.tracked_diff, \
        "precondition: the link is in the PATCH channel, not the sidecar channel"

    cb = bundle.build(s.path, a, b)
    assert cb.omitted == ("esc",)
    assert [e.path for e in cb.sidecars] == []
    assert "120000" not in cb.tracked_patch.decode("utf-8", "surrogateescape"), \
        "the refused link must leave the patch, not merely be named beside it"

    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    bundle.materialize(cb, dest)
    assert not os.path.lexists(dest / "esc")
    assert (dest / "seed.txt").read_text() == "real work\n", \
        "and the rest of the candidate still crossed"
    assert not (outside / "PWNED.txt").exists()


def test_a_rename_preimage_that_still_has_content_crosses_as_a_sidecar(tmp_path):
    """Move a file and leave a shim at the old name — an ordinary refactor.

    The patch renames `seed.txt -> renamed.txt`, so the preimage appears only in the
    REVERSED `--numstat` pass, and all the patch does with it is delete it. Unioning the two
    passes calls that "carried": measured before the fix, `sidecars []`, `omitted ()`, and a
    verifier with no `seed.txt` at all — the bundle silently missing an input while claiming
    a clean `omitted`. That is fail-OPEN, and it falsifies `build`'s own stated invariant
    that `omitted` is a superset of "the verifier will be missing something".

    `test_a_rename_names_both_sides...` passes either way, because `git mv` leaves nothing
    behind: the discriminating fixture is the one where the old name still has content.
    """
    repo, b, s = _seat(tmp_path)
    f0 = harvest.record(s.path)
    fsetup = harvest.record(s.path)
    git(s.path, "mv", "seed.txt", "renamed.txt")
    write(s.path, "seed.txt", "SHIM: moved to renamed.txt\n")
    fwork = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=fwork), s.path, b.commit)
    assert "rename from" in a.tracked_diff, "precondition: git emitted a rename"

    cb = bundle.build(s.path, a, b)
    assert [e.path for e in cb.sidecars] == ["seed.txt"], \
        "the shim is not carried by the patch, so it must be a sidecar"
    assert cb.omitted == ()

    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    bundle.materialize(cb, dest)
    # Sidecars are written AFTER the patch, so the rename's delete does not win.
    assert (dest / "seed.txt").read_text() == "SHIM: moved to renamed.txt\n"
    assert (dest / "renamed.txt").read_text() == "seed\n"


def test_a_path_both_channels_touch_is_returned_once(tmp_path):
    """`materialize`'s return is a SET of paths, so `len()` counts paths and not writes.

    The rename-with-a-shim above is the shape that produces the collision: the patch
    DELETES the preimage and a sidecar then puts the shim back, so `seed.txt` reaches the
    return through both channels. Measured before the dedupe: `('renamed.txt', 'seed.txt',
    'seed.txt')` — a three-element answer for a two-path candidate, on exactly the shape
    the rename fix introduced.
    """
    repo, b, s = _seat(tmp_path)
    f0 = harvest.record(s.path)
    fsetup = harvest.record(s.path)
    git(s.path, "mv", "seed.txt", "renamed.txt")
    write(s.path, "seed.txt", "SHIM: moved to renamed.txt\n")
    fwork = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=fwork), s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    assert [e.path for e in cb.sidecars] == ["seed.txt"], \
        "precondition: the shim crosses as a sidecar, so both channels name seed.txt"

    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    written = bundle.materialize(cb, dest)
    assert written == ("renamed.txt", "seed.txt")
    assert len(written) == len(set(written))


def test_materialize_refuses_a_sidecar_symlink_pointing_out_of_the_tree(tmp_path):
    """The containment guard runs on a PATH on both sides; a sidecar's TARGET on one.

    Under the deserialize-from-a-ledger model those guards exist for, a link plus a file
    underneath it writes outside the verifier just as effectively as an `..` in the path —
    and neither entry below has an `..` in its path.
    """
    repo, b, s = _seat(tmp_path)
    outside = tmp_path / "outside"; outside.mkdir()
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    evil = bundle.CandidateBundle(
        version=1, baseline_ref=b.ref, baseline_commit=b.commit,
        sidecars=(bundle.SidecarEntry("esc", "symlink", 0, b"../outside"),
                  bundle.SidecarEntry("esc/victim.txt", "file", 0o644, b"PWNED\n")))
    with pytest.raises(bundle.BundleError, match="points out of the tree"):
        bundle.materialize(evil, dest)
    assert not (outside / "victim.txt").exists()
    assert not os.path.lexists(dest / "esc")


def test_a_patch_that_is_not_a_patch_is_refused_before_anything_is_written(tmp_path):
    """`_covered` reads the bundle's own bytes to say what the patch carries.

    A `git apply --numstat` that fails means the engine cannot say what this bundle would
    do — so it must not be applied. Ignoring the exit code instead yields an empty
    `covered` set, which reads as "this patch carries nothing" and would let the apply run
    anyway on bytes nobody could account for.
    """
    repo, b, s = _seat(tmp_path)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    cb = bundle.CandidateBundle(version=1, baseline_ref=b.ref, baseline_commit=b.commit,
                                tracked_patch=b"this is not a unified diff\n")
    with pytest.raises(bundle.BundleError, match="cannot determine"):
        bundle.materialize(cb, dest)


# --- the three ways a path can fail to be carryable ------------------------------------

def test_a_deleted_untracked_path_is_named_in_omitted(tmp_path):
    """`paths` carries removals too, and the bundle has no delete-an-untracked channel.

    Inventing one would not help: a verifier runs setup AFTER materialization, so whatever
    setup created would come back regardless. Naming it is what makes a resulting failure
    attributable.
    """
    repo, b, s = _seat(tmp_path)
    write(s.path, "stale.txt", "from setup\n")
    f0 = harvest.record(s.path)
    fsetup = harvest.record(s.path)
    (s.path / "stale.txt").unlink()
    fwork = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=fwork), s.path, b.commit)
    assert a.paths == ("stale.txt",), "precondition: a removal is in the path set"
    cb = bundle.build(s.path, a, b)
    assert cb.omitted == ("stale.txt",)
    assert cb.sidecars == ()


def test_an_unreadable_file_is_omitted_rather_than_raising(tmp_path):
    """`build`'s contract is that every path lands in exactly one channel.

    One unreadable file must not take the whole candidate down with it — that is the
    all-or-nothing failure `harvest`'s surrogateescape decode exists to avoid one layer up.
    The `ArtifactSet` is built directly because `snapshot._digest` would raise on this file
    first, so the normal route cannot reach the branch: `build` takes an `ArtifactSet`, and
    a caller that assembled one another way is exactly who this protects.
    """
    repo, b, s = _seat(tmp_path)
    locked = write(s.path, "locked.txt", "secret\n")
    locked.chmod(0o000)
    try:
        cb = bundle.build(s.path, harvest.ArtifactSet(paths=("locked.txt",)), b)
    finally:
        locked.chmod(0o600)
    assert cb.omitted == ("locked.txt",)
    assert cb.sidecars == ()


# --- the patch channel -----------------------------------------------------------------

def test_a_rename_names_both_sides_so_neither_is_falsely_omitted(tmp_path):
    """`git apply --numstat` reports only the POSTIMAGE of a rename.

    Measured on git 2.53 for a `git mv old.txt new.txt` patch: forward `--numstat -z` gives
    `new.txt` and `-R --numstat -z` gives `old.txt`. Forward alone therefore leaves
    `old.txt` — which IS in `paths`, and IS carried by the patch — named in `omitted`, and
    §6.2's `HARVEST_INCOMPLETE` would fire on a candidate with no harvesting gap at all.
    `git diff` detects renames by default, so this is the ordinary case.

    The renamed file must exist AT B, not merely in the seat. Measured with the file
    created and committed inside the seat: `git diff <B>` sees only `new file mode ...
    new.txt`, because the preimage never existed at B — there is no rename to detect
    against the baseline, and the old name is legitimately a path the bundle carries
    nothing for.
    """
    repo, b, s = _seat(tmp_path)
    f0 = harvest.record(s.path)
    fsetup = harvest.record(s.path)
    git(s.path, "mv", "seed.txt", "renamed.txt")
    fwork = harvest.record(s.path)
    a = harvest.artifact_set(
        harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=fwork), s.path, b.commit)
    assert set(a.paths) == {"seed.txt", "renamed.txt"}, "precondition: both sides claimed"
    assert "rename from" in a.tracked_diff, \
        "precondition: git emitted a rename, not a delete plus an add"

    cb = bundle.build(s.path, a, b)
    assert cb.omitted == (), "the preimage is carried by the patch, so it is not a gap"
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    written = bundle.materialize(cb, dest)
    assert set(written) == {"seed.txt", "renamed.txt"}
    assert not (dest / "seed.txt").exists() and (dest / "renamed.txt").is_file()


def test_a_patch_that_does_not_apply_is_a_bundle_error(tmp_path):
    """A caller catching `BundleError` must not also have to catch `GitError`.

    Task 4 classifies outcomes; a raw `GitError` out of `materialize` is a failure it has
    no vocabulary for, and it would look like an engine crash rather than a bundle that
    cannot be laid down.
    """
    repo, b, s = _seat(tmp_path)
    p = _phases(s.path, lambda: write(s.path, "seed.txt", "agent edit\n"))
    a = harvest.artifact_set(p, s.path, b.commit)
    cb = bundle.build(s.path, a, b)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    # The verifier is at the right commit, so the check above passes; the patch's context
    # is what no longer matches.
    write(dest, "seed.txt", "something else entirely\n")
    with pytest.raises(bundle.BundleError, match="does not apply"):
        bundle.materialize(cb, dest)


# --- what materialize refuses ----------------------------------------------------------

def test_a_path_that_escapes_the_tree_is_refused_on_both_sides(tmp_path):
    """`root / "../x"` escapes and Path does not object — in BOTH directions.

    `snapshot` keys cannot take this shape, so both guards are for the OTHER caller: an
    `ArtifactSet` and a `CandidateBundle` are plain dataclasses a later stage may
    deserialize from a ledger. On the way IN the damage is a host file read into the
    bundle's payload; on the way OUT it is a write outside the verifier. Nothing may be
    written before the refusal, patch included.
    """
    repo, b, s = _seat(tmp_path)
    (tmp_path / "host-secret.txt").write_text("HOST-ONLY\n")
    with pytest.raises(bundle.BundleError, match="artifact path escapes"):
        bundle.build(s.path, harvest.ArtifactSet(paths=("../host-secret.txt",)), b)

    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    evil = bundle.CandidateBundle(
        version=1, baseline_ref=b.ref, baseline_commit=b.commit,
        sidecars=(bundle.SidecarEntry("../pwned.txt", "file", 0o644, b"x\n"),))
    with pytest.raises(bundle.BundleError, match="sidecar path escapes"):
        bundle.materialize(evil, dest)
    assert not (tmp_path / "pwned.txt").exists()


def test_a_sidecar_may_not_be_gits_own_directory(tmp_path):
    """MEASURED at 4545bb6: a sidecar named `.git/config` was written straight over the
    destination clone's config — taking the verifier's hooks pin and its identity, and
    installing `core.fsmonitor`, a program git EXECUTES on an ordinary `git status`. It ran.

    `_safe_rel` refused an absolute path and `..` and said nothing about this. The tracked
    channel never could carry it: `git apply --index` answers `invalid path` for a patch
    writing under `.git` at any depth, and for the case-folded and NTFS spellings too — so
    until this guard the two channels disagreed about the same path, which is the shape
    `build`'s escaping-link comment already refuses to ship.
    """
    repo, b, s = _seat(tmp_path)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    before = (dest / ".git" / "config").read_text()

    for rel in (".git/config", "vendor/x/.git/config", ".GiT/config", ".git./config",
                ".git /config", "git~1/config"):
        # The way IN: an ArtifactSet is a plain dataclass a later stage may deserialize.
        # Named rather than raised — see the worktree-gitlink test below for why the two
        # directions answer differently.
        carried = bundle.build(s.path, harvest.ArtifactSet(paths=(rel,)), b)
        assert (carried.omitted, carried.sidecars) == ((rel,), ())
        # ...and the way OUT, which is where the damage was measured.
        evil = bundle.CandidateBundle(
            version=1, baseline_ref=b.ref, baseline_commit=b.commit,
            sidecars=(bundle.SidecarEntry(rel, "file", 0o644,
                                          b"[core]\n\tfsmonitor = /tmp/rigged\n"),))
        with pytest.raises(bundle.BundleError, match="git's own directory"):
            bundle.materialize(evil, dest)
    assert (dest / ".git" / "config").read_text() == before, "the clone's config was rewritten"


def test_a_nested_git_FILE_is_omitted_rather_than_failing_the_whole_bundle(tmp_path):
    """`snapshot.take(skip_dirs=(".git",))` prunes DIRNAMES, so it never sees the `.git`
    that `git worktree add` and `git submodule add` leave behind — a FILE holding
    `gitdir: <path>`. Measured: a seat whose work is `git worktree add --detach wt`
    inventories as `('wt/.git', 'wt/seed.txt')`.

    It must not cross. Measured with `_is_dotgit` stubbed to False, that sidecar
    materialized into the verifier as `gitdir: <SEAT>/.git/worktrees/wt` — a live pointer
    back into the builder's own tree, in the one clone that exists so the builder cannot
    reach it.

    `omitted`, not a raise, on the escaping-link precedent two channels over: adding a
    worktree is an ordinary agent command, so failing the whole run on it would discard a
    candidate over a path the bundle merely cannot carry. `omitted` is what lets the
    outcome classifier answer HARVEST_INCOMPLETE if the gate then fails on the gap.
    """
    repo, b, s = _seat(tmp_path)
    p = _phases(s.path, lambda: git(s.path, "worktree", "add", "--detach", "wt"))
    a = harvest.artifact_set(p, s.path, b.commit)
    assert "wt/.git" in a.paths, f"the fixture carries no nested .git at all: {a.paths}"
    assert (s.path / "wt" / ".git").is_file(), \
        "a .git DIRECTORY is pruned by snapshot; this test is about the file skip_dirs misses"

    cb = bundle.build(s.path, a, b)
    assert "wt/.git" in cb.omitted
    assert "wt/.git" not in [e.path for e in cb.sidecars]
    assert "wt/seed.txt" in [e.path for e in cb.sidecars], \
        "the rest of the worktree went over the side with the gitlink"

    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    bundle.materialize(cb, dest)
    assert not (dest / "wt" / ".git").exists(), "the gitlink reached the verifier"
    assert (dest / "wt" / "seed.txt").read_text() == "seed\n"


def test_a_name_that_merely_starts_with_git_still_crosses(tmp_path):
    """The refusal above is a COMPONENT test, not a prefix one. `.gitignore` and `.github/`
    are ordinary candidate output, and a `foo.py~`-style trailing character has to survive
    the trailing-junk normalisation that catches `.git.` and `.git `."""
    repo, b, s = _seat(tmp_path)
    rels = (".gitignore", ".github/workflows/ci.yml", "docs/git/notes.md",
            "src/gitlab.py", "old.py~")
    p = _phases(s.path, lambda: [write(s.path, r, "content\n") for r in rels])
    a = harvest.artifact_set(p, s.path, b.commit)
    assert set(a.paths) == set(rels), f"the fixture carries nothing to refuse: {a.paths}"
    cb = bundle.build(s.path, a, b)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    bundle.materialize(cb, dest)
    assert (dest / ".github" / "workflows" / "ci.yml").read_text() == "content\n"
    assert (dest / ".gitignore").read_text() == "content\n"


def test_materialize_refuses_a_sidecar_kind_it_does_not_understand(tmp_path):
    """A kind with no honest materialization must fail closed, not be skipped: a skipped
    sidecar is a missing input the bundle claimed to carry, which is the one thing
    `omitted` exists to make impossible."""
    repo, b, s = _seat(tmp_path)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    cb = bundle.CandidateBundle(
        version=1, baseline_ref=b.ref, baseline_commit=b.commit,
        sidecars=(bundle.SidecarEntry("pipe", "special", 0o644, b""),))
    with pytest.raises(bundle.BundleError, match="unknown kind"):
        bundle.materialize(cb, dest)
    assert not (dest / "pipe").exists()


def test_materialize_refuses_a_version_it_was_not_written_for(tmp_path):
    repo, b, s = _seat(tmp_path)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    cb = bundle.CandidateBundle(version=bundle.VERSION + 1, baseline_ref=b.ref,
                                baseline_commit=b.commit)
    with pytest.raises(bundle.BundleError, match="version"):
        bundle.materialize(cb, dest)


def test_a_sidecar_replaces_whatever_already_sits_at_its_path(tmp_path):
    """Writing a link needs the path clear — `os.symlink` raises FileExistsError — and
    writing a FILE over an existing LINK without clearing it writes THROUGH the link,
    modifying whatever it names instead of the path the bundle claimed."""
    repo, b, s = _seat(tmp_path)
    dest = tmp_path / "verifier"
    fleet.clone_seat(repo, b, dest, name="verifier", identity=IDENT)
    (dest / "decoy.txt").write_text("decoy target\n")
    os.symlink("decoy.txt", dest / "seed.txt.bak")
    cb = bundle.CandidateBundle(
        version=1, baseline_ref=b.ref, baseline_commit=b.commit,
        sidecars=(bundle.SidecarEntry("seed.txt.bak", "file", 0o644, b"replaced\n"),
                  bundle.SidecarEntry("seed.txt", "symlink", 0, b"decoy.txt")))
    bundle.materialize(cb, dest)
    assert (dest / "decoy.txt").read_text() == "decoy target\n", "written through the link"
    assert not (dest / "seed.txt.bak").is_symlink()
    assert (dest / "seed.txt.bak").read_text() == "replaced\n"
    assert (dest / "seed.txt").is_symlink()


def test_build_leaves_the_gate_delta_unknown_rather_than_empty(tmp_path):
    """`Baseline.sidecars` is the precedent: `()` would say "the candidate changed nothing
    that defines the gate" when the truth is "nobody looked".

    ABOUT `build` ALONE, and no longer about the chain it sits in. `verify.gate_surface`
    measures ONE tree; the delta needs it run on two, and `build` is handed no tree the
    builder did not write — a seat path, an `ArtifactSet` and a `Baseline`, never a
    checkout. `verify.build_verifier` is where both trees exist and it writes the
    measurement back, so a candidate that reaches a gate carries a delta and a candidate
    read off `build` does not. A consumer classifying `GATE_CHANGED` must read None as
    UNKNOWN — an empty tuple here would let a candidate that rewrote the Makefile pass as
    an independent gate.
    `generator_contract_id` defaults the other way because "" admits NOTHING as permitted
    verify-origin output, which is fail-closed.
    """
    repo, b, s = _seat(tmp_path)
    p = _phases(s.path, lambda: write(s.path, "Makefile", "verify:\n\ttrue\n"))
    cb = bundle.build(s.path, harvest.artifact_set(p, s.path, b.commit), b)
    assert cb.gate_delta is None, "an empty tuple would be a claim nobody made"
    assert cb.gate_surface is None, \
        "and neither is there a surface, or the delta would look measured over nothing"
    assert cb.generator_contract_id == ""
    assert cb.version == bundle.VERSION
    assert (cb.baseline_ref, cb.baseline_commit) == (b.ref, b.commit)


def test_build_stamps_the_runs_contract_id_into_the_bundle(tmp_path):
    """The manifest's record of which contract the run declared, written by the one call
    that knows — nothing downstream can recover it from the bundle's other fields."""
    _repo, b, s = _seat(tmp_path)
    p = _phases(s.path, lambda: write(s.path, "src.py", "work\n"))
    a = harvest.artifact_set(p, s.path, b.commit)
    c = finspect.GeneratorContract(id="render-v1", relations=(("shared/*", "gen/*"),))
    assert bundle.build(s.path, a, b, contract=c).generator_contract_id == "render-v1"


def test_build_without_a_contract_stamps_the_fail_closed_sentinel(tmp_path):
    """No contract is not the same claim as an unrecorded one: "" admits nothing.

    Three spellings of "this run declared none", pinned as ONE value. The empty contract is
    the third because it is what every caller with no contract of its own passes — it is
    `detect_generators`'s answer for this repository — and the claim that such a caller
    changes no meaning holds only while it stamps the same sentinel as an omitted argument.
    """
    _repo, b, s = _seat(tmp_path)
    p = _phases(s.path, lambda: write(s.path, "src.py", "work\n"))
    a = harvest.artifact_set(p, s.path, b.commit)
    assert bundle.build(s.path, a, b).generator_contract_id == ""
    assert bundle.build(s.path, a, b, contract=None).generator_contract_id == ""
    assert bundle.build(
        s.path, a, b, contract=finspect.GeneratorContract()).generator_contract_id == ""


def _bare(**kw):
    return bundle.CandidateBundle(version=bundle.VERSION, baseline_ref="r",
                                  baseline_commit="c", **kw)


def _measured(cb, *, surface=("Makefile",), delta=("Makefile",)):
    return bundle.with_gate_measurement(cb, surface=surface, delta=delta)


def test_with_gate_measurement_returns_a_new_bundle_and_leaves_the_original():
    cb = _bare()
    out = _measured(cb)
    assert (out.gate_delta, out.gate_surface) == (("Makefile",), ("Makefile",))
    assert cb.gate_delta is None and cb.gate_surface is None, \
        "the input is frozen and must be untouched"
    # Everything else rides across: a `replace` that dropped a field would hand the gate a
    # bundle carrying no patch, which materializes clean and verifies nothing.
    assert out.baseline_commit == "c" and out.version == bundle.VERSION


def test_with_gate_measurement_carries_the_whole_candidate_across(tmp_path):
    """The measurement is written onto a REAL bundle, so every channel has to survive it."""
    _repo, b, s = _seat(tmp_path)

    def work():
        write(s.path, "seed.txt", "agent edit\n")   # the tracked-patch channel
        write(s.path, "src.py", "work\n")           # the sidecar channel

    cb = bundle.build(s.path, harvest.artifact_set(_phases(s.path, work), s.path, b.commit), b)
    assert cb.tracked_patch and cb.sidecars, "the fixture exercises only one channel"
    out = _measured(cb)
    assert out.tracked_patch == cb.tracked_patch
    assert (out.sidecars, out.omitted, out.generator_contract_id) == \
        (cb.sidecars, cb.omitted, cb.generator_contract_id)


def test_with_gate_measurement_refuses_to_overwrite_a_measurement():
    """Two measurements of one tree pair disagree only if one of them is wrong."""
    with pytest.raises(bundle.BundleError, match="already records"):
        _measured(_bare(gate_delta=()))
    # The empty MEASUREMENT is as unoverwritable as a non-empty one, and that direction is
    # the dangerous one: a second call that quietly won would let a caller turn a clean
    # gate into a changed one, or a changed one into a clean one, by calling twice.
    with pytest.raises(bundle.BundleError):
        _measured(_bare(gate_delta=("Makefile",)), delta=())
    # EITHER field already recorded is a refusal, not only the delta. The two are written
    # by one call, so a bundle holding one of them is already a record two calls disagree
    # about — and a second call that overwrote only the surface would leave a delta
    # describing paths the surface beside it never named.
    with pytest.raises(bundle.BundleError, match="gate_surface"):
        _measured(_bare(gate_surface=()))


def test_with_gate_measurement_accepts_the_empty_measurement():
    """() is a RESULT on both fields — the candidate changed nothing that defines the gate,
    and nothing in this tree defines it by any rule — so both must be storable, and both
    must come back distinguishable from the `None` they replace."""
    out = bundle.with_gate_measurement(_bare(), surface=(), delta=())
    assert (out.gate_delta, out.gate_surface) == ((), ())
    assert out.gate_delta is not None and out.gate_surface is not None


def test_with_gate_measurement_refuses_one_path_written_as_a_string():
    """`tuple("Makefile")` is eight one-character paths, and nothing downstream would say so."""
    cb = bundle.CandidateBundle(version=bundle.VERSION, baseline_ref="r", baseline_commit="c")
    with pytest.raises(bundle.BundleError, match="gate delta is a sequence"):
        bundle.with_gate_measurement(cb, surface=("Makefile",), delta="Makefile")
    # BOTH arguments, because both are path sequences and only one of them was guarded when
    # this was a one-field call.
    with pytest.raises(bundle.BundleError, match="gate surface is a sequence"):
        bundle.with_gate_measurement(cb, surface="Makefile", delta=("Makefile",))
    assert _measured(cb).gate_delta == ("Makefile",)


def test_the_descent_refuses_a_component_that_is_a_symlink(tmp_path):
    """THE MECHANISM THREE LEXICAL WAVES DID NOT HAVE. `_assert_contained` and `_escapes`
    validate a STRING and then hand the caller a NAME; the filesystem that name is finally
    resolved against is not the one the check reasoned about. `contained` resolves one
    component at a time against the PREVIOUS component's open descriptor with `O_NOFOLLOW`,
    so a link anywhere on the way is a refusal and not a redirect."""
    (tmp_path / "tree" / "real").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "f.txt").write_text("host\n")
    os.symlink(tmp_path / "outside", tmp_path / "tree" / "link")
    with pytest.raises(bundle.BundleError, match="does not stay inside the tree"):
        bundle.contained(tmp_path / "tree", "link/f.txt", "a path")
    # Non-vacuity: the same shape through a REAL directory is accepted, so the refusal above
    # is about the link and not about the descent refusing everything.
    (tmp_path / "tree" / "real" / "f.txt").write_text("ours\n")
    with bundle.contained(tmp_path / "tree", "real/f.txt", "a path") as at:
        assert at.leaf == "f.txt"
        fd = bundle.open_leaf(at, os.O_RDONLY, "a path")
        try:
            assert bundle.read_fd(fd) == b"ours\n"
        finally:
            os.close(fd)


def test_creating_the_intermediate_directories_does_not_write_through_an_existing_link(
        tmp_path):
    """`mkdir(parents=True, exist_ok=True)` is the spelling that wrote through: a component
    that already exists AS A LINK satisfies `exist_ok` and the write lands wherever it points.
    Here the directory is created and then OPENED `O_NOFOLLOW`, so the existing link fails at
    the open rather than being accepted as 'already there'."""
    (tmp_path / "tree").mkdir()
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", tmp_path / "tree" / "sub")
    with pytest.raises(bundle.BundleError, match="does not stay inside the tree"):
        bundle.contained(tmp_path / "tree", "sub/f.txt", "a path", create_dirs=True)
    assert not (tmp_path / "outside" / "f.txt").exists()


def test_the_descent_still_refuses_a_literal_dotdot_which_needs_no_link_at_all(tmp_path):
    """THE HALF THE DESCENT CANNOT SEE, which is why the string rule stays. `..` is a LITERAL
    component: the kernel resolves it with no symlink involved, so `os.open("..", dir_fd=fd)`
    walks out of the tree exactly as asked and every `O_NOFOLLOW` succeeds. Neither half
    subsumes the other."""
    (tmp_path / "tree").mkdir()
    for bad in ("../escaped.txt", "a/../../escaped.txt", "/etc/passwd"):
        with pytest.raises(bundle.BundleError, match="escapes the tree"):
            bundle.contained(tmp_path / "tree", bad, "a path")


def test_open_leaf_refuses_a_link_leaf_and_does_not_block_on_a_fifo(tmp_path):
    """The descent contains the COMPONENTS; `open_leaf` is what contains the last one.
    `O_NOFOLLOW` refuses a link there, and `O_NONBLOCK` is what keeps this from being the
    read `snapshot._special_entry` refuses to perform — a read-open on a FIFO blocks until a
    writer appears, and there is no timeout anywhere in these call paths."""
    (tmp_path / "tree").mkdir()
    (tmp_path / "host.txt").write_text("host\n")
    os.symlink(tmp_path / "host.txt", tmp_path / "tree" / "link.txt")
    with bundle.contained(tmp_path / "tree", "link.txt", "a path") as at:
        with pytest.raises(OSError):
            os.close(bundle.open_leaf(at, os.O_RDONLY, "a path"))
    os.mkfifo(tmp_path / "tree" / "pipe")
    with bundle.contained(tmp_path / "tree", "pipe", "a path") as at:
        fd = bundle.open_leaf(at, os.O_RDONLY, "a path")
        try:
            assert not stat.S_ISREG(os.fstat(fd).st_mode), \
                "the open must return so the caller can refuse it, rather than hang"
        finally:
            os.close(fd)


def test_a_chain_of_sidecar_links_cannot_write_outside_the_verifier(tmp_path):
    """THE SAME ESCAPE, FOUND BY SWEEPING ONE MODULE OVER FROM `taskbundle.materialize`.

    Every guard in this loop was per-entry and lexical — `_safe_rel` on the path, `_escapes`
    on a link's target — and sidecars are written IN ORDER, so an earlier one installs the
    filesystem a later one's NAME is resolved against. `.` and `..` escape nothing relative to
    their own entry, which is all `_escapes` is asked. Measured before the descent: the four
    sidecars below passed every check, `materialize` returned all four as written, and the file
    landed TWO DIRECTORIES ABOVE THE VERIFIER, on the host — an executable at
    `<parent>/hooks/pre-commit` in the first reproduction, which `git commit` runs.

    A `CandidateBundle`'s sidecars are INPUT under the deserialize-from-a-ledger model this
    module's own `_safe_rel` docstring is written for, so this is reachable by the same route.

    WHICH DOOR IT IS REFUSED AT MOVED. `_assert_no_collision` now catches this chain before
    the patch is applied, and by construction rather than by luck: each link has to be both a
    sidecar ENTRY and an ancestor of the next one, and one name cannot be both. So the route
    where an earlier sidecar redirects a later one is closed at the manifest, and
    `test_a_sidecar_is_refused_through_a_directory_component_that_is_a_link` is what still
    pins the descent itself.
    """
    repo = make_repo(tmp_path / "work")
    run = tmp_path / "work" / "run"
    run.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    seat = fleet.clone_seat(repo, b, tmp_path / "work" / "verifier", name="claude",
                            identity=("F", "f@example.invalid"))
    S = bundle.SidecarEntry
    cand = bundle.CandidateBundle(
        version=bundle.VERSION, baseline_ref=b.ref, baseline_commit=b.commit,
        tracked_patch=b"", sidecars=(S("a", "symlink", 0, b"."),
                                     S("a/b", "symlink", 0, b".."),
                                     S("a/b/c", "symlink", 0, b".."),
                                     S("a/b/c/outside/OWNED.txt", "file", 0o644, b"host\n")))
    with pytest.raises(bundle.BundleError,
                       match="cannot be both an entry and the path to one"):
        bundle.materialize(cand, seat.path)
    assert not (outside / "OWNED.txt").exists(), \
        "the write must never reach a host path, not even briefly"
    assert not (Path(seat.path) / "a").exists(follow_symlinks=False), \
        "refused before the first sidecar, so not even the first link was laid down"


def test_a_sidecar_is_refused_through_a_directory_component_that_is_a_link(tmp_path):
    """What still pins the DESCENT in `materialize`'s sidecar loop, now that the chain above
    is refused at the manifest.

    The link is already in the verifier when `materialize` is called, which is the difference
    that matters: it is not a sidecar, so no rule about the entry SET can see it, and every
    component of `a/OWNED.txt` is lexically clean — `_safe_rel` passes, `_escapes` has no link
    target to judge. Only opening `a` with `O_NOFOLLOW` against the root's descriptor answers.
    A verifier clone is a tree the candidate's own patch has just been applied to, so a link
    sitting at a directory component is an ordinary state rather than a contrived one.
    """
    repo = make_repo(tmp_path / "work")
    run = tmp_path / "work" / "run"
    run.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    seat = fleet.clone_seat(repo, b, tmp_path / "work" / "verifier", name="claude",
                            identity=("F", "f@example.invalid"))
    os.symlink(outside, Path(seat.path) / "a")
    cand = bundle.CandidateBundle(
        version=bundle.VERSION, baseline_ref=b.ref, baseline_commit=b.commit,
        tracked_patch=b"",
        sidecars=(bundle.SidecarEntry("a/OWNED.txt", "file", 0o644, b"host\n"),))
    with pytest.raises(bundle.BundleError,
                       match=r"passes through 'a', which is not a directory"):
        bundle.materialize(cand, seat.path)
    assert not (outside / "OWNED.txt").exists(), \
        "the write must never reach a host path, not even briefly"


def test_a_sidecar_still_replaces_what_the_patch_left_at_its_path(tmp_path):
    """Non-vacuity for the descent above: the loop still overwrites, still carries a mode, and
    still replaces a DANGLING link — `lexists` was the old spelling of that and `os.unlink` at
    a descriptor is the new one."""
    repo = make_repo(tmp_path / "work")
    run = tmp_path / "work" / "run"
    run.mkdir(parents=True)
    b = baseline.materialize(repo, run, finspect.repo_facts(repo), [], "r1")
    seat = fleet.clone_seat(repo, b, tmp_path / "work" / "verifier", name="claude",
                            identity=("F", "f@example.invalid"))
    (Path(seat.path) / "sub").mkdir()
    os.symlink("nowhere", Path(seat.path) / "sub" / "tool.sh")
    S = bundle.SidecarEntry
    cand = bundle.CandidateBundle(
        version=bundle.VERSION, baseline_ref=b.ref, baseline_commit=b.commit,
        tracked_patch=b"", sidecars=(S("sub/tool.sh", "file", 0o755, b"#!/bin/sh\n"),
                                     S("sub/link", "symlink", 0, b"tool.sh")))
    assert bundle.materialize(cand, seat.path) == ("sub/link", "sub/tool.sh")
    tool = Path(seat.path) / "sub" / "tool.sh"
    assert not tool.is_symlink() and tool.read_bytes() == b"#!/bin/sh\n"
    assert tool.stat().st_mode & 0o777 == 0o755
    assert os.readlink(Path(seat.path) / "sub" / "link") == "tool.sh"


# ---- the Fwork byte-binding -----------------------------------------------------------
def _bound_seat(tmp_path):
    """A seat harvested for real, so the binding comes from `artifact_set` and not a fixture.

    A REAL CLONE, because `artifact_set` runs `git check-attr` and `git diff` against it —
    a bare directory makes every assertion below fail for a reason none of them is about.
    """
    repo, b, s = _seat(tmp_path)
    seat = s.path
    (seat / "a.txt").write_text("the bytes the builder wrote\n")
    f0, _ = snapshot.take(seat)
    fsetup = dict(f0)
    (seat / "a.txt").write_text("the bytes the builder wrote, revised\n")
    fwork, _ = snapshot.take(seat)
    phases = harvest.Phases(f0=f0, fsetup=fsetup, fwork=fwork, fverify=dict(fwork))
    return seat, phases, b


def test_the_artifact_set_binds_the_bytes_its_path_set_was_computed_from(tmp_path):
    """THE EXTERNAL QUESTION: can anything downstream tell whether the tree still holds what
    the harvest measured? Before this field the answer was no for every consumer — the path
    set was a list of NAMES and `bundle.build`, `git diff` and a resume all re-read the live
    seat with nothing to compare against."""
    seat, phases, base = _bound_seat(tmp_path)
    arts = harvest.artifact_set(phases, seat, base.commit)
    assert arts.paths == ("a.txt",)
    assert arts.fwork is not None, "the path set was returned unbound"
    assert [e.path for e in arts.fwork] == ["a.txt"]
    assert arts.fwork[0].digest == phases.fwork["a.txt"].digest


def test_a_seat_that_moved_between_harvest_and_build_is_refused_not_bundled(tmp_path):
    """REPRODUCED BEFORE THE FIX: rewrite a harvested file after `artifact_set` and the
    bundle carried the NEW bytes under the OLD path set, silently and at exit 0. That is the
    candidate content nobody authored — the whole point of harvesting a path set is that the
    bytes behind it are the ones that were measured."""
    seat, phases, base = _bound_seat(tmp_path)
    arts = harvest.artifact_set(phases, seat, base.commit)
    (seat / "a.txt").write_text("SOMETHING ELSE ENTIRELY\n")
    with pytest.raises(bundle.BundleError, match="no longer holds the bytes"):
        bundle.build(seat, arts, base)


def test_a_mode_change_alone_is_drift_even_when_the_bytes_are_identical(tmp_path):
    """`take`'s own comment records that dropping the mode bits left `chmod u+s` invisible to
    every bracket written on this predicate. A binding that compared only digests would
    reintroduce exactly that hole one module over."""
    seat, phases, base = _bound_seat(tmp_path)
    arts = harvest.artifact_set(phases, seat, base.commit)
    (seat / "a.txt").chmod(0o755)
    with pytest.raises(bundle.BundleError, match="no longer holds the bytes"):
        bundle.build(seat, arts, base)


def test_an_untouched_seat_still_builds(tmp_path):
    """THE DISCRIMINATION CHECK. A guard that also fired on the ordinary path would be one
    nobody could pass, and every real run takes this branch."""
    seat, phases, base = _bound_seat(tmp_path)
    arts = harvest.artifact_set(phases, seat, base.commit)
    b = bundle.build(seat, arts, base)
    assert b.fwork is not None and [e.path for e in b.fwork] == ["a.txt"]
    assert bundle.unbound(b) is False


def test_an_unbound_bundle_reports_no_comparison_rather_than_no_discrepancies(tmp_path):
    """THE FAIL-OPEN THIS MUST NOT HAVE. `fwork=()` is a bound bundle with no paths;
    `fwork=None` is one nobody bound. Returning `()` for the second would let a verifier
    report "materialized clean" for a comparison it never made."""
    b = bundle.CandidateBundle(version=bundle.VERSION, baseline_ref="r", baseline_commit="c")
    assert bundle.unbound(b) is True
    out = bundle.verify_materialized(b, tmp_path)
    assert out and "no comparison was made" in out[0]
    assert out != ()


def test_a_materialized_tree_is_checked_against_the_snapshot_not_against_the_bundle(tmp_path):
    """THE CLAIM THIS UPGRADES: a verifier could already prove it materialized the bundle it
    was HANDED. The bundle is the thing that travelled, so that is the claim a swap survives.
    Checking the materialized tree against the Fwork binding is the stronger question."""
    seat, phases, base = _bound_seat(tmp_path)
    arts = harvest.artifact_set(phases, seat, base.commit)
    b = bundle.build(seat, arts, base)
    assert bundle.verify_materialized(b, seat) == ()
    (seat / "a.txt").write_text("swapped after the fact\n")
    bad = bundle.verify_materialized(b, seat)
    assert len(bad) == 1 and "a.txt" in bad[0]


def test_a_deleted_bound_path_is_named_as_absent_not_silently_skipped(tmp_path):
    """`entry_at` returns None for an absent path, and a `drift` that skipped those would
    report a tree that LOST every bound file as clean — absence reading as agreement."""
    seat, phases, base = _bound_seat(tmp_path)
    arts = harvest.artifact_set(phases, seat, base.commit)
    (seat / "a.txt").unlink()
    out = snapshot.drift(seat, arts.fwork)
    assert len(out) == 1 and "absent now" in out[0]


# ---- serialization: what `_refuse_a_second_pass` called impossible ----------------------
def test_a_bundle_round_trips_through_bytes_unchanged(tmp_path):
    """THE EXTERNAL QUESTION: is a `CandidateBundle` a value that can leave memory and come
    back the same? `runner._refuse_a_second_pass` refused every resume on the claim that
    nothing serializes one — a true statement about what existed and a false one about what
    is possible, since every field is plain data."""
    seat, phases, base = _bound_seat(tmp_path)
    arts = harvest.artifact_set(phases, seat, base.commit)
    b = bundle.build(seat, arts, base)
    assert bundle.loads(bundle.dumps(b)) == b


def test_binary_payloads_and_non_utf8_link_targets_survive_the_round_trip(tmp_path):
    """THE CASE A PER-VALUE CODEC WOULD MANGLE. `_symlink_entry`'s docstring records a
    non-UTF-8 link target taking `baseline.materialize` down with UnicodeEncodeError; a patch
    from `git diff --binary` is not text at all. Base64 for every byte field, or the round
    trip works on the easy inputs and corrupts exactly these."""
    b = bundle.CandidateBundle(
        version=bundle.VERSION, baseline_ref="r", baseline_commit="c",
        tracked_patch=b"\x00\x01\x02\xff\xfe binary \x80",
        sidecars=(bundle.SidecarEntry("caf\udce9.txt", "symlink", 0,
                                      "caf\udce9-target".encode("utf-8", "surrogateescape")),
                  bundle.SidecarEntry("bin", "file", 0o644, bytes(range(256)))))
    back = bundle.loads(bundle.dumps(b))
    assert back == b
    assert back.sidecars[1].payload == bytes(range(256))


def test_an_unbound_bundle_does_not_come_back_bound_to_nothing(tmp_path):
    """`None` AND `()` MUST SURVIVE APART. A serializer that rendered both as `[]` would turn
    "nobody bound this" into "bound, and empty" on every reload — the fail-open reading, and
    it would arrive wearing a successful round trip."""
    unbound_b = bundle.CandidateBundle(version=bundle.VERSION, baseline_ref="r",
                                       baseline_commit="c")
    bound_empty = bundle.CandidateBundle(version=bundle.VERSION, baseline_ref="r",
                                         baseline_commit="c", fwork=())
    assert bundle.loads(bundle.dumps(unbound_b)).fwork is None
    assert bundle.loads(bundle.dumps(bound_empty)).fwork == ()
    assert bundle.unbound(bundle.loads(bundle.dumps(unbound_b))) is True
    assert bundle.unbound(bundle.loads(bundle.dumps(bound_empty))) is False


def test_a_reloaded_bundle_still_proves_a_tree_against_the_harvest(tmp_path):
    """THE POINT OF PERSISTING IT AT ALL. A bundle that round-trips but loses its binding is
    bytes with no claim attached — a resume reading one could materialize it and say only
    "this is what I was given", never "this is what the harvest measured"."""
    seat, phases, base = _bound_seat(tmp_path)
    arts = harvest.artifact_set(phases, seat, base.commit)
    b = bundle.loads(bundle.dumps(bundle.build(seat, arts, base)))
    assert bundle.verify_materialized(b, seat) == ()
    (seat / "a.txt").write_text("changed after the bundle was written\n")
    assert bundle.verify_materialized(b, seat) != ()


def test_a_bundle_from_an_unknown_version_is_refused_not_partially_read():
    """FAILS CLOSED. A future build may add a field; one dropped silently here is a claim the
    candidate made that the verifier never sees. `materialize` refuses an unknown version for
    this reason — this is that rule at the other end of the wire."""
    import json as _json
    raw = _json.dumps({"version": bundle.VERSION + 99}).encode()
    with pytest.raises(bundle.BundleError, match="records version"):
        bundle.loads(raw)


def test_garbage_is_refused_with_a_sentence_rather_than_a_stray_exception():
    """A caller catches `BundleError`; a bare ValueError out of json is a class nothing in
    this package's error surface knows to catch — the shape `snapshot.take`'s
    FileNotFoundError comment records escaping a bracket measurement."""
    with pytest.raises(bundle.BundleError):
        bundle.loads(b"not json at all")
    with pytest.raises(bundle.BundleError):
        bundle.loads(b'["a", "list", "not", "an", "object"]')
    with pytest.raises(bundle.BundleError, match="missing or malformed"):
        bundle.loads(b'{"version": 1}')
