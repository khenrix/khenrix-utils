"""Bounded, target-bound file transfer for isolated fanout seats.

Manifests and file content use nonempty 4-byte big-endian length-prefixed
frames. A frame holds at most 64 KiB; content frames never cross entries.
The manifest is canonical JSON split into frames and passed separately from
the content iterator. The controller selects the private import root; a
successful import publishes exactly one ``published`` directory beneath it.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import unicodedata
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

if __package__:
    from .container_transfer_support import canonical_json, _rename_no_replace
else:
    from container_transfer_support import canonical_json, _rename_no_replace


MAX_FRAME = 64 * 1024
MAX_MANIFEST = 32 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024
MAX_ENTRIES = 100_000
_MAX_LINK = 4096
_MAX_DEPTH = 128
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[^\x00-\x1f\x7f]{1,256}\Z")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class TransferRejected(ValueError):
    """An untrusted transfer fails its identity, shape, or content checks."""


@dataclass(frozen=True, slots=True)
class TransferIdentity:
    run_id: str
    task_id: str
    target_id: str
    seat_id: str
    inputs_digest: str


@dataclass(frozen=True, slots=True)
class TransferEntry:
    path: str
    kind: str
    mode: int
    sha256: str
    length: int


@dataclass(frozen=True, slots=True)
class TransferManifest:
    schema_version: int
    identity: TransferIdentity
    entries: tuple[TransferEntry, ...]


def _frame(payload: bytes) -> bytes:
    if not 0 < len(payload) <= MAX_FRAME:
        raise TransferRejected("frame size is invalid")
    return len(payload).to_bytes(4, "big") + payload


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    parts: list[bytes] = []
    remaining = length
    while remaining:
        part = stream.read(remaining)
        if not part:
            raise TransferRejected("truncated frame")
        if not isinstance(part, bytes) or len(part) > remaining:
            raise TransferRejected("frame stream returned non-byte content")
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


def read_frame(stream: BinaryIO) -> bytes:
    """Read exactly one bounded frame from a byte stream."""
    length = int.from_bytes(_read_exact(stream, 4), "big")
    if not 0 < length <= MAX_FRAME:
        raise TransferRejected("frame size is invalid")
    return _read_exact(stream, length)


def _parts(path: str) -> tuple[str, ...]:
    if (not isinstance(path, str) or not path or path.startswith("/")
            or "\\" in path or "\x00" in path
            or unicodedata.normalize("NFC", path) != path):
        raise TransferRejected("transfer path is not canonical and relative")
    parts = tuple(path.split("/"))
    if len(parts) > _MAX_DEPTH:
        raise TransferRejected("transfer path depth ceiling exceeded")
    if any(part in {"", ".", ".."} or part.casefold() == ".git" for part in parts):
        raise TransferRejected("transfer path escapes or names Git metadata")
    try:
        if len(path.encode("utf-8")) > 4096 or any(len(part.encode("utf-8")) > 255 for part in parts):
            raise TransferRejected("transfer path byte ceiling exceeded")
    except UnicodeEncodeError as error:
        raise TransferRejected("transfer path is not UTF-8") from error
    return parts


def _validate_identity(identity: TransferIdentity) -> None:
    if not isinstance(identity, TransferIdentity):
        raise TransferRejected("transfer identity is invalid")
    for field in (identity.run_id, identity.task_id, identity.target_id, identity.seat_id):
        if not isinstance(field, str) or _IDENTIFIER.fullmatch(field) is None:
            raise TransferRejected("transfer identity is invalid")
        try:
            field.encode("utf-8")
        except UnicodeEncodeError as error:
            raise TransferRejected("transfer identity is not UTF-8") from error
    if (not isinstance(identity.inputs_digest, str)
            or _SHA256.fullmatch(identity.inputs_digest) is None):
        raise TransferRejected("transfer inputs digest is invalid")


def _validate_manifest(manifest: TransferManifest,
                       expected: TransferIdentity | None = None) -> None:
    if (not isinstance(manifest, TransferManifest)
            or type(manifest.schema_version) is not int or manifest.schema_version != 1
            or not isinstance(manifest.entries, tuple)):
        raise TransferRejected("transfer manifest schema is invalid")
    _validate_identity(manifest.identity)
    if expected is not None:
        _validate_identity(expected)
        if manifest.identity != expected:
            raise TransferRejected("transfer identity differs from the controller target")
    if len(manifest.entries) > MAX_ENTRIES:
        raise TransferRejected("transfer entry ceiling exceeded")

    seen: set[str] = set()
    directories: set[str] = set()
    preceding = ""
    total = 0
    for entry in manifest.entries:
        if not isinstance(entry, TransferEntry):
            raise TransferRejected("transfer entry is invalid")
        parts = _parts(entry.path)
        if preceding and entry.path < preceding:
            raise TransferRejected("transfer entries are not in canonical path order")
        preceding = entry.path
        folded = unicodedata.normalize("NFC", entry.path).casefold()
        if folded in seen:
            raise TransferRejected("duplicate or case-folded transfer name")
        seen.add(folded)
        if len(parts) > 1 and "/".join(parts[:-1]) not in directories:
            raise TransferRejected("transfer parent is not a declared directory")
        if (type(entry.mode) is not int or type(entry.length) is not int
                or not isinstance(entry.sha256, str)
                or _SHA256.fullmatch(entry.sha256) is None
                or not 0 <= entry.mode <= 0o777):
            raise TransferRejected("transfer entry mode, length, or digest is invalid")
        if entry.kind == "directory":
            if ((entry.mode & 0o700) != 0o700 or entry.mode & 0o022
                    or entry.length != 0 or entry.sha256 != _EMPTY_SHA256):
                raise TransferRejected("transfer directory metadata is invalid")
            directories.add(entry.path)
        elif entry.kind == "file":
            if (not entry.mode & 0o400 or entry.mode & 0o022
                    or not 0 <= entry.length <= MAX_FILE):
                raise TransferRejected("transfer file mode or size is invalid")
        elif entry.kind == "symlink":
            if entry.mode != 0o777 or not 0 < entry.length <= _MAX_LINK:
                raise TransferRejected("transfer symlink metadata is invalid")
        else:
            raise TransferRejected("transfer entry kind is not allowed")
        total += entry.length
        if total > MAX_TOTAL:
            raise TransferRejected("transfer total byte ceiling exceeded")


def encode_manifest(manifest: TransferManifest) -> bytes:
    """Encode a validated canonical manifest as bounded frames."""
    _validate_manifest(manifest)
    payload = canonical_json(asdict(manifest))
    if len(payload) > MAX_MANIFEST:
        raise TransferRejected("transfer manifest byte ceiling exceeded")
    return b"".join(_frame(payload[index:index + MAX_FRAME])
                    for index in range(0, len(payload), MAX_FRAME))


def _decode_manifest(wire: bytes, expected: TransferIdentity | None) -> TransferManifest:
    if (not isinstance(wire, bytes) or not wire
            or len(wire) > MAX_MANIFEST + 4 * ((MAX_MANIFEST + MAX_FRAME - 1) // MAX_FRAME)):
        raise TransferRejected("transfer manifest byte ceiling exceeded")
    payload = bytearray()
    offset = 0
    while offset < len(wire):
        if len(wire) - offset < 4:
            raise TransferRejected("truncated manifest frame")
        length = int.from_bytes(wire[offset:offset + 4], "big")
        offset += 4
        if not 0 < length <= MAX_FRAME:
            raise TransferRejected("manifest frame size is invalid")
        if length > len(wire) - offset:
            raise TransferRejected("truncated manifest frame")
        if offset + length < len(wire) and length != MAX_FRAME:
            raise TransferRejected("nonfinal manifest frame is short")
        if len(payload) + length > MAX_MANIFEST:
            raise TransferRejected("transfer manifest byte ceiling exceeded")
        payload.extend(wire[offset:offset + length])
        offset += length
    raw = bytes(payload)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError,
            ValueError, RecursionError) as error:
        raise TransferRejected("transfer manifest JSON is invalid") from error
    try:
        canonical = canonical_json(value)
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise TransferRejected("transfer manifest JSON is invalid") from error
    if not isinstance(value, dict) or canonical != raw or set(value) != {
        "schema_version", "identity", "entries",
    }:
        raise TransferRejected("transfer manifest is not canonical")
    identity = value["identity"]
    entries = value["entries"]
    if (not isinstance(identity, dict) or set(identity) != {
            "run_id", "task_id", "target_id", "seat_id", "inputs_digest",
        } or not isinstance(entries, list) or len(entries) > MAX_ENTRIES):
        raise TransferRejected("transfer identity or entry ceiling is invalid")
    if any(not isinstance(entry, dict) or set(entry) != {
            "path", "kind", "mode", "sha256", "length",
        } for entry in entries):
        raise TransferRejected("transfer entry shape is invalid")
    manifest = TransferManifest(
        value["schema_version"], TransferIdentity(**identity),
        tuple(TransferEntry(**entry) for entry in entries),
    )
    _validate_manifest(manifest, expected)
    return manifest


def _open_root(root: Path) -> int:
    path = Path(root)
    if not path.is_absolute():
        path = Path.cwd() / path
    components = path.parts[1:]
    if any(part in {"", ".", ".."} for part in components):
        raise TransferRejected("transfer root is not a canonical directory")
    try:
        fd = os.open("/", _DIRECTORY_FLAGS)
        try:
            for component in components:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise
    except OSError as error:
        raise TransferRejected("transfer root is not a contained directory") from error


def _open_parent(root_fd: int, parts: tuple[str, ...]) -> int:
    fd = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _link_target(path: str, payload: bytes) -> str:
    try:
        target = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise TransferRejected("transfer symlink target is not UTF-8") from error
    if not target or target.startswith("/") or "\\" in target or "\x00" in target:
        raise TransferRejected("transfer symlink escapes its root")
    resolved = list(path.split("/")[:-1])
    for component in target.split("/"):
        if component in {"", "."}:
            raise TransferRejected("transfer symlink target is not canonical")
        if component == "..":
            if not resolved:
                raise TransferRejected("transfer symlink escapes its root")
            resolved.pop()
        else:
            if component.casefold() == ".git":
                raise TransferRejected("transfer symlink names Git metadata")
            resolved.append(component)
    if not resolved:
        raise TransferRejected("transfer symlink escapes its root")
    return target


def _validate_link_graph(entries: tuple[TransferEntry, ...], links: dict[str, str]) -> None:
    """Resolve every declared link through other links before exposing the tree."""
    kinds = {entry.path: entry.kind for entry in entries}
    if {path for path, kind in kinds.items() if kind == "symlink"} != set(links):
        raise TransferRejected("transfer symlink graph is incomplete")
    for path, target in links.items():
        resolved = list(path.split("/")[:-1])
        pending = deque(target.split("/"))
        hops = 0
        while pending:
            component = pending.popleft()
            if component == "..":
                if not resolved:
                    raise TransferRejected("transfer symlink graph escapes its root")
                resolved.pop()
                continue
            resolved.append(component)
            current = "/".join(resolved)
            kind = kinds.get(current)
            if kind == "symlink":
                hops += 1
                if hops > 40:
                    raise TransferRejected("transfer symlink graph has a cycle or excessive depth")
                resolved.pop()
                pending.extendleft(reversed(links[current].split("/")))
            elif pending and kind != "directory":
                raise TransferRejected("transfer symlink traverses an undeclared directory")
        if not resolved or "/".join(resolved) not in kinds:
            raise TransferRejected("transfer symlink target is not declared")


def _source_payload(root_fd: int, entry: TransferEntry) -> Iterator[bytes]:
    parts = _parts(entry.path)
    parent_fd = _open_parent(root_fd, parts)
    try:
        if entry.kind == "directory":
            fd = os.open(parts[-1], _DIRECTORY_FLAGS, dir_fd=parent_fd)
            try:
                info = os.fstat(fd)
                if stat.S_IMODE(info.st_mode) != entry.mode:
                    raise TransferRejected("source directory mode differs from manifest")
            finally:
                os.close(fd)
            return
        if entry.kind == "symlink":
            before = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISLNK(before.st_mode):
                raise TransferRejected("source symlink kind differs from manifest")
            if stat.S_IMODE(before.st_mode) != entry.mode:
                raise TransferRejected("source symlink mode differs from manifest")
            if before.st_size != entry.length:
                raise TransferRejected("source symlink length differs from manifest")
            try:
                target = os.readlink(parts[-1], dir_fd=parent_fd)
                payload = target.encode("utf-8", "strict")
            except UnicodeEncodeError as error:
                raise TransferRejected("source symlink target is not UTF-8") from error
            after = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise TransferRejected("source symlink changed during transfer")
            if stat.S_IMODE(after.st_mode) != entry.mode:
                raise TransferRejected("source symlink mode differs from manifest")
            _link_target(entry.path, payload)
            if len(payload) != entry.length or hashlib.sha256(payload).hexdigest() != entry.sha256:
                raise TransferRejected("source symlink digest differs from manifest")
            yield payload
            return
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent_fd)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise TransferRejected("source file kind differs from manifest")
            if before.st_nlink != 1:
                raise TransferRejected("source hardlink is not transferable")
            if stat.S_IMODE(before.st_mode) != entry.mode:
                raise TransferRejected("source file mode differs from manifest")
            if before.st_size != entry.length:
                raise TransferRejected("source file length differs from manifest")
            total = 0
            digest = hashlib.sha256()
            while total < entry.length:
                wanted = min(MAX_FRAME, entry.length - total)
                pieces: list[bytes] = []
                collected = 0
                while collected < wanted:
                    part = os.read(fd, wanted - collected)
                    if not part:
                        raise TransferRejected("source file length changed during transfer")
                    pieces.append(part)
                    collected += len(part)
                chunk = b"".join(pieces)
                total += len(chunk)
                digest.update(chunk)
                yield chunk
            if os.read(fd, 1):
                raise TransferRejected("source file length changed during transfer")
            after = os.fstat(fd)
            if (total != entry.length or digest.hexdigest() != entry.sha256
                    or after.st_size != entry.length or after.st_nlink != 1
                    or after.st_mtime_ns != before.st_mtime_ns):
                raise TransferRejected("source file digest or metadata differs from manifest")
        finally:
            os.close(fd)
    except OSError as error:
        raise TransferRejected("source entry is unavailable or unsafe") from error
    finally:
        os.close(parent_fd)


def seal_transfer(manifest: TransferManifest, source_root: Path) -> Iterator[bytes]:
    """Yield bounded content frames from no-follow, digest-checked source entries."""
    _validate_manifest(manifest)
    root_fd = _open_root(source_root)
    try:
        links = {
            entry.path: b"".join(_source_payload(root_fd, entry)).decode("utf-8")
            for entry in manifest.entries if entry.kind == "symlink"
        }
        _validate_link_graph(manifest.entries, links)
        for entry in manifest.entries:
            for chunk in _source_payload(root_fd, entry):
                yield _frame(chunk)
    finally:
        os.close(root_fd)


def stage_export(root: Path, expected: TransferIdentity,
                 manifest: TransferManifest) -> tuple[bytes, Iterator[bytes]]:
    """Reopen and rehash each candidate, then return its bounded wire stream."""
    _validate_manifest(manifest, expected)
    encoded = encode_manifest(manifest)
    root_fd = _open_root(root)
    try:
        links: dict[str, str] = {}
        for entry in manifest.entries:
            for payload in _source_payload(root_fd, entry):
                if entry.kind == "symlink":
                    links[entry.path] = payload.decode("utf-8")
        _validate_link_graph(manifest.entries, links)
    finally:
        os.close(root_fd)
    return encoded, seal_transfer(manifest, root)


def _content_frame(wire: bytes) -> bytes:
    if not isinstance(wire, bytes) or len(wire) < 5:
        raise TransferRejected("content frame is truncated")
    length = int.from_bytes(wire[:4], "big")
    if not 0 < length <= MAX_FRAME or length != len(wire) - 4:
        raise TransferRejected("content frame size is invalid")
    return wire[4:]


def _next_content(frames: Iterator[bytes], remaining: int) -> bytes:
    try:
        payload = _content_frame(next(frames))
    except StopIteration as error:
        raise TransferRejected("content frame is missing") from error
    if len(payload) != min(MAX_FRAME, remaining):
        raise TransferRejected("content frame is short or crosses an entry boundary")
    return payload


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if not written:
            raise TransferRejected("transfer write made no progress")
        view = view[written:]


def _remove_tree(parent_fd: int, name: str) -> None:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        for child in os.listdir(fd):
            _remove_tree(fd, child)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def stage_import(root: Path, expected: TransferIdentity, manifest: bytes,
                 chunks: Iterable[bytes]) -> Path:
    """Validate and atomically publish one transfer under a private root."""
    parsed = _decode_manifest(manifest, expected)
    root_fd = _open_root(root)
    lock_fd = -1
    stage_name: str | None = None
    try:
        root_info = os.fstat(root_fd)
        if root_info.st_uid != os.getuid() or stat.S_IMODE(root_info.st_mode) & 0o077:
            raise TransferRejected("transfer import root is not owner-private")
        lock_fd = os.open(".transfer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                          0o600, dir_fd=root_fd)
        lock_info = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                or lock_info.st_nlink != 1 or stat.S_IMODE(lock_info.st_mode) & 0o077):
            raise TransferRejected("transfer lock is not owner-private")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            os.stat("published", dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise TransferRejected("transfer destination already exists")

        new_stage_name = ".fanout-transfer-" + secrets.token_hex(16)
        os.mkdir(new_stage_name, mode=0o700, dir_fd=root_fd)
        stage_name = new_stage_name
        stage_fd = os.open(stage_name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        try:
            frames = iter(chunks)
            directory_modes: list[tuple[tuple[str, ...], int]] = []
            links: dict[str, str] = {}
            for entry in parsed.entries:
                parts = _parts(entry.path)
                parent_fd = _open_parent(stage_fd, parts)
                try:
                    if entry.kind == "directory":
                        os.mkdir(parts[-1], mode=0o700, dir_fd=parent_fd)
                        directory_modes.append((parts, entry.mode))
                        continue
                    remaining = entry.length
                    digest = hashlib.sha256()
                    if entry.kind == "file":
                        output_fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                            os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
                        try:
                            while remaining:
                                payload = _next_content(frames, remaining)
                                _write_all(output_fd, payload)
                                digest.update(payload)
                                remaining -= len(payload)
                            if digest.hexdigest() != entry.sha256:
                                raise TransferRejected("content digest differs from manifest")
                            os.fchmod(output_fd, entry.mode)
                            os.fsync(output_fd)
                        finally:
                            os.close(output_fd)
                    else:
                        payload_parts: list[bytes] = []
                        while remaining:
                            payload = _next_content(frames, remaining)
                            payload_parts.append(payload)
                            digest.update(payload)
                            remaining -= len(payload)
                        if digest.hexdigest() != entry.sha256:
                            raise TransferRejected("content digest differs from manifest")
                        target = _link_target(entry.path, b"".join(payload_parts))
                        os.symlink(target, parts[-1], dir_fd=parent_fd)
                        link_info = os.stat(parts[-1], dir_fd=parent_fd,
                                            follow_symlinks=False)
                        if stat.S_IMODE(link_info.st_mode) != entry.mode:
                            try:
                                os.chmod(parts[-1], entry.mode, dir_fd=parent_fd,
                                         follow_symlinks=False)
                            except (OSError, NotImplementedError, ValueError) as error:
                                raise TransferRejected("transfer symlink mode cannot be set") from error
                            link_info = os.stat(parts[-1], dir_fd=parent_fd,
                                                follow_symlinks=False)
                        if (not stat.S_ISLNK(link_info.st_mode)
                                or stat.S_IMODE(link_info.st_mode) != entry.mode):
                            raise TransferRejected("transfer symlink mode differs from manifest")
                        links[entry.path] = target
                finally:
                    os.close(parent_fd)
            try:
                next(frames)
            except StopIteration:
                pass
            else:
                raise TransferRejected("transfer content has trailing frames")
            _validate_link_graph(parsed.entries, links)
            for parts, mode in reversed(directory_modes):
                directory_fd = _open_parent(stage_fd, parts)
                try:
                    fd = os.open(parts[-1], _DIRECTORY_FLAGS, dir_fd=directory_fd)
                    try:
                        os.fchmod(fd, mode)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                finally:
                    os.close(directory_fd)
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        try:
            os.stat("published", dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise TransferRejected("transfer destination already exists")
        if not _rename_no_replace(root_fd, stage_name, root_fd, "published"):
            raise TransferRejected("transfer destination exists or no-replace rename is unavailable")
        stage_name = None
        return Path(root) / "published"
    except OSError as error:
        raise TransferRejected("transfer staging failed") from error
    finally:
        if stage_name is not None:
            _remove_tree(root_fd, stage_name)
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        os.close(root_fd)


def snapshot_manifest(root: Path, identity: TransferIdentity, *,
                      max_file_bytes: int | None = None,
                      max_total_bytes: int | None = None) -> TransferManifest:
    """Snapshot a source tree through no-follow descriptors and bounded reads."""
    _validate_identity(identity)
    for ceiling in (max_file_bytes, max_total_bytes):
        if ceiling is not None and (type(ceiling) is not int or ceiling < 0):
            raise TransferRejected("snapshot byte ceiling is invalid")
    file_ceiling = min(MAX_FILE, MAX_FILE if max_file_bytes is None else max_file_bytes)
    total_ceiling = min(MAX_TOTAL, MAX_TOTAL if max_total_bytes is None else max_total_bytes)
    entries: list[TransferEntry] = []
    links: dict[str, str] = {}
    total = 0
    root_fd = _open_root(root)
    try:
        def scan(directory_fd: int, prefix: str = "") -> None:
            nonlocal total
            before_dir = os.fstat(directory_fd)
            names: list[str] = []
            with os.scandir(directory_fd) as listing:
                for item in listing:
                    if len(names) >= MAX_ENTRIES - len(entries):
                        raise TransferRejected("transfer entry ceiling exceeded")
                    names.append(item.name)
            for name in sorted(names):
                path = prefix + name
                _parts(path)
                if len(entries) >= MAX_ENTRIES:
                    raise TransferRejected("transfer entry ceiling exceeded")
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                mode = stat.S_IMODE(before.st_mode)
                if stat.S_ISDIR(before.st_mode):
                    child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                    try:
                        current = os.fstat(child_fd)
                        if (not stat.S_ISDIR(current.st_mode)
                                or (current.st_dev, current.st_ino, current.st_mode) !=
                                (before.st_dev, before.st_ino, before.st_mode)):
                            raise TransferRejected("source directory changed during scan")
                        if mode & 0o700 != 0o700 or mode & 0o022:
                            raise TransferRejected("source directory mode is not transferable")
                        entries.append(TransferEntry(path, "directory", mode, _EMPTY_SHA256, 0))
                        scan(child_fd, path + "/")
                    finally:
                        os.close(child_fd)
                elif stat.S_ISLNK(before.st_mode):
                    if mode != 0o777:
                        raise TransferRejected("source symlink mode is not transferable")
                    if before.st_size > min(_MAX_LINK, file_ceiling):
                        raise TransferRejected("source symlink size ceiling exceeded")
                    if before.st_size > total_ceiling - total:
                        raise TransferRejected("transfer total byte ceiling exceeded")
                    target = os.readlink(name, dir_fd=directory_fd)
                    try:
                        payload = target.encode("utf-8", "strict")
                    except UnicodeEncodeError as error:
                        raise TransferRejected("source symlink target is not UTF-8") from error
                    _link_target(path, payload)
                    if len(payload) > min(file_ceiling, total_ceiling - total):
                        raise TransferRejected("source symlink byte ceiling exceeded")
                    after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
                            after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
                        raise TransferRejected("source symlink changed during scan")
                    links[path] = target
                    entries.append(TransferEntry(path, "symlink", mode,
                                                 hashlib.sha256(payload).hexdigest(), len(payload)))
                    total += len(payload)
                elif stat.S_ISREG(before.st_mode):
                    if before.st_nlink != 1:
                        raise TransferRejected("source hardlink is not transferable")
                    if not mode & 0o400 or mode & 0o022:
                        raise TransferRejected("source file mode is not transferable")
                    if before.st_size > file_ceiling:
                        raise TransferRejected("source file size ceiling exceeded")
                    if before.st_size > total_ceiling - total:
                        raise TransferRejected("transfer total byte ceiling exceeded")
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory_fd)
                    try:
                        opened = os.fstat(fd)
                        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                                or (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size,
                                    opened.st_mtime_ns, opened.st_ctime_ns) !=
                                (before.st_dev, before.st_ino, before.st_mode, before.st_size,
                                 before.st_mtime_ns, before.st_ctime_ns)):
                            raise TransferRejected("source file changed during scan")
                        if opened.st_size > min(file_ceiling, total_ceiling - total):
                            raise TransferRejected("source file byte ceiling exceeded")
                        digest = hashlib.sha256()
                        length = 0
                        while length < opened.st_size:
                            chunk = os.read(fd, min(MAX_FRAME, opened.st_size - length))
                            if not chunk:
                                raise TransferRejected("source file changed during scan")
                            digest.update(chunk)
                            length += len(chunk)
                        if os.read(fd, 1):
                            raise TransferRejected("source file changed during scan")
                        after = os.fstat(fd)
                        if (length != opened.st_size or after.st_nlink != 1
                                or (opened.st_dev, opened.st_ino, opened.st_size,
                                    opened.st_mtime_ns, opened.st_ctime_ns, mode) !=
                                (after.st_dev, after.st_ino, after.st_size,
                                 after.st_mtime_ns, after.st_ctime_ns,
                                 stat.S_IMODE(after.st_mode))):
                            raise TransferRejected("source file changed during scan")
                    finally:
                        os.close(fd)
                    entries.append(TransferEntry(path, "file", mode, digest.hexdigest(), length))
                    total += length
                else:
                    raise TransferRejected("source entry kind is not transferable")
                if total > total_ceiling:
                    raise TransferRejected("transfer total byte ceiling exceeded")
            after_dir = os.fstat(directory_fd)
            if (before_dir.st_dev, before_dir.st_ino, before_dir.st_mtime_ns,
                    before_dir.st_ctime_ns) != (after_dir.st_dev, after_dir.st_ino,
                                               after_dir.st_mtime_ns, after_dir.st_ctime_ns):
                raise TransferRejected("source directory changed during scan")

        scan(root_fd)
    except OSError as error:
        raise TransferRejected("source tree is unavailable or unsafe") from error
    finally:
        os.close(root_fd)
    manifest = TransferManifest(1, identity, tuple(sorted(entries, key=lambda entry: entry.path)))
    _validate_manifest(manifest, identity)
    _validate_link_graph(manifest.entries, links)
    return manifest


_MAGIC = b"FANOUT-XFER-1\n"
_MAX_MANIFEST_WIRE = MAX_MANIFEST + 4 * ((MAX_MANIFEST + MAX_FRAME - 1) // MAX_FRAME)
_MAX_IDENTITY_REQUEST = 8192


def _read_length(stream: BinaryIO, ceiling: int) -> int:
    length = int.from_bytes(_read_exact(stream, 4), "big")
    if not 0 < length <= ceiling:
        raise TransferRejected("transfer wire length is invalid")
    return length


def _identity_root(workspace: Path, identity: TransferIdentity, *, create: bool) -> Path:
    """Resolve the one digest-named workspace without following directory links."""
    digest = hashlib.sha256(canonical_json(asdict(identity))).hexdigest()
    workspace_fd = _open_root(workspace)
    try:
        try:
            if create:
                os.mkdir("inputs", 0o700, dir_fd=workspace_fd)
        except FileExistsError:
            pass
        inputs_fd = os.open("inputs", _DIRECTORY_FLAGS, dir_fd=workspace_fd)
        try:
            info = os.fstat(inputs_fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise TransferRejected("transfer inputs root is not owner-private")
            with os.scandir(inputs_fd) as listing:
                for index, item in enumerate(listing):
                    if index > 0 or item.name != digest:
                        raise TransferRejected("transfer workspace belongs to another identity")
            try:
                if create:
                    os.mkdir(digest, 0o700, dir_fd=inputs_fd)
            except FileExistsError:
                pass
            root_fd = os.open(digest, _DIRECTORY_FLAGS, dir_fd=inputs_fd)
            try:
                root_info = os.fstat(root_fd)
                if root_info.st_uid != os.getuid() or stat.S_IMODE(root_info.st_mode) & 0o077:
                    raise TransferRejected("transfer identity root is not owner-private")
            finally:
                os.close(root_fd)
        finally:
            os.close(inputs_fd)
    except OSError as error:
        raise TransferRejected("transfer identity root is unavailable") from error
    finally:
        os.close(workspace_fd)
    return Path(workspace) / "inputs" / digest


def _import_content(stream: BinaryIO, manifest: TransferManifest) -> Iterator[bytes]:
    for entry in manifest.entries:
        if entry.kind == "directory":
            continue
        remaining = entry.length
        while remaining:
            payload = read_frame(stream)
            remaining -= len(payload)
            yield _frame(payload)
    if stream.read(1):
        raise TransferRejected("transfer content has trailing bytes")


def _read_identity_request(stream: BinaryIO) -> TransferIdentity:
    if _read_exact(stream, len(_MAGIC)) != _MAGIC:
        raise TransferRejected("transfer request magic is invalid")
    raw = _read_exact(stream, _read_length(stream, _MAX_IDENTITY_REQUEST))
    if stream.read(1):
        raise TransferRejected("transfer request has trailing bytes")
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {
                "run_id", "task_id", "target_id", "seat_id", "inputs_digest",
            } or canonical_json(value) != raw):
            raise TransferRejected("transfer identity request is not canonical")
        identity = TransferIdentity(**value)
        _validate_identity(identity)
        return identity
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise TransferRejected("transfer identity request is invalid") from error


def main(argv: list[str] | None = None, *, stdin: BinaryIO | None = None,
         stdout: BinaryIO | None = None, workspace: Path = Path("/workspace")) -> int:
    parser = argparse.ArgumentParser(description="Bounded fanout workspace transfer")
    parser.add_argument("command", choices=("import", "export"))
    command = parser.parse_args(argv).command
    source = stdin if stdin is not None else sys.stdin.buffer
    sink = stdout if stdout is not None else sys.stdout.buffer
    if command == "import":
        if _read_exact(source, len(_MAGIC)) != _MAGIC:
            raise TransferRejected("transfer import magic is invalid")
        wire = _read_exact(source, _read_length(source, _MAX_MANIFEST_WIRE))
        manifest = _decode_manifest(wire, None)
        roots = {entry.path.split("/", 1)[0] for entry in manifest.entries}
        if (roots != {"repo", "context"} or
                {entry.path: entry.kind for entry in manifest.entries}.get("repo") != "directory" or
                {entry.path: entry.kind for entry in manifest.entries}.get("context") != "directory"):
            raise TransferRejected("transfer import may publish only repo and context")
        root = _identity_root(workspace, manifest.identity, create=True)
        stage_import(root, manifest.identity, wire, _import_content(source, manifest))
        sink.write(hashlib.sha256(wire).hexdigest().encode("ascii") + b"\n")
    else:
        identity = _read_identity_request(source)
        root = _identity_root(workspace, identity, create=False) / "published" / "repo"
        manifest = snapshot_manifest(root, identity)
        wire, frames = stage_export(root, identity, manifest)
        sink.write(_MAGIC + len(wire).to_bytes(4, "big") + wire)
        for frame in frames:
            sink.write(frame)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TransferRejected as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error
