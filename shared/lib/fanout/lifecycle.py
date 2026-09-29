"""Non-mutating candidate collection, transactional handover, and exact-owner GC."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import fcntl
import json
import os
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping, Sequence

from .artifacts import canonical_json
from .controller import (
    LifecycleController,
    assert_controller,
    controller_directory,
    read_evidence,
    write_evidence,
)
from .errors import (
    CandidateValidationError,
    CandidateVerificationError,
    GarbageCollectionError,
    HandoverError,
    LifecycleError,
)
from .repo import RepositoryBaseline, SeatWorkspace, capture_repository_baseline, create_seat_workspace
from .verification import (
    CandidateBundle,
    CandidateEntry,
    CandidateVerification,
    _apply_candidate,
    _assert_candidate_applied,
    _checks_digest,
    _environment_digest,
    _link_target,
    _open_parent,
    _read_all,
    _remove_path,
    _set_directory_mode,
    _set_symlink_mode,
    _write_all,
    _write_entry,
    _write_directory,
    _baseline_directories,
    create_candidate,
    _run_checks,
    validate_candidate_verification,
)


_HANDOVER_DISPOSITION_ISSUER = object()
_HANDOVER_TRANSACTION_ISSUER = object()


@dataclass(frozen=True, slots=True)
class CollectedCandidate:
    """One read-only collected candidate associated with its originating seat."""

    seat_id: str
    candidate: CandidateBundle

    def __post_init__(self) -> None:
        _identity(self.seat_id, "collected seat id")
        if not isinstance(self.candidate, CandidateBundle):
            raise LifecycleError("collected candidate is invalid")


@dataclass(frozen=True, slots=True)
class HandoverResult:
    """Authenticated disposition of one exact caller-worktree transaction."""

    baseline_digest: str
    candidate_digest: str
    journal: Path
    transaction_root: Path
    status: str
    controller_id: str
    task_id: str | None = None
    recovered: bool = field(default=False, compare=False)
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _digest(self.baseline_digest, "handover baseline digest")
        _digest(self.candidate_digest, "handover candidate digest")
        _digest(self.controller_id, "handover controller id")
        if self.status not in {"committed", "rolled-back", "conflict", "not-mutated"}:
            raise LifecycleError("handover disposition is invalid")
        if self.task_id is not None:
            _identity(self.task_id, "handover task id")
        if not isinstance(self.recovered, bool):
            raise LifecycleError("handover recovery marker is invalid")
        if self._issuer is not _HANDOVER_DISPOSITION_ISSUER:
            raise LifecycleError("handover disposition is not authenticated")
        if not Path(self.journal).is_absolute() or not Path(self.transaction_root).is_absolute():
            raise LifecycleError("handover paths must be absolute")
        object.__setattr__(self, "journal", Path(self.journal))
        object.__setattr__(self, "transaction_root", Path(self.transaction_root))

    def __bool__(self) -> bool:
        """Preserve the legacy answer: whether this call performed a rollback."""
        return self.recovered

    @property
    def evidence_digest(self) -> str:
        return hashlib.sha256(canonical_json({
            "baseline_digest": self.baseline_digest,
            "candidate_digest": self.candidate_digest,
            "controller_id": self.controller_id,
            "schema_version": "fanout-handover-disposition-v1",
            "status": self.status,
            "task_id": self.task_id,
            "transaction_root": os.fspath(self.transaction_root),
        })).hexdigest()


@dataclass(frozen=True, slots=True)
class HandoverTransaction:
    """Controller-issued identity for one not-yet-mutated handover attempt."""

    baseline_digest: str
    candidate_digest: str
    journal: Path
    transaction_root: Path
    transaction_sha256: str
    controller_id: str
    task_id: str | None = None
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _digest(self.baseline_digest, "handover baseline digest")
        _digest(self.candidate_digest, "handover candidate digest")
        _digest(self.transaction_sha256, "handover transaction digest")
        _digest(self.controller_id, "handover controller id")
        if self.task_id is not None:
            _identity(self.task_id, "handover task id")
        if self._issuer is not _HANDOVER_TRANSACTION_ISSUER:
            raise LifecycleError("handover transaction is not authenticated")
        if not Path(self.journal).is_absolute() or not Path(self.transaction_root).is_absolute():
            raise LifecycleError("handover transaction paths must be absolute")
        object.__setattr__(self, "journal", Path(self.journal))
        object.__setattr__(self, "transaction_root", Path(self.transaction_root))


@dataclass(frozen=True, slots=True)
class _HandoverRecoveryState:
    destination: Path
    identity: tuple[int, int]
    baseline_digest: str
    candidate_digest: str
    task_id: str | None
    transaction_sha256: str
    status: str
    backup_digest: str | None


@dataclass(frozen=True, slots=True)
class _OwnedTreeEntry:
    """One descendant identity captured with a directory cleanup capability."""

    path: str
    device: int
    inode: int
    kind: str

    def __post_init__(self) -> None:
        _owned_path(self.path)
        for value in (self.device, self.inode):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GarbageCollectionError("run-owned inode identity is invalid")
        if self.kind not in {"file", "directory", "symlink", "special"}:
            raise GarbageCollectionError("run-owned path kind is invalid")


@dataclass(frozen=True, slots=True)
class RunOwnedPath:
    """An inode-bound capability to delete one exact run-owned file or directory."""

    run_root: Path
    root_device: int
    root_inode: int
    path: str
    device: int
    inode: int
    kind: str
    controller_id: str
    owned_entries: tuple[_OwnedTreeEntry, ...] = ()
    evidence_name: str = ""
    evidence_digest: str = ""
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        root = Path(self.run_root)
        if not root.is_absolute():
            raise GarbageCollectionError("run-owned root must be absolute")
        _owned_path(self.path)
        for value in (self.root_device, self.root_inode, self.device, self.inode):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GarbageCollectionError("run-owned inode identity is invalid")
        if self.kind not in {"file", "directory", "symlink", "special"}:
            raise GarbageCollectionError("run-owned path kind is invalid")
        _digest(self.controller_id, "run-owned controller id")
        entries = tuple(self.owned_entries)
        if any(not isinstance(entry, _OwnedTreeEntry) for entry in entries):
            raise GarbageCollectionError("run-owned descendant identities are invalid")
        if self.kind != "directory" and entries:
            raise GarbageCollectionError("a non-directory run-owned path cannot have descendants")
        if len({entry.path for entry in entries}) != len(entries):
            raise GarbageCollectionError("run-owned descendant identities must be unique")
        if self._issuer is not _CLAIM_ISSUER:
            raise GarbageCollectionError("run-owned claims must be issued by an authenticated controller")
        if (
            not isinstance(self.evidence_name, str)
            or not self.evidence_name.endswith(".json")
            or len(self.evidence_name) != 69
            or any(character not in "0123456789abcdef" for character in self.evidence_name[:-5])
        ):
            raise GarbageCollectionError("run-owned claim evidence is invalid")
        _digest(self.evidence_digest, "run-owned claim evidence digest")
        if self.evidence_name != f"{self.evidence_digest}.json":
            raise GarbageCollectionError("run-owned claim evidence does not match its digest")
        object.__setattr__(self, "run_root", root)
        object.__setattr__(self, "owned_entries", tuple(sorted(entries, key=lambda item: os.fsencode(item.path))))


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    path: str
    kind: str
    mode: int
    data: bytes = b""


@dataclass(frozen=True, slots=True)
class _PinnedDestination:
    """The exact caller directory inode locked for one handover transaction."""

    path: Path
    descriptor: int
    device: int
    inode: int


@dataclass(slots=True)
class _MutationTracker:
    """Write-ahead path states needed to reverse only our own direct mutations."""

    operations: list["_Mutation"] = field(default_factory=list)

    def record(
        self,
        action: str,
        path: str,
        expected: "_ExpectedPath | None",
        *,
        before: "_ExpectedPath | None" = None,
        before_known: bool = False,
        source_quarantine: str | None = None,
    ) -> None:
        self.operations.append(
            _Mutation(
                action,
                path,
                expected,
                before=before,
                before_known=before_known,
                source_quarantine=source_quarantine,
            )
        )

    def complete(self, action: str, path: str) -> None:
        self.pending(action, path).completed = True

    def pending(self, action: str, path: str) -> "_Mutation":
        for mutation in reversed(self.operations):
            if mutation.action == action and mutation.path == path and not mutation.completed:
                return mutation
        raise HandoverError("handover mutation evidence has no durable intent")

    def pending_path(self, path: str) -> "_Mutation":
        for mutation in reversed(self.operations):
            if mutation.path == path and not mutation.completed:
                return mutation
        raise HandoverError("handover mutation evidence has no durable intent")


@dataclass(frozen=True, slots=True)
class _ExpectedPath:
    """The exact one-path state expected immediately after a journaled mutation."""

    kind: str
    mode: int
    data_digest: str


@dataclass(slots=True)
class _Mutation:
    """One intended direct mutation, retained even if the process dies mid-operation."""

    action: str
    path: str
    expected: _ExpectedPath | None
    before: _ExpectedPath | None = None
    before_known: bool = False
    completed: bool = False
    source_removed: bool = False
    created_identity: tuple[int, int, str] | None = None
    bound_identity: tuple[int, int, str] | None = None
    source_quarantine: str | None = None


class _TransactionQuarantine:
    """Controller-private storage for names detached during one caller transaction."""

    def __init__(self, transaction_root: Path) -> None:
        self.path = transaction_root / "quarantine"
        try:
            try:
                os.mkdir(self.path, mode=0o700)
            except FileExistsError:
                pass
            _fsync_directory(transaction_root)
            path_information = self.path.lstat()
            if self.path.is_symlink() or not stat.S_ISDIR(path_information.st_mode):
                raise OSError(errno.ENOTDIR, "handover quarantine is not a real directory")
            self.descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            opened = os.fstat(self.descriptor)
            if (opened.st_dev, opened.st_ino) != (path_information.st_dev, path_information.st_ino):
                os.close(self.descriptor)
                raise OSError(errno.ESTALE, "handover quarantine identity changed")
        except OSError as error:
            raise HandoverError("handover quarantine cannot be created safely") from error

    def __enter__(self) -> "_TransactionQuarantine":
        return self

    def __exit__(self, *_: object) -> None:
        os.close(self.descriptor)

    def retain_exact(
        self,
        parent_fd: int,
        name: str,
        expected: os.stat_result,
        path: str,
        temporary: str,
    ) -> os.stat_result:
        """Move an exact caller source into its durable transaction-private slot."""
        moved = self._move(parent_fd, name, expected, path, temporary=temporary)
        if moved is None:
            raise CandidateVerificationError("candidate destination changed during transaction quarantine")
        return moved[1]

    def contains(self, temporary: str | None) -> bool:
        if temporary is None:
            return False
        try:
            os.stat(temporary, dir_fd=self.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise HandoverError("transaction quarantine could not be inspected") from error
        return True

    def restore_source(
        self,
        temporary: str,
        parent_fd: int,
        name: str,
        expected: _ExpectedPath,
    ) -> bool | None:
        """Restore a retained original by no-replace rename, preserving its exact inode."""
        try:
            os.stat(temporary, dir_fd=self.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise HandoverError("transaction quarantine could not be inspected") from error
        actual = _tree_entry_at(self.descriptor, temporary)
        if (
            actual is not None
            and actual.kind == "directory"
            and expected.kind == "directory"
            and actual.data == b""
        ):
            information = os.stat(temporary, dir_fd=self.descriptor, follow_symlinks=False)
            return self.restore_directory_with_mode(
                temporary,
                parent_fd,
                name,
                information,
                expected.mode,
            )
        if not _matches_expected_path(actual, expected):
            raise HandoverError("transaction quarantine source no longer matches its durable intent")
        return self.restore(temporary, parent_fd, name)

    def restore_directory_with_mode(
        self,
        temporary: str,
        parent_fd: int,
        name: str,
        expected: os.stat_result,
        mode: int,
    ) -> bool:
        """Change a verified quarantined directory, then no-replace restore that same inode."""
        descriptor = os.open(
            temporary,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=self.descriptor,
        )
        try:
            opened = os.fstat(descriptor)
            if (
                (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
                or not stat.S_ISDIR(opened.st_mode)
            ):
                raise HandoverError("quarantined directory identity changed before mode update")
            original_mode = stat.S_IMODE(opened.st_mode)
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            if _rename_no_replace(self.descriptor, temporary, parent_fd, name):
                os.fsync(parent_fd)
                os.fsync(self.descriptor)
                return True
            os.fchmod(descriptor, original_mode)
            os.fsync(descriptor)
            return False
        finally:
            os.close(descriptor)

    def detach_expected(
        self,
        parent_fd: int,
        name: str,
        expected: _ExpectedPath,
        path: str,
    ) -> tuple[str, os.stat_result] | None:
        """Detach a current candidate state only when its complete bytes still match."""
        try:
            information = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        temporary = f"restore-{uuid.uuid4().hex}"
        try:
            if not _rename_no_replace(parent_fd, name, self.descriptor, temporary):
                return None
            os.fsync(parent_fd)
            os.fsync(self.descriptor)
            moved = os.stat(temporary, dir_fd=self.descriptor, follow_symlinks=False)
        except OSError as error:
            raise HandoverError("transaction-owned caller path could not be quarantined for rollback") from error
        if (
            (moved.st_dev, moved.st_ino) != (information.st_dev, information.st_ino)
            or _kind(moved.st_mode) != _kind(information.st_mode)
        ):
            self.restore_moved(temporary, parent_fd, name, moved)
            return None
        try:
            actual = _tree_entry_at(self.descriptor, temporary)
        except HandoverError:
            self.restore_moved(temporary, parent_fd, name, moved)
            raise
        if not _matches_expected_path(actual, expected):
            self.restore_moved(temporary, parent_fd, name, moved)
            return None
        return temporary, moved

    def detach_identity(
        self,
        parent_fd: int,
        name: str,
        identity: tuple[int, int, str],
        expected: _ExpectedPath,
        path: str,
    ) -> tuple[str, os.stat_result] | None:
        """Detach only a journaled created inode that still has its exact completed state."""
        try:
            information = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if (information.st_dev, information.st_ino, _kind(information.st_mode)) != identity:
            return None
        detached = self._move(parent_fd, name, information, path)
        if detached is None:
            return None
        temporary, moved = detached
        actual = _tree_entry_at(self.descriptor, temporary)
        if _matches_expected_path(actual, expected):
            return detached
        if self.restore_moved(temporary, parent_fd, name, moved):
            return None
        raise HandoverError(
            "transaction-created caller path changed during rollback; exact evidence remains quarantined"
        )

    def discard(self, temporary: str, information: os.stat_result) -> None:
        try:
            _remove_quarantined_entry(self.descriptor, temporary, information)
        except OSError as error:
            raise HandoverError("transaction-owned caller path could not be removed from quarantine") from error

    def restore(self, temporary: str, parent_fd: int, name: str) -> bool:
        return _restore_quarantined_name(self.descriptor, temporary, parent_fd, name)

    def restore_moved(
        self,
        temporary: str,
        parent_fd: int,
        name: str,
        information: os.stat_result,
    ) -> bool:
        if stat.S_ISDIR(information.st_mode):
            return self.restore_directory_with_mode(
                temporary,
                parent_fd,
                name,
                information,
                stat.S_IMODE(information.st_mode),
            )
        return self.restore(temporary, parent_fd, name)

    def _move(
        self,
        parent_fd: int,
        name: str,
        expected: os.stat_result,
        path: str,
        *,
        temporary: str | None = None,
    ) -> tuple[str, os.stat_result] | None:
        temporary = temporary or f"apply-{uuid.uuid4().hex}"
        try:
            if not _rename_no_replace(parent_fd, name, self.descriptor, temporary):
                raise CandidateVerificationError("candidate destination cannot be quarantined without replacement")
            os.fsync(parent_fd)
            os.fsync(self.descriptor)
            moved = os.stat(temporary, dir_fd=self.descriptor, follow_symlinks=False)
        except CandidateVerificationError:
            raise
        except OSError as error:
            raise CandidateVerificationError(f"candidate destination quarantine failed: {path}") from error
        if (
            (moved.st_dev, moved.st_ino) != (expected.st_dev, expected.st_ino)
            or _kind(moved.st_mode) != _kind(expected.st_mode)
        ):
            self.restore_moved(temporary, parent_fd, name, moved)
            return None
        return temporary, moved


_CLAIM_ISSUER = object()


def _rename_no_replace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> bool:
    """Move one entry only if its destination spelling remains unclaimed.

    There is intentionally no check-then-``os.rename`` fallback: that fallback
    would turn an error path into a peer overwrite. Unsupported platforms fail
    closed so the caller can retain the private quarantine artifact instead.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            rename = libc.renameatx_np
            flags = 0x00000004  # RENAME_EXCL
        elif sys.platform.startswith("linux"):
            rename = libc.renameat2
            flags = 1  # RENAME_NOREPLACE
        else:
            return False
    except (AttributeError, OSError):
        return False
    rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = rename(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        flags,
    )
    if result == 0:
        return True
    failure = ctypes.get_errno()
    if failure in {errno.EEXIST, errno.ENOTEMPTY, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL}:
        return False
    raise OSError(failure, os.strerror(failure))


class _HandoverJournal:
    """Append-only write-ahead evidence; every record and its directory are fsynced."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).absolute()
        self._parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(
                self.path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._parent_fd,
            )
        except BaseException:
            os.close(self._parent_fd)
            raise
        self._handle = os.fdopen(descriptor, "wb")
        self._sequence = 0
        self._failed = False
        self._committed = False
        os.fsync(self._parent_fd)

    @classmethod
    def reopen(cls, path: Path) -> "_HandoverJournal":
        """Resume an existing journal only after its canonical sequence is validated."""
        result = object.__new__(cls)
        result.path = Path(path).absolute()
        result._parent_fd = os.open(result.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _trim_torn_journal(result.path, result._parent_fd)
            records = _read_journal_records(result.path)
            descriptor = os.open(
                result.path.name,
                os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW,
                dir_fd=result._parent_fd,
            )
        except BaseException:
            os.close(result._parent_fd)
            raise
        result._handle = os.fdopen(descriptor, "ab")
        result._sequence = len(records)
        result._failed = False
        result._committed = bool(records and records[-1]["step"] == "handover-complete")
        return result

    def record(
        self,
        step: str,
        path: str | None = None,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if not isinstance(step, str) or not step or "\x00" in step:
            raise HandoverError("handover journal step is invalid")
        if path is not None and (not isinstance(path, str) or "\x00" in path):
            raise HandoverError("handover journal path is invalid")
        if self._failed:
            raise HandoverError("handover journal cannot append after a failed record")
        if self._committed:
            raise HandoverError("handover journal is already committed")
        sequence = self._sequence + 1
        value: dict[str, object] = {"sequence": sequence, "step": step}
        if path is not None:
            value["path"] = path
        if details is not None:
            value["details"] = dict(details)
        try:
            self._handle.write(canonical_json(value))
            self._handle.flush()
            os.fsync(self._handle.fileno())
            os.fsync(self._parent_fd)
        except BaseException:
            self._failed = True
            raise
        self._sequence = sequence
        if step == "handover-complete":
            self._committed = True

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def committed(self) -> bool:
        return self._committed

    def poison(self) -> None:
        """Forbid further appends after an operation observed journal failure."""
        self._failed = True

    def close(self) -> None:
        try:
            if not self._handle.closed:
                try:
                    self._handle.flush()
                    os.fsync(self._handle.fileno())
                finally:
                    self._handle.close()
        finally:
            if self._parent_fd >= 0:
                try:
                    os.fsync(self._parent_fd)
                finally:
                    os.close(self._parent_fd)
                    self._parent_fd = -1


def collect_candidates(
    baseline: RepositoryBaseline,
    workspaces: Mapping[str, SeatWorkspace],
) -> tuple[CollectedCandidate, ...]:
    """Read seat candidates without changing the caller, a seat, or peer Git metadata."""
    _baseline(baseline)
    if not isinstance(workspaces, Mapping):
        raise LifecycleError("candidate workspaces must be a mapping")
    collected: list[CollectedCandidate] = []
    for seat_id in sorted(workspaces):
        _identity(seat_id, "candidate seat id")
        workspace = workspaces[seat_id]
        if not isinstance(workspace, SeatWorkspace):
            raise LifecycleError("candidate workspace must be a SeatWorkspace")
        if workspace.seat_id != seat_id or workspace.baseline_digest != baseline.digest:
            raise LifecycleError("candidate workspace has another seat or baseline association")
        collected.append(CollectedCandidate(seat_id, create_candidate(baseline, workspace)))
    return tuple(collected)


def prepare_handover_transaction(
    baseline: RepositoryBaseline,
    verification: CandidateVerification,
    *,
    controller: LifecycleController,
    task_id: str | None = None,
    locked_destination: _PinnedDestination | None = None,
) -> HandoverTransaction:
    """Create the authenticated transaction identity before execution intent."""
    _baseline(baseline)
    try:
        assert_controller(controller)
        _verification(baseline, verification, controller)
    except Exception as error:
        raise HandoverError(
            "handover transaction requires an authenticated controller receipt"
        ) from error
    if task_id is not None:
        try:
            _identity(task_id, "handover task id")
        except LifecycleError as error:
            raise HandoverError("handover task binding is invalid") from error
    destination = baseline.repository.resolve()
    _separate_controller_root(controller, destination)
    lock = _destination_lock(destination) if locked_destination is None else nullcontext(locked_destination)
    with lock as pinned:
        _assert_destination_path(pinned)
        if pinned.path != destination:
            raise HandoverError("prepared destination lock differs")
        _require_destination_baseline(baseline, pinned)
        transaction_root = _new_transaction_root(controller)
        journal: _HandoverJournal | None = None
        try:
            journal = _HandoverJournal(transaction_root / "handover.jsonl")
            destination_device, destination_inode = _directory_identity(
                pinned.descriptor
            )
            details = _handover_opening_details(
                baseline,
                verification,
                controller,
                destination,
                destination_device,
                destination_inode,
                task_id,
            )
            journal.record("transaction-opened", details=details)
            journal.close()
            journal = None
        except BaseException as error:
            _close_journal(journal)
            if isinstance(error, HandoverError):
                raise
            raise HandoverError("handover transaction could not be prepared") from error
    return HandoverTransaction(
        baseline.digest,
        verification.candidate.digest,
        transaction_root / "handover.jsonl",
        transaction_root,
        _handover_transaction_digest(transaction_root, details),
        controller.controller_id,
        task_id,
        _issuer=_HANDOVER_TRANSACTION_ISSUER,
    )


def handover_candidate(
    baseline: RepositoryBaseline,
    verification: CandidateVerification,
    *,
    controller: LifecycleController,
    task_id: str | None = None,
    transaction: HandoverTransaction | None = None,
    locked_destination: _PinnedDestination | None = None,
) -> HandoverResult:
    """Stage and atomically hand over a controller-authenticated candidate receipt."""
    _baseline(baseline)
    try:
        assert_controller(controller)
        _verification(baseline, verification, controller)
    except HandoverError:
        raise
    except Exception as error:
        raise HandoverError("handover requires an authenticated controller receipt") from error
    if task_id is not None:
        try:
            _identity(task_id, "handover task id")
        except LifecycleError as error:
            raise HandoverError("handover task binding is invalid") from error
    if transaction is not None:
        if (
            not isinstance(transaction, HandoverTransaction)
            or transaction._issuer is not _HANDOVER_TRANSACTION_ISSUER
            or transaction.controller_id != controller.controller_id
            or transaction.baseline_digest != baseline.digest
            or transaction.candidate_digest != verification.candidate.digest
            or transaction.task_id != task_id
        ):
            raise HandoverError("handover transaction changed association")
    destination = baseline.repository.resolve()
    _separate_controller_root(controller, destination)
    lock = _destination_lock(destination) if locked_destination is None else nullcontext(locked_destination)
    with lock as pinned:
        _assert_destination_path(pinned)
        if pinned.path != destination:
            raise HandoverError("prepared destination lock differs")
        _require_destination_baseline(baseline, pinned)
        transaction_root = (
            _new_transaction_root(controller)
            if transaction is None
            else _controller_transaction_root(
                controller,
                transaction.transaction_root,
            )
        )
        journal: _HandoverJournal | None = None
        backup: Path | None = None
        backup_digest: str | None = None
        backup_ready = False
        destination_mutation_started = False
        mutations: _MutationTracker | None = None
        completion_acknowledged = False
        try:
            destination_device, destination_inode = _directory_identity(pinned.descriptor)
            details = _handover_opening_details(
                baseline,
                verification,
                controller,
                destination,
                destination_device,
                destination_inode,
                task_id,
            )
            if transaction is None:
                journal = _HandoverJournal(transaction_root / "handover.jsonl")
                journal.record("transaction-opened", details=details)
            else:
                records = _read_journal_records(transaction.journal)
                if (
                    len(records) != 1
                    or records[0]["step"] != "transaction-opened"
                    or records[0].get("details") != details
                    or transaction.journal
                    != transaction_root / "handover.jsonl"
                    or transaction.transaction_sha256
                    != _handover_transaction_digest(transaction_root, details)
                ):
                    raise HandoverError(
                        "prepared handover transaction evidence changed"
                    )
                journal = _HandoverJournal.reopen(transaction.journal)
            journal.record("baseline-verified")
            journal.record("stage-create-intent")
            stage = create_seat_workspace(baseline, transaction_root, "handover-stage").root.resolve()
            journal.record("stage-created")
            _apply_candidate(
                baseline,
                verification.candidate,
                stage,
                before_operation=lambda action, path: journal.record(f"stage-{action}-intent", path),
                operation=lambda action, path: journal.record(f"stage-{action}", path),
            )
            outcomes, failure = _run_checks(stage, verification.checks, verification.environment)
            journal.record("stage-check-outcomes", details=_check_evidence(outcomes, failure))
            if failure is not None:
                journal.record("stage-verification-failed")
                raise HandoverError("staged candidate did not pass its immutable checks")
            if outcomes != verification.outcomes:
                journal.record("stage-evidence-mismatch")
                raise HandoverError("staged candidate verifier evidence changed")
            journal.record("stage-verified")

            _require_destination_baseline(baseline, pinned)
            _preflight_destination_paths(baseline, verification.candidate, pinned.descriptor)
            journal.record("destination-revalidated")
            pre_handover = _snapshot_tree(pinned.descriptor)
            backup_digest = _tree_digest(pre_handover)
            backup = transaction_root / "backup"
            journal.record("backup-intent", "backup")
            _make_private_directory(backup)
            _restore_tree(backup, pre_handover)
            if _tree_digest(_snapshot_tree(backup)) != backup_digest:
                raise HandoverError("pre-handover backup did not verify")
            backup_ready = True
            journal.record("backup-created", details={"tree_digest": backup_digest})

            expected = pre_handover
            mutations = _MutationTracker()

            def before_destination_operation(action: str, path: str) -> None:
                nonlocal destination_mutation_started
                _require_tree(pinned.descriptor, expected)
                before = _expected_path(expected, path)
                after = _expected_tree_after_operation(expected, verification.candidate, action, path)
                target = _expected_path(after, path)
                source_quarantine = f"source-{uuid.uuid4().hex}"
                mutations.record(
                    action,
                    path,
                    target,
                    before=before,
                    before_known=True,
                    source_quarantine=source_quarantine,
                )
                journal.record(
                    f"destination-{action}-intent",
                    path,
                    details={
                        "before": _expected_path_payload(before),
                        "expected": _expected_path_payload(target),
                        "source_quarantine": source_quarantine,
                    },
                )
                destination_mutation_started = True

            def after_destination_operation(action: str, path: str) -> None:
                nonlocal expected
                expected = _expected_tree_after_operation(expected, verification.candidate, action, path)
                _require_tree(pinned.descriptor, expected)
                journal.record(f"destination-{action}", path)
                mutations.complete(action, path)

            def guard_destination_mutation(action: str, path: str) -> None:
                _require_tree(pinned.descriptor, expected)

            with _TransactionQuarantine(transaction_root) as quarantine:
                def quarantine_destination(
                    parent_fd: int,
                    name: str,
                    information: os.stat_result,
                    path: str,
                ) -> None:
                    mutation = mutations.pending_path(path)
                    assert mutation.source_quarantine is not None
                    quarantine.retain_exact(
                        parent_fd,
                        name,
                        information,
                        path,
                        mutation.source_quarantine,
                    )
                    journal.record(
                        "destination-source-removed",
                        path,
                        details={"action": mutation.action},
                    )
                    mutation.source_removed = True

                def destination_created(path: str, information: os.stat_result) -> None:
                    mutation = mutations.pending_path(path)
                    identity = (
                        information.st_dev,
                        information.st_ino,
                        _kind(information.st_mode),
                    )
                    journal.record(
                        "destination-created",
                        path,
                        details={
                            "action": mutation.action,
                            "device": identity[0],
                            "inode": identity[1],
                            "kind": identity[2],
                        },
                    )
                    mutation.created_identity = identity

                def destination_directory_mode(
                    parent_fd: int,
                    name: str,
                    information: os.stat_result,
                    mode: int,
                    path: str,
                ) -> None:
                    mutation = mutations.pending_path(path)
                    assert mutation.source_quarantine is not None
                    moved = quarantine.retain_exact(
                        parent_fd,
                        name,
                        information,
                        path,
                        mutation.source_quarantine,
                    )
                    identity = (moved.st_dev, moved.st_ino, _kind(moved.st_mode))
                    journal.record(
                        "destination-source-bound",
                        path,
                        details={
                            "action": mutation.action,
                            "device": identity[0],
                            "inode": identity[1],
                            "kind": identity[2],
                        },
                    )
                    mutation.bound_identity = identity
                    if not quarantine.restore_directory_with_mode(
                        mutation.source_quarantine,
                        parent_fd,
                        name,
                        moved,
                        mode,
                    ):
                        raise CandidateVerificationError(
                            f"candidate directory changed during mode update: {path}; "
                            "exact evidence remains quarantined"
                        )

                _apply_candidate(
                    baseline,
                    verification.candidate,
                    pinned.descriptor,
                    before_operation=before_destination_operation,
                    operation=after_destination_operation,
                    mutation_guard=guard_destination_mutation,
                    quarantine=quarantine_destination,
                    created=destination_created,
                    directory_mode=destination_directory_mode,
                )
            _require_tree(pinned.descriptor, expected)
            journal.record("destination-applied")
            _assert_candidate_applied(pinned.descriptor, verification.candidate)
            journal.record("final-checks-intent")
            destination_mutation_started = True
            _require_tree(pinned.descriptor, expected)
            journal.record("final-check-workspace-intent")
            final_workspace = create_seat_workspace(
                baseline,
                transaction_root,
                "handover-final-check",
            ).root.resolve()
            _restore_tree(final_workspace, _snapshot_tree(pinned.descriptor))
            journal.record("final-check-workspace-created")
            final_outcomes, final_failure = _run_checks(
                final_workspace,
                verification.checks,
                verification.environment,
            )
            journal.record("final-check-outcomes", details=_check_evidence(final_outcomes, final_failure))
            _require_tree(pinned.descriptor, expected)
            if final_failure is not None:
                journal.record("final-verification-failed")
                raise HandoverError("final caller verification failed")
            journal.record("final-verification-passed")
            _assert_destination_path(pinned)
            journal.record("handover-complete")
            completion_acknowledged = True
            try:
                _assert_destination_path(pinned)
            except HandoverError as error:
                # `handover-complete` is the durable linearization point. A later
                # pathname rebind cannot be rolled back through its replacement;
                # the journal's pinned inode makes recovery refuse that replacement.
                raise HandoverError(
                    "handover committed at its pinned destination but the destination pathname was rebound"
                ) from error
            result = HandoverResult(
                baseline.digest,
                verification.candidate.digest,
                journal.path.absolute(),
                transaction_root,
                "committed",
                controller.controller_id,
                task_id,
                _issuer=_HANDOVER_DISPOSITION_ISSUER,
            )
            try:
                journal.close()
            except BaseException:
                # Completion was already fsynced; closing cannot revoke it.
                pass
            journal = None
            return result
        except BaseException as error:
            if completion_acknowledged:
                _close_journal(journal)
                raise HandoverError("handover committed but its result could not be finalized") from error
            if journal is not None:
                journal.poison()
            if backup_ready and destination_mutation_started:
                assert backup is not None and backup_digest is not None
                try:
                    _rollback_from_backup(
                        pinned.descriptor,
                        backup,
                        backup_digest,
                        None,
                        mutations=mutations,
                    )
                    _finalize_rollback_journal(journal, discard_unacknowledged_completion=True)
                    journal = None
                except BaseException as rollback_error:
                    try:
                        _finalize_rollback_conflict_journal(
                            journal,
                            discard_unacknowledged_completion=True,
                        )
                        journal = None
                    except BaseException as disposition_error:
                        _close_journal(journal)
                        raise HandoverError(
                            "handover rollback conflict could not be recorded durably"
                        ) from disposition_error
                    raise HandoverError(
                        f"{error}; rollback could not restore the caller"
                    ) from rollback_error
            elif backup_ready and journal is not None:
                _close_journal(journal)
                journal = None
            _close_journal(journal)
            if isinstance(error, HandoverError):
                raise
            raise HandoverError("handover failed and restored the pre-handover backup") from error


def recover_handover(
    controller: LifecycleController,
    transaction_root: Path | str,
    *,
    conflict_disposition: bool = False,
    expected_task_id: str | None = None,
    expected_baseline_digest: str | None = None,
    expected_candidate_digest: str | None = None,
    expected_transaction_digest: str | None = None,
) -> HandoverResult:
    """Return or establish the authenticated disposition of one exact transaction."""
    try:
        assert_controller(controller)
        transaction = _controller_transaction_root(controller, transaction_root)
        state = _recovery_state(
            controller,
            transaction,
            terminal_conflict=conflict_disposition,
        )
        _assert_handover_recovery_binding(
            state,
            expected_task_id=expected_task_id,
            expected_baseline_digest=expected_baseline_digest,
            expected_candidate_digest=expected_candidate_digest,
            expected_transaction_digest=expected_transaction_digest,
        )
    except HandoverError:
        raise
    except (OSError, StopIteration, ValueError, TypeError) as error:
        raise HandoverError("handover recovery evidence is unavailable") from error

    if state.status != "active":
        return _handover_disposition(controller, transaction, state)

    with _destination_lock(state.destination) as pinned:
        state = _recovery_state(
            controller,
            transaction,
            terminal_conflict=conflict_disposition,
        )
        _assert_handover_recovery_binding(
            state,
            expected_task_id=expected_task_id,
            expected_baseline_digest=expected_baseline_digest,
            expected_candidate_digest=expected_candidate_digest,
            expected_transaction_digest=expected_transaction_digest,
        )
        if state.status != "active":
            return _handover_disposition(controller, transaction, state)
        assert state.backup_digest is not None
        mutations = _recovery_mutations(_read_journal_records(transaction / "handover.jsonl"))
        refreshed_device, refreshed_inode = _directory_identity(pinned.descriptor)
        if (refreshed_device, refreshed_inode) != state.identity:
            raise HandoverError("handover recovery destination identity changed")
        backup = transaction / "backup"
        if backup.is_symlink() or not backup.is_dir():
            raise HandoverError("handover recovery backup is unavailable")
        journal: _HandoverJournal | None = None
        try:
            journal = _HandoverJournal.reopen(transaction / "handover.jsonl")
        except BaseException:
            journal = None
        try:
            _rollback_from_backup(
                pinned.descriptor,
                backup,
                state.backup_digest,
                journal,
                mutations=mutations,
            )
        except BaseException:
            _finalize_rollback_conflict_journal(journal)
            journal = None
            if conflict_disposition:
                conflict = _recovery_state(
                    controller,
                    transaction,
                    terminal_conflict=True,
                )
                return _handover_disposition(controller, transaction, conflict)
            raise
        finally:
            _close_journal(journal)
        try:
            _assert_destination_path(pinned)
        except HandoverError as error:
            # A durable rollback applies to the pinned inode only.  If its
            # pathname was rebound afterward, never report that peer pathname
            # as the recovered caller; its terminal journal prevents a later
            # recovery from rewriting the replacement.
            raise HandoverError(
                "handover recovery completed at its pinned destination but the destination pathname was rebound"
            ) from error
        terminal = _recovery_state(controller, transaction)
        if terminal.status != "rolled-back":
            raise HandoverError("handover rollback lacks its durable disposition")
        return _handover_disposition(
            controller,
            transaction,
            terminal,
            recovered=True,
        )


def validate_handover_disposition(
    controller: LifecycleController,
    disposition: HandoverResult,
    *,
    task_id: str | None = None,
    baseline_digest: str | None = None,
    candidate_digest: str | None = None,
    transaction_digest: str | None = None,
) -> None:
    """Reauthenticate a disposition against its canonical controller journal."""
    if (
        not isinstance(disposition, HandoverResult)
        or disposition._issuer is not _HANDOVER_DISPOSITION_ISSUER
    ):
        raise HandoverError("authenticated handover disposition is required")
    try:
        assert_controller(controller)
        transaction = _controller_transaction_root(
            controller,
            disposition.transaction_root,
        )
        state = _recovery_state(controller, transaction, terminal_conflict=True)
    except HandoverError:
        raise
    except Exception as error:
        raise HandoverError("handover disposition evidence is unavailable") from error
    if (
        disposition.controller_id != controller.controller_id
        or disposition.journal != transaction / "handover.jsonl"
        or disposition.baseline_digest != state.baseline_digest
        or disposition.candidate_digest != state.candidate_digest
        or disposition.task_id != state.task_id
        or disposition.status != state.status
        or (task_id is not None and disposition.task_id != task_id)
        or (
            baseline_digest is not None
            and disposition.baseline_digest != baseline_digest
        )
        or (
            candidate_digest is not None
            and disposition.candidate_digest != candidate_digest
        )
        or (
            transaction_digest is not None
            and state.transaction_sha256 != transaction_digest
        )
    ):
        raise HandoverError("handover disposition changed transaction association")


def _assert_handover_recovery_binding(
    state: _HandoverRecoveryState,
    *,
    expected_task_id: str | None,
    expected_baseline_digest: str | None,
    expected_candidate_digest: str | None,
    expected_transaction_digest: str | None,
) -> None:
    if expected_task_id is not None:
        try:
            expected_task_id = _identity(expected_task_id, "handover task id")
        except LifecycleError as error:
            raise HandoverError("handover recovery task binding is invalid") from error
    for value, label in (
        (expected_baseline_digest, "handover baseline digest"),
        (expected_candidate_digest, "handover candidate digest"),
        (expected_transaction_digest, "handover transaction digest"),
    ):
        if value is not None:
            try:
                _digest(value, label)
            except LifecycleError as error:
                raise HandoverError(
                    "handover recovery digest binding is invalid"
                ) from error
    if (
        (expected_task_id is not None and state.task_id != expected_task_id)
        or (
            expected_baseline_digest is not None
            and state.baseline_digest != expected_baseline_digest
        )
        or (
            expected_candidate_digest is not None
            and state.candidate_digest != expected_candidate_digest
        )
        or (
            expected_transaction_digest is not None
            and state.transaction_sha256 != expected_transaction_digest
        )
    ):
        raise HandoverError("handover recovery changed transaction binding")


def _handover_disposition(
    controller: LifecycleController,
    transaction: Path,
    state: _HandoverRecoveryState,
    *,
    recovered: bool = False,
) -> HandoverResult:
    if state.status == "active":
        raise HandoverError("active handover has no terminal disposition")
    return HandoverResult(
        state.baseline_digest,
        state.candidate_digest,
        transaction / "handover.jsonl",
        transaction,
        state.status,
        controller.controller_id,
        state.task_id,
        recovered=recovered,
        _issuer=_HANDOVER_DISPOSITION_ISSUER,
    )


def claim_run_path(controller: LifecycleController, path: str) -> RunOwnedPath:
    """Bind a cleanup capability to one currently existing inode below a run-owned root."""
    try:
        assert_controller(controller)
    except LifecycleError as error:
        raise GarbageCollectionError("an authenticated controller-owned run root is required") from error
    relative = _owned_path(path)
    _claimable_path(relative)
    root = controller.root
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise GarbageCollectionError("run-owned root cannot be opened safely") from error
    try:
        root_info = os.fstat(root_fd)
        parent_fd = _open_parent(root_fd, PurePosixPath(relative).parts, create=False)
        try:
            try:
                information = os.stat(PurePosixPath(relative).parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError as error:
                raise GarbageCollectionError("run-owned path is missing") from error
            entries = _snapshot_owned_entries(parent_fd, PurePosixPath(relative).parts[-1], information)
        finally:
            os.close(parent_fd)
    except CandidateValidationError as error:
        raise GarbageCollectionError("run-owned path cannot be opened safely") from error
    finally:
        os.close(root_fd)
    payload = _claim_payload(
        root,
        root_info.st_dev,
        root_info.st_ino,
        relative,
        information.st_dev,
        information.st_ino,
        _kind(information.st_mode),
        controller.controller_id,
        entries,
    )
    try:
        evidence_name, evidence_digest = write_evidence(controller, "gc-claim", payload)
    except Exception as error:
        raise GarbageCollectionError("run-owned claim cannot be durably issued") from error
    return RunOwnedPath(
        root,
        root_info.st_dev,
        root_info.st_ino,
        relative,
        information.st_dev,
        information.st_ino,
        _kind(information.st_mode),
        controller.controller_id,
        entries,
        evidence_name,
        evidence_digest,
        _CLAIM_ISSUER,
    )


def garbage_collect(controller: LifecycleController, owned_paths: Sequence[RunOwnedPath]) -> tuple[Path, ...]:
    """Delete only unchanged inode-bound paths, never a broad run root or a peer path."""
    try:
        assert_controller(controller)
    except LifecycleError as error:
        raise GarbageCollectionError("an authenticated controller-owned run root is required") from error
    if isinstance(owned_paths, (str, bytes)):
        raise GarbageCollectionError("run-owned paths must be a sequence")
    claims = tuple(owned_paths)
    if any(not isinstance(claim, RunOwnedPath) for claim in claims):
        raise GarbageCollectionError("run-owned paths are invalid")
    _no_owned_overlap(claims)
    removed: list[Path] = []
    for claim in claims:
        if claim.controller_id != controller.controller_id or claim.run_root != controller.root:
            raise GarbageCollectionError("run-owned claim belongs to another controller")
        _validate_owned_claim(controller, claim)
        try:
            root_fd = os.open(claim.run_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise GarbageCollectionError("run-owned root changed before cleanup") from error
        try:
            root_info = os.fstat(root_fd)
            if (root_info.st_dev, root_info.st_ino) != (claim.root_device, claim.root_inode):
                raise GarbageCollectionError("run-owned root ownership no longer verifies")
            parts = PurePosixPath(claim.path).parts
            parent_fd = _open_parent(root_fd, parts, create=False)
            try:
                information = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                if (
                    (information.st_dev, information.st_ino) != (claim.device, claim.inode)
                    or _kind(information.st_mode) != claim.kind
                ):
                    raise GarbageCollectionError("run-owned path ownership no longer verifies")
                _quarantine_and_remove_owned_claim(
                    controller,
                    parent_fd,
                    parts[-1],
                    information,
                    claim,
                )
            finally:
                os.close(parent_fd)
        except CandidateValidationError as error:
            raise GarbageCollectionError("run-owned path cannot be opened safely") from error
        except OSError as error:
            raise GarbageCollectionError("run-owned path could not be deleted") from error
        finally:
            os.close(root_fd)
        removed.append(claim.run_root / claim.path)
    return tuple(removed)


def _verification(
    baseline: RepositoryBaseline,
    value: CandidateVerification,
    controller: LifecycleController,
) -> None:
    if not isinstance(value, CandidateVerification):
        raise HandoverError("handover requires a freshly verified candidate receipt")
    try:
        validate_candidate_verification(controller, value)
    except CandidateValidationError as error:
        raise HandoverError("handover receipt is not durably authenticated") from error
    if not value.valid:
        raise HandoverError("handover refuses a failed candidate receipt")
    if value.candidate_digest != value.candidate.digest:
        raise HandoverError("verified candidate digest association changed")
    if value.baseline_digest != baseline.digest or value.candidate.baseline_digest != baseline.digest:
        raise HandoverError("verified candidate uses another immutable baseline")
    if not value.checks:
        raise HandoverError("verified candidate has no immutable argv checks")
    if value.checks_digest != _checks_digest(value.checks):
        raise HandoverError("verified candidate check association changed")
    if value.environment_digest != _environment_digest(value.environment):
        raise HandoverError("verified candidate environment association changed")


def _separate_controller_root(controller: LifecycleController, destination: Path) -> None:
    root = controller.root.resolve()
    try:
        root.relative_to(destination)
    except ValueError:
        try:
            destination.relative_to(root)
        except ValueError:
            return
    raise HandoverError("handover controller root must not contain the caller repository")


@contextmanager
def _destination_lock(destination: Path) -> Iterator[_PinnedDestination]:
    """Use the caller directory inode as the one lock shared by all controllers."""
    try:
        descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise HandoverError("destination cannot be locked safely") from error
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            raise HandoverError("destination transaction lock failed") from error
        information = os.fstat(descriptor)
        if not stat.S_ISDIR(information.st_mode):
            raise HandoverError("destination cannot be locked safely")
        yield _PinnedDestination(destination, descriptor, information.st_dev, information.st_ino)
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _new_transaction_root(controller: LifecycleController) -> Path:
    parent = controller_directory(controller, "transactions")
    try:
        transaction = Path(tempfile.mkdtemp(prefix=".fanout-handover-", dir=parent)).resolve()
        transaction.relative_to(parent.resolve())
        _fsync_directory(parent)
        return transaction
    except (OSError, ValueError) as error:
        raise HandoverError("handover transaction root cannot be created") from error


def _require_destination_baseline(baseline: RepositoryBaseline, destination: _PinnedDestination) -> None:
    _assert_destination_path(destination)
    try:
        current = capture_repository_baseline(destination.path)
    except Exception as error:
        raise HandoverError("destination baseline cannot be recaptured") from error
    _assert_destination_path(destination)
    if current.digest != baseline.digest:
        raise HandoverError("destination baseline drifted before handover")


def _assert_destination_path(destination: _PinnedDestination) -> None:
    try:
        information = destination.path.lstat()
    except OSError as error:
        raise HandoverError("destination pathname identity changed") from error
    if destination.path.is_symlink() or not stat.S_ISDIR(information.st_mode):
        raise HandoverError("destination pathname identity changed")
    if (information.st_dev, information.st_ino) != (destination.device, destination.inode):
        raise HandoverError("destination pathname identity changed")


def _directory_identity(path: Path | int) -> tuple[int, int]:
    if isinstance(path, bool):
        raise HandoverError("destination is unavailable")
    if isinstance(path, int):
        try:
            information = os.fstat(path)
        except OSError as error:
            raise HandoverError("destination is unavailable") from error
        if not stat.S_ISDIR(information.st_mode):
            raise HandoverError("destination is not a real directory")
        return information.st_dev, information.st_ino
    try:
        information = path.lstat()
    except OSError as error:
        raise HandoverError("destination is unavailable") from error
    if path.is_symlink() or not stat.S_ISDIR(information.st_mode):
        raise HandoverError("destination is not a real directory")
    return information.st_dev, information.st_ino


def _make_private_directory(path: Path) -> None:
    try:
        os.mkdir(path, mode=0o700)
        _fsync_directory(path.parent)
        _fsync_directory(path)
    except OSError as error:
        raise HandoverError("handover backup directory cannot be created") from error


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise HandoverError("handover directory cannot be synced safely") from error
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _preflight_destination_paths(
    baseline: RepositoryBaseline,
    candidate: CandidateBundle,
    destination: Path | int,
) -> None:
    """Refuse caller bytes, shapes, and ignored paths not represented by the baseline."""
    actual = {entry.path: entry for entry in _snapshot_tree(destination)}
    baseline_entries = {entry.path: entry for entry in baseline.entries}
    baseline_directories = _baseline_directories(baseline)
    affected = set(candidate.deleted_paths) | {entry.path for entry in candidate.entries}
    for path in tuple(affected):
        parts = PurePosixPath(path).parts
        affected.update("/".join(parts[:index]) for index in range(1, len(parts)))
    for path in sorted(affected, key=lambda item: (len(PurePosixPath(item).parts), item.encode("utf-8"))):
        observed = actual.get(path)
        expected = baseline_entries.get(path)
        if expected is not None:
            if observed != _TreeEntry(path, expected.kind, expected.mode, expected.data):
                raise HandoverError(f"destination path drifted before handover: {path}")
        elif path in baseline_directories:
            if observed is None or observed.kind != "directory":
                raise HandoverError(f"destination path shape drifted before handover: {path}")
        elif observed is not None:
            raise HandoverError(f"destination path collision before handover: {path}")
    for path in candidate.deleted_paths:
        if path in baseline_directories:
            _require_baseline_subtree(path, actual, baseline_entries, baseline_directories)


def _require_baseline_subtree(
    root: str,
    actual: Mapping[str, _TreeEntry],
    baseline_entries: Mapping[str, object],
    baseline_directories: set[str],
) -> None:
    prefix = root + "/"
    expected_paths = {
        path for path in set(baseline_entries) | baseline_directories
        if path == root or path.startswith(prefix)
    }
    actual_paths = {path for path in actual if path == root or path.startswith(prefix)}
    if actual_paths != expected_paths:
        raise HandoverError(f"destination directory collision before handover: {root}")
    for path in expected_paths:
        observed = actual[path]
        entry = baseline_entries.get(path)
        if entry is None:
            if observed.kind != "directory":
                raise HandoverError(f"destination directory shape drifted before handover: {path}")
        elif observed != _TreeEntry(path, entry.kind, entry.mode, entry.data):
            raise HandoverError(f"destination path drifted before handover: {path}")


def _require_tree(destination: Path | int, expected: tuple[_TreeEntry, ...]) -> None:
    actual = _snapshot_tree(destination)
    if actual != expected:
        raise HandoverError("destination changed during exclusive handover")


def _expected_tree_after_operation(
    expected: tuple[_TreeEntry, ...],
    candidate: CandidateBundle,
    action: str,
    path: str,
) -> tuple[_TreeEntry, ...]:
    entries = {entry.path: entry for entry in expected}
    if action == "delete":
        if path not in entries:
            raise HandoverError("destination deletion no longer has its expected identity")
        entries.pop(path)
    elif action in {"directory-prepare", "directory-mode", "write"}:
        candidate_entry = next((entry for entry in candidate.entries if entry.path == path), None)
        if candidate_entry is None:
            raise HandoverError("destination operation is not part of the candidate")
        if action.startswith("directory-") and candidate_entry.kind != "directory":
            raise HandoverError("destination directory operation is not part of the candidate")
        if action == "write" and candidate_entry.kind == "directory":
            raise HandoverError("destination write operation is not part of the candidate")
        mode = 0o700 if action == "directory-prepare" else candidate_entry.mode
        entries[path] = _TreeEntry(
            path,
            candidate_entry.kind,
            mode,
            candidate_entry.data,
        )
    else:
        raise HandoverError("destination operation is invalid")
    return tuple(sorted(entries.values(), key=lambda entry: entry.path.encode("utf-8")))


def _check_evidence(outcomes: tuple[object, ...], failure: str | None) -> dict[str, object]:
    return {
        "failure": failure,
        "outcomes": [
            {
                "artifacts": [
                    {"digest": artifact.digest, "path": artifact.path, "size": artifact.size}
                    for artifact in outcome.artifacts
                ],
                "argv_digest": outcome.argv_digest,
                "failure": outcome.failure,
                "index": outcome.index,
                "returncode": outcome.returncode,
                "status": outcome.status,
            }
            for outcome in outcomes
        ],
    }


def _rollback_from_backup(
    destination: Path | int,
    backup: Path,
    backup_digest: str,
    journal: _HandoverJournal | None,
    *,
    mutations: _MutationTracker | None = None,
) -> None:
    if journal is not None:
        journal.record("rollback-intent")
    restored = _snapshot_tree(backup)
    if _tree_digest(restored) != backup_digest:
        raise HandoverError("pre-handover backup changed before rollback")
    if mutations is None:
        _restore_tree(destination, restored)
        if _tree_digest(_snapshot_tree(destination)) != backup_digest:
            raise HandoverError("rollback did not restore the pre-handover caller tree")
    else:
        with _TransactionQuarantine(backup.parent) as quarantine:
            _rollback_owned_changes(destination, mutations, restored, quarantine)
    if journal is not None:
        journal.record("rollback-complete")


def _rollback_owned_changes(
    destination: Path | int,
    mutations: _MutationTracker,
    backup: tuple[_TreeEntry, ...],
    quarantine: _TransactionQuarantine,
) -> None:
    """Reverse operations only while their exact candidate output still owns the path."""
    original = {entry.path: entry for entry in backup}
    expected_outputs = {mutation.path: mutation.expected for mutation in mutations.operations}
    pending = list(reversed(mutations.operations))
    while pending:
        deferred: list[_Mutation] = []
        progressed = False
        for mutation in pending:
            try:
                restored = _restore_owned_path(
                    destination,
                    mutation.path,
                    original.get(mutation.path),
                    expected_outputs,
                    mutation=mutation,
                    quarantine=quarantine,
                )
            except (CandidateValidationError, CandidateVerificationError) as error:
                raise HandoverError("transaction-owned caller path could not be restored") from error
            if restored:
                progressed = True
                expected_outputs[mutation.path] = (
                    mutation.before
                    if mutation.before_known
                    else _expected_from_tree_entry(original.get(mutation.path))
                )
            else:
                deferred.append(mutation)
        if not deferred:
            _require_rollback_prestate(destination, mutations, backup, quarantine)
            return
        if not progressed:
            raise HandoverError("transaction rollback retained unresolved mutation state")
        pending = deferred


def _tree_entry_at(destination: Path | int, path: str) -> _TreeEntry | None:
    parts = PurePosixPath(path).parts
    try:
        with _opened_tree_root(destination) as root_fd:
            try:
                parent_fd = _open_parent(root_fd, parts, create=False)
            except (CandidateValidationError, FileNotFoundError):
                return None
            try:
                try:
                    information = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return None
                mode = stat.S_IMODE(information.st_mode)
                if stat.S_ISDIR(information.st_mode):
                    return _TreeEntry(path, "directory", mode)
                if stat.S_ISLNK(information.st_mode):
                    return _TreeEntry(path, "symlink", mode, os.fsencode(os.readlink(parts[-1], dir_fd=parent_fd)))
                if stat.S_ISREG(information.st_mode):
                    descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
                    try:
                        opened = os.fstat(descriptor)
                        if (opened.st_dev, opened.st_ino) != (information.st_dev, information.st_ino):
                            raise HandoverError("destination changed during transaction rollback")
                        return _TreeEntry(path, "file", stat.S_IMODE(opened.st_mode), _read_all(descriptor))
                    finally:
                        os.close(descriptor)
                raise HandoverError("destination contains a special file during transaction rollback")
            finally:
                os.close(parent_fd)
    except OSError as error:
        raise HandoverError("destination changed during transaction rollback") from error


def _expected_path(entries: tuple[_TreeEntry, ...], path: str) -> _ExpectedPath | None:
    entry = next((item for item in entries if item.path == path), None)
    if entry is None:
        return None
    return _ExpectedPath(entry.kind, entry.mode, hashlib.sha256(entry.data).hexdigest())


def _expected_path_payload(expected: _ExpectedPath | None) -> dict[str, object]:
    if expected is None:
        return {"absent": True}
    return {
        "kind": expected.kind,
        "mode": expected.mode,
        "data_sha256": expected.data_digest,
    }


def _expected_path_from_payload(value: object) -> _ExpectedPath | None:
    if not isinstance(value, dict):
        raise HandoverError("handover recovery mutation evidence is malformed")
    if value == {"absent": True}:
        return None
    if set(value) != {"kind", "mode", "data_sha256"}:
        raise HandoverError("handover recovery mutation evidence is malformed")
    kind, mode, digest = value["kind"], value["mode"], value["data_sha256"]
    if kind not in {"file", "symlink", "directory"} or type(mode) is not int or not 0 <= mode <= 0o777:
        raise HandoverError("handover recovery mutation evidence is malformed")
    _digest(digest, "handover recovery mutation digest")
    return _ExpectedPath(kind, mode, digest)


def _matches_expected_path(actual: _TreeEntry | None, expected: _ExpectedPath | None) -> bool:
    if expected is None:
        return actual is None
    return (
        actual is not None
        and actual.kind == expected.kind
        and actual.mode == expected.mode
        and hashlib.sha256(actual.data).hexdigest() == expected.data_digest
    )


def _restore_owned_path(
    destination: Path | int,
    path: str,
    original: _TreeEntry | None,
    expected_outputs: Mapping[str, _ExpectedPath | None],
    *,
    mutation: _Mutation,
    quarantine: _TransactionQuarantine,
) -> bool:
    expected = mutation.expected
    actual = _tree_entry_at(destination, path)
    before = mutation.before if mutation.before_known else _expected_from_tree_entry(original)
    if mutation.completed and not _matches_expected_path(actual, expected):
        if _matches_expected_path(actual, before):
            return True
        if actual is not None or not quarantine.contains(mutation.source_quarantine):
            return False
    if not mutation.completed and _matches_expected_path(actual, before):
        return True
    if mutation.created_identity is not None:
        if actual is None and quarantine.contains(mutation.source_quarantine):
            return _restore_pre_handover_path(
                destination,
                path,
                original,
                expected_outputs,
                mutation,
                quarantine,
            )
        if not _matches_expected_path(actual, expected):
            return False
        return _restore_created_identity(
            destination,
            path,
            original,
            expected_outputs,
            mutation.created_identity,
            expected,
            mutation,
            quarantine,
        )
    if actual is None and quarantine.contains(mutation.source_quarantine):
        return _restore_pre_handover_path(
            destination,
            path,
            original,
            expected_outputs,
            mutation,
            quarantine,
        )
    if not mutation.completed and mutation.action == "delete":
        if actual is not None:
            return _matches_expected_path(actual, before)
        if mutation.source_removed or quarantine.contains(mutation.source_quarantine):
            return _restore_pre_handover_path(
                destination,
                path,
                original,
                expected_outputs,
                mutation,
                quarantine,
            )
        return False
    if not mutation.completed and actual is None:
        if mutation.source_removed or quarantine.contains(mutation.source_quarantine):
            return _restore_pre_handover_path(
                destination,
                path,
                original,
                expected_outputs,
                mutation,
                quarantine,
            )
        return False
    directory_mode = (
        before is not None
        and before.kind == "directory"
        and expected is not None
        and expected.kind == "directory"
    )
    if directory_mode and mutation.bound_identity is not None:
        return _restore_directory_mode_if_owned(
            destination,
            path,
            before,
            expected,
            mutation.bound_identity,
            expected_outputs,
            quarantine,
            temporary=mutation.source_quarantine,
        )
    if not mutation.completed:
        return False
    if expected is None:
        return _restore_pre_handover_path(
            destination,
            path,
            original,
            expected_outputs,
            mutation,
            quarantine,
        )
    parts = PurePosixPath(path).parts
    try:
        parent_fd = _open_rollback_parent(destination, parts, expected_outputs)
        if parent_fd is None:
            return False
        try:
            detached = quarantine.detach_expected(parent_fd, parts[-1], expected, path)
            if detached is None:
                return True
            temporary, information = detached
            try:
                quarantine.discard(temporary, information)
            except HandoverError:
                quarantine.restore_moved(temporary, parent_fd, parts[-1], information)
                return False
        finally:
            parent_expected = expected_outputs.get("/".join(parts[:-1]))
            if parent_expected is not None and parent_expected.kind == "directory":
                os.fchmod(parent_fd, parent_expected.mode)
                os.fsync(parent_fd)
            os.close(parent_fd)
    except (CandidateValidationError, OSError) as error:
        raise HandoverError("transaction-owned caller path could not be quarantined for rollback") from error
    return _restore_pre_handover_path(
        destination,
        path,
        original,
        expected_outputs,
        mutation,
        quarantine,
    )


def _expected_from_tree_entry(entry: _TreeEntry | None) -> _ExpectedPath | None:
    if entry is None:
        return None
    return _ExpectedPath(entry.kind, entry.mode, hashlib.sha256(entry.data).hexdigest())


def _restore_created_identity(
    destination: Path | int,
    path: str,
    original: _TreeEntry | None,
    expected_outputs: Mapping[str, _ExpectedPath | None],
    identity: tuple[int, int, str],
    expected: _ExpectedPath,
    mutation: _Mutation,
    quarantine: _TransactionQuarantine,
) -> bool:
    """Reverse a partial output only while its journaled created inode still owns the name."""
    parts = PurePosixPath(path).parts
    try:
        parent_fd = _open_rollback_parent(destination, parts, expected_outputs)
        if parent_fd is None:
            return False
        try:
            detached = quarantine.detach_identity(parent_fd, parts[-1], identity, expected, path)
            if detached is None:
                actual = _tree_entry_at(destination, path)
                if _matches_expected_path(actual, _expected_from_tree_entry(original)):
                    return True
                if actual is None and quarantine.contains(mutation.source_quarantine):
                    return _restore_pre_handover_path(
                        destination,
                        path,
                        original,
                        expected_outputs,
                        mutation,
                        quarantine,
                    )
                return False
            temporary, information = detached
            try:
                quarantine.discard(temporary, information)
            except HandoverError:
                quarantine.restore_moved(temporary, parent_fd, parts[-1], information)
                return False
        finally:
            parent_expected = expected_outputs.get("/".join(parts[:-1]))
            if parent_expected is not None and parent_expected.kind == "directory":
                os.fchmod(parent_fd, parent_expected.mode)
                os.fsync(parent_fd)
            os.close(parent_fd)
    except (CandidateValidationError, OSError) as error:
        raise HandoverError("transaction-created caller path could not be quarantined for rollback") from error
    return _restore_pre_handover_path(
        destination,
        path,
        original,
        expected_outputs,
        mutation,
        quarantine,
    )


def _restore_pre_handover_path(
    destination: Path | int,
    path: str,
    original: _TreeEntry | None,
    expected_outputs: Mapping[str, _ExpectedPath | None],
    mutation: _Mutation,
    quarantine: _TransactionQuarantine,
) -> bool:
    """Prefer the retained original inode; old journals fall back to backup bytes."""
    temporary = mutation.source_quarantine
    if temporary is None or not quarantine.contains(temporary):
        return _restore_absent_path(destination, path, original)
    before = mutation.before if mutation.before_known else _expected_from_tree_entry(original)
    if before is None:
        raise HandoverError("transaction quarantine retained a source for an absent pre-state")
    parts = PurePosixPath(path).parts
    try:
        parent_fd = _open_rollback_parent(destination, parts, expected_outputs)
        if parent_fd is None:
            return False
        try:
            restored = quarantine.restore_source(temporary, parent_fd, parts[-1], before)
            return bool(restored)
        finally:
            parent_expected = expected_outputs.get("/".join(parts[:-1]))
            if parent_expected is not None and parent_expected.kind == "directory":
                os.fchmod(parent_fd, parent_expected.mode)
                os.fsync(parent_fd)
            os.close(parent_fd)
    except (CandidateValidationError, OSError) as error:
        raise HandoverError("transaction source could not be restored from quarantine") from error


def _open_rollback_parent(
    destination: Path | int,
    parts: tuple[str, ...],
    expected_outputs: Mapping[str, _ExpectedPath | None],
) -> int | None:
    """Open the path's real parent and temporarily unlock only candidate-owned ancestors."""
    try:
        with _opened_tree_root(destination) as root_fd:
            descriptor = os.dup(root_fd)
            try:
                for index, part in enumerate(parts[:-1], start=1):
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                    expected = expected_outputs.get("/".join(parts[:index]))
                    if expected is None or expected.kind != "directory":
                        continue
                    opened = os.fstat(descriptor)
                    actual = _TreeEntry(
                        "/".join(parts[:index]),
                        "directory",
                        stat.S_IMODE(opened.st_mode),
                    )
                    if not _matches_expected_path(actual, expected):
                        os.close(descriptor)
                        return None
                    if index == len(parts) - 1:
                        os.fchmod(descriptor, 0o700)
                        os.fsync(descriptor)
                return descriptor
            except BaseException:
                os.close(descriptor)
                raise
    except (CandidateValidationError, OSError):
        return None


def _restore_absent_path(destination: Path | int, path: str, original: _TreeEntry | None) -> bool:
    """Restore one backup entry with only exclusive creation, never replacement."""
    if original is None:
        return _path_is_absent(destination, path)
    parts = PurePosixPath(path).parts
    try:
        with _opened_tree_root(destination) as root_fd:
            try:
                parent_fd = _open_parent(root_fd, parts, create=False)
            except (CandidateValidationError, FileNotFoundError):
                return False
            try:
                try:
                    os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    return False
                if original.kind == "directory":
                    os.mkdir(parts[-1], mode=original.mode, dir_fd=parent_fd)
                    child_fd = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                    try:
                        os.fchmod(child_fd, original.mode)
                        os.fsync(child_fd)
                    finally:
                        os.close(child_fd)
                elif original.kind == "symlink":
                    os.symlink(original.data, os.fsencode(parts[-1]), dir_fd=parent_fd)
                    _set_symlink_mode(parent_fd, parts[-1], original.mode, original.path)
                else:
                    descriptor = os.open(
                        parts[-1],
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        original.mode,
                        dir_fd=parent_fd,
                    )
                    try:
                        _write_all(descriptor, original.data)
                        os.fchmod(descriptor, original.mode)
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                os.fsync(parent_fd)
                return True
            finally:
                os.close(parent_fd)
    except FileExistsError:
        return False
    except (CandidateValidationError, OSError) as error:
        raise HandoverError("transaction-owned caller path could not be restored exclusively") from error


def _path_is_absent(destination: Path | int, path: str) -> bool:
    parts = PurePosixPath(path).parts
    try:
        with _opened_tree_root(destination) as root_fd:
            try:
                parent_fd = _open_parent(root_fd, parts, create=False)
            except (CandidateValidationError, FileNotFoundError):
                return True
            try:
                try:
                    os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return True
                return False
            finally:
                os.close(parent_fd)
    except (CandidateValidationError, OSError) as error:
        raise HandoverError("transaction-owned caller path could not be inspected") from error


def _restore_directory_mode_if_owned(
    destination: Path | int,
    path: str,
    before: _ExpectedPath,
    expected: _ExpectedPath,
    identity: tuple[int, int, str] | None,
    expected_outputs: Mapping[str, _ExpectedPath | None],
    quarantine: _TransactionQuarantine,
    *,
    temporary: str | None,
) -> bool:
    if temporary is None:
        return False
    parts = PurePosixPath(path).parts
    try:
        parent_fd = _open_rollback_parent(destination, parts, expected_outputs)
        if parent_fd is None:
            return False
        try:
            try:
                information = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                return False
            actual = _TreeEntry(path, "directory", stat.S_IMODE(information.st_mode))
            if (
                identity is None
                or (information.st_dev, information.st_ino, _kind(information.st_mode)) != identity
                or not _matches_expected_path(actual, expected)
            ):
                return False
            moved = quarantine.retain_exact(parent_fd, parts[-1], information, path, temporary)
            return quarantine.restore_directory_with_mode(
                temporary,
                parent_fd,
                parts[-1],
                moved,
                before.mode,
            )
        finally:
            parent_expected = expected_outputs.get("/".join(parts[:-1]))
            if parent_expected is not None and parent_expected.kind == "directory":
                os.fchmod(parent_fd, parent_expected.mode)
                os.fsync(parent_fd)
            os.close(parent_fd)
    except (CandidateValidationError, OSError) as error:
        raise HandoverError("transaction-owned caller directory mode could not be restored") from error


def _require_rollback_prestate(
    destination: Path | int,
    mutations: _MutationTracker,
    backup: tuple[_TreeEntry, ...],
    quarantine: _TransactionQuarantine,
) -> None:
    """Prove every mutated baseline subtree and retained source before terminal rollback."""
    current = _snapshot_tree(destination)
    for path in {mutation.path for mutation in mutations.operations}:
        prefix = f"{path}/"
        expected_subtree = tuple(
            entry for entry in backup
            if entry.path == path or entry.path.startswith(prefix)
        )
        actual_subtree = tuple(
            entry for entry in current
            if entry.path == path or entry.path.startswith(prefix)
        )
        if actual_subtree != expected_subtree:
            raise HandoverError("transaction rollback retained unresolved mutation state")
    if any(
        quarantine.contains(mutation.source_quarantine)
        for mutation in mutations.operations
    ):
        raise HandoverError("transaction rollback retained unresolved mutation state")


def _unlock_owned_parent(
    destination: Path | int,
    path: str,
    expected_outputs: Mapping[str, _ExpectedPath | None],
) -> bool:
    """Temporarily make a still-candidate-owned parent writable for a child reversal."""
    parts = PurePosixPath(path).parts
    if len(parts) < 2:
        return False
    parent_path = "/".join(parts[:-1])
    expected = expected_outputs.get(parent_path)
    if expected is None or expected.kind != "directory":
        return False
    if not _matches_expected_path(_tree_entry_at(destination, parent_path), expected):
        return False
    try:
        with _opened_tree_root(destination) as root_fd:
            parent_fd = _open_parent(root_fd, parts, create=False)
            try:
                os.fchmod(parent_fd, 0o700)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    except (CandidateValidationError, OSError) as error:
        raise HandoverError("transaction-owned caller directory could not be prepared for rollback") from error
    return True


