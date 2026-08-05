"""Filesystem inventory keyed on CONTENT, never on lstat (spec §7.3)."""
import os
import shutil
import signal
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import snapshot, storage  # noqa: E402
from forge_fixtures import make_repo  # noqa: E402


def _within(seconds, fn, *args, **kwargs):
    """Run `fn` under a SIGALRM deadline so a blocking bug FAILS the test instead of
    hanging the suite. A signal, not a worker thread: a thread stuck in a blocking open()
    cannot be cancelled, and a non-daemon thread would then hang the interpreter at exit
    rather than the test. The handler raises, so PEP 475's EINTR retry does not resume the
    blocked call."""
    def _fire(signum, frame):
        raise TimeoutError(f"call blocked for more than {seconds}s")
    prev = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn(*args, **kwargs)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev)


def test_rmtree_and_copy_produces_no_phantom_changes(tmp_path):
    """The bug this predicate exists to prevent: new inodes, identical bytes."""
    src = tmp_path / "t"; src.mkdir()
    (src / "a.txt").write_text("a\n")
    (src / "sub").mkdir(); (src / "sub" / "b.txt").write_text("b\n")
    before, _ = snapshot.take(src)
    copy = tmp_path / "copy"
    shutil.copytree(src, copy)
    shutil.rmtree(src)
    shutil.copytree(copy, src)
    after, _ = snapshot.take(src)
    assert snapshot.diff(before, after) == {}


def test_a_moved_mtime_over_identical_bytes_is_not_a_change(tmp_path):
    """The round trip above cannot catch an mtime-keyed predicate — copytree uses copy2,
    which preserves mtime exactly. A setup step that re-emits a generated file byte for
    byte does move it, and must still read as unchanged. utime rather than a sleep so the
    precondition holds on a filesystem with coarse timestamps."""
    d = tmp_path / "t"; d.mkdir()
    p = d / "gen.txt"; p.write_text("same\n")
    before, _ = snapshot.take(d)
    stamp = p.stat().st_mtime_ns
    p.write_text("same\n")
    os.utime(p, ns=(stamp + 10**9, stamp + 10**9))
    assert p.stat().st_mtime_ns != stamp, "precondition: the rewrite must move mtime"
    after, _ = snapshot.take(d)
    assert snapshot.diff(before, after) == {}


def test_detects_added_removed_modified(tmp_path):
    d = tmp_path / "t"; d.mkdir()
    (d / "keep.txt").write_text("k\n")
    (d / "gone.txt").write_text("g\n")
    (d / "edit.txt").write_text("v1\n")
    before, _ = snapshot.take(d)
    (d / "gone.txt").unlink()
    (d / "edit.txt").write_text("v2\n")
    (d / "new.txt").write_text("n\n")
    after, _ = snapshot.take(d)
    assert snapshot.diff(before, after) == {
        "gone.txt": "removed", "edit.txt": "modified", "new.txt": "added"}


def test_mode_change_alone_is_a_modification(tmp_path):
    d = tmp_path / "t"; d.mkdir()
    p = d / "s.sh"; p.write_text("#!/bin/sh\n"); p.chmod(0o644)
    before, _ = snapshot.take(d)
    p.chmod(0o755)
    after, _ = snapshot.take(d)
    assert snapshot.diff(before, after) == {"s.sh": "modified"}


def test_symlinks_are_recorded_but_never_followed(tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "secret.txt").write_text("secret\n")
    d = tmp_path / "t"; d.mkdir()
    (d / "link").symlink_to(outside)
    entries, _ = snapshot.take(d)
    assert entries["link"].kind == "symlink"
    assert not any(e.startswith("link/") for e in entries)


def test_a_link_target_that_is_not_utf8_is_inventoried_rather_than_raising(tmp_path):
    """`os.readlink` returns surrogates for a name that is not valid UTF-8, and a plain
    `.encode()` raises UnicodeEncodeError on them.

    This function runs on EVERY phase of `harvest.record`, so the raise would not cost one
    entry — it would take the whole inventory of the seat down, in a class that is neither
    `SnapshotError` nor `HarvestError`. `baseline._sha256_link` and `fleet._sha256_link`
    carry the identical encode so the three digests stay comparable, which is what Plan D's
    D-1 spends: the manifest and F0 must agree on a link.
    """
    import hashlib
    d = tmp_path / "t"; d.mkdir()
    os.symlink(b"caf\xe9.txt", d / "link")
    entries, breaches = snapshot.take(d)
    assert breaches == []
    assert entries["link"].kind == "symlink"
    assert entries["link"].digest == hashlib.sha256(b"caf\xe9.txt").hexdigest(), \
        "the digest is over the raw bytes of the name, whatever encoding they are in"


