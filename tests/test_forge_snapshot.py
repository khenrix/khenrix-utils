"""Filesystem inventory keyed on CONTENT, never on lstat (spec §7.3)."""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))

from forge import snapshot, storage  # noqa: E402
from forge_fixtures import make_repo  # noqa: E402


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


def test_git_is_skipped_by_default(tmp_path):
    repo = make_repo(tmp_path)
    entries, _ = snapshot.take(repo)
    assert not any(p.startswith(".git/") for p in entries)
    assert "seed.txt" in entries


def test_quota_breach_fails_closed_with_no_partial_inventory(tmp_path):
    d = tmp_path / "t"; d.mkdir()
    for i in range(5):
        (d / f"f{i}.txt").write_text("x\n")
    entries, breaches = snapshot.take(d, quota=storage.Quota(
        max_files=2, max_file_bytes=1000, max_total_bytes=10_000))
    assert entries == {} and breaches and "files" in breaches[0]


def test_symlinked_dirs_also_count_against_the_file_quota(tmp_path):
    """They arrive in os.walk's dirnames, never filenames — counting only filenames lets
    a tree of them return an over-quota inventory with no breach reported."""
    target = tmp_path / "target"; target.mkdir()
    d = tmp_path / "t"; d.mkdir()
    for i in range(5):
        (d / f"L{i}").symlink_to(target)
    entries, breaches = snapshot.take(d, quota=storage.Quota(
        max_files=2, max_file_bytes=1000, max_total_bytes=10_000))
    assert entries == {} and breaches and "files" in breaches[0]
