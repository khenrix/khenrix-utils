"""Owner-bound, fsynced file CAS stores for one private fanout run root."""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .artifacts import ArtifactStore, canonical_json
from .errors import (
    ExecutionConflictError, ExecutionValidationError, RunAuthorizationError,
    RunStateError, SchedulerConflictError, SchedulerStateError,
)
from .execute import (
    ExecutionRecord, ExecutionService, ExecutionSnapshot, _execution_record_from_dict,
)
from .plan import FanoutPlanV1, validate_plan
from .runstate import (
    LocalAnchorAuthority, OwnerCapability, RunJournal, _assert_current_entry, _local_absolute_path,
    _open_private_directory, _private_stat, _read_private_fd, _write_all,
)
from .scheduler_authority import (
    BackendRecord, Scheduler, SchedulerBackend, SchedulerSnapshot,
    _snapshot_dict, _snapshot_from_dict,
)


_LOCK_SCHEMA = "fanout-file-backend-lock-v1"
_META_SCHEMA = "fanout-file-backend-v2"
_SCHEDULER_RECORD_SCHEMA = "fanout-file-scheduler-record-v1"
_MAX_META_BYTES = 4 * 1024
_MAX_RECORD_BYTES = 8 * 1024 * 1024
_LOCAL_LOCK = threading.Lock()


class _FileBackend:
    """The shared file transaction; subclasses own only their wire record."""

    _kind = ""
    _state_error = RunStateError
    _conflict_error = RunStateError

    def __init__(self, root: Path | str, *, run_id: str,
                 owner: OwnerCapability, create: bool = False,
                 inspect_only: bool = False) -> None:
        if not isinstance(owner, OwnerCapability):
            raise RunAuthorizationError("file backend requires an owner capability")
        if not isinstance(run_id, str) or not run_id or len(run_id.encode("utf-8")) > 128:
            raise self._state_error("file backend run id is invalid")
        try:
            path = _local_absolute_path(root, "file backend run root")
            root_fd = _open_private_directory(path, create=False, exclusive=False)
        except (OSError, RunStateError) as error:
            raise self._state_error("file backend run root is unavailable or unsafe") from error
        self._root = path
        self._run_id = run_id
        self._owner = owner
        self._closed = False
        self._inspect_only = inspect_only
        self._known_record: tuple[int, tuple[int, int], str] | None = None
        try:
            metadata, meta_identity, lock_digest = self._bootstrap(root_fd, create=create)
            self._root_identity = (metadata["root_device"], metadata["root_inode"])
            self._lock_identity = (metadata["lock_device"], metadata["lock_inode"])
            self._meta_identity = meta_identity
            self._lock_digest = lock_digest
            lock_fd = self._open_lock(root_fd)
            os.close(lock_fd)
        finally:
            os.close(root_fd)
        self.read()

    @classmethod
    def create(cls, root: Path | str, *, run_id: str,
               owner: OwnerCapability) -> "_FileBackend":
        return cls(root, run_id=run_id, owner=owner, create=True)

    @classmethod
    def resume(cls, root: Path | str, *, run_id: str,
               owner: OwnerCapability) -> "_FileBackend":
        return cls(root, run_id=run_id, owner=owner)

    @classmethod
    def inspect(cls, root: Path | str, *, run_id: str,
                owner: OwnerCapability) -> "_FileBackend":
        return cls(root, run_id=run_id, owner=owner, inspect_only=True)

    def identity(self) -> str:
        return f"local-file-{self._kind}-v1"

    def key(self) -> str:
        digest = hashlib.sha256(self._run_id.encode("utf-8")).hexdigest()
        return f"{self._kind}/{digest}"

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "_FileBackend":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    @property
    def _meta_name(self) -> str:
        return f".fanout-{self._kind}.meta.json"

    @property
    def _lock_name(self) -> str:
        return f".fanout-{self._kind}.lock"

    @property
    def _record_name(self) -> str:
        return f".fanout-{self._kind}.record.json"

    def _owner_mac(self, unsigned: dict[str, object], owner: OwnerCapability) -> str:
        return hmac.digest(
            owner._mac_key(), b"fanout-file-backend-v1" + canonical_json(unsigned), "sha256",
        ).hex()

    def _temporary_name(self, part: str) -> str:
        return f".fanout-{self._kind}-{part}-tmp-{secrets.token_hex(16)}"

    def _valid_temporary_name(self, value: object, part: str) -> bool:
        return isinstance(value, str) and re.fullmatch(
            rf"\.fanout-{self._kind}-{part}-tmp-[0-9a-f]{{32}}", value,
        ) is not None

    def _entry_exists(self, root_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise self._state_error(f"file backend cannot inspect entry: {name}") from error
        return True

    @staticmethod
    def _private_bootstrap_stat(info: os.stat_result, links: tuple[int, ...]) -> bool:
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_nlink in links and stat.S_IMODE(info.st_mode) == 0o600)

    def _unlink_own_temporary(self, root_fd: int, name: str,
                              identity: tuple[int, int]) -> None:
        try:
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if ((info.st_dev, info.st_ino) != identity
                or not self._private_bootstrap_stat(info, (1, 2))):
            raise self._state_error("file backend temporary entry was replaced")
        os.unlink(name, dir_fd=root_fd)

    def _publish_exclusive(self, root_fd: int, name: str, temporary: str,
                           data: bytes) -> None:
        if len(data) > _MAX_META_BYTES:
            raise self._state_error("file backend bootstrap entry exceeds its byte limit")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=root_fd)
        info = os.fstat(fd)
        identity = (info.st_dev, info.st_ino)
        try:
            _write_all(fd, data)
            os.fsync(fd)
            _assert_current_entry(root_fd, temporary, fd, identity,
                                  "file backend temporary entry")
            os.link(temporary, name, src_dir_fd=root_fd, dst_dir_fd=root_fd,
                    follow_symlinks=False)
            published = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if ((published.st_dev, published.st_ino) != identity
                    or not self._private_bootstrap_stat(published, (1, 2))):
                raise self._state_error("file backend publication entry changed")
            self._unlink_own_temporary(root_fd, temporary, identity)
            os.fsync(root_fd)
        finally:
            os.close(fd)
            self._unlink_own_temporary(root_fd, temporary, identity)

    def _open_bootstrap_lock(self, root_fd: int) -> int:
        try:
            fd = os.open(self._lock_name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=root_fd)
        except OSError as error:
            raise self._state_error("file backend lock is missing or unsafe") from error
        try:
            info = os.fstat(fd)
            entry = os.stat(self._lock_name, dir_fd=root_fd, follow_symlinks=False)
            if (not self._private_bootstrap_stat(info, (1, 2))
                    or not self._private_bootstrap_stat(entry, (1, 2))
                    or (info.st_dev, info.st_ino) != (entry.st_dev, entry.st_ino)):
                raise self._state_error("file backend lock entry is unsafe")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _bootstrap(self, root_fd: int, *, create: bool
                   ) -> tuple[dict[str, object], tuple[int, int], str]:
        try:
            if create and not self._entry_exists(root_fd, self._lock_name):
                if self._entry_exists(root_fd, self._meta_name):
                    raise self._state_error("file backend metadata exists without a lock")
                root_info = os.fstat(root_fd)
                temporary = self._temporary_name("lock")
                unsigned: dict[str, object] = {
                    "schema_version": _LOCK_SCHEMA,
                    "kind": self._kind,
                    "run_id": self._run_id,
                    "run_root": str(self._root),
                    "root_device": root_info.st_dev,
                    "root_inode": root_info.st_ino,
                    "backend_identity": self.identity(),
                    "backend_key": self.key(),
                    "publication_temp": temporary,
                }
                claim = {**unsigned, "owner_mac": self._owner_mac(unsigned, self._owner)}
                try:
                    self._publish_exclusive(root_fd, self._lock_name, temporary,
                                            canonical_json(claim))
                except FileExistsError:
                    if not self._entry_exists(root_fd, self._lock_name):
                        raise
            lock_fd = self._open_bootstrap_lock(root_fd)
            try:
                with _LOCAL_LOCK:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    try:
                        _, lock_identity, lock_digest = self._read_lock_claim(
                            root_fd, recover_link=not self._inspect_only,
                        )
                        lock_info = os.fstat(lock_fd)
                        if lock_identity != (lock_info.st_dev, lock_info.st_ino):
                            raise self._state_error("file backend lock changed while locking")
                        metadata_existed = self._entry_exists(root_fd, self._meta_name)
                        if not metadata_existed:
                            if self._inspect_only:
                                raise self._state_error("file backend metadata is absent during inspection")
                            root_info = os.fstat(root_fd)
                            temporary = self._temporary_name("meta")
                            unsigned = {
                                "schema_version": _META_SCHEMA,
                                "kind": self._kind,
                                "run_id": self._run_id,
                                "run_root": str(self._root),
                                "root_device": root_info.st_dev,
                                "root_inode": root_info.st_ino,
                                "lock_device": lock_info.st_dev,
                                "lock_inode": lock_info.st_ino,
                                "backend_identity": self.identity(),
                                "backend_key": self.key(),
                                "publication_temp": temporary,
                            }
                            metadata = {
                                **unsigned, "owner_mac": self._owner_mac(unsigned, self._owner),
                            }
                            self._publish_exclusive(root_fd, self._meta_name, temporary,
                                                    canonical_json(metadata))
                        metadata, meta_identity = self._read_metadata(
                            root_fd, recover_link=not self._inspect_only,
                        )
                        if (metadata["lock_device"], metadata["lock_inode"]) != lock_identity:
                            raise self._state_error("file backend metadata names another lock")
                        if create and metadata_existed:
                            raise self._state_error("file backend already exists")
                        return metadata, meta_identity, lock_digest
                    finally:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        except RunAuthorizationError:
            raise
        except (OSError, RunStateError) as error:
            raise self._state_error("file backend exclusive bootstrap failed") from error

    def _read_lock_claim(self, root_fd: int, *, recover_link: bool = False
                         ) -> tuple[dict[str, object], tuple[int, int], str]:
        data, identity, links = self._read_file(
            root_fd, self._lock_name, _MAX_META_BYTES, allow_linked=recover_link,
        )
        claim = self._parse_json(data, "file backend lock claim")
        fields = {
            "schema_version", "kind", "run_id", "run_root", "root_device",
            "root_inode", "backend_identity", "backend_key", "publication_temp",
            "owner_mac",
        }
        if set(claim) != fields:
            raise self._state_error("file backend lock claim fields are invalid")
        info = os.fstat(root_fd)
        expected = {
            "schema_version": _LOCK_SCHEMA,
            "kind": self._kind,
            "run_id": self._run_id,
            "run_root": str(self._root),
            "root_device": info.st_dev,
            "root_inode": info.st_ino,
            "backend_identity": self.identity(),
            "backend_key": self.key(),
        }
        if any(claim[name] != value for name, value in expected.items()):
            raise self._state_error("file backend lock belongs to another run root or identity")
        if (any(type(claim[name]) is not int or claim[name] < 1
                for name in ("root_device", "root_inode"))
                or not self._valid_temporary_name(claim["publication_temp"], "lock")):
            raise self._state_error("file backend lock claim binding is invalid")
        unsigned = {key: value for key, value in claim.items() if key != "owner_mac"}
        mac = claim["owner_mac"]
        if not isinstance(mac, str) or not hmac.compare_digest(
            mac, self._owner_mac(unsigned, self._owner),
        ):
            raise RunAuthorizationError("owner capability cannot authenticate file backend")
        if links == 2:
            self._finish_bootstrap_link(root_fd, self._lock_name,
                                        claim["publication_temp"], identity, data)
        return claim, identity, hashlib.sha256(data).hexdigest()

    def _read_metadata(self, root_fd: int, *, recover_link: bool = False
                       ) -> tuple[dict[str, object], tuple[int, int]]:
        data, identity, links = self._read_file(
            root_fd, self._meta_name, _MAX_META_BYTES, allow_linked=recover_link,
        )
        metadata = self._parse_json(data, "file backend metadata")
        fields = {
            "schema_version", "kind", "run_id", "run_root", "root_device",
            "root_inode", "lock_device", "lock_inode", "backend_identity",
            "backend_key", "publication_temp", "owner_mac",
        }
        if set(metadata) != fields:
            raise self._state_error("file backend metadata fields are invalid")
        info = os.fstat(root_fd)
        expected = {
            "schema_version": _META_SCHEMA,
            "kind": self._kind,
            "run_id": self._run_id,
            "run_root": str(self._root),
            "root_device": info.st_dev,
            "root_inode": info.st_ino,
            "backend_identity": self.identity(),
            "backend_key": self.key(),
        }
        if any(metadata[name] != value for name, value in expected.items()):
            raise self._state_error("file backend belongs to another run root or identity")
        if (any(type(metadata[name]) is not int or metadata[name] < 1
                for name in ("root_device", "root_inode", "lock_device", "lock_inode"))
                or not self._valid_temporary_name(metadata["publication_temp"], "meta")):
            raise self._state_error("file backend inode binding is invalid")
        unsigned = {key: value for key, value in metadata.items() if key != "owner_mac"}
        mac = metadata["owner_mac"]
        if not isinstance(mac, str) or not hmac.compare_digest(
            mac, self._owner_mac(unsigned, self._owner),
        ):
            raise RunAuthorizationError("owner capability cannot authenticate file backend")
        if links == 2:
            self._finish_bootstrap_link(root_fd, self._meta_name,
                                        metadata["publication_temp"], identity, data)
        return metadata, identity

    def _finish_bootstrap_link(self, root_fd: int, name: str, temporary: str,
                               identity: tuple[int, int], data: bytes) -> None:
        try:
            final = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if (final.st_dev, final.st_ino) != identity:
                raise self._state_error("file backend bootstrap entry changed")
            if final.st_nlink == 2:
                linked = os.stat(temporary, dir_fd=root_fd, follow_symlinks=False)
                if (not self._private_bootstrap_stat(final, (2,))
                        or not self._private_bootstrap_stat(linked, (2,))
                        or (linked.st_dev, linked.st_ino) != identity):
                    raise self._state_error("file backend bootstrap hardlink is unsafe")
                os.unlink(temporary, dir_fd=root_fd)
                os.fsync(root_fd)
            elif not self._private_bootstrap_stat(final, (1,)):
                raise self._state_error("file backend bootstrap entry is unsafe")
        except OSError as error:
            raise self._state_error("file backend bootstrap link is not durable") from error
        retained, retained_identity, retained_links = self._read_file(
            root_fd, name, _MAX_META_BYTES,
        )
        if (retained != data or retained_identity != identity or retained_links != 1):
            raise self._state_error("file backend bootstrap entry changed during recovery")

    def _read_file(self, root_fd: int, name: str, limit: int, *,
                   allow_linked: bool = False) -> tuple[bytes, tuple[int, int], int]:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
        except OSError as error:
            raise self._state_error(f"file backend entry is missing or unsafe: {name}") from error
        try:
            info = os.fstat(fd)
            links = info.st_nlink
            if links == 1:
                data = _read_private_fd(fd, limit, name)
            elif allow_linked and self._private_bootstrap_stat(info, (2,)):
                if info.st_size > limit:
                    raise self._state_error(f"file backend entry exceeds its limit: {name}")
                chunks = bytearray()
                while len(chunks) < info.st_size:
                    chunk = os.pread(fd, min(64 * 1024, info.st_size - len(chunks)),
                                     len(chunks))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                after = os.fstat(fd)
                if (len(chunks) != info.st_size or after.st_size != info.st_size
                        or (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)):
                    raise self._state_error(f"file backend entry changed while reading: {name}")
                data = bytes(chunks)
            else:
                raise self._state_error(f"file backend entry has an unsafe hardlink: {name}")
            identity = (info.st_dev, info.st_ino)

            def assert_entry() -> None:
                if links == 1:
                    _assert_current_entry(root_fd, name, fd, identity, name)
                    return
                descriptor = os.fstat(fd)
                entry = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if (not self._private_bootstrap_stat(descriptor, (2,))
                        or not self._private_bootstrap_stat(entry, (2,))
                        or (descriptor.st_dev, descriptor.st_ino) != identity
                        or (entry.st_dev, entry.st_ino) != identity
                        or descriptor.st_size != info.st_size
                        or entry.st_size != info.st_size):
                    raise self._state_error(f"file backend entry changed while reading: {name}")

            assert_entry()
            os.fsync(fd)
            os.fsync(root_fd)
            assert_entry()
            return data, identity, links
        except (OSError, RunStateError) as error:
            raise self._state_error(f"file backend entry is unsafe or not durable: {name}") from error
        finally:
            os.close(fd)

    def _parse_json(self, data: bytes, label: str) -> dict[str, object]:
        try:
            parsed = json.loads(data.decode("utf-8"))
            if not isinstance(parsed, dict) or canonical_json(parsed) != data:
                raise ValueError("noncanonical JSON")
        except (UnicodeDecodeError, ValueError, TypeError) as error:
            raise self._state_error(f"{label} is malformed or noncanonical") from error
        return parsed

    def _open_root(self) -> int:
        if self._closed:
            raise self._state_error("file backend is closed")
        try:
            fd = _open_private_directory(self._root, create=False, exclusive=False)
        except (OSError, RunStateError) as error:
            raise self._state_error("file backend run root is unavailable or unsafe") from error
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != self._root_identity:
            os.close(fd)
            raise self._state_error("file backend run root inode changed")
        return fd

    def _open_lock(self, root_fd: int) -> int:
        try:
            fd = os.open(self._lock_name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=root_fd)
        except OSError as error:
            raise self._state_error("file backend lock is missing or unsafe") from error
        try:
            info = os.fstat(fd)
            if not _private_stat(info) or (info.st_dev, info.st_ino) != self._lock_identity:
                raise self._state_error("file backend lock inode changed")
            _assert_current_entry(root_fd, self._lock_name, fd, self._lock_identity,
                                  "file backend lock")
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _locked_root(self) -> Iterator[int]:
        root_fd = self._open_root()
        lock_fd: int | None = None
        try:
            metadata, identity = self._read_metadata(root_fd)
            if identity != self._meta_identity or (
                metadata["lock_device"], metadata["lock_inode"]
            ) != self._lock_identity:
                raise self._state_error("file backend metadata inode changed")
            lock_fd = self._open_lock(root_fd)
            with _LOCAL_LOCK:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    _, locked_lock_identity, lock_digest = self._read_lock_claim(root_fd)
                    if (locked_lock_identity != self._lock_identity
                            or lock_digest != self._lock_digest):
                        raise self._state_error("file backend lock claim changed")
                    _, locked_identity = self._read_metadata(root_fd)
                    if locked_identity != self._meta_identity:
                        raise self._state_error("file backend metadata changed while locking")
                    _assert_current_entry(root_fd, self._lock_name, lock_fd,
                                          self._lock_identity, "file backend lock")
                    yield root_fd
                except (OSError, RunStateError) as error:
                    raise self._state_error("file backend lock or directory is unsafe") from error
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            os.close(root_fd)

    def _read_current(self, root_fd: int):
        try:
            entry = os.stat(self._record_name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            if self._known_record is not None:
                raise self._state_error("file backend record disappeared")
            return None
        except OSError as error:
            raise self._state_error("file backend record is unsafe") from error
        if entry.st_nlink == 2 and self._known_record is None:
            if self._inspect_only:
                raise self._state_error("file backend record is incomplete during inspection")
            self._finish_initial_link(root_fd, entry)
        data, identity, _ = self._read_file(root_fd, self._record_name, _MAX_RECORD_BYTES)
        value = self._parse_json(data, "file backend record")
        try:
            record = self._decode_record(value)
        except (RunStateError, SchedulerStateError, ExecutionValidationError, ValueError, TypeError) as error:
            raise self._state_error("file backend record snapshot is invalid") from error
        snapshot = record.snapshot
        if (snapshot.run_id != self._run_id
                or snapshot.backend_identity != self.identity()
                or snapshot.backend_key != self.key()):
            raise self._state_error("file backend record belongs to another run")
        if self._known_record is not None:
            known_revision, known_identity, known_digest = self._known_record
            if record.revision < known_revision or (
                record.revision == known_revision and (
                    identity != known_identity or hashlib.sha256(data).hexdigest() != known_digest
                )
            ):
                raise self._state_error("file backend record inode or content was replaced")
        self._known_record = (record.revision, identity, hashlib.sha256(data).hexdigest())
        return record

    def _finish_initial_link(self, root_fd: int, entry: os.stat_result) -> None:
        """Finish a killed exclusive create after its complete record became visible."""
        pattern = re.compile(rf"\.fanout-{self._kind}-tmp-[0-9a-f]{{32}}\Z")
        linked = []
        for name in os.listdir(root_fd):
            if not pattern.fullmatch(name):
                continue
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == (entry.st_dev, entry.st_ino):
                linked.append(name)
        if (len(linked) != 1 or not stat.S_ISREG(entry.st_mode)
                or entry.st_uid != os.getuid() or stat.S_IMODE(entry.st_mode) != 0o600):
            raise self._state_error("file backend record has an unsafe hardlink")
        try:
            os.unlink(linked[0], dir_fd=root_fd)
            os.fsync(root_fd)
        except OSError as error:
            raise self._state_error("file backend initial publication is not durable") from error

    def read(self):
        with self._locked_root() as root_fd:
            return self._read_current(root_fd)

    def compare_and_set(self, expected_revision: int, snapshot,
                        *, owner: OwnerCapability):
        if self._inspect_only:
            raise self._state_error("file backend inspection cannot mutate state")
        if not isinstance(owner, OwnerCapability) or not hmac.compare_digest(
            owner._mac_key(), self._owner._mac_key(),
        ):
            raise RunAuthorizationError("owner capability cannot mutate file backend")
        if type(expected_revision) is not int or expected_revision < 0:
            raise self._conflict_error("file backend expected revision is invalid")
        next_revision = expected_revision + 1
        candidate, data = self._encode_record(next_revision, snapshot)
        if len(data) > _MAX_RECORD_BYTES:
            raise self._state_error("file backend record exceeds its byte limit")
        with self._locked_root() as root_fd:
            current = self._read_current(root_fd)
            actual = 0 if current is None else current.revision
            if expected_revision != actual:
                raise self._conflict_error("file backend revision conflict")
            temporary = f".fanout-{self._kind}-tmp-{secrets.token_hex(16)}"
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=root_fd)
                try:
                    _write_all(fd, data)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if expected_revision == 0:
                    os.link(temporary, self._record_name,
                            src_dir_fd=root_fd, dst_dir_fd=root_fd,
                            follow_symlinks=False)
                    os.unlink(temporary, dir_fd=root_fd)
                else:
                    os.replace(temporary, self._record_name,
                               src_dir_fd=root_fd, dst_dir_fd=root_fd)
                os.fsync(root_fd)
            except OSError as error:
                raise self._state_error("file backend atomic write failed") from error
            finally:
                try:
                    os.unlink(temporary, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
            stored = self._read_current(root_fd)
            if stored != candidate:
                raise self._state_error("file backend did not retain the exact CAS value")
            return stored

    def _encode_record(self, revision: int, snapshot):
        raise NotImplementedError

    def _decode_record(self, value: dict[str, object]):
        raise NotImplementedError


class FileSchedulerBackend(_FileBackend):
    """File-backed implementation of the v2 scheduler CAS protocol."""

    _kind = "scheduler"
    _state_error = SchedulerStateError
    _conflict_error = SchedulerConflictError

    def _encode_record(self, revision: int,
                       snapshot: SchedulerSnapshot) -> tuple[BackendRecord, bytes]:
        if not isinstance(snapshot, SchedulerSnapshot) or snapshot.backend_revision != revision:
            raise SchedulerStateError("scheduler CAS snapshot revision is invalid")
        document = _snapshot_dict(snapshot)
        value = {
            "schema_version": _SCHEDULER_RECORD_SCHEMA,
            "revision": revision,
            "snapshot": document,
            "snapshot_sha256": hashlib.sha256(canonical_json(document)).hexdigest(),
        }
        record = self._decode_record(value)
        if (record.snapshot.run_id != self._run_id
                or record.snapshot.backend_identity != self.identity()
                or record.snapshot.backend_key != self.key()):
            raise SchedulerStateError("scheduler CAS snapshot belongs to another run")
        return record, canonical_json(value)

    def _decode_record(self, value: dict[str, object]) -> BackendRecord:
        if (set(value) != {"schema_version", "revision", "snapshot", "snapshot_sha256"}
                or value["schema_version"] != _SCHEDULER_RECORD_SCHEMA
                or type(value["revision"]) is not int or value["revision"] < 1
                or not isinstance(value["snapshot"], dict)):
            raise SchedulerStateError("scheduler file record fields are invalid")
        snapshot = _snapshot_from_dict(value["snapshot"])
        if (snapshot.backend_revision != value["revision"]
                or value["snapshot_sha256"] != hashlib.sha256(
                    canonical_json(value["snapshot"]),
                ).hexdigest()):
            raise SchedulerStateError("scheduler file record digest or revision is invalid")
        return BackendRecord(value["revision"], snapshot)


class FileExecutionBackend(_FileBackend):
    """File-backed implementation of the execution metadata CAS protocol."""

    _kind = "execution"
    _state_error = ExecutionConflictError
    _conflict_error = ExecutionConflictError

    def max_record_bytes(self) -> int:
        return _MAX_RECORD_BYTES

    def _encode_record(self, revision: int,
                       snapshot: ExecutionSnapshot) -> tuple[ExecutionRecord, bytes]:
        record = ExecutionRecord(revision, snapshot)
        if (record.snapshot.run_id != self._run_id
                or record.snapshot.backend_identity != self.identity()
                or record.snapshot.backend_key != self.key()):
            raise ExecutionValidationError("execution CAS snapshot belongs to another run")
        return record, record.canonical_bytes

    def _decode_record(self, value: dict[str, object]) -> ExecutionRecord:
        return _execution_record_from_dict(value)


def resume_scheduler_from_amendments(
    *, initial_plan: FanoutPlanV1, journal: RunJournal,
    backend: SchedulerBackend, artifacts: ArtifactStore,
    owner: OwnerCapability, anchor_store: LocalAnchorAuthority,
) -> Scheduler:
    """Authenticate all amendment documents before scheduler authority can repair."""
    journal.authorize_owner(owner)
    inputs = journal.inputs
    plan = validate_plan(initial_plan)
    if inputs.compiled_plan_sha256 != hashlib.sha256(
        canonical_json(plan.to_dict()),
    ).hexdigest():
        raise ExecutionConflictError("initial plan does not match journal inputs")
    documents = {}
    for revision in sorted(journal.state.amendments):
        documents[revision] = ExecutionService.recovery_documents(
            journal, artifacts, revision=revision,
        )
    try:
        record = backend.read()
    except Exception as error:
        raise ExecutionConflictError("scheduler backend read failed") from error
    if record is not None:
        if not isinstance(record, BackendRecord):
            raise ExecutionConflictError("scheduler backend record is invalid")
        revision = record.snapshot.plan_revision
        if revision == 1:
            expected_plan, expected_inputs = plan, inputs
        else:
            document = documents.get(revision)
            if document is None:
                raise ExecutionConflictError(
                    "scheduler revision has no authenticated amendment documents"
                )
            expected_plan, expected_inputs = document.plan, document.inputs
        if (
            record.snapshot.plan_sha256 != expected_inputs.compiled_plan_sha256
            or record.snapshot.inputs_digest != expected_inputs.digest
        ):
            raise ExecutionConflictError("scheduler amendment binding changed")
    else:
        expected_plan, expected_inputs = plan, inputs
    return Scheduler.resume(
        expected_plan, expected_inputs, backend, artifacts,
        owner=owner, anchor_store=anchor_store,
    )