def _close_journal(journal: _HandoverJournal | None) -> None:
    if journal is None:
        return
    try:
        journal.close()
    except BaseException:
        pass


def _finalize_rollback_journal(
    journal: _HandoverJournal | None,
    *,
    discard_unacknowledged_completion: bool = False,
) -> None:
    """Close a poisoned writer, trim only its final torn tail, then durably terminalize rollback."""
    if journal is None:
        return
    path = journal.path
    _close_journal(journal)
    if discard_unacknowledged_completion:
        try:
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise HandoverError("handover journal parent is unavailable") from error
        try:
            _trim_torn_journal(path, parent_fd)
            _discard_unacknowledged_completion(path, parent_fd)
        finally:
            os.close(parent_fd)
    recovered = _HandoverJournal.reopen(path)
    try:
        recovered.record("rollback-complete")
    finally:
        recovered.close()


def _finalize_rollback_conflict_journal(
    journal: _HandoverJournal | None,
    *,
    discard_unacknowledged_completion: bool = False,
) -> None:
    """Leave durable, retryable evidence when rollback cannot restore its pre-state."""
    if journal is None:
        return
    path = journal.path
    _close_journal(journal)
    if discard_unacknowledged_completion:
        try:
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise HandoverError("handover journal parent is unavailable") from error
        try:
            _trim_torn_journal(path, parent_fd)
            _discard_unacknowledged_completion(path, parent_fd)
        finally:
            os.close(parent_fd)
    recovered = _HandoverJournal.reopen(path)
    try:
        recovered.record(
            "rollback-conflict",
            details={"disposition": "pre-handover-state-not-restored"},
        )
    finally:
        recovered.close()


