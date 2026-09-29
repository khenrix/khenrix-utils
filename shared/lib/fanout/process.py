"""Bounded, shell-free child-process execution for fanout providers."""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .errors import ProcessValidationError, SlotCapacityConflictError, SlotTimeoutError


DEFAULT_EXECUTOR_SLOTS = 6
DEFAULT_MAX_DEPTH = 2
_SAFE_ENVIRONMENT_NAMES = frozenset({
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "TERM",
    "TMPDIR",
    "TZ",
})
_POLL_INTERVAL = 0.02
_CLAUDE_VERTEX_NAMES = frozenset({
    "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION",
})


class ProcessStatus(str, Enum):
    """The terminal category of one attempted child command."""

    EXIT = "exit"
    SPAWN_ERROR = "spawn-error"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class TimeoutEvidence:
    """Evidence retained when a command exceeded its deadline."""

    timeout: float
    terminated_group: bool
    killed_group: bool
    reaped: bool


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Captured terminal evidence for an argv child command."""

    status: ProcessStatus
    returncode: int | None
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    timeout: TimeoutEvidence | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessCommand:
    """One validated shell-free command, with private input carried on stdin."""

    argv: Sequence[str]
    stdin: bytes = field(repr=False)
    cwd: Path | str = "."
    timeout: float = 120.0
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    slot_cap: int = DEFAULT_EXECUTOR_SLOTS
    slot_timeout: float = 30.0
    slot_root: Path | str | None = None
    max_depth: int = DEFAULT_MAX_DEPTH
    term_grace: float = 0.5
    reap_timeout: float = 2.0
    native_original: ProcessCommand | None = field(default=None, repr=False, compare=False)
    native_boundary: object | None = field(default=None, repr=False, compare=False)
    native_request: object | None = field(default=None, repr=False, compare=False)
    native_controller: object | None = field(default=None, repr=False, compare=False)
    native_verification: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.argv, str) or not isinstance(self.argv, Sequence):
            raise ProcessValidationError("argv must be a non-empty sequence of strings")
        argv = tuple(self.argv)
        if not argv or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv):
            raise ProcessValidationError("argv must contain non-empty strings without NUL bytes")
        if not isinstance(self.stdin, bytes):
            raise ProcessValidationError("stdin must be bytes")
        cwd = Path(self.cwd)
        if not cwd.is_dir():
            raise ProcessValidationError("cwd must name an existing directory")
        if not isinstance(self.environment, Mapping):
            raise ProcessValidationError("environment must be a mapping")
        _validate_duration("timeout", self.timeout, positive=True)
        _validate_duration("slot timeout", self.slot_timeout, positive=False)
        _validate_duration("TERM grace", self.term_grace, positive=False)
        _validate_duration("reap timeout", self.reap_timeout, positive=True)
        _validate_slot_cap(self.slot_cap)
        if isinstance(self.max_depth, bool) or not isinstance(self.max_depth, int) or self.max_depth < 0:
            raise ProcessValidationError("max depth must be a non-negative integer")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "cwd", cwd)


def build_child_environment(base: Mapping[str, str] | None = None, *,
                            overrides: Mapping[str, str] | None = None,
                            max_depth: int = DEFAULT_MAX_DEPTH) -> dict[str, str]:
    """Build the minimal environment admitted to an executor child.

    Ambient variables are never inherited wholesale.  In particular, Git's repository
    selectors and injected config variables, ambient provider credentials, and memory
    helpers cannot cross this boundary because none are inherited.
    """
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
        raise ProcessValidationError("max depth must be a non-negative integer")
    source = os.environ if base is None else base
    if not isinstance(source, Mapping):
        raise ProcessValidationError("base environment must be a mapping")
    # A pinned agy binary must not replace itself between fanout rounds.
    child: dict[str, str] = {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"}
    for name in _SAFE_ENVIRONMENT_NAMES:
        value = source.get(name)
        if value is None:
            continue
        _validate_environment_value(name, value)
        child[name] = value

    if overrides is not None:
        if not isinstance(overrides, Mapping):
            raise ProcessValidationError("environment overrides must be a mapping")
        if _CLAUDE_VERTEX_NAMES.intersection(overrides):
            route = default_claude_vertex_route()
            if route is None or {name: overrides.get(name) for name in _CLAUDE_VERTEX_NAMES} != route:
                raise ProcessValidationError("Claude Vertex route differs from private settings")
        for name, value in overrides.items():
            if name == "AGY_ADC_AUTH":
                if value != "true":
                    raise ProcessValidationError("AGY_ADC_AUTH override must be literal true")
            elif name == "GOOGLE_APPLICATION_CREDENTIALS":
                adc = default_claude_adc_path()
                if adc is None or value != str(adc):
                    raise ProcessValidationError(
                        "GOOGLE_APPLICATION_CREDENTIALS override must name the private default ADC file"
                    )
            elif name in {"GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_REGION"}:
                if value != "eu":
                    raise ProcessValidationError(f"{name} override must be literal eu")
            elif name == "HOME":
                home = Path(value)
                if not home.is_absolute() or not home.is_dir() or home.is_symlink():
                    raise ProcessValidationError("HOME override must name a real private directory")
                info = home.lstat()
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ProcessValidationError("HOME override must name a real private directory")
            elif name == "XDG_CONFIG_HOME":
                home = overrides.get("HOME")
                if home is None or value != str(Path(home) / ".config"):
                    raise ProcessValidationError("XDG_CONFIG_HOME must be inside the private HOME")
                try:
                    info = Path(value).lstat()
                except OSError as error:
                    raise ProcessValidationError("XDG_CONFIG_HOME must be a real private directory") from error
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    raise ProcessValidationError("XDG_CONFIG_HOME must be a real private directory")
            elif name in _CLAUDE_VERTEX_NAMES:
                pass  # The complete route was checked against private settings above.
            elif name not in _SAFE_ENVIRONMENT_NAMES:
                raise ProcessValidationError(f"environment override is not allowlisted: {name}")
            _validate_environment_value(name, value)
            child[name] = value

    raw_depth = source.get("LLM_FANOUT_DEPTH", "0")
    _validate_environment_value("LLM_FANOUT_DEPTH", raw_depth)
    try:
        depth = int(raw_depth)
    except ValueError as error:
        raise ProcessValidationError("LLM_FANOUT_DEPTH must be a non-negative integer") from error
    if depth < 0 or str(depth) != raw_depth:
        raise ProcessValidationError("LLM_FANOUT_DEPTH must be a non-negative integer")
    if depth >= max_depth:
        raise ProcessValidationError(f"fanout depth {depth} reaches configured limit {max_depth}")

    # Set rather than drop these: an absent Git config variable restores the user's files.
    child.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "KHENRIX_NESTED_AGENT": "1",
        "LLM_FANOUT_DEPTH": str(depth + 1),
    })
    return child


def default_claude_adc_path() -> Path | None:
    """Return the owner's private default ADC file path without opening its contents."""
    path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    try:
        info = path.lstat()
    except OSError:
        return None
    if (not path.is_absolute() or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600):
        return None
    return path