def test_git_is_skipped_by_default(tmp_path):
    repo = make_repo(tmp_path)
    entries, _ = snapshot.take(repo)
    assert not any(p.startswith(".git/") for p in entries)
    assert "seed.txt" in entries


def test_a_symlink_wearing_a_skipped_name_is_recorded_rather_than_vanishing(tmp_path):
    """A SKIPPED NAME SAYS "DO NOT DESCEND", NEVER "THIS PATH DOES NOT EXIST".

    `skip_dirs` was applied to `dirnames` BEFORE the branch that records a directory symlink,
    so a link merely NAMED like a skipped directory never reached it and left no entry at all.
    Measured: creating or removing `tree/.git -> elsewhere` moved the tree and left
    byte-for-byte the inventory an untouched tree leaves — which `review`'s bracket reads as a
    checkout that stood still, in the guard that decides whether a round's findings are
    admissible. Nothing is descended into either way; only the link itself is recorded."""
    outside = tmp_path / "elsewhere"; outside.mkdir()
    (outside / "planted.txt").write_text("a reviewer wrote this\n")
    d = tmp_path / "t"; d.mkdir()
    (d / "kept.txt").write_text("a\n")
    before, _ = snapshot.take(d, skip_dirs=(".git", "__pycache__"))
    for name in (".git", "__pycache__"):
        (d / name).symlink_to(outside)
    after, breaches = snapshot.take(d, skip_dirs=(".git", "__pycache__"))
    assert breaches == []
    assert sorted(after) == [".git", "__pycache__", "kept.txt"]
    assert all(after[n].kind == "symlink" for n in (".git", "__pycache__"))
    assert not any(p.startswith((".git/", "__pycache__/")) for p in after), \
        "recorded, and still never descended into"
    assert snapshot.diff(before, after) == {".git": "added", "__pycache__": "added"}


def test_a_real_directory_wearing_a_skipped_name_is_still_skipped(tmp_path):
    """The other half: the pruning that `skip_dirs` is for is unchanged for the ordinary case
    a real cache directory presents, at the root and at depth."""
    d = tmp_path / "t"; d.mkdir()
    (d / "kept.txt").write_text("a\n")
    for rel in ("__pycache__", "pkg/__pycache__"):
        (d / rel).mkdir(parents=True)
        (d / rel / "mod.pyc").write_bytes(b"\x00compiled\n")
    entries, _ = snapshot.take(d, skip_dirs=("__pycache__",))
    assert sorted(entries) == ["kept.txt"]


def test_quota_breach_fails_closed_with_no_partial_inventory(tmp_path):
    d = tmp_path / "t"; d.mkdir()
    for i in range(5):
        (d / f"f{i}.txt").write_text("x\n")
    entries, breaches = snapshot.take(d, quota=storage.Quota(
        max_files=2, max_file_bytes=1000, max_total_bytes=10_000))
    assert entries == {}, "a breach must discard the inventory, not report it partial"
    assert breaches
    assert "files" in breaches[0]


def test_symlinked_dirs_also_count_against_the_file_quota(tmp_path):
    """They arrive in os.walk's dirnames, never filenames — counting only filenames lets
    a tree of them return an over-quota inventory with no breach reported."""
    target = tmp_path / "target"; target.mkdir()
    d = tmp_path / "t"; d.mkdir()
    for i in range(5):
        (d / f"L{i}").symlink_to(target)
    entries, breaches = snapshot.take(d, quota=storage.Quota(
        max_files=2, max_file_bytes=1000, max_total_bytes=10_000))
    assert entries == {}, "a breach must discard the inventory, not report it partial"
    assert breaches
    assert "files" in breaches[0]