def _discard_unacknowledged_completion(path: Path, parent_fd: int) -> None:
    """A completion call that raised never authorizes recovery to treat its record as committed."""
    try:
        descriptor = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as error:
        raise HandoverError("handover journal is unavailable") from error
    try:
        data = bytearray()
        while len(data) <= 8 * 1024 * 1024:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > 8 * 1024 * 1024:
            raise HandoverError("handover journal exceeds its limit")
        lines = bytes(data).splitlines(keepends=True)
        if not lines or not lines[-1].endswith(b"\n"):
            return
        try:
            record = json.loads(lines[-1].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HandoverError("handover journal record is malformed") from error
        if not isinstance(record, dict) or canonical_json(record) != lines[-1]:
            raise HandoverError("handover journal record is malformed")
        if record.get("step") != "handover-complete":
            return
        os.ftruncate(descriptor, len(data) - len(lines[-1]))
        os.fsync(descriptor)
        os.fsync(parent_fd)
    finally:
        os.close(descriptor)


def _controller_transaction_root(controller: LifecycleController, value: Path | str) -> Path:
    transactions = controller_directory(controller, "transactions").resolve()
    transaction = Path(value)
    if not transaction.is_absolute():
        transaction = transaction.absolute()
    try:
        information = transaction.lstat()
        if transaction.is_symlink() or not stat.S_ISDIR(information.st_mode):
            raise HandoverError("handover recovery transaction root is unsafe")
        resolved = transaction.resolve(strict=True)
        if resolved.parent != transactions:
            raise HandoverError("handover recovery transaction is not controller-owned")
        return resolved
    except HandoverError:
        raise
    except OSError as error:
        raise HandoverError("handover recovery transaction root is unavailable") from error


def _handover_opening_details(
    baseline: RepositoryBaseline,
    verification: CandidateVerification,
    controller: LifecycleController,
    destination: Path,
    destination_device: int,
    destination_inode: int,
    task_id: str | None,
) -> dict[str, object]:
    return {
        "baseline_digest": baseline.digest,
        "candidate_digest": verification.candidate.digest,
        "controller_id": controller.controller_id,
        "destination": os.fspath(destination),
        "destination_device": destination_device,
        "destination_inode": destination_inode,
        "task_id": task_id,
    }


def _handover_transaction_digest(
    transaction: Path,
    details: Mapping[str, object],
) -> str:
    return hashlib.sha256(canonical_json({
        "opening": dict(details),
        "schema_version": "fanout-handover-transaction-v1",
        "transaction_root": os.fspath(transaction),
    })).hexdigest()


def _recovery_state(
    controller: LifecycleController,
    transaction: Path,
    *,
    terminal_conflict: bool = False,
) -> _HandoverRecoveryState:
    records = _read_journal_records(transaction / "handover.jsonl")
    openings = [record for record in records if record["step"] == "transaction-opened"]
    if len(openings) != 1:
        raise HandoverError("handover recovery record is malformed")
    details = openings[0].get("details")
    legacy_fields = {
        "baseline_digest", "candidate_digest", "controller_id", "destination",
        "destination_device", "destination_inode",
    }
    if (
        not isinstance(details, dict)
        or frozenset(details)
        not in {
            frozenset(legacy_fields),
            frozenset((*legacy_fields, "task_id")),
        }
    ):
        raise HandoverError("handover recovery record is malformed")
    transaction_sha256 = _handover_transaction_digest(transaction, details)
    baseline_digest = _digest(
        details.get("baseline_digest"), "handover recovery baseline digest"
    )
    candidate_digest = _digest(
        details.get("candidate_digest"), "handover recovery candidate digest"
    )
    task_id = details.get("task_id")
    if task_id is not None:
        try:
            task_id = _identity(task_id, "handover recovery task id")
        except LifecycleError as error:
            raise HandoverError("handover recovery task binding is invalid") from error
    if details.get("controller_id") != controller.controller_id:
        raise HandoverError("handover recovery belongs to another controller")
    destination_text = details.get("destination")
    if not isinstance(destination_text, str):
        raise HandoverError("handover recovery destination is malformed")
    destination = Path(destination_text)
    if not destination.is_absolute():
        raise HandoverError("handover recovery destination is malformed")
    identity = _directory_identity(destination)
    recorded_identity = (details.get("destination_device"), details.get("destination_inode"))
    if identity != recorded_identity:
        raise HandoverError("handover recovery destination identity changed")
    completions = [record for record in records if record["step"] == "handover-complete"]
    if len(completions) > 1:
        raise HandoverError("handover recovery completion record is malformed")
    if completions:
        if any(
            record["step"] in {"rollback-complete", "rollback-conflict"}
            for record in records
        ):
            raise HandoverError("handover recovery has conflicting terminal records")
        return _HandoverRecoveryState(
            destination, identity, baseline_digest, candidate_digest,
            task_id, transaction_sha256, "committed", None,
        )
    rollbacks = [record for record in records if record["step"] == "rollback-complete"]
    if len(rollbacks) > 1:
        raise HandoverError("handover recovery rollback record is malformed")
    if rollbacks:
        return _HandoverRecoveryState(
            destination, identity, baseline_digest, candidate_digest,
            task_id, transaction_sha256, "rolled-back", None,
        )
    conflicts = [record for record in records if record["step"] == "rollback-conflict"]
    if conflicts and terminal_conflict:
        return _HandoverRecoveryState(
            destination, identity, baseline_digest, candidate_digest,
            task_id, transaction_sha256, "conflict", None,
        )
    backups = [record for record in records if record["step"] == "backup-created"]
    if not backups:
        return _HandoverRecoveryState(
            destination, identity, baseline_digest, candidate_digest,
            task_id, transaction_sha256, "not-mutated", None,
        )
    if len(backups) != 1:
        raise HandoverError("handover recovery backup record is malformed")
    backup_details = backups[0].get("details")
    if not isinstance(backup_details, dict):
        raise HandoverError("handover recovery backup record is malformed")
    backup_digest = _digest(backup_details.get("tree_digest"), "handover recovery backup digest")
    if not any(
        record["step"].startswith("destination-") and record["step"].endswith("-intent")
        or record["step"] == "final-checks-intent"
        for record in records
    ):
        return _HandoverRecoveryState(
            destination, identity, baseline_digest, candidate_digest,
            task_id, transaction_sha256, "not-mutated", None,
        )
    return _HandoverRecoveryState(
        destination, identity, baseline_digest, candidate_digest,
        task_id, transaction_sha256, "active", backup_digest,
    )


def _recovery_mutations(records: tuple[dict[str, object], ...]) -> _MutationTracker:
    """Rebuild only durable direct-operation intents; a crash may omit their completion records."""
    tracker = _MutationTracker()
    for record in records:
        step = record["step"]
        if not isinstance(step, str) or not step.startswith("destination-"):
            continue
        if step.endswith("-intent"):
            action = step[len("destination-"):-len("-intent")]
            if action not in {"delete", "directory-prepare", "write", "directory-mode"}:
                raise HandoverError("handover recovery mutation evidence is malformed")
            path = record.get("path")
            details = record.get("details")
            if not isinstance(path, str) or not isinstance(details, dict):
                raise HandoverError("handover recovery mutation evidence is malformed")
            if set(details) == {"expected"}:
                tracker.record(action, path, _expected_path_from_payload(details["expected"]))
            elif set(details) == {"before", "expected"}:
                tracker.record(
                    action,
                    path,
                    _expected_path_from_payload(details["expected"]),
                    before=_expected_path_from_payload(details["before"]),
                    before_known=True,
                )
            elif set(details) == {"before", "expected", "source_quarantine"}:
                source_quarantine = details["source_quarantine"]
                if (
                    not isinstance(source_quarantine, str)
                    or len(source_quarantine) != 39
                    or not source_quarantine.startswith("source-")
                    or any(character not in "0123456789abcdef" for character in source_quarantine[7:])
                ):
                    raise HandoverError("handover recovery mutation evidence is malformed")
                tracker.record(
                    action,
                    path,
                    _expected_path_from_payload(details["expected"]),
                    before=_expected_path_from_payload(details["before"]),
                    before_known=True,
                    source_quarantine=source_quarantine,
                )
            else:
                raise HandoverError("handover recovery mutation evidence is malformed")
            continue
        if step in {"destination-source-removed", "destination-created", "destination-source-bound"}:
            path = record.get("path")
            details = record.get("details")
            if not isinstance(path, str) or not isinstance(details, dict):
                raise HandoverError("handover recovery mutation evidence is malformed")
            action = details.get("action")
            if action not in {"delete", "directory-prepare", "write", "directory-mode"}:
                raise HandoverError("handover recovery mutation evidence is malformed")
            mutation = tracker.pending(action, path)
            if step == "destination-source-removed":
                if set(details) != {"action"} or mutation.source_removed:
                    raise HandoverError("handover recovery mutation evidence is malformed")
                mutation.source_removed = True
            elif step == "destination-created":
                if set(details) != {"action", "device", "inode", "kind"} or mutation.created_identity is not None:
                    raise HandoverError("handover recovery mutation evidence is malformed")
                device, inode, kind = details["device"], details["inode"], details["kind"]
                if (
                    type(device) is not int
                    or type(inode) is not int
                    or device < 0
                    or inode < 0
                    or kind not in {"file", "directory", "symlink", "special"}
                ):
                    raise HandoverError("handover recovery mutation evidence is malformed")
                mutation.created_identity = (device, inode, kind)
            else:
                if set(details) != {"action", "device", "inode", "kind"} or mutation.bound_identity is not None:
                    raise HandoverError("handover recovery mutation evidence is malformed")
                device, inode, kind = details["device"], details["inode"], details["kind"]
                if (
                    type(device) is not int
                    or type(inode) is not int
                    or device < 0
                    or inode < 0
                    or kind != "directory"
                ):
                    raise HandoverError("handover recovery mutation evidence is malformed")
                mutation.bound_identity = (device, inode, kind)
            continue
        action = step[len("destination-"):]
        if action not in {"delete", "directory-prepare", "write", "directory-mode"}:
            continue
        path = record.get("path")
        if not isinstance(path, str):
            raise HandoverError("handover recovery mutation evidence is malformed")
        tracker.complete(action, path)
    return tracker


def _read_journal_records(path: Path) -> tuple[dict[str, object], ...]:
    """Read canonical complete journal records, ignoring only an unterminated torn tail."""
    try:
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise HandoverError("handover journal parent is unavailable") from error
    try:
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as error:
            raise HandoverError("handover journal is unavailable") from error
        try:
            data = bytearray()
            while len(data) <= 8 * 1024 * 1024:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > 8 * 1024 * 1024:
                raise HandoverError("handover journal exceeds its limit")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)
    lines = bytes(data).splitlines(keepends=True)
    records: list[dict[str, object]] = []
    for index, line in enumerate(lines):
        if not line.endswith(b"\n"):
            if index == len(lines) - 1:
                break
            raise HandoverError("handover journal has a torn interior record")
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HandoverError("handover journal record is malformed") from error
        if not isinstance(record, dict) or canonical_json(record) != line:
            raise HandoverError("handover journal record is noncanonical")
        if (
            set(record) - {"sequence", "step", "path", "details"}
            or record.get("sequence") != index + 1
            or not isinstance(record.get("step"), str)
            or not record["step"]
        ):
            raise HandoverError("handover journal record is malformed")
        if "path" in record and not isinstance(record["path"], str):
            raise HandoverError("handover journal record is malformed")
        if "details" in record and not isinstance(record["details"], dict):
            raise HandoverError("handover journal record is malformed")
        records.append(record)
    if not records:
        raise HandoverError("handover journal has no durable records")
    return tuple(records)


def _trim_torn_journal(path: Path, parent_fd: int) -> None:
    """Discard only an unterminated tail, which was never a durable journal record."""
    try:
        descriptor = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as error:
        raise HandoverError("handover journal is unavailable") from error
    try:
        data = bytearray()
        while len(data) <= 8 * 1024 * 1024:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > 8 * 1024 * 1024:
            raise HandoverError("handover journal exceeds its limit")
        final_newline = bytes(data).rfind(b"\n") + 1
        if final_newline != len(data):
            os.ftruncate(descriptor, final_newline)
            os.fsync(descriptor)
            os.fsync(parent_fd)
    finally:
        os.close(descriptor)


@contextmanager
def _opened_tree_root(root: Path | int) -> Iterator[int]:
    if isinstance(root, bool):
        raise HandoverError("handover tree is not a real directory")
    if isinstance(root, int):
        try:
            descriptor = os.dup(root)
        except OSError as error:
            raise HandoverError("handover tree is not a real directory") from error
    else:
        try:
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise HandoverError("handover tree is not a real directory") from error
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISDIR(information.st_mode):
            raise HandoverError("handover tree is not a real directory")
        yield descriptor
    finally:
        os.close(descriptor)


def _snapshot_tree(root: Path | int) -> tuple[_TreeEntry, ...]:
    entries: list[_TreeEntry] = []
    with _opened_tree_root(root) as root_fd:
        _snapshot_directory(root_fd, (), entries, top_level=True)
    return tuple(sorted(entries, key=lambda item: item.path.encode("utf-8")))


def _snapshot_directory(fd: int, prefix: tuple[str, ...], entries: list[_TreeEntry], *, top_level: bool) -> None:
    for name in sorted(os.listdir(fd), key=os.fsencode):
        if top_level and name == ".git":
            continue
        path = "/".join((*prefix, name))
        if ".git" in PurePosixPath(path).parts:
            raise HandoverError("handover backup refuses a nested Git metadata path")
        try:
            information = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError as error:
            raise HandoverError("handover tree changed during backup") from error
        mode = stat.S_IMODE(information.st_mode)
        if stat.S_ISDIR(information.st_mode):
            entries.append(_TreeEntry(path, "directory", mode))
            try:
                child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as error:
                raise HandoverError("handover tree contains an unsafe directory") from error
            try:
                _snapshot_directory(child_fd, (*prefix, name), entries, top_level=False)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(information.st_mode):
            try:
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as error:
                raise HandoverError("handover tree changed during backup") from error
            try:
                opened = os.fstat(file_fd)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (information.st_dev, information.st_ino):
                    raise HandoverError("handover tree changed during backup")
                data = _read_all(file_fd)
            finally:
                os.close(file_fd)
            entries.append(_TreeEntry(path, "file", mode, data))
        elif stat.S_ISLNK(information.st_mode):
            try:
                target = os.fsencode(os.readlink(name, dir_fd=fd))
            except OSError as error:
                raise HandoverError("handover tree changed during backup") from error
            try:
                _link_target(path, target)
            except CandidateValidationError as error:
                raise HandoverError(f"handover backup refuses an escaping symlink: {path}") from error
            entries.append(_TreeEntry(path, "symlink", mode, target))
        else:
            raise HandoverError(f"handover backup refuses a special file: {path}")


def _restore_tree(root: Path | int, entries: tuple[_TreeEntry, ...]) -> None:
    with _opened_tree_root(root) as root_fd:
        _clear_directory(root_fd, top_level=True)
    directories = sorted((item for item in entries if item.kind == "directory"), key=lambda item: (len(PurePosixPath(item.path).parts), item.path))
    for item in directories:
        _make_directory(root, item.path, 0o700)
    for item in entries:
        if item.kind == "file":
            _write_entry(root, CandidateEntry(item.path, "file", item.mode, item.data))
        elif item.kind == "symlink":
            _write_entry(root, CandidateEntry(item.path, "symlink", item.mode, item.data))
    for item in reversed(directories):
        _make_directory(root, item.path, item.mode)


def _clear_directory(fd: int, *, top_level: bool) -> None:
    # A failed handover may have just finalized a read-only candidate directory.
    # Its saved mode is restored after clearing, so grant only this owned descriptor
    # temporary owner access to make the rollback tree removable.
    if not top_level:
        try:
            os.fchmod(fd, 0o700)
            os.fsync(fd)
        except OSError as error:
            raise HandoverError("handover directory cannot be prepared for rollback") from error
    for name in os.listdir(fd):
        if top_level and name == ".git":
            continue
        information = os.stat(name, dir_fd=fd, follow_symlinks=False)
        _remove_owned(fd, name, information)
    os.fsync(fd)


def _snapshot_owned_entries(parent_fd: int, name: str, information: os.stat_result) -> tuple[_OwnedTreeEntry, ...]:
    if not stat.S_ISDIR(information.st_mode):
        return ()
    try:
        directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as error:
        raise GarbageCollectionError("run-owned directory cannot be opened safely") from error
    try:
        opened = os.fstat(directory_fd)
        if (opened.st_dev, opened.st_ino) != (information.st_dev, information.st_ino):
            raise GarbageCollectionError("run-owned directory changed while it was claimed")
        entries: list[_OwnedTreeEntry] = []
        _snapshot_owned_directory(directory_fd, (), entries)
        return tuple(entries)
    finally:
        os.close(directory_fd)


def _snapshot_owned_directory(fd: int, prefix: tuple[str, ...], entries: list[_OwnedTreeEntry]) -> None:
    try:
        names = sorted(os.listdir(fd), key=os.fsencode)
    except OSError as error:
        raise GarbageCollectionError("run-owned directory changed while it was claimed") from error
    for name in names:
        try:
            information = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError as error:
            raise GarbageCollectionError("run-owned directory changed while it was claimed") from error
        path = "/".join((*prefix, name))
        entries.append(_OwnedTreeEntry(path, information.st_dev, information.st_ino, _kind(information.st_mode)))
        if not stat.S_ISDIR(information.st_mode):
            continue
        try:
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except OSError as error:
            raise GarbageCollectionError("run-owned directory contains an unsafe descendant") from error
        try:
            opened = os.fstat(child_fd)
            if (opened.st_dev, opened.st_ino) != (information.st_dev, information.st_ino):
                raise GarbageCollectionError("run-owned directory changed while it was claimed")
            _snapshot_owned_directory(child_fd, (*prefix, name), entries)
        finally:
            os.close(child_fd)