def default_claude_vertex_route() -> dict[str, str] | None:
    """Read only the owner's pinned Vertex route from private Claude settings.

    Restricted Claude ignores user settings, so the selected non-secret routing
    fields must be passed explicitly.  Credentials themselves remain on disk.
    """
    directory = Path.home() / ".claude"
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProcessValidationError("Claude Vertex settings directory is unsafe") from error
    try:
        directory_info = os.fstat(directory_fd)
        if (not stat.S_ISDIR(directory_info.st_mode)
                or directory_info.st_uid != os.getuid()
                or stat.S_IMODE(directory_info.st_mode) != 0o700):
            raise ProcessValidationError("Claude Vertex settings directory is unsafe")
        try:
            settings_fd = os.open("settings.json", os.O_RDONLY | os.O_NOFOLLOW,
                                  dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ProcessValidationError("Claude Vertex settings file is unsafe") from error
        try:
            info = os.fstat(settings_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 128 * 1024):
                raise ProcessValidationError("Claude Vertex settings file is unsafe")
            raw = os.read(settings_fd, info.st_size + 1)
            if len(raw) != info.st_size:
                raise ProcessValidationError("Claude Vertex settings file changed while reading")
        finally:
            os.close(settings_fd)
    finally:
        os.close(directory_fd)
    try:
        document = json.loads(raw)
        values = document["env"]
    except (ValueError, TypeError, KeyError) as error:
        raise ProcessValidationError("Claude Vertex settings are invalid") from error
    if not isinstance(values, dict):
        raise ProcessValidationError("Claude Vertex settings are invalid")
    route = {name: values.get(name) for name in _CLAUDE_VERTEX_NAMES}
    if (route["CLAUDE_CODE_USE_VERTEX"] != "1"
            or route["CLOUD_ML_REGION"] != "eu"
            or not isinstance(route["ANTHROPIC_VERTEX_PROJECT_ID"], str)
            or re.fullmatch(r"[a-z][a-z0-9-]{4,62}",
                            route["ANTHROPIC_VERTEX_PROJECT_ID"]) is None):
        raise ProcessValidationError("Claude Vertex route is not an admitted EU route")
    return route


@contextlib.contextmanager
def executor_slot(*, root: Path | str | None = None, cap: int = DEFAULT_EXECUTOR_SLOTS,
                  timeout: float = 30.0) -> Iterator[None]:
    """Hold one owner-local, kernel-released executor slot across processes.

    Admission is bounded but deliberately non-fair: callers repeatedly scan from
    the lowest free slot, so a continually losing caller may time out.
    """
    _validate_slot_cap(cap)
    _validate_duration("slot timeout", timeout, positive=False)
    try:
        import fcntl
    except ImportError as error:  # pragma: no cover - fanout's supported hosts are POSIX.
        raise ProcessValidationError("machine-wide slots require POSIX file locking") from error

    root_fd = _open_slot_root(root)
    lock_fd: int | None = None
    deadline = time.monotonic() + timeout
    try:
        _ensure_slot_capacity(root_fd, cap, fcntl, deadline)
        while lock_fd is None:
            for index in range(cap):
                candidate = _open_slot_file(root_fd, index)
                try:
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    os.close(candidate)
                    if error.errno in {errno.EACCES, errno.EAGAIN}:
                        continue
                    raise ProcessValidationError("cannot acquire executor slot") from error
                lock_fd = candidate
                break
            if lock_fd is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SlotTimeoutError(f"no executor slot became available within {timeout:g}s")
            time.sleep(min(_POLL_INTERVAL, remaining))
        yield
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(root_fd)


@contextlib.contextmanager
def exact_session_lock(executor_id: str, session_id: str, *,
                       root: Path | str | None = None,
                       timeout: float = 30.0) -> Iterator[None]:
    """Serialize one exact provider session across coordinators and processes."""
    if (
        not isinstance(executor_id, str) or not executor_id or "\x00" in executor_id
        or not isinstance(session_id, str) or not session_id or "\x00" in session_id
    ):
        raise ProcessValidationError("exact session lock requires provider and session identities")
    _validate_duration("session lock timeout", timeout, positive=False)
    try:
        import fcntl
    except ImportError as error:  # pragma: no cover - supported hosts are POSIX.
        raise ProcessValidationError("exact session locks require POSIX file locking") from error
    identity = hashlib.sha256(
        executor_id.encode("utf-8") + b"\0" + session_id.encode("utf-8")
    ).hexdigest()
    root_fd = _open_slot_root(root)
    lock_fd = _open_private_state_file(root_fd, f"session-{identity}.lock")
    try:
        _lock_before_deadline(lock_fd, fcntl, time.monotonic() + timeout)
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
            os.close(root_fd)


def run_command(command: ProcessCommand, *,
                base_environment: Mapping[str, str] | None = None) -> ProcessResult:
    """Run one argv command with bounded slot admission and process-group cleanup."""
    if not isinstance(command, ProcessCommand):
        raise ProcessValidationError("command must be a ProcessCommand")
    if os.name != "posix":  # pragma: no cover - the contract requires a killable process group.
        raise ProcessValidationError("fanout process execution requires POSIX sessions")
    native = command.native_boundary is not None
    if native and base_environment is not None:
        raise ProcessValidationError("native seat environment is fixed at boundary issuance")
    environment = None if native else build_child_environment(
        base_environment, overrides=command.environment, max_depth=command.max_depth,
    )
    with executor_slot(root=command.slot_root, cap=command.slot_cap, timeout=command.slot_timeout):
        if native:
            from .native_boundary import validate_native_boundary
            original = command.native_original
            if original is None:
                raise ProcessValidationError("native seat command lacks its exact original")
            validate_native_boundary(
                command.native_boundary, command.native_request, original,
                command.native_controller, command.native_verification,
            )
            expected = ("/usr/bin/sandbox-exec", "-f",
                        str(command.native_boundary.profile_path),
                        *command.native_boundary.resolved_argv)
            expected_environment = {
                "HOME": str(command.native_boundary.state_root / "home"),
                "TMPDIR": str(command.native_boundary.state_root / "tmp"),
            }
            if (tuple(command.argv) != expected
                    or dict(command.environment) != expected_environment
                    or command.stdin != original.stdin or command.cwd != original.cwd
                    or command.timeout != original.timeout
                    or command.slot_cap != original.slot_cap
                    or command.slot_timeout != original.slot_timeout
                    or command.slot_root != original.slot_root
                    or command.max_depth != original.max_depth
                    or command.term_grace != original.term_grace
                    or command.reap_timeout != original.reap_timeout):
                raise ProcessValidationError("native seat command wrapper changed")
            environment = dict(command.native_boundary.frozen_environment)
        assert environment is not None
        try:
            process = subprocess.Popen(
                command.argv,
                cwd=command.cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            return ProcessResult(
                status=ProcessStatus.SPAWN_ERROR,
                returncode=None,
                stdout=b"",
                stderr=b"",
                error=f"{type(error).__name__}: {error.strerror or 'could not start command'}",
            )
        try:
            stdout, stderr = process.communicate(input=command.stdin, timeout=command.timeout)
        except subprocess.TimeoutExpired as first:
            return _timed_out_result(process, command, first)
        except BaseException:
            _cleanup_group(process, command.term_grace, command.reap_timeout)
            raise
    return ProcessResult(
        status=ProcessStatus.EXIT,
        returncode=process.returncode,
        stdout=_as_bytes(stdout),
        stderr=_as_bytes(stderr),
    )


def _timed_out_result(process: subprocess.Popen[bytes], command: ProcessCommand,
                      first: subprocess.TimeoutExpired) -> ProcessResult:
    terminated, killed, reaped = _cleanup_group(
        process,
        command.term_grace,
        command.reap_timeout,
    )
    stdout = _as_bytes(first.output)
    stderr = _as_bytes(first.stderr)
    try:
        drained_stdout, drained_stderr = process.communicate(timeout=command.reap_timeout)
        stdout = _prefer_complete_capture(stdout, _as_bytes(drained_stdout))
        stderr = _prefer_complete_capture(stderr, _as_bytes(drained_stderr))
    except subprocess.TimeoutExpired as second:
        stdout = _prefer_complete_capture(stdout, _as_bytes(second.output))
        stderr = _prefer_complete_capture(stderr, _as_bytes(second.stderr))
        _close_pipes(process)
    return ProcessResult(
        status=ProcessStatus.TIMEOUT,
        returncode=process.returncode if reaped else None,
        stdout=stdout,
        stderr=stderr,
        timeout=TimeoutEvidence(
            timeout=command.timeout,
            terminated_group=terminated,
            killed_group=killed,
            reaped=reaped,
        ),
    )


def _cleanup_group(process: subprocess.Popen[bytes], grace: float,
                   reap_timeout: float) -> tuple[bool, bool, bool]:
    """TERM, then always KILL and bounded-reap an unreaped session leader."""
    pgid = process.pid
    if pgid == os.getpgrp():  # Defensive latch if session creation is ever accidentally removed.
        raise ProcessValidationError("refusing to signal the caller's process group")
    interrupted: BaseException | None = None
    terminated = False
    killed = False
    reaped = False
    with _block_sigint_during_cleanup():
        try:
            terminated = _signal_group(pgid, signal.SIGTERM)
            if terminated and grace:
                # Do not poll/wait here: reaping the leader before SIGKILL would make its
                # numeric pgid reusable while descendants still need cleanup.
                time.sleep(grace)
        except BaseException as error:
            interrupted = error
        finally:
            killed = _signal_group(pgid, signal.SIGKILL)
            try:
                reaped = _bounded_reap(process, reap_timeout)
            except BaseException as error:
                if interrupted is None:
                    interrupted = error
    if interrupted is not None:
        raise interrupted
    return terminated, killed, reaped


@contextlib.contextmanager
def _block_sigint_during_cleanup() -> Iterator[None]:
    """Defer normal Ctrl-C delivery until the non-interruptible cleanup section ends."""
    if not hasattr(signal, "pthread_sigmask"):
        yield
        return
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _signal_group(pgid: int, signal_number: int) -> bool:
    try:
        os.killpg(pgid, signal_number)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def _bounded_reap(process: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _close_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _open_slot_root(root: Path | str | None) -> int:
    path = Path(root) if root is not None else Path.home() / ".cache" / "khenrix" / "llm-fanout" / "slots"
    raw_parts = path.parts[1:] if path.is_absolute() else path.parts
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ProcessValidationError("executor slot root must not contain traversal components")
    final_name = path.name
    if not final_name or final_name in {".", ".."}:
        raise ProcessValidationError("executor slot root must not contain traversal components")
    # Parent aliases (notably macOS /var -> /private/var) are OS path spelling,
    # not slot-root identity. Resolve only the parent: the final component stays
    # descriptor-relative and O_NOFOLLOW so it cannot be swapped for a symlink.
    parent = path.parent.resolve(strict=False)
    parts = parent.parts[1:] if parent.is_absolute() else parent.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ProcessValidationError("executor slot root must not contain traversal components")
    try:
        fd = os.open(os.sep if parent.is_absolute() else ".", os.O_RDONLY | os.O_DIRECTORY)
    except ProcessValidationError:
        raise
    except OSError as error:
        raise ProcessValidationError("cannot open owner-local executor slot root") from error
    try:
        for part in parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = child_fd
        try:
            os.mkdir(final_name, mode=0o700, dir_fd=fd)
        except FileExistsError:
            pass
        child_fd = os.open(
            final_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=fd,
        )
        os.close(fd)
        fd = child_fd
        _validate_private_slot_directory(fd)
        return fd
    except OSError as error:
        os.close(fd)
        raise ProcessValidationError("cannot open owner-local executor slot root") from error
    except BaseException:
        os.close(fd)
        raise


def _validate_private_slot_directory(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ProcessValidationError("executor slot root must be an owner-private directory")


def _ensure_slot_capacity(root_fd: int, cap: int, fcntl: object, deadline: float) -> None:
    lock_fd = _open_private_state_file(root_fd, ".capacity.lock")
    try:
        _lock_before_deadline(lock_fd, fcntl, deadline)
        capacity_fd = _open_private_state_file(root_fd, ".capacity")
        try:
            raw = _read_all(capacity_fd)
            if not raw:
                os.write(capacity_fd, f"{cap}\n".encode("ascii"))
                os.fsync(capacity_fd)
                os.fsync(root_fd)
                return
            try:
                stored = int(raw.decode("ascii"))
            except (UnicodeDecodeError, ValueError) as error:
                raise ProcessValidationError("executor slot capacity record is invalid") from error
            _validate_slot_cap(stored)
            if raw != f"{stored}\n".encode("ascii"):
                raise ProcessValidationError("executor slot capacity record is invalid")
            if stored != cap:
                raise SlotCapacityConflictError(
                    f"executor slot root capacity is {stored}, not requested {cap}"
                )
        finally:
            os.close(capacity_fd)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _lock_before_deadline(fd: int, fcntl: object, deadline: float) -> None:
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise ProcessValidationError("cannot acquire executor slot capacity lock") from error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SlotTimeoutError("executor slot capacity record was not available before timeout")
        time.sleep(min(_POLL_INTERVAL, remaining))


def _open_private_state_file(root_fd: int, name: str) -> int:
    for _attempt in range(3):
        try:
            fd = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ProcessValidationError("executor slot state file is unsafe") from error
        break
    else:
        raise ProcessValidationError("executor slot state file could not be created safely")
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ProcessValidationError("executor slot state file is unsafe")
    return fd


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 128)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        if sum(map(len, chunks)) > 128:
            raise ProcessValidationError("executor slot capacity record is invalid")


def _open_slot_file(root_fd: int, index: int) -> int:
    for _attempt in range(3):
        try:
            fd = os.open(
                f"slot-{index}",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            # APFS can report ENOENT for a simultaneous O_CREAT through the same
            # directory descriptor even though the competing creator has published
            # the name. The private root makes this the only retriable open error.
            continue
        except OSError as error:
            raise ProcessValidationError("executor slot file is unsafe") from error
        break
    else:
        raise ProcessValidationError("executor slot file could not be created safely")
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ProcessValidationError("executor slot file is unsafe")
    return fd


def _validate_slot_cap(cap: int) -> None:
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        raise ProcessValidationError("executor slot cap must be a positive integer")


def _validate_duration(name: str, value: float, *, positive: bool) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProcessValidationError(f"{name} must be finite")
    if value <= 0 if positive else value < 0:
        qualifier = "positive" if positive else "non-negative"
        raise ProcessValidationError(f"{name} must be {qualifier}")


def _validate_environment_value(name: str, value: object) -> None:
    if not isinstance(value, str) or "\x00" in value:
        raise ProcessValidationError(f"environment value for {name} must be a string without NUL bytes")


def _as_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode()


def _prefer_complete_capture(partial: bytes, later: bytes) -> bytes:
    if not later or later.startswith(partial):
        return later or partial
    if partial.startswith(later):
        return partial
    return partial + later
