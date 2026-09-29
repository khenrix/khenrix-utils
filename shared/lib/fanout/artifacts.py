"""Content-addressed, contained, durable artifact storage."""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .errors import (
    ArtifactExistsError,
    ArtifactIntegrityError,
    ArtifactPathError,
    ArtifactQuotaError,
)


def canonical_json(value: Any) -> bytes:
    """Encode a JSON value in the one spelling used for durable artifacts."""
    return (json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """The name, byte count, and SHA-256 identity of one stored artifact."""

    path: str
    digest: str
    size: int


@dataclass(frozen=True, slots=True)
class ArtifactLimits:
    """Maximum bytes for one artifact and for the entire run root."""

    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_file_bytes < 0 or self.max_total_bytes < 0:
            raise ValueError("artifact limits must be non-negative")


class ArtifactStore:
    """Store immutable, digest-verified artifacts beneath one pinned run root."""

    _LOCK_NAME = ".artifact.lock"
    _TEMP_DIR = ".fanout-artifact-tmp"
    _TEMP_NAME = re.compile(r"[0-9]+\.[0-9a-f]{32}\.tmp\Z")
    _CHUNK_SIZE = 64 * 1024
    _LOCK_ATTEMPTS = 32

    def __init__(self, root: Path | str, *, limits: ArtifactLimits | None = None,
                 _create_root: bool = True) -> None:
        path = Path(root)
        self.limits = limits or ArtifactLimits()
        self.root = path if path.is_absolute() else Path.cwd() / path
        self._root_fd = self._open_or_create_root(path, create=_create_root)

    @classmethod
    def open_existing(cls, root: Path | str, *,
                      limits: ArtifactLimits | None = None) -> "ArtifactStore":
        """Pin a preexisting store without creating a missing path component."""
        return cls(root, limits=limits, _create_root=False)

    def close(self) -> None:
        """Release this store's pinned run-root descriptor."""
        fd = getattr(self, "_root_fd", None)
        if fd is not None:
            self._root_fd = None
            os.close(fd)

    def __enter__(self) -> "ArtifactStore":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    def write_bytes(self, path: str, data: bytes | bytearray | memoryview) -> ArtifactRef:
        """Publish bytes once at *path* and return their content-bound reference."""
        parts = self._parts(path)
        self._reject_reserved_path(parts)
        payload = bytes(data)
        self._check_file_quota(len(payload))
        digest = hashlib.sha256(payload).hexdigest()

        with self._write_lock() as root_fd:
            self._recover_orphans(root_fd)
            parent_fd = self._open_parent(root_fd, parts, create=True)
            try:
                self._assert_destination_missing(parent_fd, parts)
                total = self._stored_bytes(root_fd)
                if total + len(payload) > self.limits.max_total_bytes:
                    raise ArtifactQuotaError(
                        f"artifact total quota exceeded: {total + len(payload)} > "
                        f"{self.limits.max_total_bytes} bytes"
                    )
                self._publish_exclusive(root_fd, parent_fd, parts, payload)
            finally:
                os.close(parent_fd)
        return ArtifactRef(path="/".join(parts), digest=digest, size=len(payload))

    def write_json(self, path: str, value: Any) -> ArtifactRef:
        """Publish a canonical UTF-8 JSON artifact."""
        return self.write_bytes(path, canonical_json(value))

    def read_bytes(self, ref: ArtifactRef) -> bytes:
        """Read *ref* only when its exact stored bytes still verify."""
        if not isinstance(ref, ArtifactRef):
            raise TypeError("ref must be an ArtifactRef")
        parts = self._parts(ref.path)
        self._reject_reserved_path(parts)
        if len(ref.digest) != 64 or any(char not in "0123456789abcdef" for char in ref.digest):
            raise ArtifactIntegrityError("artifact reference has an invalid SHA-256 digest")
        if ref.size < 0:
            raise ArtifactIntegrityError("artifact reference has a negative size")

        root_fd = self._root_fd_copy()
        try:
            parent_fd = self._open_parent(root_fd, parts, create=False)
        finally:
            os.close(root_fd)
        try:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ArtifactPathError(f"artifact is not a regular contained file: {ref.path}") from error
                raise
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ArtifactPathError(f"artifact is not a regular file: {ref.path}")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(fd, self._CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.limits.max_file_bytes:
                        raise ArtifactQuotaError(
                            f"artifact file quota exceeded while reading {ref.path}: "
                            f"{total} > {self.limits.max_file_bytes} bytes"
                        )
                    chunks.append(chunk)
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

        data = b"".join(chunks)
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != ref.size or digest != ref.digest:
            raise ArtifactIntegrityError(f"artifact digest mismatch: {ref.path}")
        return data

    def read_json(self, ref: ArtifactRef) -> Any:
        """Read a verified UTF-8 JSON artifact."""
        return json.loads(self.read_bytes(ref).decode("utf-8"))

    def read_by_digest(self, path: str, digest: str) -> bytes:
        """Resolve a deterministic content-addressed path without trusting caller size."""
        parts = self._parts(path)
        self._reject_reserved_path(parts)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ArtifactIntegrityError("artifact digest is invalid")
        root_fd = self._root_fd_copy()
        try:
            parent_fd = self._open_parent(root_fd, parts, create=False)
        finally:
            os.close(root_fd)
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ArtifactPathError(f"artifact is not a regular contained file: {path}")
                size = info.st_size
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)
        return self.read_bytes(ArtifactRef(path, digest, size))

    def _check_file_quota(self, size: int) -> None:
        if size > self.limits.max_file_bytes:
            raise ArtifactQuotaError(
                f"artifact file quota exceeded: {size} > {self.limits.max_file_bytes} bytes"
            )

    @staticmethod
    def _parts(path: str) -> tuple[str, ...]:
        if not isinstance(path, str) or not path or "\\" in path:
            raise ArtifactPathError("artifact path must be a non-empty relative POSIX path")
        pure = PurePosixPath(path)
        if not pure.parts or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise ArtifactPathError(f"artifact path escapes its root: {path!r}")
        return pure.parts

    def _reject_reserved_path(self, parts: tuple[str, ...]) -> None:
        if parts[0] == self._TEMP_DIR:
            raise ArtifactPathError(f"artifact path uses reserved namespace: {'/'.join(parts)}")

    def _root_fd_copy(self) -> int:
        if self._root_fd is None:
            raise ValueError("artifact store is closed")
        return os.dup(self._root_fd)

    @staticmethod
    def _open_or_create_root(path: Path, *, create: bool = True) -> int:
        """Pin *path*, optionally creating it, without resolving untrusted components."""
        parts = path.parts[1:] if path.is_absolute() else path.parts
        if any(part in {"", ".", ".."} for part in parts):
            raise ArtifactPathError(f"artifact root is not a contained directory path: {path}")
        try:
            fd = os.open(os.sep if path.is_absolute() else ".", os.O_RDONLY | os.O_DIRECTORY)
        except OSError as error:
            raise ArtifactPathError(f"cannot open artifact root base: {path}") from error
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as error:
                    if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.ENOENT}:
                        raise ArtifactPathError(f"artifact root is not a directory: {path}") from error
                    raise
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _write_lock(self) -> Iterator[int]:
        root_fd = self._root_fd_copy()
        try:
            lock_fd = self._open_lock(root_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                yield root_fd
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        finally:
            os.close(root_fd)

    def _open_lock(self, root_fd: int) -> int:
        """Open the private lock, tolerating only concurrent first-create races."""
        last_error: OSError | None = None
        for _ in range(self._LOCK_ATTEMPTS):
            try:
                lock_fd = os.open(self._LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW, dir_fd=root_fd)
            except FileNotFoundError as error:
                last_error = error
                try:
                    lock_fd = os.open(
                        self._LOCK_NAME,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=root_fd,
                    )
                except (FileExistsError, FileNotFoundError) as error:
                    last_error = error
                    time.sleep(0.001)
                    continue
                except OSError as error:
                    raise ArtifactPathError("cannot create artifact lock") from error
            except OSError as error:
                raise ArtifactPathError("cannot open artifact lock") from error
            try:
                info = os.fstat(lock_fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) & 0o077
                ):
                    raise ArtifactPathError("artifact lock is not a private regular file")
                return lock_fd
            except BaseException:
                os.close(lock_fd)
                raise
        raise ArtifactPathError("artifact lock creation did not stabilize") from last_error

    def _recover_orphans(self, root_fd: int) -> None:
        temp_fd = self._open_temp_dir(root_fd, create=True)
        try:
            for name in os.listdir(temp_fd):
                if not self._TEMP_NAME.fullmatch(name):
                    continue
                info = os.stat(name, dir_fd=temp_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    raise ArtifactPathError(f"artifact temporary entry is a directory: {name}")
                os.unlink(name, dir_fd=temp_fd)
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)

    def _stored_bytes(self, root_fd: int) -> int:
        return self._stored_bytes_in(os.dup(root_fd), top_level=True)

    def _stored_bytes_in(self, fd: int, *, top_level: bool) -> int:
        total = 0
        try:
            for name in os.listdir(fd):
                if top_level and name in {self._LOCK_NAME, self._TEMP_DIR}:
                    continue
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
                elif stat.S_ISDIR(info.st_mode):
                    try:
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                        dir_fd=fd)
                    except OSError as error:
                        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                            raise ArtifactPathError(f"artifact path changed while counting: {name}") from error
                        raise
                    total += self._stored_bytes_in(child, top_level=False)
            return total
        finally:
            os.close(fd)

    def _assert_destination_missing(self, parent_fd: int, parts: tuple[str, ...]) -> None:
        try:
            info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISREG(info.st_mode):
            raise ArtifactExistsError(f"artifact already exists: {'/'.join(parts)}")
        raise ArtifactPathError(f"artifact destination is not a regular file: {'/'.join(parts)}")

    def _open_temp_dir(self, root_fd: int, *, create: bool) -> int:
        if create:
            try:
                os.mkdir(self._TEMP_DIR, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        try:
            return os.open(self._TEMP_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                           dir_fd=root_fd)
        except OSError as error:
            raise ArtifactPathError("artifact temporary namespace is not a directory") from error

    def _publish_exclusive(self, root_fd: int, parent_fd: int, parts: tuple[str, ...],
                           data: bytes) -> None:
        temp_fd = self._open_temp_dir(root_fd, create=True)
        temp_name = f"{os.getpid()}.{secrets.token_hex(16)}.tmp"
        temp_created = False
        try:
            fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=temp_fd)
            temp_created = True
            try:
                self._write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(temp_name, parts[-1], src_dir_fd=temp_fd, dst_dir_fd=parent_fd,
                        follow_symlinks=False)
            except FileExistsError as error:
                self._assert_destination_missing(parent_fd, parts)
                raise ArtifactExistsError(f"artifact already exists: {'/'.join(parts)}") from error
            os.unlink(temp_name, dir_fd=temp_fd)
            temp_created = False
            os.fsync(temp_fd)
            os.fsync(parent_fd)
        finally:
            if temp_created:
                try:
                    os.unlink(temp_name, dir_fd=temp_fd)
                except FileNotFoundError:
                    pass
            os.close(temp_fd)

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short artifact write")
            view = view[written:]

    def _open_parent(self, root_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
        fd = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as error:
                    if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.ENOENT}:
                        raise ArtifactPathError(
                            f"artifact path contains a symlink or non-directory: {'/'.join(parts)}"
                        ) from error
                    raise
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise
