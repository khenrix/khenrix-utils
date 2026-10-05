"""Target-bound, bounded container transfer behavior."""
from __future__ import annotations

import errno
import hashlib
import io
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.artifacts import canonical_json  # noqa: E402
from shared.lib.fanout import container_transfer as transfer  # noqa: E402
from shared.lib.fanout import container_transfer_support as support  # noqa: E402
from shared.lib.fanout import lifecycle  # noqa: E402
from shared.lib.fanout.container_transfer import (  # noqa: E402
    TransferEntry,
    TransferIdentity,
    TransferManifest,
    TransferRejected,
    encode_manifest,
    seal_transfer,
    stage_export,
    stage_import,
)


IDENTITY = TransferIdentity(
    "run-synthetic", "task-plan", "target-one", "seat-claude", "a" * 64,
)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _entry(path: str, payload: bytes = b"", *, kind: str = "file",
           mode: int = 0o644) -> TransferEntry:
    return TransferEntry(path, kind, mode, hashlib.sha256(payload).hexdigest(), len(payload))


def _manifest(*entries: TransferEntry,
              identity: TransferIdentity = IDENTITY) -> TransferManifest:
    return TransferManifest(1, identity, tuple(entries))


def _frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


def _manifest_bytes(manifest: TransferManifest) -> bytes:
    payload = canonical_json(asdict(manifest))
    return b"".join(_frame(payload[index:index + 65536])
                    for index in range(0, len(payload), 65536))


def _published_absent(root: Path) -> None:
    assert not (root / "published").exists()
    assert not any(path.name.startswith(".fanout-transfer-") for path in root.iterdir())