def test_a_root_that_cannot_be_walked_is_refused_not_reported_as_clean(tmp_path):
    """os.walk's default onerror swallows the error and yields nothing, so each of these
    returned a clean ({}, []) — which diff() reads as "the agent deleted the tree"."""
    with pytest.raises(snapshot.SnapshotError, match="cannot walk"):
        snapshot.take(tmp_path / "never-created")
    afile = tmp_path / "a.txt"; afile.write_text("x\n")
    with pytest.raises(snapshot.SnapshotError, match="cannot walk"):
        snapshot.take(afile)
    # Inline rather than a skipif on the whole test: root reads through mode 000, and the
    # two cases above are worth keeping under root rather than skipping all three.
    if os.geteuid() != 0:
        locked = tmp_path / "locked"; locked.mkdir()
        (locked / "unseen.txt").write_text("x\n")
        locked.chmod(0o000)
        try:
            with pytest.raises(snapshot.SnapshotError, match="cannot walk"):
                snapshot.take(locked)
        finally:
            locked.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through mode 000")
def test_an_unreadable_subdirectory_is_refused_not_silently_omitted(tmp_path):
    """The partial-inventory case: the walk yielded the readable part and dropped the rest
    with breaches == [], which is exactly what take()'s docstring forbids — and worse than
    the unreadable-FILE case beside it, since a directory drops a whole subtree."""
    d = tmp_path / "t"; d.mkdir()
    (d / "visible.txt").write_text("v\n")
    locked = d / "locked"; locked.mkdir()
    (locked / "hidden.txt").write_text("h\n")
    locked.chmod(0o000)
    try:
        with pytest.raises(snapshot.SnapshotError, match="cannot walk"):
            snapshot.take(d)
    finally:
        locked.chmod(0o755)


def test_a_fifo_or_socket_is_recorded_without_being_opened(tmp_path):
    """A read-open on a FIFO blocks until a writer appears — an unbounded hang with no
    timeout in the call path — and a socket raises ENXIO. Deadlined so the bug fails the
    test rather than wedging the suite."""
    d = tmp_path / "t"; d.mkdir()
    os.mkfifo(d / "p.fifo")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(d / "s.sock"))
    (d / "ordinary.txt").write_text("still inventoried\n")
    try:
        entries, breaches = _within(5.0, snapshot.take, d)
    finally:
        sock.close()
    assert breaches == []
    assert entries["p.fifo"].kind == "special"
    assert entries["s.sock"].kind == "special"
    assert entries["p.fifo"].digest != entries["s.sock"].digest, \
        "the file type stands in for content, so two types must not collide"
    assert entries["ordinary.txt"].kind == "file"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through mode 000")
def test_an_unreadable_file_raises_rather_than_reporting_a_digest_it_never_read(tmp_path):
    """The chosen contract: no honest digest means a loud failure, never a substitute
    value that would report a later edit to that file as unchanged."""
    d = tmp_path / "t"; d.mkdir()
    p = d / "locked.txt"; p.write_text("secret\n"); p.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            snapshot.take(d)
    finally:
        p.chmod(0o644)


def test_the_walk_order_is_sorted_so_the_first_breach_is_deterministic(tmp_path):
    """take() returns the FIRST breach and the caps are running totals, so an unsorted
    walk makes a tree over two caps report a different line run to run."""
    flat = tmp_path / "flat"; flat.mkdir()
    for name in ("m.txt", "a.txt", "z.txt", "b.txt", "c.txt"):
        (flat / name).write_text("x\n")
    entries, _ = snapshot.take(flat)
    assert list(entries) == ["a.txt", "b.txt", "c.txt", "m.txt", "z.txt"], "filenames unsorted"

    # Separately, because os.walk is top-down: a directory's own files all precede its
    # subdirectories', so a tree with both is never globally sorted even when both are.
    nested = tmp_path / "nested"; nested.mkdir()
    for name in ("m", "a", "z"):
        (nested / name).mkdir()
        (nested / name / "f.txt").write_text("x\n")
    entries, _ = snapshot.take(nested)
    assert list(entries) == ["a/f.txt", "m/f.txt", "z/f.txt"], "dirnames unsorted"