def _remove_owned_claim(
    parent_fd: int,
    name: str,
    information: os.stat_result,
    entries: tuple[_OwnedTreeEntry, ...],
    prefix: tuple[str, ...] = (),
    *,
    quarantine_fd: int | None = None,
) -> None:
    expected = _owned_children(entries, prefix)
    if stat.S_ISDIR(information.st_mode):
        try:
            directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as error:
            raise GarbageCollectionError("run-owned directory changed before cleanup") from error
        try:
            opened = os.fstat(directory_fd)
            if (opened.st_dev, opened.st_ino) != (information.st_dev, information.st_ino):
                raise GarbageCollectionError("run-owned path ownership no longer verifies")
            names = sorted(os.listdir(directory_fd), key=os.fsencode)
            if set(names) != set(expected):
                raise GarbageCollectionError("run-owned descendant ownership no longer verifies")
            for child_name in names:
                child = expected[child_name]
                try:
                    child_info = os.stat(child_name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as error:
                    raise GarbageCollectionError("run-owned descendant ownership no longer verifies") from error
                if (
                    (child_info.st_dev, child_info.st_ino) != (child.device, child.inode)
                    or _kind(child_info.st_mode) != child.kind
                ):
                    raise GarbageCollectionError("run-owned descendant ownership no longer verifies")
                if quarantine_fd is None:
                    _remove_owned_claim(directory_fd, child_name, child_info, entries, (*prefix, child_name))
                else:
                    _quarantine_and_remove_owned_child(
                        directory_fd,
                        child_name,
                        child_info,
                        entries,
                        (*prefix, child_name),
                        quarantine_fd,
                    )
        finally:
            os.close(directory_fd)
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError as error:
            raise GarbageCollectionError("run-owned directory could not be deleted") from error
    else:
        if expected:
            raise GarbageCollectionError("run-owned path ownership no longer verifies")
        try:
            os.unlink(name, dir_fd=parent_fd)
        except OSError as error:
            raise GarbageCollectionError("run-owned path could not be deleted") from error
    os.fsync(parent_fd)


def _quarantine_and_remove_owned_child(
    source_parent_fd: int,
    name: str,
    information: os.stat_result,
    entries: tuple[_OwnedTreeEntry, ...],
    prefix: tuple[str, ...],
    quarantine_fd: int,
) -> None:
    """Give each claimed descendant the same move-then-verify deletion boundary as its root."""
    temporary = f"child-{uuid.uuid4().hex}"
    moved = False
    try:
        if not _rename_no_replace(source_parent_fd, name, quarantine_fd, temporary):
            raise GarbageCollectionError("run-owned descendant could not be quarantined without replacement")
        moved = True
        os.fsync(source_parent_fd)
        os.fsync(quarantine_fd)
        moved_information = os.stat(temporary, dir_fd=quarantine_fd, follow_symlinks=False)
        if (
            (moved_information.st_dev, moved_information.st_ino) != (information.st_dev, information.st_ino)
            or _kind(moved_information.st_mode) != _kind(information.st_mode)
        ):
            if _restore_quarantined_name(quarantine_fd, temporary, source_parent_fd, name):
                moved = False
            else:
                raise GarbageCollectionError(
                    "run-owned descendant ownership changed before quarantine verification; "
                    "exact evidence remains quarantined for recovery"
                )
            raise GarbageCollectionError("run-owned descendant ownership changed before quarantine verification")
        try:
            _remove_owned_claim(
                quarantine_fd,
                temporary,
                moved_information,
                entries,
                prefix,
                quarantine_fd=quarantine_fd,
            )
        except GarbageCollectionError:
            if _restore_quarantined_name(quarantine_fd, temporary, source_parent_fd, name):
                moved = False
                raise
            raise GarbageCollectionError(
                "run-owned descendant cleanup failed; exact evidence remains quarantined for recovery"
            )
        moved = False
    except GarbageCollectionError:
        raise
    except OSError as error:
        raise GarbageCollectionError("run-owned descendant could not be quarantined safely") from error
    finally:
        if moved:
            try:
                os.fsync(quarantine_fd)
            except OSError:
                pass


def _restore_quarantined_name(
    quarantine_fd: int,
    temporary: str,
    destination_parent_fd: int,
    name: str,
) -> bool:
    """Put an unremoved artifact back only if no peer has claimed its original spelling."""
    original_mode: int | None = None
    source_descriptor: int | None = None
    try:
        information = os.stat(temporary, dir_fd=quarantine_fd, follow_symlinks=False)
        if stat.S_ISDIR(information.st_mode):
            source_descriptor = os.open(
                temporary,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=quarantine_fd,
            )
            opened = os.fstat(source_descriptor)
            if (opened.st_dev, opened.st_ino) != (information.st_dev, information.st_ino):
                return False
            original_mode = stat.S_IMODE(opened.st_mode)
            os.fchmod(source_descriptor, 0o700)
            os.fsync(source_descriptor)
        if not _rename_no_replace(quarantine_fd, temporary, destination_parent_fd, name):
            if source_descriptor is not None and original_mode is not None:
                os.fchmod(source_descriptor, original_mode)
                os.fsync(source_descriptor)
            return False
        os.fsync(destination_parent_fd)
        os.fsync(quarantine_fd)
        if source_descriptor is not None and original_mode is not None:
            os.fchmod(source_descriptor, original_mode)
            os.fsync(source_descriptor)
        return True
    except OSError:
        # An independently-created replacement owns the spelling now; leave this
        # unverified artifact in the private quarantine instead of overwriting it.
        return False
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)


def _quarantine_and_remove_owned_claim(
    controller: LifecycleController,
    source_parent_fd: int,
    name: str,
    information: os.stat_result,
    claim: RunOwnedPath,
) -> None:
    """Atomically move a claimed name aside before its decisive inode verification and removal."""
    quarantine = controller_directory(controller, "quarantine")
    try:
        quarantine_fd = os.open(quarantine, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise GarbageCollectionError("run-owned quarantine is unavailable") from error
    temporary = f"{claim.evidence_digest}-{uuid.uuid4().hex}"
    moved = False
    try:
        if not _rename_no_replace(source_parent_fd, name, quarantine_fd, temporary):
            raise GarbageCollectionError("run-owned path could not be quarantined without replacement")
        moved = True
        os.fsync(source_parent_fd)
        os.fsync(quarantine_fd)
        moved_information = os.stat(temporary, dir_fd=quarantine_fd, follow_symlinks=False)
        if (
            (moved_information.st_dev, moved_information.st_ino) != (claim.device, claim.inode)
            or _kind(moved_information.st_mode) != claim.kind
        ):
            if _restore_quarantined_name(quarantine_fd, temporary, source_parent_fd, name):
                moved = False
            else:
                raise GarbageCollectionError(
                    "run-owned path ownership changed before quarantine verification; "
                    "exact evidence remains quarantined for recovery"
                )
            raise GarbageCollectionError("run-owned path ownership changed before quarantine verification")
        try:
            _remove_owned_claim(
                quarantine_fd,
                temporary,
                moved_information,
                claim.owned_entries,
                quarantine_fd=quarantine_fd,
            )
        except GarbageCollectionError as error:
            if _restore_quarantined_name(quarantine_fd, temporary, source_parent_fd, name):
                moved = False
                raise
            raise GarbageCollectionError(
                f"{error}; exact evidence remains quarantined for recovery"
            ) from error
        moved = False
    except GarbageCollectionError:
        raise
    except OSError as error:
        raise GarbageCollectionError("run-owned path could not be quarantined safely") from error
    finally:
        if moved:
            try:
                os.fsync(quarantine_fd)
            except OSError:
                pass
        os.close(quarantine_fd)


def _owned_children(entries: tuple[_OwnedTreeEntry, ...], prefix: tuple[str, ...]) -> dict[str, _OwnedTreeEntry]:
    children: dict[str, _OwnedTreeEntry] = {}
    depth = len(prefix)
    for entry in entries:
        parts = PurePosixPath(entry.path).parts
        if parts[:depth] == prefix and len(parts) == depth + 1:
            children[parts[-1]] = entry
    return children


def _remove_quarantined_entry(parent_fd: int, name: str, expected: os.stat_result) -> None:
    """Delete only the exact private inode that a transaction just moved aside."""
    try:
        actual = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise HandoverError("transaction quarantine changed before deletion") from error
    if (
        (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
        or _kind(actual.st_mode) != _kind(expected.st_mode)
    ):
        raise HandoverError("transaction quarantine ownership changed before deletion")
    try:
        if stat.S_ISDIR(actual.st_mode):
            os.rmdir(name, dir_fd=parent_fd)
        else:
            os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as error:
        raise HandoverError("transaction quarantine entry could not be deleted") from error


def _remove_owned(parent_fd: int, name: str, information: os.stat_result) -> None:
    if stat.S_ISDIR(information.st_mode):
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            _clear_directory(child_fd, top_level=False)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _make_directory(root: Path | int, path: str, mode: int) -> None:
    parts = PurePosixPath(path).parts
    try:
        with _opened_tree_root(root) as root_fd:
            parent_fd = _open_parent(root_fd, parts, create=True)
            try:
                try:
                    os.mkdir(parts[-1], mode=mode, dir_fd=parent_fd)
                except FileExistsError:
                    information = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(information.st_mode):
                        raise HandoverError("backup directory conflicts during restore")
                directory_fd = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                try:
                    os.fchmod(directory_fd, mode)
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    except CandidateValidationError as error:
        raise HandoverError("backup directory cannot be restored safely") from error


def _tree_digest(entries: tuple[_TreeEntry, ...]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry.path.encode("utf-8") + b"\0")
        digest.update(entry.kind.encode("ascii") + b"\0")
        digest.update(f"{entry.mode:o}".encode("ascii") + b"\0")
        digest.update(hashlib.sha256(entry.data).digest())
    return digest.hexdigest()


def _claimable_path(path: str) -> None:
    """Never let cleanup erase the controller's own authentication or evidence state."""
    top_level = PurePosixPath(path).parts[0]
    if top_level in {"controller.json", "receipts", "locks", "quarantine"}:
        raise GarbageCollectionError("run-owned claim targets controller control state")
    if path in {"transactions", "verifier"}:
        raise GarbageCollectionError("run-owned claim must name one exact controller artifact")


def _claim_payload(
    root: Path,
    root_device: int,
    root_inode: int,
    path: str,
    device: int,
    inode: int,
    kind: str,
    controller_id: str,
    entries: tuple[_OwnedTreeEntry, ...],
) -> dict[str, object]:
    return {
        "controller_id": controller_id,
        "device": device,
        "inode": inode,
        "kind": kind,
        "owned_entries": [
            {
                "device": entry.device,
                "inode": entry.inode,
                "kind": entry.kind,
                "path": entry.path,
            }
            for entry in entries
        ],
        "path": path,
        "root": os.fspath(root),
        "root_device": root_device,
        "root_inode": root_inode,
    }


def _validate_owned_claim(controller: LifecycleController, claim: RunOwnedPath) -> None:
    try:
        payload = read_evidence(
            controller,
            "gc-claim",
            claim.evidence_name,
            claim.evidence_digest,
        )
    except Exception as error:
        raise GarbageCollectionError("run-owned claim evidence is unavailable") from error
    expected = _claim_payload(
        claim.run_root,
        claim.root_device,
        claim.root_inode,
        claim.path,
        claim.device,
        claim.inode,
        claim.kind,
        claim.controller_id,
        claim.owned_entries,
    )
    if canonical_json(payload) != canonical_json(expected):
        raise GarbageCollectionError("run-owned claim evidence does not match its ownership identity")


def _owned_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise GarbageCollectionError("run-owned path is invalid")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or not pure.parts
        or str(pure) != value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise GarbageCollectionError("run-owned path escapes its root")
    return value


def _no_owned_overlap(claims: tuple[RunOwnedPath, ...]) -> None:
    seen: set[tuple[Path, str]] = set()
    for claim in claims:
        key = (claim.run_root, claim.path)
        if key in seen:
            raise GarbageCollectionError("run-owned paths must be unique")
        seen.add(key)
    for index, left in enumerate(claims):
        for right in claims[index + 1:]:
            if left.run_root == right.run_root and (
                left.path.startswith(right.path + "/") or right.path.startswith(left.path + "/")
            ):
                raise GarbageCollectionError("run-owned paths must not overlap")


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "file"
    return "special"


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or "/" in value or "\\" in value:
        raise LifecycleError(f"{label} must be a simple non-empty identity")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise LifecycleError(f"{label} must be valid UTF-8") from error
    if len(encoded) > 128 or any(ord(character) < 0x20 for character in value):
        raise LifecycleError(f"{label} must be bounded")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise LifecycleError(f"{label} must be a lower-case SHA-256 digest")
    return value


def _baseline(value: RepositoryBaseline) -> None:
    if not isinstance(value, RepositoryBaseline):
        raise LifecycleError("baseline must be a RepositoryBaseline")