@pytest.mark.parametrize("fail_parent_sync", [False, True])
def test_import_ack_requires_published_parent_fsync(tmp_path, monkeypatch, fail_parent_sync):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    manifest = _manifest(_entry("context", kind="directory", mode=0o755),
        _entry("context/plan.json", b"{}\n"), _entry("repo", kind="directory", mode=0o755),
        _entry("repo/item", b"published bytes"))
    wire = _manifest_bytes(manifest)
    stream = transfer._MAGIC + len(wire).to_bytes(4, "big") + wire + _frame(b"{}\n") + _frame(b"published bytes")
    identity_hash = hashlib.sha256(canonical_json(asdict(IDENTITY))).hexdigest()
    root = workspace / "inputs" / identity_hash
    published = root / "published"
    real_rename, real_fsync, real_remove = transfer._rename_no_replace, transfer.os.fsync, transfer._remove_tree
    events = []
    parent_fd = None
    def rename(source_fd, source_name, destination_fd, destination_name):
        nonlocal parent_fd
        result = real_rename(source_fd, source_name, destination_fd, destination_name)
        if result:
            parent_fd = destination_fd
            events.append("published")
        return result
    def fsync(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (workspace.stat().st_dev, workspace.stat().st_ino):
            events.append("workspace-fsync")
        elif root.parent.exists() and (info.st_dev, info.st_ino) == (root.parent.stat().st_dev, root.parent.stat().st_ino):
            events.append("inputs-fsync")
        if fd == parent_fd and (info.st_dev, info.st_ino) == (root.stat().st_dev, root.stat().st_ino):
            assert published.is_dir()
            events.append("parent-fsync")
            if fail_parent_sync:
                raise OSError(errno.EIO, "actual post-rename parent sync failure")
        real_fsync(fd)
    def remove(fd, name):
        assert not published.exists(), "published stage ownership must already be released"
        return real_remove(fd, name)
    monkeypatch.setattr(transfer, "_rename_no_replace", rename)
    monkeypatch.setattr(transfer.os, "fsync", fsync)
    monkeypatch.setattr(transfer, "_remove_tree", remove)
    ack = io.BytesIO()
    if fail_parent_sync:
        with pytest.raises(TransferRejected, match="staging failed") as rejected:
            transfer.main(["import"], stdin=io.BytesIO(stream), stdout=ack, workspace=workspace)
        assert isinstance(rejected.value.__cause__, OSError)
        assert rejected.value.__cause__.errno == errno.EIO
        assert ack.getvalue() == b""
    else:
        assert transfer.main(["import"], stdin=io.BytesIO(stream), stdout=ack, workspace=workspace) == 0
        assert ack.getvalue() == hashlib.sha256(wire).hexdigest().encode() + b"\n"
    assert events == ["inputs-fsync", "workspace-fsync", "published", "parent-fsync"]
    assert (published / "repo/item").read_bytes() == b"published bytes"
    assert (published / "context/plan.json").read_bytes() == b"{}\n"
    assert not list(root.glob(".fanout-transfer-*"))
    with pytest.raises(TransferRejected, match="destination already exists"):
        transfer.main(["import"], stdin=io.BytesIO(stream), stdout=io.BytesIO(), workspace=workspace)
    assert events.count("published") == 1 and events.count("parent-fsync") == 1


def _set_symlink_mode_or_skip(link: Path, mode: int) -> None:
    if os.chmod in os.supports_follow_symlinks:
        try:
            os.chmod(link, mode, follow_symlinks=False)
        except NotImplementedError:
            pass
        except OSError as error:
            if error.errno not in {errno.ENOSYS, errno.ENOTSUP}:
                raise
    if link.lstat().st_mode & 0o777 != mode:
        pytest.skip("filesystem cannot set symlink mode")


def test_round_trip_preserves_admitted_plan_skill_bundle_checkpoint_and_in_tree_symlink(tmp_path):
    source = tmp_path / "controller-created-workspace"
    source.mkdir()
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    remotes = subprocess.run(["git", "-C", str(source), "remote"], check=True,
                             capture_output=True, text=True)
    assert remotes.stdout == ""

    plan = b'{"all_admitted_tasks":["task-plan","task-check"]}\n'
    bundle = b'{"skills":["review","test"],"version":1}\n'
    checkpoint = b'{"verified_checkpoint":"peer-after-first-round-barrier"}\n'
    program = b"print('synthetic target')\n"
    (source / "context").mkdir(mode=0o755)
    (source / "src").mkdir(mode=0o755)
    (source / "context").chmod(0o755)
    (source / "src").chmod(0o755)
    (source / "context/plan.json").write_bytes(plan)
    (source / "context/skills.json").write_bytes(bundle)
    (source / "context/peer-checkpoint.json").write_bytes(checkpoint)
    for name in ("context/plan.json", "context/skills.json", "context/peer-checkpoint.json"):
        (source / name).chmod(0o644)
    (source / "src/main.py").write_bytes(program)
    (source / "src/main.py").chmod(0o755)
    source_link = source / "src/current.py"
    source_link.symlink_to("main.py")
    _set_symlink_mode_or_skip(source_link, 0o777)

    manifest = _manifest(
        _entry("context", kind="directory", mode=0o755),
        _entry("context/peer-checkpoint.json", checkpoint),
        _entry("context/plan.json", plan),
        _entry("context/skills.json", bundle),
        _entry("src", kind="directory", mode=0o755),
        _entry("src/current.py", b"main.py", kind="symlink", mode=0o777),
        _entry("src/main.py", program, mode=0o755),
    )
    guest_root = tmp_path / "guest-private"
    guest_root.mkdir(mode=0o700)
    published = stage_import(guest_root, IDENTITY, _manifest_bytes(manifest),
                             seal_transfer(manifest, source))
    assert published == guest_root / "published"
    assert (published / "context/plan.json").read_bytes() == plan
    assert (published / "context/skills.json").read_bytes() == bundle
    assert (published / "context/peer-checkpoint.json").read_bytes() == checkpoint
    assert (published / "src/current.py").is_symlink()
    assert os.readlink(published / "src/current.py") == "main.py"
    assert (published / "src/current.py").read_bytes() == program
    assert not (published / ".git").exists()
    assert not any(path.name.startswith(".fanout-transfer-") for path in guest_root.iterdir())

    export_manifest, export_frames = stage_export(published, IDENTITY, manifest)
    assert export_manifest == _manifest_bytes(manifest)
    host_root = tmp_path / "host-private"
    host_root.mkdir(mode=0o700)
    exported = stage_import(host_root, IDENTITY, export_manifest, export_frames)
    assert (exported / "context/plan.json").read_bytes() == plan
    assert (exported / "context/peer-checkpoint.json").read_bytes() == checkpoint
    assert (exported / "src/current.py").is_symlink()
    assert not (exported / ".git").exists()


def test_import_publishes_symlink_with_manifest_mode(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    manifest = _manifest(
        _entry("link", b"target", kind="symlink", mode=0o777),
        _entry("target", b"payload"),
    )

    published = stage_import(root, IDENTITY, _manifest_bytes(manifest),
                             [_frame(b"target"), _frame(b"payload")])

    assert (published / "link").lstat().st_mode & 0o777 == 0o777
    assert (published / "link").read_bytes() == b"payload"
    assert transfer.snapshot_manifest(published, IDENTITY) == manifest


def test_import_rejects_symlink_mode_it_cannot_set(tmp_path, monkeypatch):
    probe = tmp_path / "probe"
    probe.symlink_to("target")
    if probe.lstat().st_mode & 0o777 == 0o777:
        pytest.skip("filesystem already creates transferable symlinks")
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    manifest = _manifest(
        _entry("link", b"target", kind="symlink", mode=0o777),
        _entry("target", b"payload"),
    )

    def unsupported(*_args, **_kwargs):
        raise NotImplementedError("no no-follow chmod")

    monkeypatch.setattr(transfer.os, "chmod", unsupported)
    with pytest.raises(TransferRejected, match="symlink mode"):
        stage_import(root, IDENTITY, _manifest_bytes(manifest),
                     [_frame(b"target"), _frame(b"payload")])
    _published_absent(root)


@pytest.mark.parametrize("name", [
    "../escape", "/absolute", "a/../b", "a\x00b", "a//b", "a/./b", "a\\b", ".git/config",
])
def test_import_rejects_noncanonical_or_git_name_without_publishing(tmp_path, name):
    manifest = _manifest(_entry(name, b"payload"))
    with pytest.raises(TransferRejected):
        stage_import(tmp_path, IDENTITY, _manifest_bytes(manifest), [_frame(b"payload")])
    _published_absent(tmp_path)


@pytest.mark.parametrize("entry,payload", [
    (_entry("link", b"../escape", kind="symlink", mode=0o777), b"../escape"),
    (_entry("item", b"payload", kind="hardlink"), b"payload"),
    (TransferEntry("item", "file", 0o644, EMPTY_SHA256, 64 * 1024 * 1024 + 1), b""),
    (_entry("item", b"payload", mode=0o4755), b"payload"),
])
def test_unsafe_kind_size_mode_or_symlink_fails_without_publishing(tmp_path, entry, payload):
    with pytest.raises(TransferRejected):
        stage_import(tmp_path, IDENTITY, _manifest_bytes(_manifest(entry)),
                     [_frame(payload)] if payload else [])
    _published_absent(tmp_path)


@pytest.mark.parametrize("paths", [("a", "a"), ("A", "a")])
def test_duplicate_or_casefolded_name_fails_without_publishing(tmp_path, paths):
    manifest = _manifest(*(_entry(path, b"x") for path in paths))
    with pytest.raises(TransferRejected):
        stage_import(tmp_path, IDENTITY, _manifest_bytes(manifest),
                     [_frame(b"x"), _frame(b"x")])
    _published_absent(tmp_path)


def test_manifest_replay_to_another_target_fails_before_publishing(tmp_path):
    manifest = _manifest(_entry("item", b"payload"))
    other_target = replace(IDENTITY, target_id="target-two")
    with pytest.raises(TransferRejected, match="identity"):
        stage_import(tmp_path, other_target, _manifest_bytes(manifest), [_frame(b"payload")])
    _published_absent(tmp_path)


@pytest.mark.parametrize("frames", [
    [_frame(b"changed")],
    [b"\x00\x00\x00\x07pay"],
    [_frame(b"payload"), _frame(b"extra")],
    [_frame(b"payload"), b"\x00\x00"],
    [_frame(b"payload" * 10000)],
])
def test_changed_truncated_trailing_or_oversized_content_frame_never_publishes(tmp_path, frames):
    manifest = _manifest(_entry("item", b"payload"))
    with pytest.raises(TransferRejected):
        stage_import(tmp_path, IDENTITY, _manifest_bytes(manifest), frames)
    _published_absent(tmp_path)


def test_short_nonfinal_content_frame_is_rejected(tmp_path):
    manifest = _manifest(_entry("item", b"payload"))
    with pytest.raises(TransferRejected, match="frame"):
        stage_import(tmp_path, IDENTITY, _manifest_bytes(manifest),
                     [_frame(b"pay"), _frame(b"load")])
    _published_absent(tmp_path)


@pytest.mark.parametrize("wire", [b"\x00\x00\x00", _frame(b"{}") + b"\x00\x00",
                                   (65537).to_bytes(4, "big") + b"x"])
def test_truncated_trailing_or_oversized_manifest_frame_fails_closed(tmp_path, wire):
    with pytest.raises(TransferRejected):
        stage_import(tmp_path, IDENTITY, wire, [])
    _published_absent(tmp_path)


def test_noncanonical_manifest_frame_split_is_rejected(tmp_path):
    raw = canonical_json(asdict(_manifest()))
    with pytest.raises(TransferRejected, match="frame"):
        stage_import(tmp_path, IDENTITY, _frame(raw[:8]) + _frame(raw[8:]), [])
    _published_absent(tmp_path)


def test_nonfinite_json_number_is_rejected_as_transfer_error(tmp_path):
    raw = canonical_json(asdict(_manifest())).replace(b'"schema_version":1',
                                                        b'"schema_version":1e999')
    with pytest.raises(TransferRejected):
        stage_import(tmp_path, IDENTITY, _frame(raw), [])
    _published_absent(tmp_path)


def test_manifest_spanning_multiple_bounded_frames_imports(tmp_path):
    entries = tuple(_entry(f"context-{index:04d}-" + "x" * 90,
                           kind="directory", mode=0o700) for index in range(600))
    manifest = _manifest(*entries)
    wire = encode_manifest(manifest)
    assert len(wire) > 65536
    assert wire[:4] == (65536).to_bytes(4, "big")
    published = stage_import(tmp_path, IDENTITY, wire, [])
    assert len(tuple(published.iterdir())) == 600


def test_total_declared_bytes_over_ceiling_are_rejected_before_payload(tmp_path):
    entries = tuple(TransferEntry(f"file-{index}", "file", 0o644,
                                  EMPTY_SHA256, 64 * 1024 * 1024)
                    for index in range(9))
    manifest = _manifest(*entries)
    with pytest.raises(TransferRejected, match="total"):
        stage_import(tmp_path, IDENTITY, _manifest_bytes(manifest), [])
    _published_absent(tmp_path)


def test_overdeep_transfer_tree_is_rejected_before_staging(tmp_path):
    entries = tuple(_entry("/".join(["a"] * depth), kind="directory", mode=0o700)
                    for depth in range(1, 130))
    with pytest.raises(TransferRejected, match="depth"):
        stage_import(tmp_path, IDENTITY, _manifest_bytes(_manifest(*entries)), [])
    _published_absent(tmp_path)


def test_file_larger_than_one_frame_round_trips(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    payload = b"x" * 65536 + b"y" * 17
    (source / "item").write_bytes(payload)
    (source / "item").chmod(0o644)
    manifest = _manifest(_entry("item", payload))
    encoded, chunks = stage_export(source, IDENTITY, manifest)
    destination = tmp_path / "destination"
    destination.mkdir(mode=0o700)
    published = stage_import(destination, IDENTITY, encoded, chunks)
    assert (published / "item").read_bytes() == payload


def test_snapshot_refuses_same_inode_growth_between_stat_and_open_before_read(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "item"
    path.write_bytes(b"1234")
    path.chmod(0o600)
    identity = path.stat().st_ino
    original_open, original_read = os.open, os.read
    opened, reads = [], []
    def grow(name, flags, *args, **kwargs):
        if name == "item" and not opened:
            opened.append(identity)
            path.write_bytes(b"12345")
        return original_open(name, flags, *args, **kwargs)
    def observe(fd, size):
        if os.fstat(fd).st_ino == identity:
            reads.append(size)
        return original_read(fd, size)
    monkeypatch.setattr(transfer, "MAX_FILE", 4)
    monkeypatch.setattr(os, "open", grow)
    monkeypatch.setattr(os, "read", observe)
    with pytest.raises(TransferRejected):
        transfer.snapshot_manifest(source, IDENTITY)
    assert opened == [identity]
    assert path.stat().st_ino == identity
    assert reads == []


@pytest.mark.parametrize("limits", ((4, 6), (4, 5)))
def test_snapshot_optional_limits_accept_exact_bytes_or_refuse_before_remaining_read(tmp_path, monkeypatch, limits):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"xy")
    (source / "b").write_bytes(b"1234")
    reads = []
    original = os.read
    identity = (source / "b").stat().st_ino
    def observe(fd, size):
        if os.fstat(fd).st_ino == identity:
            reads.append(size)
        return original(fd, size)
    monkeypatch.setattr(os, "read", observe)
    if limits == (4, 6):
        manifest = transfer.snapshot_manifest(source, IDENTITY, max_file_bytes=4, max_total_bytes=6)
        assert [(entry.path, entry.length, entry.mode) for entry in manifest.entries] == [("a", 2, 0o600), ("b", 4, 0o600)]
        assert manifest == transfer.snapshot_manifest(source, IDENTITY)
        assert reads == [4, 1, 4, 1]
    else:
        with pytest.raises(TransferRejected, match="total"):
            transfer.snapshot_manifest(source, IDENTITY, max_file_bytes=4, max_total_bytes=5)
        assert reads == []


@pytest.mark.parametrize("limits", ((3, 10), (10, 3)))
def test_snapshot_optional_limits_refuse_symlink_before_readlink(tmp_path, monkeypatch, limits):
    source = tmp_path / "source"
    source.mkdir()
    link = source / "link"
    link.symlink_to("abcd")
    _set_symlink_mode_or_skip(link, 0o777)
    reads = []
    original = os.readlink
    def observe(path, **kwargs):
        reads.append(path)
        return original(path, **kwargs)
    monkeypatch.setattr(os, "readlink", observe)
    with pytest.raises(TransferRejected, match="ceiling"):
        transfer.snapshot_manifest(source, IDENTITY, max_file_bytes=limits[0], max_total_bytes=limits[1])
    assert reads == []


def test_existing_published_destination_is_not_clobbered(tmp_path):
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "keep").write_bytes(b"original")
    manifest = _manifest(_entry("item", b"payload"))
    with pytest.raises(TransferRejected):
        stage_import(tmp_path, IDENTITY, _manifest_bytes(manifest), [_frame(b"payload")])
    assert (destination / "keep").read_bytes() == b"original"
    assert not (destination / "item").exists()


def test_random_staging_name_collision_preserves_existing_directory(tmp_path, monkeypatch):
    collision = tmp_path / (".fanout-transfer-" + "0" * 32)
    collision.mkdir(mode=0o700)
    (collision / "keep").write_bytes(b"owner data")
    monkeypatch.setattr(transfer.secrets, "token_hex", lambda _count: "0" * 32)
    manifest = _manifest(_entry("item", b"payload"))
    with pytest.raises(TransferRejected):
        stage_import(tmp_path, IDENTITY, _manifest_bytes(manifest), [_frame(b"payload")])
    assert (collision / "keep").read_bytes() == b"owner data"
    assert not (tmp_path / "published").exists()


def _chained_link_manifest(link_target: bytes, *, file_payload: bytes | None = None):
    entries = [
        _entry("a", kind="directory", mode=0o755),
        _entry("a/b", kind="directory", mode=0o755),
        _entry("a/b/c", kind="directory", mode=0o755),
        _entry("a/b/c/d", kind="directory", mode=0o755),
        _entry("a/b/c/d/link", link_target, kind="symlink", mode=0o777),
        _entry("a/b/jump", b"../../foo", kind="symlink", mode=0o777),
        _entry("foo", kind="directory", mode=0o755),
    ]
    if file_payload is not None:
        entries.append(_entry("foo/file", file_payload))
    return _manifest(*entries)


def _chained_link_frames(link_target: bytes, *, file_payload: bytes | None = None):
    frames = [_frame(link_target), _frame(b"../../foo")]
    if file_payload is not None:
        frames.append(_frame(file_payload))
    return frames


def test_import_rejects_symlink_chain_that_escapes_published_tree(tmp_path):
    root = tmp_path / "import" / "private"
    root.parent.mkdir()
    root.mkdir(mode=0o700)
    (root.parent / "outside").write_bytes(b"outside-secret")
    target = b"../../jump/../../../outside"
    manifest = _chained_link_manifest(target)

    with pytest.raises(TransferRejected, match="symlink"):
        stage_import(root, IDENTITY, _manifest_bytes(manifest),
                     _chained_link_frames(target))
    _published_absent(root)
    assert (root.parent / "outside").read_bytes() == b"outside-secret"


def test_export_rejects_symlink_chain_that_reads_outside_source_root(tmp_path):
    source = tmp_path / "source" / "private"
    (source / "a/b/c/d").mkdir(parents=True)
    (source / "foo").mkdir()
    (source.parent.parent / "outside").write_bytes(b"outside-secret")
    target = "../../jump/../../../outside"
    (source / "a/b/c/d/link").symlink_to(target)
    (source / "a/b/jump").symlink_to("../../foo")
    for name in ("a", "a/b", "a/b/c", "a/b/c/d", "foo"):
        (source / name).chmod(0o755)
    for name in ("a/b/c/d/link", "a/b/jump"):
        _set_symlink_mode_or_skip(source / name, 0o777)
    assert (source / "a/b/c/d/link").read_bytes() == b"outside-secret"
    manifest = _chained_link_manifest(target.encode())

    with pytest.raises(TransferRejected, match="symlink"):
        stage_export(source, IDENTITY, manifest)
    with pytest.raises(TransferRejected, match="symlink"):
        list(seal_transfer(manifest, source))


def test_safe_chained_symlink_stays_inside_published_tree(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    target = b"../../jump/file"
    manifest = _chained_link_manifest(target, file_payload=b"inside")

    published = stage_import(root, IDENTITY, _manifest_bytes(manifest),
                             _chained_link_frames(target, file_payload=b"inside"))
    assert (published / "a/b/c/d/link").read_bytes() == b"inside"


def test_publish_race_never_replaces_new_empty_destination(tmp_path, monkeypatch):
    manifest = _manifest(_entry("item", b"payload"))
    real_stat = os.stat
    checks = 0
    raced_inode = None

    def stat_with_race(path, *args, **kwargs):
        nonlocal checks, raced_inode
        if path == "published" and kwargs.get("dir_fd") is not None:
            checks += 1
            if checks == 2:
                with pytest.raises(FileNotFoundError):
                    real_stat(path, *args, **kwargs)
                os.mkdir("published", mode=0o700, dir_fd=kwargs["dir_fd"])
                raced_inode = real_stat(path, *args, **kwargs).st_ino
                raise FileNotFoundError(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(transfer.os, "stat", stat_with_race)
    with pytest.raises(TransferRejected, match="destination"):
        stage_import(tmp_path, IDENTITY, _manifest_bytes(manifest), [_frame(b"payload")])
    assert raced_inode is not None
    assert real_stat(tmp_path / "published").st_ino == raced_inode
    assert not any(path.name.startswith(".fanout-transfer-") for path in tmp_path.iterdir())


def test_source_hardlink_and_symlinked_parent_are_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "item").write_bytes(b"payload")
    os.link(source / "item", source / "second-link")
    manifest = _manifest(_entry("item", b"payload"))
    with pytest.raises(TransferRejected, match="hardlink"):
        list(seal_transfer(manifest, source))
    with pytest.raises(TransferRejected, match="hardlink"):
        stage_export(source, IDENTITY, manifest)

    (source / "redirect").symlink_to(tmp_path)
    redirected = _manifest(_entry("redirect", kind="directory", mode=0o755),
                           _entry("redirect/item", b"payload"))
    with pytest.raises(TransferRejected):
        list(seal_transfer(redirected, source))


@pytest.mark.parametrize("operation", ["seal", "export"])
def test_direct_transfer_rejects_symlink_mode_different_from_manifest(tmp_path, operation):
    source = tmp_path / "source"
    source.mkdir()
    (source / "target").write_bytes(b"target")
    link = source / "link"
    link.symlink_to("target")
    _set_symlink_mode_or_skip(link, 0o700)
    manifest = _manifest(
        _entry("link", b"target", kind="symlink", mode=0o777),
        _entry("target", b"target"),
    )

    with pytest.raises(TransferRejected, match="symlink mode"):
        if operation == "seal":
            list(seal_transfer(manifest, source))
        else:
            stage_export(source, IDENTITY, manifest)


@pytest.mark.parametrize("operation", ("seal", "export"))
@pytest.mark.parametrize("target", ("keep", "peer", "other"))
def test_direct_symlink_payload_checks_actual_length_before_readlink(tmp_path, monkeypatch, operation, target):
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep").write_bytes(b"xy")
    (source / "keep").chmod(0o644)
    entries = [
        _entry("keep", b"xy"),
        _entry("link", b"keep", kind="symlink", mode=0o777),
    ]
    if target != "keep":
        (source / target).write_bytes(b"")
        (source / target).chmod(0o644)
        entries.append(_entry(target))
    manifest = _manifest(*entries)
    link = source / "link"
    link.symlink_to(target)
    _set_symlink_mode_or_skip(link, 0o777)
    assert link.lstat().st_size == {"keep": 4, "peer": 4, "other": 5}[target]
    reads = []
    original_readlink = os.readlink

    def observe(path, *args, **kwargs):
        value = original_readlink(path, *args, **kwargs)
        if path == "link":
            reads.append(value)
        return value

    monkeypatch.setattr(transfer, "MAX_FILE", 4)
    monkeypatch.setattr(transfer, "MAX_TOTAL", 6)
    monkeypatch.setattr(transfer.os, "readlink", observe)

    def collect():
        if operation == "seal":
            return list(seal_transfer(manifest, source))
        return list(stage_export(source, IDENTITY, manifest)[1])

    if target == "keep":
        assert collect() == [_frame(b"xy"), _frame(b"keep")]
        assert reads == ["keep", "keep"] if operation == "seal" else reads == ["keep", "keep", "keep"]
    elif target == "peer":
        with pytest.raises(TransferRejected, match="symlink digest"):
            collect()
        assert reads == ["peer"]
    else:
        with pytest.raises(TransferRejected):
            collect()
        assert reads == []


def test_direct_transfer_rejects_symlink_mode_changed_during_readlink(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "target").write_bytes(b"target")
    link = source / "link"
    link.symlink_to("target")
    _set_symlink_mode_or_skip(link, 0o700)
    _set_symlink_mode_or_skip(link, 0o777)
    manifest = _manifest(
        _entry("link", b"target", kind="symlink", mode=0o777),
        _entry("target", b"target"),
    )
    original_readlink = os.readlink

    def change_mode_during_readlink(path, *args, **kwargs):
        target = original_readlink(path, *args, **kwargs)
        if path == "link":
            os.chmod(link, 0o700, follow_symlinks=False)
        return target

    monkeypatch.setattr(transfer.os, "readlink", change_mode_during_readlink)
    with pytest.raises(TransferRejected, match="symlink mode"):
        list(seal_transfer(manifest, source))
    assert link.lstat().st_mode & 0o777 == 0o700


def test_export_rehashes_candidate_and_rejects_changed_bytes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "item").write_bytes(b"changed")
    (source / "item").chmod(0o644)
    manifest = _manifest(_entry("item", b"payload"))
    with pytest.raises(TransferRejected, match="digest"):
        list(stage_export(source, IDENTITY, manifest)[1])


def test_manifest_entry_ceiling_is_checked_before_payload(tmp_path):
    entry = asdict(_entry("item", b"x"))
    document = {"schema_version": 1, "identity": asdict(IDENTITY),
                "entries": [entry] * 100001}
    raw = canonical_json(document)
    wire = b"".join(_frame(raw[index:index + 65536])
                    for index in range(0, len(raw), 65536))
    with pytest.raises(TransferRejected, match="entry"):
        stage_import(tmp_path, IDENTITY, wire, [])
    _published_absent(tmp_path)


def test_transfer_script_starts_with_only_its_support_sibling(tmp_path):
    source = Path(transfer.__file__)
    shutil.copy2(source, tmp_path / source.name)
    support = source.with_name("container_transfer_support.py")
    if support.exists():
        shutil.copy2(support, tmp_path / support.name)
    result = subprocess.run([sys.executable, str(tmp_path / source.name), "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "import" in result.stdout and "export" in result.stdout


def test_snapshot_manifest_records_regular_tree_and_safe_link(tmp_path):
    root = tmp_path / "repo"
    root.mkdir(mode=0o700)
    (root / "src").mkdir(mode=0o755)
    (root / "src/main.py").write_bytes(b"print('ok')\n")
    (root / "src/main.py").chmod(0o644)
    link = root / "current.py"
    link.symlink_to("src/main.py")
    _set_symlink_mode_or_skip(link, 0o777)
    manifest = transfer.snapshot_manifest(root, IDENTITY)
    assert [(entry.path, entry.kind) for entry in manifest.entries] == [
        ("current.py", "symlink"), ("src", "directory"), ("src/main.py", "file"),
    ]
    assert manifest.entries[0].mode == 0o777
    assert manifest.entries[-1].sha256 == hashlib.sha256(b"print('ok')\n").hexdigest()
    assert b"".join(seal_transfer(manifest, root))


def test_snapshot_manifest_rejects_nontransferable_symlink_mode(tmp_path):
    root = tmp_path / "repo"
    root.mkdir(mode=0o700)
    (root / "target").write_bytes(b"target")
    link = root / "link"
    link.symlink_to("target")
    _set_symlink_mode_or_skip(link, 0o700)

    with pytest.raises(TransferRejected, match="symlink mode"):
        transfer.snapshot_manifest(root, IDENTITY)


@pytest.mark.parametrize("unsafe", ["git", "hardlink", "device", "casefold", "escape", "depth", "size"])
def test_snapshot_manifest_rejects_unsafe_source_entries(tmp_path, unsafe, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir(mode=0o700)
    if unsafe == "git":
        (root / ".git").mkdir()
    elif unsafe == "hardlink":
        (root / "file").write_bytes(b"a")
        os.link(root / "file", root / "peer")
    elif unsafe == "device":
        os.mkfifo(root / "pipe")
    elif unsafe == "casefold":
        (root / "A").write_bytes(b"a")
        (root / "a").write_bytes(b"b")
        if os.path.samefile(root / "A", root / "a"):
            pytest.skip("filesystem does not store distinct case-folded names")
    elif unsafe == "escape":
        (root / "link").symlink_to("../outside")
    elif unsafe == "depth":
        current = root
        for _ in range(129):
            current = current / "x"
            current.mkdir()
    else:
        (root / "large").write_bytes(b"x")
        monkeypatch.setattr(transfer, "MAX_FILE", 0)
    with pytest.raises(TransferRejected):
        transfer.snapshot_manifest(root, IDENTITY)


def test_snapshot_manifest_rejects_file_changed_during_read(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir(mode=0o700)
    item = root / "file"
    item.write_bytes(b"before")
    original = transfer.os.read
    changed = False

    def read_and_change(fd, count):
        nonlocal changed
        payload = original(fd, count)
        if payload and not changed:
            changed = True
            item.write_bytes(b"after!")
        return payload

    monkeypatch.setattr(transfer.os, "read", read_and_change)
    with pytest.raises(TransferRejected, match="changed"):
        transfer.snapshot_manifest(root, IDENTITY)


def _cli_import_wire(manifest, *frames):
    encoded = encode_manifest(manifest)
    return b"FANOUT-XFER-1\n" + len(encoded).to_bytes(4, "big") + encoded + b"".join(frames)


def test_cli_import_publishes_only_repo_and_context_under_identity_digest(tmp_path):
    payload = b"hello"
    manifest = _manifest(
        _entry("context", kind="directory", mode=0o700),
        _entry("context/plan.json", b"{}\n"),
        _entry("repo", kind="directory", mode=0o700),
        _entry("repo/file", payload),
    )
    wire = _cli_import_wire(manifest, _frame(b"{}\n"), _frame(payload))
    output = io.BytesIO()
    assert transfer.main(["import"], stdin=io.BytesIO(wire), stdout=output,
                         workspace=tmp_path) == 0
    digest = hashlib.sha256(canonical_json(asdict(IDENTITY))).hexdigest()
    published = tmp_path / "inputs" / digest / "published"
    assert (published / "repo/file").read_bytes() == payload
    assert (published / "context/plan.json").read_bytes() == b"{}\n"
    assert output.getvalue() == hashlib.sha256(encode_manifest(manifest)).hexdigest().encode() + b"\n"


@pytest.mark.parametrize("change", ["magic", "truncated", "trailing", "oversize", "extra-root"])
def test_cli_import_bad_wire_never_publishes(tmp_path, change):
    manifest = _manifest(_entry("context", kind="directory", mode=0o700),
                         _entry("repo", kind="directory", mode=0o700))
    wire = _cli_import_wire(manifest)
    if change == "magic":
        wire = b"BAD" + wire[3:]
    elif change == "truncated":
        wire = wire[:-1]
    elif change == "trailing":
        wire += b"x"
    elif change == "oversize":
        wire = b"FANOUT-XFER-1\n" + (transfer.MAX_MANIFEST + 1).to_bytes(4, "big")
    else:
        manifest = _manifest(_entry("other", kind="directory", mode=0o700))
        wire = _cli_import_wire(manifest)
    with pytest.raises(TransferRejected):
        transfer.main(["import"], stdin=io.BytesIO(wire), stdout=io.BytesIO(),
                      workspace=tmp_path)
    assert not (tmp_path / "inputs").exists() or not list((tmp_path / "inputs").rglob("published"))


def test_cli_export_requires_exact_identity_request_before_writing(tmp_path):
    manifest = _manifest(_entry("context", kind="directory", mode=0o700),
                         _entry("repo", kind="directory", mode=0o700),
                         _entry("repo/file", b"hello"))
    transfer.main(["import"], stdin=io.BytesIO(_cli_import_wire(manifest, _frame(b"hello"))),
                  stdout=io.BytesIO(), workspace=tmp_path)
    request = b"FANOUT-XFER-1\n" + len(canonical_json(asdict(IDENTITY))).to_bytes(4, "big") + canonical_json(asdict(IDENTITY))
    output = io.BytesIO()
    assert transfer.main(["export"], stdin=io.BytesIO(request), stdout=output,
                         workspace=tmp_path) == 0
    assert output.getvalue().startswith(b"FANOUT-XFER-1\n")
    assert _frame(b"hello") in output.getvalue()
    for invalid in (request + b"x", request[:-1], request.replace(b"target-one", b"target-two")):
        with pytest.raises(TransferRejected):
            transfer.main(["export"], stdin=io.BytesIO(invalid), stdout=io.BytesIO(),
                          workspace=tmp_path)


def test_standalone_cli_import_works_with_only_two_copied_modules(tmp_path):
    module_dir = tmp_path / "modules"
    module_dir.mkdir()
    source = Path(transfer.__file__)
    for name in ("container_transfer.py", "container_transfer_support.py"):
        shutil.copy2(source.with_name(name), module_dir / name)
    manifest = _manifest(_entry("context", kind="directory", mode=0o700),
                         _entry("repo", kind="directory", mode=0o700),
                         _entry("repo/file", b"hello"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = ("import sys; from pathlib import Path; "
              "sys.path.insert(0, sys.argv[1]); import container_transfer; "
              "container_transfer.main([sys.argv[3]], workspace=Path(sys.argv[2]))")
    result = subprocess.run([sys.executable, "-c", script, str(module_dir), str(workspace), "import"],
                            input=_cli_import_wire(manifest, _frame(b"hello")),
                            capture_output=True)
    assert result.returncode == 0, result.stderr
    digest = hashlib.sha256(canonical_json(asdict(IDENTITY))).hexdigest()
    assert (workspace / "inputs" / digest / "published/repo/file").read_bytes() == b"hello"
    assert result.stdout == hashlib.sha256(encode_manifest(manifest)).hexdigest().encode() + b"\n"
    identity_bytes = canonical_json(asdict(IDENTITY))
    request = b"FANOUT-XFER-1\n" + len(identity_bytes).to_bytes(4, "big") + identity_bytes
    exported = subprocess.run([sys.executable, "-c", script, str(module_dir), str(workspace), "export"],
                              input=request, capture_output=True)
    assert exported.returncode == 0, exported.stderr
    assert exported.stdout.startswith(b"FANOUT-XFER-1\n")
    assert exported.stdout.endswith(_frame(b"hello"))


def test_shared_reexports_preserve_canonical_bytes_and_no_replace(tmp_path):
    assert transfer.canonical_json is canonical_json is support.canonical_json
    assert transfer._rename_no_replace is lifecycle._rename_no_replace is support._rename_no_replace
    assert canonical_json({"z": 2, "å": "värde"}) == b'{"z":2,"\xc3\xa5":"v\xc3\xa4rde"}\n'
    (tmp_path / "source").write_bytes(b"source")
    (tmp_path / "destination").write_bytes(b"destination")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert not lifecycle._rename_no_replace(parent_fd, "source", parent_fd, "destination")
    finally:
        os.close(parent_fd)
    assert (tmp_path / "source").read_bytes() == b"source"
    assert (tmp_path / "destination").read_bytes() == b"destination"


def test_surrogate_transfer_name_and_identity_fail_as_transfer_rejections():
    with pytest.raises(TransferRejected):
        transfer._parts("bad-\udcff")
    with pytest.raises(TransferRejected):
        transfer._validate_identity(replace(IDENTITY, run_id="bad-\udcff"))


@pytest.mark.parametrize("operation", ["snapshot", "seal"])
def test_swapped_fifo_cannot_block_regular_file_open(tmp_path, operation):
    root = tmp_path / "repo"
    root.mkdir(mode=0o700)
    script = """
import hashlib
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from shared.lib.fanout import container_transfer as transfer
root = Path(sys.argv[2])
(root / 'item').write_bytes(b'payload')
os.mkfifo(root / 'replacement')
identity = transfer.TransferIdentity('run', 'task', 'target', 'seat', 'a' * 64)
manifest = transfer.TransferManifest(1, identity, (
    transfer.TransferEntry('item', 'file', 0o644, hashlib.sha256(b'payload').hexdigest(), 7),
))
original_open = os.open
swapped = False
def swap_before_open(name, flags, *args, **kwargs):
    global swapped
    if name == 'item' and flags & os.O_NOFOLLOW and not swapped:
        os.rename(root / 'item', root / 'old')
        os.rename(root / 'replacement', root / 'item')
        swapped = True
    return original_open(name, flags, *args, **kwargs)
transfer.os.open = swap_before_open
try:
    if sys.argv[3] == 'snapshot':
        transfer.snapshot_manifest(root, identity)
    else:
        list(transfer.seal_transfer(manifest, root))
except transfer.TransferRejected:
    raise SystemExit(0)
raise SystemExit(3)
"""
    result = subprocess.run([sys.executable, "-c", script,
                             str(Path(__file__).resolve().parents[1]), str(root), operation],
                            capture_output=True, timeout=2)
    assert result.returncode == 0, result.stderr


def test_snapshot_caps_directory_names_before_sorting(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir(mode=0o700)
    seen = 0

    class Name(str):
        @property
        def name(self):
            return str(self)

    class Listing:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            nonlocal seen
            for index in range(transfer.MAX_ENTRIES + 2):
                seen += 1
                if seen > transfer.MAX_ENTRIES + 1:
                    raise AssertionError("directory enumeration exceeded the entry budget")
                yield Name(f"entry-{index}")

    with monkeypatch.context() as patch:
        patch.setattr(transfer.os, "listdir", lambda _fd: iter(Listing()))
        patch.setattr(transfer.os, "scandir", lambda _fd: Listing())
        with pytest.raises(TransferRejected, match="entry"):
            transfer.snapshot_manifest(root, IDENTITY)
    assert seen == transfer.MAX_ENTRIES + 1


def test_identity_root_rejects_sibling_without_materializing_all_names(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    inputs.mkdir(mode=0o700)
    digest = hashlib.sha256(canonical_json(asdict(IDENTITY))).hexdigest()
    (inputs / digest).mkdir(mode=0o700)
    (inputs / "sibling").mkdir(mode=0o700)

    def unbounded_listdir(_fd):
        raise MemoryError("directory was materialized")

    with monkeypatch.context() as patch:
        patch.setattr(transfer.os, "listdir", unbounded_listdir)
        with pytest.raises(TransferRejected, match="another identity"):
            transfer._identity_root(tmp_path, IDENTITY, create=False)


def test_cli_export_accepts_maximum_valid_unicode_identity_request(tmp_path):
    identity = TransferIdentity("😀" * 256, "😀" * 256,
                                "😀" * 256, "😀" * 256, "a" * 64)
    manifest = _manifest(_entry("context", kind="directory", mode=0o700),
                         _entry("repo", kind="directory", mode=0o700),
                         _entry("repo/item", b"payload"), identity=identity)
    transfer.main(["import"], stdin=io.BytesIO(_cli_import_wire(manifest, _frame(b"payload"))),
                  stdout=io.BytesIO(), workspace=tmp_path)
    identity_bytes = canonical_json(asdict(identity))
    assert 2048 < len(identity_bytes) < 8192
    request = b"FANOUT-XFER-1\n" + len(identity_bytes).to_bytes(4, "big") + identity_bytes
    output = io.BytesIO()
    assert transfer.main(["export"], stdin=io.BytesIO(request), stdout=output,
                         workspace=tmp_path) == 0
    assert output.getvalue().endswith(_frame(b"payload"))


def test_dockerfile_copies_exact_two_file_transfer_context():
    dockerfile = (Path(__file__).resolve().parents[1] /
                  "components/fanout-container/Dockerfile").read_text()
    assert "COPY --from=fanout_transfer container_transfer.py container_transfer_support.py /opt/fanout/" in dockerfile.splitlines()