def test_the_review_round_bracket_sees_a_setuid_bit(tmp_path):
    """THE EXTERNAL QUESTION, asked at the level the answer matters: not "does Entry hold a
    mode" but "can the round bracket distinguish these two trees". Measured before the fix,
    `worktree_identity` returned the SAME digest either side of `chmod u+s` — so a reviewer
    could make a binary setuid in its own tree and the round read `after == before`: tree
    undisturbed, findings admissible, fix budget spent on them.

    It reaches through `review.worktree_identity` on purpose. A test at `snapshot.take` alone
    leaves the composition unasserted, which is how `kind` came to be carried by review's
    digest and not by `snapshot.diff`, one field apart, for however long.
    """
    from forge import review  # noqa: PLC0415
    root = tmp_path / "t"; root.mkdir()
    p = root / "run.sh"; p.write_text("#!/bin/sh\n", encoding="utf-8")
    p.chmod(0o755)
    quota = storage.Quota(max_files=1000, max_file_bytes=10 ** 7, max_total_bytes=10 ** 8)
    before, _ = review.worktree_identity(root, quota)
    p.chmod(0o4755)
    after, _ = review.worktree_identity(root, quota)
    assert before != after, "the round bracket read a setuid binary as no change at all"


def test_the_inventory_sees_a_setuid_bit(tmp_path):
    """The same claim at the leaf, so a failure says WHICH layer lost the bit."""
    root = tmp_path / "t"; root.mkdir()
    p = root / "run.sh"; p.write_text("#!/bin/sh\n", encoding="utf-8")
    p.chmod(0o755)
    before, breaches = snapshot.take(root)
    assert breaches == []
    p.chmod(0o4755)
    after, breaches = snapshot.take(root)
    assert breaches == []
    assert before != after, "chmod u+s left a byte-identical inventory"
    assert snapshot.diff(before, after) == {"run.sh": "modified"}


def test_the_inventory_sees_setgid_and_sticky_too(tmp_path):
    """The discrimination check for the bit above. One bit fixed and two left blind is the
    same defect with a smaller surface."""
    root = tmp_path / "t"; root.mkdir()
    p = root / "run.sh"; p.write_text("#!/bin/sh\n", encoding="utf-8")
    for mode in (0o755, 0o4755, 0o2755, 0o1755):
        p.chmod(mode)
        inv, _ = snapshot.take(root)
        assert inv["run.sh"].mode == mode, (oct(mode), oct(inv["run.sh"].mode))


def test_a_special_entry_carries_its_special_bits_too(tmp_path):
    """`_special_entry` masks the same way `take`'s regular branch did. Fixing one and not
    the other is the same hole wearing the other branch — and this package has closed a hole
    at the spelling it was found in more than once."""
    root = tmp_path / "t"; root.mkdir()
    p = root / "pipe"
    os.mkfifo(p)
    os.chmod(p, 0o644)
    a, _ = snapshot.take(root)
    os.chmod(p, 0o2644)
    b, _ = snapshot.take(root)
    assert a != b, "a setgid FIFO left a byte-identical inventory"


def test_diff_compares_kind_even_when_every_other_field_matches():
    """ONE PREDICATE, NOT TWO. `review._inventory_digest` includes `kind` and says so;
    `snapshot.diff` did not. Today they agree on every tree that can be built, because
    `_special_entry` folds the file type into the digest and `_symlink_entry` digests the
    target text — so this is not a reproducible miss and no test here should pretend it is
    (two drafts of this task asserted a FIFO miss that PASSED before any fix).

    What it is, is two definitions of "did this path change" maintained separately, one field
    apart. This states the predicate directly over hand-built entries, which is the only way
    to assert it: a filesystem cannot produce two entries agreeing on digest, mode and size
    and differing in kind, and that is precisely why the gap stays invisible until the day
    `_special_entry`'s digest changes.
    """
    a = {"x": snapshot.Entry("x", "d", 0o644, 0, "file")}
    b = {"x": snapshot.Entry("x", "d", 0o644, 0, "special")}
    assert snapshot.diff(a, b) == {"x": "modified"}


def test_the_two_inventory_predicates_read_the_same_fields():
    """The structural claim, asserted where it can actually fail: `review._inventory_digest`
    builds its row from five fields and `snapshot.diff` must compare the same five. A future
    field added to `Entry` and wired into one of them is the next instance of this defect."""
    import dataclasses  # noqa: PLC0415
    fields = {f.name for f in dataclasses.fields(snapshot.Entry)}
    assert fields == {"path", "digest", "mode", "size", "kind"}, (
        "Entry gained or lost a field — check that BOTH snapshot.diff and "
        "review._inventory_digest were updated, not just one")
