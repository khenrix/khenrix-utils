"""Immutable caller snapshots replayed into separate run-owned seat workspaces.

Read-only seats retain provider-native read-only mode as a second guard. This
portable boundary cannot contain a malicious same-user binary that writes to
arbitrary absolute paths outside its assigned workspace.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Iterable, Mapping

from .artifacts import canonical_json
from .controller import (
    LifecycleController,
    assert_controller,
    read_evidence,
    write_evidence,
)
from .errors import (
    RepositoryError,
    RepositoryIsolationError,
    RepositoryQuotaError,
    RepositorySecretError,
    RepositoryValidationError,
)

if TYPE_CHECKING:
    from .targets import TargetBinding, TargetSpec


_GIT = "git"
_GIT_OPTIONS = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                "-c", f"core.hooksPath={os.devnull}")
_INDEX_ASSUME_UNCHANGED = 0x8000
_INDEX_INTENT_TO_ADD = 0x20000000
_INDEX_SKIP_WORKTREE = 0x40000000
_INDEX_SEMANTIC_FLAGS = _INDEX_ASSUME_UNCHANGED | _INDEX_INTENT_TO_ADD | _INDEX_SKIP_WORKTREE
_INDEX_STAGE_MASK = 0x3000
_INDEX_EXTENDED = 0x4000
_INDEX_NAME_MASK = 0x0FFF
_INDEX_DEBUG = re.compile(
    rb"  ctime: [0-9]+:[0-9]+\n"
    rb"  mtime: [0-9]+:[0-9]+\n"
    rb"  dev: [0-9]+\tino: [0-9]+\n"
    rb"  uid: [0-9]+\tgid: [0-9]+\n"
    rb"  size: [0-9]+\tflags: ([0-9a-f]+)\n"
)
_MAX_IDENTITY_BYTES = 128
_HIGH_RISK_NAMES = frozenset({
    ".env", ".envrc", ".netrc", ".npmrc", ".pgpass", ".pypirc", "credentials",
    "id_ed25519", "id_rsa",
})
_SECRET_PATTERNS = (
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(
        rb"(?i)\b(?:api[_-]?key|access[_-]?key|secret|token|password)\b\s*[:=]\s*"
        rb"(?:['\"])?[A-Za-z0-9_./+=-]{8,}"
    ),
)


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True, slots=True)
class RepoLimits:
    """Byte limits checked before baseline bytes reach a provider workspace."""

    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            type(self.max_file_bytes) is not int or type(self.max_total_bytes) is not int
            or self.max_file_bytes < 0 or self.max_total_bytes < 0
        ):
            raise RepositoryValidationError("repository byte limits must be non-negative integers")


@dataclass(frozen=True, slots=True)
class RepositoryEntry:
    """One full-byte worktree or HEAD entry; symlink data is its target text."""

    path: str
    mode: int
    kind: str
    data: bytes = field(repr=False)
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        _path(self.path, "repository entry path")
        if self.kind not in {"file", "symlink"}:
            raise RepositoryValidationError("repository entry kind must be file or symlink")
        if type(self.mode) is not int or self.mode < 0:
            raise RepositoryValidationError("repository entry mode is invalid")
        if not isinstance(self.data, bytes):
            raise RepositoryValidationError("repository entry data must be bytes")
        object.__setattr__(self, "digest", hashlib.sha256(self.data).hexdigest())


@dataclass(frozen=True, slots=True)
class RepositoryDirectory:
    """One mode-bound directory required by the immutable worktree snapshot."""

    path: str
    mode: int

    def __post_init__(self) -> None:
        _path(self.path, "repository directory path")
        if type(self.mode) is not int or not 0 <= self.mode <= 0o777:
            raise RepositoryValidationError("repository directory mode is invalid")


@dataclass(frozen=True, slots=True)
class RepositoryIndexEntry:
    """One staged Git entry and its supported persistent index behavior flags."""

    path: str
    mode: int
    stage: int
    data: bytes = field(repr=False)
    flags: int = 0

    def __post_init__(self) -> None:
        _path(self.path, "repository index path")
        if type(self.mode) is not int or self.mode < 0:
            raise RepositoryValidationError("repository index mode is invalid")
        if type(self.stage) is not int or not 0 <= self.stage <= 3:
            raise RepositoryValidationError("repository index stage is invalid")
        if not isinstance(self.data, bytes):
            raise RepositoryValidationError("repository index data must be bytes")
        if (
            type(self.flags) is not int or self.flags < 0
            or self.flags & ~_INDEX_SEMANTIC_FLAGS
            or (self.stage != 0 and self.flags)
            or (self.flags & _INDEX_INTENT_TO_ADD and self.data)
        ):
            raise RepositoryValidationError("repository index flags are unsupported")


@dataclass(frozen=True, slots=True)
class RepositoryBaseline:
    """A complete immutable source snapshot independent of later caller changes."""

    repository: Path
    head: str
    head_entries: tuple[RepositoryEntry, ...]
    index_entries: tuple[RepositoryIndexEntry, ...]
    entries: tuple[RepositoryEntry, ...]
    directories: tuple[RepositoryDirectory, ...]
    deleted_paths: tuple[str, ...]
    limits: RepoLimits
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        repository = Path(self.repository)
        if not repository.is_absolute():
            raise RepositoryValidationError("baseline repository path must be absolute")
        if not isinstance(self.head, str) or len(self.head) != 40:
            raise RepositoryValidationError("baseline HEAD must be a full object id")
        for collection, expected in (
            (self.head_entries, RepositoryEntry), (self.index_entries, RepositoryIndexEntry),
            (self.entries, RepositoryEntry),
            (self.directories, RepositoryDirectory),
        ):
            if not isinstance(collection, tuple) or any(not isinstance(item, expected) for item in collection):
                raise RepositoryValidationError("baseline entries have an invalid shape")
        if len({item.path for item in self.head_entries}) != len(self.head_entries):
            raise RepositoryValidationError("baseline HEAD paths must be unique")
        if len({item.path for item in self.entries}) != len(self.entries):
            raise RepositoryValidationError("baseline worktree paths must be unique")
        if len({item.path for item in self.directories}) != len(self.directories):
            raise RepositoryValidationError("baseline directory paths must be unique")
        if len({(item.path, item.stage) for item in self.index_entries}) != len(self.index_entries):
            raise RepositoryValidationError("baseline index entries must be unique by path and stage")
        deleted = tuple(self.deleted_paths)
        if any(not isinstance(item, str) for item in deleted):
            raise RepositoryValidationError("baseline deleted paths must be strings")
        for item in deleted:
            _path(item, "baseline deleted path")
        if len(set(deleted)) != len(deleted):
            raise RepositoryValidationError("baseline deleted paths must be unique")
        if set(deleted) & {item.path for item in self.entries}:
            raise RepositoryValidationError("baseline cannot contain and delete one path")
        if not isinstance(self.limits, RepoLimits):
            raise RepositoryValidationError("baseline limits are invalid")
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "deleted_paths", deleted)
        digest = hashlib.sha256(_baseline_bytes(self)).hexdigest()
        object.__setattr__(self, "digest", digest)


@dataclass(frozen=True, slots=True)
class SeatWorkspace:
    """One newly materialized, remote-less Git workspace owned by a fanout run."""

    root: Path
    seat_id: str
    baseline_digest: str


_WORKSPACE_ISSUER = object()


@dataclass(frozen=True, slots=True)
class SeatWorkspaceVerification:
    """Controller-authenticated binding of one Task 13 seat workspace."""

    workspace: SeatWorkspace
    root_device: int
    root_inode: int
    git_device: int
    git_inode: int
    controller_id: str
    evidence_name: str
    evidence_digest: str
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, SeatWorkspace):
            raise RepositoryValidationError("workspace verification requires a SeatWorkspace")
        for value in (self.root_device, self.root_inode, self.git_device, self.git_inode):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RepositoryValidationError("workspace verification inode evidence is invalid")
        if not _is_digest(self.controller_id):
            raise RepositoryValidationError("workspace verification controller identity is invalid")
        if (
            self._issuer is not _WORKSPACE_ISSUER
            or not isinstance(self.evidence_name, str)
            or self.evidence_name != f"{self.evidence_digest}.json"
            or not _is_digest(self.evidence_digest)
        ):
            raise RepositoryValidationError(
                "workspace verification must be issued by an authenticated controller"
            )


def verify_seat_workspace(
    baseline: RepositoryBaseline,
    workspace: SeatWorkspace,
    *,
    controller: LifecycleController,
) -> SeatWorkspaceVerification:
    """Issue durable evidence for an exact, remote-less, run-owned baseline replay."""
    if not isinstance(baseline, RepositoryBaseline) or not isinstance(workspace, SeatWorkspace):
        raise RepositoryValidationError("workspace verification inputs are invalid")
    try:
        assert_controller(controller)
    except Exception as error:
        raise RepositoryValidationError(
            "workspace verification requires an authenticated lifecycle controller"
        ) from error
    root = workspace.root.resolve()
    if workspace.baseline_digest != baseline.digest:
        raise RepositoryValidationError("workspace uses another immutable baseline")
    try:
        root.relative_to(controller.root)
    except ValueError as error:
        raise RepositoryIsolationError("workspace is not owned by the lifecycle controller") from error
    try:
        captured = capture_repository_baseline(root, limits=baseline.limits)
        if (
            captured.head_entries != baseline.head_entries
            or captured.index_entries != baseline.index_entries
            or captured.entries != baseline.entries
            or captured.directories != baseline.directories
            or captured.deleted_paths != baseline.deleted_paths
        ):
            raise RepositoryValidationError("workspace does not replay the immutable baseline")
        if _git(root, "remote"):
            raise RepositoryIsolationError("seat workspace must not retain a Git remote")
        root_info = root.lstat()
        git_info = (root / ".git").lstat()
    except RepositoryError:
        raise
    except OSError as error:
        raise RepositoryValidationError("workspace verification evidence is unavailable") from error
    if (
        root.is_symlink()
        or not stat.S_ISDIR(root_info.st_mode)
        or (root / ".git").is_symlink()
        or not stat.S_ISDIR(git_info.st_mode)
    ):
        raise RepositoryIsolationError("workspace root and Git directory must be real directories")
    payload = _workspace_payload(
        workspace,
        root,
        root_info,
        git_info,
        controller.controller_id,
    )
    try:
        evidence_name, evidence_digest = write_evidence(
            controller,
            "seat-workspace",
            payload,
        )
    except Exception as error:
        raise RepositoryValidationError("workspace verification evidence could not be persisted") from error
    return SeatWorkspaceVerification(
        SeatWorkspace(root, workspace.seat_id, workspace.baseline_digest),
        root_info.st_dev,
        root_info.st_ino,
        git_info.st_dev,
        git_info.st_ino,
        controller.controller_id,
        evidence_name,
        evidence_digest,
        _WORKSPACE_ISSUER,
    )


def validate_seat_workspace(
    controller: LifecycleController,
    verification: SeatWorkspaceVerification,
) -> None:
    """Reauthenticate one issued workspace binding without trusting caller paths."""
    if not isinstance(verification, SeatWorkspaceVerification):
        raise RepositoryValidationError("verified seat workspace evidence is required")
    try:
        assert_controller(controller)
        if verification.controller_id != controller.controller_id:
            raise RepositoryValidationError("workspace evidence belongs to another controller")
        root = verification.workspace.root
        root_info = root.lstat()
        git_info = (root / ".git").lstat()
        if (
            root.is_symlink()
            or not stat.S_ISDIR(root_info.st_mode)
            or (root / ".git").is_symlink()
            or not stat.S_ISDIR(git_info.st_mode)
            or (root_info.st_dev, root_info.st_ino)
            != (verification.root_device, verification.root_inode)
            or (git_info.st_dev, git_info.st_ino)
            != (verification.git_device, verification.git_inode)
            or _git(root, "remote")
        ):
            raise RepositoryIsolationError("verified seat workspace identity changed")
        persisted = read_evidence(
            controller,
            "seat-workspace",
            verification.evidence_name,
            verification.evidence_digest,
        )
        expected = _workspace_payload(
            verification.workspace,
            root,
            root_info,
            git_info,
            verification.controller_id,
        )
        if canonical_json(persisted) != canonical_json(expected):
            raise RepositoryValidationError("workspace evidence changed association")
    except RepositoryError:
        raise
    except Exception as error:
        raise RepositoryValidationError("workspace verification evidence is unavailable") from error


def validate_seat_workspace_baseline(
    baseline: RepositoryBaseline,
    controller: LifecycleController,
    verification: SeatWorkspaceVerification,
) -> str:
    """Freshly prove the exact baseline bytes in a verified first-turn workspace."""
    if not isinstance(baseline, RepositoryBaseline):
        raise RepositoryValidationError("workspace baseline evidence is invalid")
    validate_seat_workspace(controller, verification)
    if verification.workspace.baseline_digest != baseline.digest:
        raise RepositoryValidationError("workspace uses another immutable baseline")
    captured = capture_repository_baseline(
        verification.workspace.root,
        limits=baseline.limits,
    )
    if (
        captured.head_entries != baseline.head_entries
        or captured.index_entries != baseline.index_entries
        or captured.entries != baseline.entries
        or captured.directories != baseline.directories
        or captured.deleted_paths != baseline.deleted_paths
    ):
        raise RepositoryValidationError("workspace does not replay the immutable baseline")
    return hashlib.sha256(canonical_json({
        "baseline_digest": baseline.digest,
        "schema_version": "fanout-seat-baseline-replay-v1",
        "seat_id": verification.workspace.seat_id,
        "workspace_evidence_sha256": verification.evidence_digest,
    })).hexdigest()


def resume_seat_workspace(
    workspace: SeatWorkspace,
    *,
    controller: LifecycleController,
    evidence_digest: str,
) -> SeatWorkspaceVerification:
    """Reissue an in-memory receipt from exact durable controller evidence."""
    if not isinstance(workspace, SeatWorkspace) or not _is_digest(evidence_digest):
        raise RepositoryValidationError("workspace recovery evidence is invalid")
    name = f"{evidence_digest}.json"
    try:
        payload = read_evidence(controller, "seat-workspace", name, evidence_digest)
    except Exception as error:
        raise RepositoryValidationError("workspace recovery evidence is unavailable") from error
    expected = {
        "baseline_digest", "controller_id", "git_device", "git_inode", "root",
        "root_device", "root_inode", "seat_id",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise RepositoryValidationError("workspace recovery evidence is malformed")
    if (
        payload.get("baseline_digest") != workspace.baseline_digest
        or payload.get("controller_id") != controller.controller_id
        or payload.get("root") != os.fspath(workspace.root.resolve())
        or payload.get("seat_id") != workspace.seat_id
    ):
        raise RepositoryValidationError("workspace recovery evidence changed association")
    values = tuple(payload.get(name) for name in (
        "root_device", "root_inode", "git_device", "git_inode",
    ))
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise RepositoryValidationError("workspace recovery inode evidence is invalid")
    receipt = SeatWorkspaceVerification(
        SeatWorkspace(workspace.root.resolve(), workspace.seat_id, workspace.baseline_digest),
        values[0],
        values[1],
        values[2],
        values[3],
        controller.controller_id,
        name,
        evidence_digest,
        _WORKSPACE_ISSUER,
    )
    validate_seat_workspace(controller, receipt)
    return receipt


def _workspace_payload(
    workspace: SeatWorkspace,
    root: Path,
    root_info: os.stat_result,
    git_info: os.stat_result,
    controller_id: str,
) -> dict[str, object]:
    return {
        "baseline_digest": workspace.baseline_digest,
        "controller_id": controller_id,
        "git_device": git_info.st_dev,
        "git_inode": git_info.st_ino,
        "root": os.fspath(root),
        "root_device": root_info.st_dev,
        "root_inode": root_info.st_ino,
        "seat_id": workspace.seat_id,
    }


def capture_repository_baseline(
    repository: Path | str, *, limits: RepoLimits | None = None,
) -> RepositoryBaseline:
    """Read a full caller state without changing its files, index, refs, config, or worktrees."""
    applied_limits = limits or RepoLimits()
    if not isinstance(applied_limits, RepoLimits):
        raise RepositoryValidationError("limits must be a RepoLimits instance")
    root = _repository_root(repository)
    head = _git(root, "rev-parse", "--verify", "HEAD").strip().decode("ascii", "strict")
    if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
        raise RepositoryValidationError("repository HEAD is not a full object id")
    budget = _Budget(applied_limits)
    head_entries = tuple(_capture_head_entries(root, budget))
    index_entries = tuple(_capture_index_entries(root, budget))
    worktree_entries, deleted_paths = _capture_worktree_entries(root, head_entries, index_entries, budget)
    directories = tuple(_capture_directory_entries(root))
    return RepositoryBaseline(
        repository=root, head=head, head_entries=head_entries, index_entries=index_entries,
        entries=tuple(worktree_entries), directories=directories,
        deleted_paths=tuple(deleted_paths), limits=applied_limits,
    )


def capture_and_bind_targets(
    specs: Mapping[str, TargetSpec], roots: Mapping[str, Path],
) -> tuple[Mapping[str, TargetBinding], Mapping[str, RepositoryBaseline]]:
    """Capture every target after raw Git binding, then recheck every checkout."""
    from .targets import bind_captured_baseline, resolve_target, validate_target_bindings

    if (not isinstance(specs, Mapping) or not specs or not isinstance(roots, Mapping)
            or set(specs) != set(roots)):
        raise RepositoryValidationError("target specs and roots must have identical non-empty ids")
    checked = {}
    initial_status = {}
    for key in sorted(specs):
        if not isinstance(key, str) or getattr(specs[key], "id", None) != key:
            raise RepositoryValidationError("target registry id differs from its spec")
        try:
            checked[key] = resolve_target(specs[key], Path(roots[key]))
            initial_status[key] = _git(
                checked[key].root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
            )
        except RepositoryValidationError as error:
            raise RepositoryValidationError(f"target {key}: {error}") from error
    validate_target_bindings(checked, writable_ids=set())
    captured = {}
    complete = {}
    for key in sorted(checked):
        try:
            captured[key] = capture_repository_baseline(checked[key].root)
            complete[key] = bind_captured_baseline(checked[key], captured[key])
        except RepositoryValidationError as error:
            raise RepositoryValidationError(f"target {key}: {error}") from error
    for key in sorted(complete):
        try:
            current = resolve_target(specs[key], roots[key])
            if current != checked[key]:
                raise RepositoryValidationError("target Git identity changed during capture")
            status = _git(
                current.root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
            )
            if status != initial_status[key]:
                raise RepositoryValidationError("target worktree changed during capture")
            if capture_repository_baseline(current.root).digest != captured[key].digest:
                raise RepositoryValidationError("target baseline changed during capture")
        except RepositoryValidationError as error:
            raise RepositoryValidationError(f"target {key}: {error}") from error
    validate_target_bindings(complete, writable_ids=set())
    return MappingProxyType(complete), MappingProxyType(captured)


def revalidate_target_bindings(
    bindings: Mapping[str, TargetBinding], *, writable_ids: set[str],
) -> Mapping[str, RepositoryBaseline]:
    """Authenticate saved bindings against live roots, refs, status, and baseline bytes."""
    from .targets import validate_target_bindings

    validate_target_bindings(bindings, writable_ids=writable_ids)
    specs = {key: binding.spec for key, binding in bindings.items()}
    roots = {key: binding.root for key, binding in bindings.items()}
    current, baselines = capture_and_bind_targets(specs, roots)
    for key, binding in bindings.items():
        if current[key].head_ref != binding.head_ref:
            raise RepositoryValidationError(f"target {key}: current HEAD differs from saved binding")
        if current[key] != binding:
            raise RepositoryValidationError(f"target {key}: saved Git or baseline binding changed")
        if key in writable_ids:
            if binding.branch_oid is not None:
                try:
                    current_ref = _git(binding.root, "symbolic-ref", "-q", "--no-recurse", "HEAD").decode().strip()
                except RepositoryValidationError as error:
                    raise RepositoryValidationError(
                        f"target {key}: existing ticket branch must be current HEAD",
                    ) from error
                if current_ref != binding.spec.branch_ref or binding.branch_oid != binding.base_oid:
                    raise RepositoryValidationError(
                        f"target {key}: existing ticket branch must be current HEAD",
                    )
            if _git(binding.root, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
                raise RepositoryValidationError(f"target {key}: repository writer requires a clean checkout")
    return baselines


def create_seat_workspace(
    baseline: RepositoryBaseline, run_root: Path | str, seat_id: str,
) -> SeatWorkspace:
    """Create one distinct, remote-less seat checkout solely from an immutable baseline."""
    if not isinstance(baseline, RepositoryBaseline):
        raise RepositoryValidationError("baseline must be a RepositoryBaseline")
    _identity(seat_id, "seat id")
    root = Path(run_root)
    if root.exists() and root.is_symlink():
        raise RepositoryIsolationError("run root must not be a symlink")
    root = root.resolve(strict=False)
    try:
        root.relative_to(baseline.repository)
    except ValueError:
        pass
    else:
        raise RepositoryIsolationError("run root must not be the caller repository or its descendant")
    root.mkdir(parents=True, exist_ok=True)
    seats = root / "seats"
    seats.mkdir(exist_ok=True)
    destination = seats / seat_id
    if destination.exists() or destination.is_symlink():
        raise RepositoryValidationError(f"seat workspace already exists: {destination}")
    destination.mkdir()
    template = Path(tempfile.mkdtemp(prefix=".fanout-empty-template-", dir=seats))
    try:
        _git(destination, "init", "-q", f"--template={template}")
        _write_synthetic_head(destination, baseline)
        _write_index(destination, baseline.index_entries)
        _materialize_worktree(destination, baseline.entries, baseline.directories)
        _replay_index_flags(destination, baseline.index_entries, baseline.deleted_paths)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(template, ignore_errors=True)
    return SeatWorkspace(root=destination, seat_id=seat_id, baseline_digest=baseline.digest)


def _repository_root(repository: Path | str) -> Path:
    candidate = Path(repository)
    if not candidate.is_dir():
        raise RepositoryValidationError("repository must name an existing directory")
    try:
        answer = _git(candidate, "rev-parse", "--show-toplevel").strip().decode("utf-8", "surrogateescape")
    except RepositoryValidationError as error:
        raise RepositoryValidationError("repository must name a Git worktree") from error
    root = Path(answer).resolve()
    if not root.is_dir():
        raise RepositoryValidationError("Git returned a missing repository root")
    return root


def _capture_head_entries(root: Path, budget: "_Budget") -> Iterable[RepositoryEntry]:
    output = _git(root, "ls-tree", "-r", "-z", "HEAD")
    for record in _records(output):
        metadata, separator, encoded_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[1] != b"blob":
            raise RepositoryValidationError("Git returned an invalid HEAD tree entry")
        mode = _git_mode(fields[0])
        path = _decode_path(encoded_path)
        data = _git(root, "cat-file", "blob", fields[2].decode("ascii", "strict"))
        budget.add(path, data)
        kind = "symlink" if mode == 0o120000 else "file"
        if kind == "symlink":
            _reject_escaping_link(root, path, data)
        else:
            _reject_secret(path, data)
        yield RepositoryEntry(path, mode, kind, data)


def _capture_index_entries(root: Path, budget: "_Budget") -> Iterable[RepositoryIndexEntry]:
    # Git documents --debug as unstable; reject any unfamiliar layout or bit before a seat can run.
    output = _git(root, "ls-files", "--stage", "--debug", "-z")
    offset = 0
    while offset < len(output):
        terminator = output.find(b"\0", offset)
        if terminator < 0:
            raise RepositoryValidationError("Git returned malformed index debug output")
        record = output[offset:terminator]
        debug = _INDEX_DEBUG.match(output, terminator + 1)
        if debug is None:
            raise RepositoryValidationError("Git returned an unknown index debug layout")
        offset = debug.end()
        metadata, separator, encoded_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise RepositoryValidationError("Git returned an invalid index entry")
        mode, object_id, stage = _git_mode(fields[0]), fields[1], fields[2]
        try:
            decoded_stage = int(stage)
        except ValueError as error:
            raise RepositoryValidationError("Git returned an invalid index stage") from error
        path = _decode_path(encoded_path)
        raw_flags = int(debug.group(1), 16)
        if (
            raw_flags & ~(_INDEX_NAME_MASK | _INDEX_STAGE_MASK | _INDEX_EXTENDED | _INDEX_SEMANTIC_FLAGS)
            or (raw_flags & _INDEX_STAGE_MASK) >> 12 != decoded_stage
            or bool(raw_flags & _INDEX_EXTENDED)
            != bool(raw_flags & (_INDEX_INTENT_TO_ADD | _INDEX_SKIP_WORKTREE))
        ):
            raise RepositoryValidationError("Git returned unsupported index flags")
        data = _git(root, "cat-file", "blob", object_id.decode("ascii", "strict"))
        budget.add(path, data)
        if mode != 0o120000:
            _reject_secret(path, data)
        yield RepositoryIndexEntry(path, mode, decoded_stage, data, raw_flags & _INDEX_SEMANTIC_FLAGS)


def _capture_worktree_entries(
    root: Path, head_entries: tuple[RepositoryEntry, ...],
    index_entries: tuple[RepositoryIndexEntry, ...], budget: "_Budget",
) -> tuple[list[RepositoryEntry], list[str]]:
    listed = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    paths = {_decode_path(record) for record in _records(listed)}
    paths.update(item.path for item in head_entries)
    paths.update(item.path for item in index_entries)
    entries: list[RepositoryEntry] = []
    deleted: list[str] = []
    for path in sorted(paths):
        entry_path = root / Path(path)
        try:
            information = entry_path.lstat()
        except FileNotFoundError:
            deleted.append(path)
            continue
        if stat.S_ISLNK(information.st_mode):
            data = os.fsencode(os.readlink(entry_path))
            budget.add(path, data)
            _reject_escaping_link(root, path, data)
            entries.append(RepositoryEntry(path, stat.S_IMODE(information.st_mode), "symlink", data))
        elif stat.S_ISREG(information.st_mode):
            with entry_path.open("rb") as handle:
                data = handle.read()
            budget.add(path, data)
            _reject_secret(path, data)
            entries.append(RepositoryEntry(path, stat.S_IMODE(information.st_mode), "file", data))
        else:
            raise RepositoryIsolationError(f"baseline path is not a regular file or symlink: {path}")
    return entries, deleted


def _capture_directory_entries(root: Path) -> Iterable[RepositoryDirectory]:
    """Bind every real, non-Git worktree directory, including empty directories, to the baseline."""
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise RepositoryIsolationError("baseline directory root is unavailable") from error
    directories: list[RepositoryDirectory] = []
    try:
        _capture_directory_modes(root_fd, (), directories, top_level=True)
    finally:
        os.close(root_fd)
    return tuple(sorted(directories, key=lambda entry: entry.path.encode("utf-8", "surrogateescape")))


def _capture_directory_modes(
    parent_fd: int,
    prefix: tuple[str, ...],
    directories: list[RepositoryDirectory],
    *,
    top_level: bool,
) -> None:
    try:
        names = sorted(os.listdir(parent_fd), key=os.fsencode)
    except OSError as error:
        raise RepositoryIsolationError("baseline directory cannot be listed safely") from error
    for name in names:
        if name == ".git":
            if top_level:
                continue
            raise RepositoryIsolationError("baseline contains nested Git metadata")
        try:
            information = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise RepositoryIsolationError(f"baseline directory is unavailable: {name}") from error
        if not stat.S_ISDIR(information.st_mode):
            continue
        path = "/".join((*prefix, name))
        directories.append(RepositoryDirectory(path, stat.S_IMODE(information.st_mode)))
        try:
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as error:
            raise RepositoryIsolationError(f"baseline directory is not a real directory: {path}") from error
        try:
            opened = os.fstat(child_fd)
            if (opened.st_dev, opened.st_ino) != (information.st_dev, information.st_ino):
                raise RepositoryIsolationError(f"baseline directory changed while captured: {path}")
            _capture_directory_modes(child_fd, (*prefix, name), directories, top_level=False)
        finally:
            os.close(child_fd)


def _write_synthetic_head(destination: Path, baseline: RepositoryBaseline) -> None:
    staged = _index_info(destination, (
        (entry.path, entry.mode, 0, entry.data) for entry in baseline.head_entries
    ))
    _update_index(destination, staged)
    tree = _git(destination, "write-tree").strip().decode("ascii", "strict")
    commit = _git(
        destination, "commit-tree", tree, "-m", "immutable llm-fanout baseline",
        extra_environment={
            "GIT_AUTHOR_NAME": "llm-fanout", "GIT_AUTHOR_EMAIL": "fanout@invalid",
            "GIT_COMMITTER_NAME": "llm-fanout", "GIT_COMMITTER_EMAIL": "fanout@invalid",
        },
    ).strip().decode("ascii", "strict")
    _git(destination, "update-ref", "refs/heads/fanout-baseline", commit)
    _git(destination, "symbolic-ref", "HEAD", "refs/heads/fanout-baseline")


def _write_index(destination: Path, entries: tuple[RepositoryIndexEntry, ...]) -> None:
    _git(destination, "read-tree", "--empty")
    staged = _index_info(destination, ((entry.path, entry.mode, entry.stage, entry.data) for entry in entries))
    _update_index(destination, staged)


def _replay_index_flags(
    destination: Path,
    entries: tuple[RepositoryIndexEntry, ...],
    deleted_paths: tuple[str, ...],
) -> None:
    intent = tuple(entry.path for entry in entries if entry.flags & _INDEX_INTENT_TO_ADD)
    if intent:
        paths = b"".join(os.fsencode(path) + b"\0" for path in intent)
        deleted = set(deleted_paths)
        placeholders: list[Path] = []
        created_directories: list[Path] = []
        original_modes: dict[Path, int] = {}
        try:
            for entry in entries:
                if entry.flags & _INDEX_INTENT_TO_ADD and entry.path in deleted:
                    _prepare_deleted_intent(
                        destination, entry, placeholders, created_directories, original_modes,
                    )
            _git(destination, "update-index", "--force-remove", "-z", "--stdin", input=paths)
            _git(
                destination, "add", "-N", "-f", "--pathspec-from-file=-", "--pathspec-file-nul",
                input=paths, extra_environment={"GIT_LITERAL_PATHSPECS": "1"},
            )
        finally:
            for placeholder in reversed(placeholders):
                placeholder.unlink()
            for directory in reversed(created_directories):
                directory.rmdir()
            for directory, mode in reversed(tuple(original_modes.items())):
                directory.chmod(mode)
    for flag, option in (
        (_INDEX_SKIP_WORKTREE, "--skip-worktree"),
        (_INDEX_ASSUME_UNCHANGED, "--assume-unchanged"),
    ):
        paths = b"".join(os.fsencode(entry.path) + b"\0" for entry in entries if entry.flags & flag)
        if paths:
            _git(destination, "update-index", option, "-z", "--stdin", input=paths)


def _prepare_deleted_intent(
    destination: Path,
    entry: RepositoryIndexEntry,
    placeholders: list[Path],
    created_directories: list[Path],
    original_modes: dict[Path, int],
) -> None:
    if entry.mode not in {0o100644, 0o100755}:
        raise RepositoryIsolationError("deleted intent-to-add path has an unsupported object type")
    parent = destination
    try:
        for part in PurePosixPath(entry.path).parts[:-1]:
            _make_seat_directory_writable(parent, original_modes)
            child = parent / part
            try:
                child_information = child.lstat()
            except FileNotFoundError:
                child.mkdir(mode=0o700)
                created_directories.append(child)
                child.chmod(0o700)
            else:
                if not stat.S_ISDIR(child_information.st_mode):
                    raise RepositoryIsolationError("deleted intent-to-add parent is not a real directory")
            parent = child
        _make_seat_directory_writable(parent, original_modes)
        placeholder = parent / PurePosixPath(entry.path).name
        with placeholder.open("xb"):
            pass
        placeholders.append(placeholder)
        placeholder.chmod(entry.mode & 0o777)
    except OSError as error:
        raise RepositoryIsolationError("deleted intent-to-add path cannot be materialized safely") from error


def _make_seat_directory_writable(directory: Path, original_modes: dict[Path, int]) -> None:
    information = directory.lstat()
    if not stat.S_ISDIR(information.st_mode):
        raise RepositoryIsolationError("deleted intent-to-add parent is not a real directory")
    mode = stat.S_IMODE(information.st_mode)
    if mode & 0o700 != 0o700:
        original_modes.setdefault(directory, mode)
        directory.chmod(mode | 0o700)


def _index_info(destination: Path, entries: Iterable[tuple[str, int, int, bytes]]) -> bytes:
    lines: list[bytes] = []
    for path, mode, stage, data in entries:
        oid = _git(destination, "hash-object", "-w", "--stdin", input=data).strip()
        lines.append(f"{mode:o} ".encode("ascii") + oid + f" {stage}\t".encode("ascii") + os.fsencode(path) + b"\0")
    return b"".join(lines)


def _update_index(destination: Path, data: bytes) -> None:
    if data:
        _git(destination, "update-index", "-z", "--index-info", input=data)


def _materialize_worktree(
    destination: Path,
    entries: tuple[RepositoryEntry, ...],
    directories: tuple[RepositoryDirectory, ...],
) -> None:
    for directory in sorted(directories, key=lambda item: (len(PurePosixPath(item.path).parts), item.path)):
        (destination / Path(directory.path)).mkdir(parents=True, exist_ok=True)
    for entry in entries:
        output = destination / Path(entry.path)
        output.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "symlink":
            os.symlink(os.fsdecode(entry.data), output)
        else:
            with output.open("xb") as handle:
                handle.write(entry.data)
            os.chmod(output, entry.mode)
    for directory in sorted(directories, key=lambda item: (-len(PurePosixPath(item.path).parts), item.path)):
        os.chmod(destination / Path(directory.path), directory.mode)


def _git(
    cwd: Path, *args: str, input: bytes | None = None,
    extra_environment: Mapping[str, str] | None = None,
) -> bytes:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    })
    if extra_environment:
        environment.update(extra_environment)
    result = subprocess.run(
        (_GIT, "-C", os.fspath(cwd), *_GIT_OPTIONS, *args), input=input,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, check=False,
    )
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise RepositoryValidationError(f"git {' '.join(args[:2]) or 'command'} failed: {message}")
    return result.stdout


def _path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RepositoryValidationError(f"{label} must be a non-empty string")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise RepositoryIsolationError(f"{label} escapes its repository")
    if "\x00" in value:
        raise RepositoryValidationError(f"{label} contains NUL")
    return value


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or "/" in value or "\\" in value:
        raise RepositoryValidationError(f"{label} must be a simple non-empty identity")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RepositoryValidationError(f"{label} must be valid UTF-8") from error
    if len(encoded) > _MAX_IDENTITY_BYTES or any(ord(character) < 0x20 for character in value):
        raise RepositoryValidationError(f"{label} must be a bounded identity")
    return value


def _decode_path(value: bytes) -> str:
    path = os.fsdecode(value)
    return _path(path, "Git path")


def _git_mode(value: bytes) -> int:
    try:
        mode = int(value, 8)
    except ValueError as error:
        raise RepositoryValidationError("Git returned an invalid mode") from error
    if mode not in {0o100644, 0o100755, 0o120000}:
        raise RepositoryIsolationError("baseline contains an unsupported Git entry type")
    return mode


def _records(value: bytes) -> tuple[bytes, ...]:
    if not value:
        return ()
    if not value.endswith(b"\0"):
        raise RepositoryValidationError("Git returned malformed NUL-delimited output")
    return tuple(record for record in value[:-1].split(b"\0") if record)


def _reject_escaping_link(root: Path, path: str, data: bytes) -> None:
    target = os.fsdecode(data)
    if os.path.isabs(target):
        raise RepositoryIsolationError(f"symlink escapes repository: {path}")
    resolved = (root / Path(path).parent / target).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise RepositoryIsolationError(f"symlink escapes repository: {path}") from error


def _reject_secret(path: str, data: bytes) -> None:
    name = PurePosixPath(path).name
    if any(name == candidate or name.startswith(candidate + ".") for candidate in _HIGH_RISK_NAMES):
        raise RepositorySecretError(f"repository baseline contains a high-risk secret filename: {path}")
    if any(pattern.search(data) for pattern in _SECRET_PATTERNS):
        raise RepositorySecretError(f"repository baseline contains a possible secret: {path}")


@dataclass(slots=True)
class _Budget:
    limits: RepoLimits
    total: int = 0

    def add(self, path: str, data: bytes) -> None:
        size = len(data)
        if size > self.limits.max_file_bytes:
            raise RepositoryQuotaError(
                f"repository file quota exceeded for {path}: {size} > {self.limits.max_file_bytes} bytes"
            )
        self.total += size
        if self.total > self.limits.max_total_bytes:
            raise RepositoryQuotaError(
                f"repository total quota exceeded: {self.total} > {self.limits.max_total_bytes} bytes"
            )


def _baseline_bytes(baseline: RepositoryBaseline) -> bytes:
    digest = hashlib.sha256()
    # V2 blocks recovery with old receipts that never bound semantic index flags.
    digest.update(b"fanout-repository-baseline-v2\0")
    for label, entries in ((b"head", baseline.head_entries), (b"index", baseline.index_entries), (b"worktree", baseline.entries)):
        digest.update(label + b"\0")
        for entry in entries:
            digest.update(entry.path.encode("utf-8", "surrogateescape") + b"\0")
            digest.update(f"{entry.mode:o}".encode() + b"\0")
            if isinstance(entry, RepositoryEntry):
                digest.update(entry.kind.encode() + b"\0")
            else:
                digest.update(str(entry.stage).encode() + b"\0")
                digest.update(str(entry.flags).encode() + b"\0")
            digest.update(hashlib.sha256(entry.data).digest())
    digest.update(b"head-oid\0" + baseline.head.encode("ascii") + b"\0")
    for path in baseline.deleted_paths:
        digest.update(b"deleted\0" + path.encode("utf-8", "surrogateescape") + b"\0")
    for directory in baseline.directories:
        digest.update(b"directory\0" + directory.path.encode("utf-8", "surrogateescape") + b"\0")
        digest.update(f"{directory.mode:o}".encode() + b"\0")
    return digest.digest()
