"""What crosses from a seat into a verifier — and what provably does not."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

import pytest  # noqa: E402
from forge import baseline, bundle, fleet, harvest, inspect as finspect  # noqa: E402
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

    `verify.gate_surface` measures ONE tree; the delta needs it run on two, and `build` is
    handed no tree the builder did not write. So it makes no claim, and a consumer
    classifying `GATE_CHANGED` must read None as UNKNOWN — an empty tuple here would let a
    candidate that rewrote the Makefile pass as an independent gate.
    `generator_contract_id` defaults the other way because "" admits NOTHING as permitted
    verify-origin output, which is fail-closed.
    """
    repo, b, s = _seat(tmp_path)
    p = _phases(s.path, lambda: write(s.path, "Makefile", "verify:\n\ttrue\n"))
    cb = bundle.build(s.path, harvest.artifact_set(p, s.path, b.commit), b)
    assert cb.gate_delta is None, "an empty tuple would be a claim nobody made"
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


def test_with_gate_delta_returns_a_new_bundle_and_leaves_the_original():
    cb = _bare()
    out = bundle.with_gate_delta(cb, ("Makefile",))
    assert out.gate_delta == ("Makefile",)
    assert cb.gate_delta is None, "the input is frozen and must be untouched"
    # Everything else rides across: a `replace` that dropped a field would hand the gate a
    # bundle carrying no patch, which materializes clean and verifies nothing.
    assert out.baseline_commit == "c" and out.version == bundle.VERSION


def test_with_gate_delta_carries_the_whole_candidate_across(tmp_path):
    """The measurement is written onto a REAL bundle, so every channel has to survive it."""
    _repo, b, s = _seat(tmp_path)

    def work():
        write(s.path, "seed.txt", "agent edit\n")   # the tracked-patch channel
        write(s.path, "src.py", "work\n")           # the sidecar channel

    cb = bundle.build(s.path, harvest.artifact_set(_phases(s.path, work), s.path, b.commit), b)
    assert cb.tracked_patch and cb.sidecars, "the fixture exercises only one channel"
    out = bundle.with_gate_delta(cb, ("Makefile",))
    assert out.tracked_patch == cb.tracked_patch
    assert (out.sidecars, out.omitted, out.generator_contract_id) == \
        (cb.sidecars, cb.omitted, cb.generator_contract_id)


def test_with_gate_delta_refuses_to_overwrite_a_measurement():
    """Two measurements of one tree pair disagree only if one of them is wrong."""
    with pytest.raises(bundle.BundleError, match="already records"):
        bundle.with_gate_delta(_bare(gate_delta=()), ("Makefile",))
    # The empty MEASUREMENT is as unoverwritable as a non-empty one, and that direction is
    # the dangerous one: a second call that quietly won would let a caller turn a clean
    # gate into a changed one, or a changed one into a clean one, by calling twice.
    with pytest.raises(bundle.BundleError):
        bundle.with_gate_delta(_bare(gate_delta=("Makefile",)), ())


def test_with_gate_delta_accepts_the_empty_measurement():
    """() is a RESULT here — the candidate changed nothing that defines the gate — and it
    is the only value that can reach PASS, so it must be storable."""
    assert bundle.with_gate_delta(_bare(), ()).gate_delta == ()
    assert bundle.with_gate_delta(_bare(), ()).gate_delta is not None
