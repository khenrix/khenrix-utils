"""Controller-sealed admission for one Linux container provider turn.

The public provider entry remains closed. This seam is for a controller-owned
private diagnostic permit; its transport must reattest the live VM, container,
socket, image, and executable before a CLI process is invoked.
"""
from __future__ import annotations

import fcntl
import base64
import hashlib
import json
import math
import os
import re
import secrets
import selectors
import stat
import subprocess
import threading
import time
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping, Protocol

from .artifacts import ArtifactLimits, ArtifactRef, ArtifactStore, canonical_json
from .broker import Broker, BrokerPermit, BrokerRejected, _safe_response_body
from .container_broker import FrameStream, HostRelay, PermitLedger, attempt_limit, _digest as _permit_digest
from .container_source import runtime_source_digest
from .container_runtime import (ContainerHandle, ContainerLauncher, ContainerSpec,
                                DOCKER, GuestSocketAttestation,
                                RuntimeTranscript, decode_launch_record, private_volume_names)
from .container_transfer_transport import (
    CandidateExportReceipt, InputSourceGrant, InputTransferReceipt,
    abandon_input_claim, admitted_request_digest,
    authenticate_launch_record, original_input_launch,
    _prepare_quality_handoff, _reopen_quality_handoff_preparation,
    reopen_quality_handoff,
    export_candidate as _export_candidate,
    reopen_input_receipt as _reopen_input_receipt,
    stage_inputs as _stage_inputs,
    validate_input_receipt,
)
from .container_vm import (CONTEXT, PROFILE, VMProbe, VMProfileSpec,
                           parse_profile_config, require_vm_probe)
from .controller import (LifecycleController, assert_controller,
                         controller_directory, read_evidence, write_evidence)
from .errors import ArtifactExistsError, ProcessValidationError, ProviderProtocolError, ProviderRequestError
from .process import (ProcessCommand, ProcessResult, ProcessStatus,
                      TimeoutEvidence, _cleanup_group, build_child_environment,
                      executor_slot)
from .providers import (AgyAdapter, ClaudeAdapter, ExecutorProfile, ProviderRequest, ProviderResult,
                        ProviderRegistry, _make_result, _render_profile_argv)


_CATEGORY = "container-seat-admission"
_HANDLE_CATEGORY = "container-seat-launch"
_CLEANUP_CATEGORY = "container-seat-cleanup-failure"
_SCHEMA = "fanout-container-boundary-v4"
_SESSION_SCHEMA = "fanout-container-sessions-v2"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_PROVIDERS = frozenset({"claude", "codex", "agy"})
_DROP_GUEST_UID = (
    "import os,sys;os.setgroups([]);os.setgid(65532);os.setuid(65532);"
    "os.execvp('python3',['python3',*sys.argv[1:]])"
)
_AGY_SETTINGS_BYTES = b'{"modelProvider":"gemini"}\n'
_MAX_CLI_STDOUT = 4 * 1024 * 1024
_MAX_CLI_STDERR = 512 * 1024
_SSH_OPERATIONS = frozenset({"vm_mounts", "vm_nonce", "relay_directory_prepare", "relay_source_transfer", "socket_attestation", "relay_drain", "socket_closed_check"})
_AGY_SETTINGS_SCRIPT = """import hashlib,json,os,stat,sys
root=sys.argv[1]; uid=int(sys.argv[2]); data=b'{"modelProvider":"gemini"}\\n'; limit=4096
def check_dir(fd):
 s=os.fstat(fd)
 if not stat.S_ISDIR(s.st_mode) or s.st_uid!=uid or stat.S_IMODE(s.st_mode)!=0o700:
  raise SystemExit(1)
fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
 check_dir(fd)
 for name in ('.gemini','antigravity-cli'):
  try: os.mkdir(name,0o700,dir_fd=fd)
  except FileExistsError: pass
  child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
  check_dir(child); os.close(fd); fd=child
 try:
  item=os.open('settings.json',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
 except FileNotFoundError:
  item=os.open('settings.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
  try: os.write(item,data); os.fsync(item)
  finally: os.close(item)
  os.fsync(fd)
  item=os.open('settings.json',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
 try:
  s=os.fstat(item)
  if not stat.S_ISREG(s.st_mode) or s.st_uid!=uid or stat.S_IMODE(s.st_mode)!=0o600 or s.st_nlink!=1 or s.st_size>limit:
   raise SystemExit(1)
  raw=os.read(item,limit+1)
  if len(raw)>limit: raise SystemExit(1)
  try: pairs=json.loads(raw.decode('utf-8'),object_pairs_hook=list)
  except (ValueError,RecursionError): raise SystemExit(1)
  if pairs!=[('modelProvider','gemini')]: raise SystemExit(1)
 finally: os.close(item)
 print(hashlib.sha256(data).hexdigest())
finally: os.close(fd)
"""
_AGY_QUALITY_AGENT_SCRIPT = """import hashlib,os,stat,sys
root=sys.argv[1]; uid=int(sys.argv[2]); action=sys.argv[3]; encoded=sys.argv[4]
if action not in ('create','verify') or not 0<len(encoded)<=8192: raise SystemExit(1)
data=bytes.fromhex(encoded)
def check_dir(fd):
 s=os.fstat(fd)
 if not stat.S_ISDIR(s.st_mode) or s.st_uid!=uid or stat.S_IMODE(s.st_mode)!=0o700:
  raise SystemExit(1)
fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
 check_dir(fd)
 paths=[root]; identities=[(os.fstat(fd).st_dev,os.fstat(fd).st_ino)]
 for name in ('.gemini','config','agents'):
  if action=='create':
   try: os.mkdir(name,0o700,dir_fd=fd)
   except FileExistsError: pass
  child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
  os.close(fd); fd=child; check_dir(fd)
  paths.append(paths[-1]+'/'+name); identities.append((os.fstat(fd).st_dev,os.fstat(fd).st_ino))
 try:
  item=os.open('eval-no-tools.md',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
 except FileNotFoundError:
  if action!='create': raise
  item=os.open('eval-no-tools.md',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
  try:
   pending=memoryview(data)
   while pending:
    count=os.write(item,pending)
    if count<=0: raise SystemExit(1)
    pending=pending[count:]
   os.fsync(item)
  finally: os.close(item)
  os.fsync(fd)
  item=os.open('eval-no-tools.md',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
 try:
  s=os.fstat(item)
  if not stat.S_ISREG(s.st_mode) or s.st_uid!=uid or stat.S_IMODE(s.st_mode)!=0o600 or s.st_nlink!=1 or s.st_size!=len(data):
   raise SystemExit(1)
  raw=os.read(item,len(data)+1)
  named=os.stat('eval-no-tools.md',dir_fd=fd,follow_symlinks=False)
  fields=('st_dev','st_ino','st_mode','st_uid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
  if raw!=data or tuple(getattr(named,k) for k in fields)!=tuple(getattr(s,k) for k in fields):
   raise SystemExit(1)
  for path,identity in zip(paths,identities):
   parent=os.stat(path,follow_symlinks=False)
   if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=uid or stat.S_IMODE(parent.st_mode)!=0o700 or (parent.st_dev,parent.st_ino)!=identity:
    raise SystemExit(1)
 finally: os.close(item)
 print(hashlib.sha256(data).hexdigest())
finally: os.close(fd)
"""


def _as_guest_relay_uid(*python_args: str) -> tuple[str, ...]:
    return ("sudo", "-n", "python3", "-c", _DROP_GUEST_UID,
            *python_args)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _require_sha(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ProviderRequestError(f"container {name} digest is invalid")
    return value


def build_host_control_environment(argv: tuple[str, ...], *,
                                   overrides: Mapping[str, str] | None = None,
                                   max_depth: int = 2) -> dict[str, str]:
    """Keep filtered child environments, adding host HOME only for Colima."""
    environment = build_child_environment(overrides=overrides, max_depth=max_depth)
    if not argv or argv[0] != "colima":
        return environment
    if overrides and "HOME" in overrides:
        raise ProviderRequestError("Colima host HOME cannot be overridden")
    try:
        home = Path.home()
        metadata = home.lstat()
    except (OSError, RuntimeError) as error:
        raise ProviderRequestError("Colima host HOME is unavailable") from error
    if (not home.is_absolute() or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022):
        raise ProviderRequestError("Colima host HOME must be an owned directory without group/world write access")
    environment["HOME"] = str(home)
    return environment


def _run_bounded_command(command: ProcessCommand, *,
                         stdout_cap: int = _MAX_CLI_STDOUT,
                         stderr_cap: int = _MAX_CLI_STDERR,
                         before_spawn: Callable[[], None] | None = None,
                         acquire_slot: bool = True,
                         output_limit_result: bool = False) -> ProcessResult:
    """Capture a Docker exec without letting guest output grow host memory."""
    if (not isinstance(command, ProcessCommand)
            or type(stdout_cap) is not int or stdout_cap < 1
            or type(stderr_cap) is not int or stderr_cap < 1):
        raise ProviderRequestError("container process capture limit is invalid")
    environment = build_host_control_environment(
        command.argv, overrides=command.environment, max_depth=command.max_depth)
    with (executor_slot(root=command.slot_root, cap=command.slot_cap,
                        timeout=command.slot_timeout) if acquire_slot else nullcontext()):
        if before_spawn is not None:
            before_spawn()
        try:
            process = subprocess.Popen(
                command.argv, cwd=command.cwd, env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True,
            )
        except OSError as error:
            return ProcessResult(ProcessStatus.SPAWN_ERROR, None, b"", b"",
                                 error=f"{type(error).__name__}: {error.strerror or 'could not start command'}")
        streams = {"stdout": bytearray(), "stderr": bytearray()}
        selector = None
        try:
            selector = selectors.DefaultSelector()
            assert process.stdin and process.stdout and process.stderr
            for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            pending = memoryview(command.stdin)
            if pending:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
            deadline = time.monotonic() + command.timeout
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    terminated, killed, reaped = _cleanup_group(
                        process, command.term_grace, command.reap_timeout)
                    return ProcessResult(
                        ProcessStatus.TIMEOUT, process.returncode if reaped else None,
                        bytes(streams["stdout"]), bytes(streams["stderr"]),
                        timeout=TimeoutEvidence(command.timeout, terminated, killed, reaped),
                    )
                for key, _ in selector.select(min(remaining, 0.1)):
                    if key.data == "stdin":
                        try:
                            written = os.write(key.fd, pending[:65536])
                        except BrokenPipeError:
                            written = len(pending)
                        pending = pending[written:]
                        if not pending:
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                        continue
                    output = streams[key.data]
                    cap = stdout_cap if key.data == "stdout" else stderr_cap
                    if command.max_output_bytes is not None:
                        cap = min(cap, len(output) + command.max_output_bytes
                                  - sum(len(value) for value in streams.values()))
                    chunk = os.read(key.fd, min(65536, cap - len(output) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(output) + len(chunk) > cap:
                        _cleanup_group(process, command.term_grace, command.reap_timeout)
                        if output_limit_result:
                            return ProcessResult(
                                ProcessStatus.OUTPUT_LIMIT, process.returncode,
                                bytes(streams["stdout"]), bytes(streams["stderr"]),
                                error=key.data, capture_complete=False)
                        raise ProviderRequestError("container process output limit exceeded")
                    else:
                        output.extend(chunk)
            try:
                returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                terminated, killed, reaped = _cleanup_group(
                    process, command.term_grace, command.reap_timeout)
                return ProcessResult(
                    ProcessStatus.TIMEOUT, process.returncode if reaped else None,
                    bytes(streams["stdout"]), bytes(streams["stderr"]),
                    timeout=TimeoutEvidence(command.timeout, terminated, killed, reaped),
                )
            return ProcessResult(ProcessStatus.EXIT, returncode,
                                 bytes(streams["stdout"]), bytes(streams["stderr"]))
        except BaseException:
            if process.returncode is None:
                _cleanup_group(process, command.term_grace, command.reap_timeout)
            raise
        finally:
            if selector is not None:
                selector.close()
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()


def _run_control_command(argv: tuple[str, ...], *, data: bytes = b"",
                         timeout: float = 30) -> ProcessResult:
    return _run_bounded_command(
        ProcessCommand(argv, data, timeout=timeout), stdout_cap=1024 * 1024,
        stderr_cap=_MAX_CLI_STDERR, acquire_slot=False, output_limit_result=True)


def _quality_docker_argv(argv, data):
    operations = {('image', 'inspect'), ('volume', 'create'), ('volume', 'inspect'),
                  ('network', 'inspect'), ('container', 'ls')}
    if (type(argv) is not tuple or argv[:len(DOCKER)] != DOCKER
            or any(type(value) is not str or "\x00" in value for value in argv)
            or data is not None and type(data) is not bytes):
        raise ProviderRequestError("quality Docker context differs")
    command = argv[len(DOCKER):]
    if command[:1] == ('exec',):
        handle = ContainerHandle(command[4] if len(command) > 4 else '', '', '', '')
        if (argv in (quality_agy_agent_command(handle).argv,
                     quality_agy_agent_command(handle, verify_only=True).argv)
                or any(argv == _quality_cli_command(handle, provider).argv for provider in _PROVIDERS)):
            if data not in (None, b''):
                raise ProviderRequestError('quality no-model control input differs')
            return
    if (not command or not (command[:2] in operations
            or command[0] in {'ps', 'inspect', 'info', 'create', 'start', 'stop', 'update'})):
        raise ProviderRequestError("quality Docker operation is unknown")


def _quality_docker_marker(marker, descriptor, identity=None):
    info = os.fstat(descriptor)
    named = marker.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or not stat.S_ISREG(named.st_mode)
            or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)
            or identity is not None and [info.st_dev, info.st_ino] != identity):
        raise ProviderRequestError("quality Docker marker identity or type changed")
    return [info.st_dev, info.st_ino]


@contextmanager
def _quality_docker_lock(controller, allocation, *, create=False, identity=None):
    marker = controller_directory(controller, "locks") / f"quality-docker-{allocation}"
    try:
        descriptor = os.open(marker, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            | (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
    except FileExistsError as error:
        raise ProviderRequestError("quality Docker allocation already attempted") from error
    except OSError as error:
        raise ProviderRequestError("quality Docker marker is unavailable") from error
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProviderRequestError("quality Docker command is pending") from error
        _quality_docker_marker(marker, descriptor, identity)
        yield marker, descriptor
    finally:
        os.close(descriptor)


def _quality_docker_capacity(limits):
    reference = ['0' * 64 + '.json', '0' * 64]
    capacity = (len(canonical_json(limits))
        + len(canonical_json({'controls': reference}))
        + limits['max_commands'] * sum(len(canonical_json({kind: reference}))
            for kind in ('attempt', 'terminal')))
    if capacity > ArtifactLimits().max_file_bytes:
        raise ProviderRequestError('quality Docker marker capacity exceeds artifact file limit')
    return capacity


def _quality_docker_append(marker, descriptor, value, identity, limits):
    _quality_docker_marker(marker, descriptor, identity)
    data = canonical_json(value)
    if os.lseek(descriptor, 0, os.SEEK_END) + len(data) > _quality_docker_capacity(limits):
        raise ProviderRequestError("quality Docker marker limit exhausted")
    pending = memoryview(data)
    while pending:
        count = os.write(descriptor, pending)
        if count <= 0:
            raise ProviderRequestError("quality Docker marker write failed")
        pending = pending[count:]
    os.fsync(descriptor)
    _quality_docker_marker(marker, descriptor, identity)


def _quality_docker_remaining(proof):
    monotonic, wall = time.monotonic(), time.time()
    if monotonic < proof['started_monotonic'] or wall < proof['started_wall']:
        raise ProviderRequestError("quality Docker original clock differs")
    remaining = min(proof['deadline_monotonic'] - monotonic, proof['deadline_wall'] - wall)
    if remaining <= 0:
        raise ProviderRequestError("quality Docker attempt or outer deadline exhausted")
    return remaining


def _quality_docker_consumed(controller, descriptor, proof, authority):
    capacity = _quality_docker_capacity(proof['limits'])
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = os.read(descriptor, capacity + 1)
    if len(raw) > capacity:
        raise ProviderRequestError("quality Docker marker is oversized")
    lines = raw.splitlines(keepends=True)
    try:
        values = [json.loads(line) for line in lines]
        if (len(values) < 2 or len(values) > 2 + 2 * proof['limits']['max_commands']
                or any(canonical_json(value) != line for value, line in zip(values, lines))
                or canonical_json(values[0]) != canonical_json(proof['limits'])
                or values[1] != {'controls': list(authority)}):
            raise ValueError
        consumed = 0
        from .container_routes import _read_owned_json
        claims_root = controller_directory(controller, 'locks')
        claim_prefix = 'quality-docker-' + proof['limits']['allocation'] + '-attempt-'
        retained_root = controller_directory(controller, 'quarantine') / (
            'quality-docker-' + proof['limits']['allocation'])
        claimed = (len(values) - 1) // 2
        if {path.name for path in claims_root.iterdir() if path.name.startswith(claim_prefix)} != {
                claim_prefix + str(attempt) for attempt in range(1, claimed + 1)}:
            raise ValueError
        try:
            names = {path.name for path in retained_root.iterdir()}
        except FileNotFoundError:
            names = set()
        if names - {ArtifactStore._LOCK_NAME, ArtifactStore._TEMP_DIR} != {
                str(attempt) for attempt in range(1, claimed + 1)}:
            raise ValueError
        for offset in range(2, len(values), 2):
            attempt = consumed + 1
            item = values[offset]
            if type(item) is not dict or set(item) != {'attempt'}:
                raise ValueError
            attempt_ref = item['attempt']
            if type(attempt_ref) is not list or len(attempt_ref) != 2:
                raise ValueError
            pending = read_evidence(controller, 'quality-docker-attempt', *attempt_ref)
            if (set(pending) != {'controls', 'attempt', 'argv', 'stdin_sha256'}
                    or pending['controls'] != list(authority) or type(pending['attempt']) is not int
                    or pending['attempt'] != attempt or type(pending['argv']) is not list):
                raise ValueError
            _quality_docker_argv(tuple(pending['argv']), None)
            _require_sha(pending['stdin_sha256'], 'quality Docker stdin')
            claim = canonical_json({'attempt': attempt_ref})
            claim_path = claims_root / (claim_prefix + str(attempt))
            retained_claim, _ = _read_owned_json(claim_path)
            if claim_path.lstat().st_nlink != 1 or retained_claim != {'attempt': attempt_ref}:
                raise ValueError
            with ArtifactStore.open_existing(retained_root) as retained:
                retained.read_bytes(ArtifactRef(f'{attempt}/attempt.json',
                    hashlib.sha256(claim).hexdigest(), len(claim)))
            if offset + 1 == len(values):
                raise ProviderRequestError('quality Docker command is pending')
            item = values[offset + 1]
            if type(item) is not dict or set(item) != {'terminal'}:
                raise ValueError
            terminal = read_evidence(controller, 'quality-docker-terminal', *item['terminal'])
            if (set(terminal) != {'controls', 'attempt', 'attempt_authority', 'status',
                    'returncode', 'capture_complete', 'stdout', 'stderr'}
                    or terminal['controls'] != list(authority)
                    or type(terminal['attempt']) is not int or terminal['attempt'] != attempt
                    or terminal['attempt_authority'] != attempt_ref
                    or terminal['status'] != ProcessStatus.EXIT.value
                    or terminal['capture_complete'] is not True
                    or type(terminal['returncode']) is not int):
                raise ProviderRequestError('quality Docker command did not settle within its controls')
            with ArtifactStore.open_existing(retained_root) as retained:
                for stream, cap in (('stdout', 1024 * 1024), ('stderr', 512 * 1024)):
                    value = terminal[stream]
                    if (type(value) is not dict or set(value) != {'path', 'digest', 'size'}
                            or value['path'] != f'{attempt}/{stream}.bin'
                            or type(value['size']) is not int or not 0 <= value['size'] <= cap):
                        raise ValueError
                    _require_sha(value['digest'], 'quality Docker capture')
                    info = (retained_root / value['path']).lstat()
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                        raise ValueError
                    retained.read_bytes(ArtifactRef(**value))
            consumed = attempt
        return consumed
    except ProviderRequestError:
        raise
    except Exception as error:
        raise ProviderRequestError('quality Docker original command evidence differs') from error


def quality_docker_runner(controller: LifecycleController, *, allocation_digest: str,
                          max_commands: int, command_seconds: float, outer_seconds: float,
                          original_controls: tuple[str, str] | None = None):
    """Create once, or continue only the exact authenticated original controls."""
    assert_controller(controller)
    _require_sha(allocation_digest, "quality allocation")
    if (type(max_commands) is not int or max_commands < 1
            or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                   for value in (command_seconds, outer_seconds))
            or command_seconds > outer_seconds):
        raise ProviderRequestError("quality Docker process controls differ")
    limits = {"allocation": allocation_digest, "max_commands": max_commands,
              "command_seconds": command_seconds, "outer_seconds": outer_seconds}
    _quality_docker_capacity(limits)
    if original_controls is None:
        with _quality_docker_lock(controller, allocation_digest, create=True) as (marker, descriptor):
            identity = _quality_docker_marker(marker, descriptor)
            monotonic, wall = time.monotonic(), time.time()
            proof = {'schema': 'fanout-quality-docker-controls-v2', 'limits': limits,
                'marker_identity': identity, 'started_monotonic': monotonic,
                'deadline_monotonic': monotonic + outer_seconds, 'started_wall': wall,
                'deadline_wall': wall + outer_seconds}
            _quality_docker_append(marker, descriptor, limits, identity, limits)
            authority = write_evidence(controller, 'quality-docker-controls', proof)
            _quality_docker_append(marker, descriptor, {'controls': list(authority)}, identity, limits)
            parent = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    else:
        if type(original_controls) is not tuple or len(original_controls) != 2:
            raise ProviderRequestError('quality Docker original controls are invalid')
        authority = original_controls
        proof = read_evidence(controller, 'quality-docker-controls', *authority)
        if (set(proof) != {'schema', 'limits', 'marker_identity', 'started_monotonic',
                'deadline_monotonic', 'started_wall', 'deadline_wall'}
                or proof['schema'] != 'fanout-quality-docker-controls-v2'
                or canonical_json(proof['limits']) != canonical_json(limits)
                or type(proof['marker_identity']) is not list or len(proof['marker_identity']) != 2
                or any(type(value) is not int or value < 0 for value in proof['marker_identity'])
                or any(type(proof[key]) is not float or not math.isfinite(proof[key])
                    for key in ('started_monotonic', 'deadline_monotonic', 'started_wall', 'deadline_wall'))
                or proof['deadline_monotonic'] != proof['started_monotonic'] + outer_seconds
                or proof['deadline_wall'] != proof['started_wall'] + outer_seconds):
            raise ProviderRequestError('quality Docker original controls differ')
        with _quality_docker_lock(controller, allocation_digest,
                identity=proof['marker_identity']) as (_, descriptor):
            _quality_docker_consumed(controller, descriptor, proof, authority)
            _quality_docker_remaining(proof)

    def run(argv, data):
        _quality_docker_argv(argv, data)
        if canonical_json(read_evidence(controller, 'quality-docker-controls', *authority)) != canonical_json(proof):
            raise ProviderRequestError('quality Docker controls changed')
        with _quality_docker_lock(controller, allocation_digest,
                identity=proof['marker_identity']) as (marker, descriptor):
            attempted = _quality_docker_consumed(controller, descriptor, proof, authority)
            if attempted >= max_commands:
                raise ProviderRequestError('quality Docker attempt or outer deadline exhausted')
            _quality_docker_remaining(proof)
            attempted += 1
            attempt = write_evidence(controller, 'quality-docker-attempt', {
                'controls': list(authority), 'attempt': attempted,
                'argv': list(argv), 'stdin_sha256': hashlib.sha256(data or b'').hexdigest()})
            claims_root = controller_directory(controller, 'locks')
            claim_path = claims_root / f'quality-docker-{allocation_digest}-attempt-{attempted}'
            try:
                claim_fd = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except OSError as error:
                raise ProviderRequestError('quality Docker command already claimed or unavailable') from error
            with os.fdopen(claim_fd, 'wb') as claim_file:
                claim_file.write(canonical_json({'attempt': list(attempt)}))
                claim_file.flush()
                os.fsync(claim_file.fileno())
            parent_fd = os.open(claims_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            with ArtifactStore(controller_directory(controller, 'quarantine') /
                               f'quality-docker-{allocation_digest}') as retained:
                retained.write_json(f'{attempted}/attempt.json', {'attempt': list(attempt)})
            _quality_docker_append(marker, descriptor, {'attempt': list(attempt)},
                proof['marker_identity'], proof['limits'])
            remaining = _quality_docker_remaining(proof)
            result = _run_bounded_command(ProcessCommand(argv, data or b'',
                timeout=min(command_seconds, remaining), max_output_bytes=1024 * 1024),
                stdout_cap=1024 * 1024, stderr_cap=512 * 1024, acquire_slot=False)
            with ArtifactStore(controller_directory(controller, 'quarantine') /
                               f'quality-docker-{allocation_digest}') as retained:
                stdout = retained.write_bytes(f'{attempted}/stdout.bin', result.stdout)
                stderr = retained.write_bytes(f'{attempted}/stderr.bin', result.stderr)
            terminal = write_evidence(controller, 'quality-docker-terminal', {
                'controls': list(authority), 'attempt': attempted, 'attempt_authority': list(attempt),
                'status': result.status.value, 'returncode': result.returncode,
                'capture_complete': result.capture_complete,
                'stdout': asdict(stdout), 'stderr': asdict(stderr)})
            _quality_docker_append(marker, descriptor, {'terminal': list(terminal)},
                proof['marker_identity'], proof['limits'])
            if result.status is not ProcessStatus.EXIT or not result.capture_complete:
                raise ProviderRequestError('quality Docker command did not settle within its controls')
            completed = subprocess.CompletedProcess(argv, result.returncode, result.stdout, result.stderr)
            completed.quality_terminal = terminal
            return completed
    run.controller = controller
    run.controls = authority
    return run


class ContainerTransport(Protocol):
    """Live proof and one CLI process; production implementations use pinned control."""

    def attest_vm(self) -> VMProbe: ...
    def attest_container(self) -> ContainerHandle: ...
    def attest_socket(self) -> GuestSocketAttestation: ...
    def attest_image(self) -> str: ...
    def attest_cli(self, provider: str) -> str: ...
    def run(self, command: ProcessCommand,
            boundary: "ContainerSeatBoundary") -> ProcessResult: ...


class PinnedDockerTransport:
    """Retain the launcher result; every turn reuses its running validator."""

    def __init__(self, controller: LifecycleController, spec: ContainerSpec,
                 handle: ContainerHandle, launcher: ContainerLauncher,
                 vm_attestor: Callable[[], VMProbe],
                 socket_attestor: Callable[[str], GuestSocketAttestation],
                 image_attestor: Callable[[], str],
                 cli_attestor: Callable[[str], str],
                 process_runner: Callable[[ProcessCommand], ProcessResult],
                 origin: tuple[str, str]):
        self.controller = controller
        self.spec = spec
        self.handle = handle
        self.launcher = launcher
        self._vm_attestor = vm_attestor
        self._socket_attestor = socket_attestor
        self._image_attestor = image_attestor
        self._cli_attestor = cli_attestor
        self._process_runner = process_runner
        self._retained_process_runner = process_runner
        self.handle_evidence_name, self.handle_evidence_digest = origin
        self._quarantined = False
        self._production_edge = False
        self.stop_evidence: tuple[str, str] | None = None
        self._stopped_inspect: bytes | None = None
        self.process_result: ProcessResult | None = None
        self.runtime_observation: list[dict[str, object]] = []
        self._lifecycle_scope = None
        self._retained_lifecycle_scope = None
        self.lifecycle_snapshot = None
        self.runtime_proof = None

    def stage_inputs(self, request: ProviderRequest,
                     source_grant: InputSourceGrant) -> InputTransferReceipt:
        return _stage_inputs(self.controller, request, self, source_grant)

    def reopen_input_receipt(self, request: ProviderRequest,
                             admission: "ContainerAdmission") -> InputTransferReceipt:
        return _reopen_input_receipt(self.controller, request, self, admission)

    def export_candidate(self, request: ProviderRequest,
                         boundary: "ContainerSeatBoundary",
                         private_root: Path) -> CandidateExportReceipt:
        return _export_candidate(request, boundary, private_root)

    def handoff_quality_inputs(self, request: ProviderRequest,
                               admission: "ContainerAdmission") -> "PinnedDockerTransport":
        """Stop the no-model importer and retain its exact volumes/relay for RO3."""
        scope = self._scope()
        with nullcontext() if scope is None else scope.guard:
            return self._handoff_quality_inputs(request, admission, scope)

    def _handoff_quality_inputs(self, request, admission, scope):
        control = getattr(self, "_control", None)
        if control is not None:
            if (type(control) is not PinnedSSHSeatControl or control.controller is not self.controller
                    or control.spec != self.spec or control.ledger is not admission.ledger
                    or control.broker.permit != admission.broker_permit
                    or control.permit_id != admission.permit_id or control.epoch != admission.epoch):
                raise ProviderRequestError("quality handoff retained control differs")
            control._lease.require_held()
        snapshot = context = None
        if scope is not None:
            scope.authorize()
            context = scope.active[self.handle.container_id]['member']['context']
            snapshot, _ = scope.snapshot(self)
        preparation = _prepare_quality_handoff(self.controller, request, self, admission)
        _, receipt, _, original_spec, spec, origin, stopped = _reopen_quality_handoff_preparation(
            self.controller, request, preparation)
        launcher = ContainerLauncher(self.launcher._docker,
                                     guest_socket_attestor=self.launcher._socket_attestor)
        model = handle = None
        try:
            inventory = None
            if scope is not None:
                scope.complete_handoff(self, request, admission, preparation)
                scope.reserve_registered(context, spec, admission.ledger, admission.broker_permit, admission.epoch)
                control.spec = spec
                inventory = scope.prepare(launcher, spec, context, control)
            handle = launcher._start_readonly_handoff(spec, original_spec, receipt.original_handle, origin, stopped, scope=inventory)
            record = {"spec": asdict(spec), "handle": asdict(handle), "route": "quality-handoff",
                      "volumes": launcher._volume_expectations[1],
                      "handoff": json.loads(canonical_json(asdict(launcher._handoff))),
                      "original_handoff": list(preparation)}
            if snapshot is not None:
                record['handoff_scope'] = list(snapshot)
            authenticate_launch_record(self.controller, record, spec, handle,
                                       inputs_digest=request.inputs_digest)
            launch = write_evidence(self.controller, _HANDLE_CATEGORY, record)
            model = type(self)(self.controller, spec, handle, launcher, self._vm_attestor,
                self._socket_attestor, self._image_attestor, self._cli_attestor,
                self._retained_process_runner, launch)
            model.handoff_evidence = write_evidence(self.controller, "quality-container-input-handoff", {
                "schema": "quality-container-handoff-v1", "preparation": list(preparation),
                "model_launch": list(launch)})
            reopen_quality_handoff(self.controller, request, model.handoff_evidence,
                                   ledger=admission.ledger, sessions=admission.session_store)
            if self._production_edge:
                model._image_attestor = lambda: type(self)._attest_image(launcher, spec)
                model._cli_attestor = lambda provider: type(self)._attest_linux_cli(spec, provider, handle)
                model._process_runner = lambda command: type(self)._docker_exec(handle, command, receipt.workdir)
                model._retained_process_runner = model._process_runner
            if control is not None:
                control._lease.require_held()
                control.spec = spec
                model._control = control
                self._control = None
            model._production_edge = self._production_edge
            model._model_workdir = receipt.workdir
            if scope is not None:
                model._lifecycle_scope = model._retained_lifecycle_scope = scope
                scope.register(model, context)
            model.attest_container()
            return model
        except BaseException as primary:
            if scope is not None:
                try:
                    scope.fail(spec, handle, control, primary, phase='quality_handoff')
                except BaseException as cleanup:
                    primary.add_note(f"Quality handoff lifecycle cleanup also failed: {type(cleanup).__name__}")
            try:
                if handle is not None:
                    type(self)._quarantine_handle(spec, handle, launcher)
            except BaseException as cleanup:
                primary.add_note(_cleanup_failure_note(self.controller, "quality-handoff", cleanup,
                    primary=primary, spec=spec, handle=handle))
            raise

    @classmethod
    def start(cls, controller: LifecycleController, spec: ContainerSpec, *,
              launcher: ContainerLauncher,
              vm_attestor: Callable[[], VMProbe],
              socket_attestor: Callable[[str], GuestSocketAttestation],
              image_attestor: Callable[[], str],
              cli_attestor: Callable[[str], str],
              process_runner: Callable[[ProcessCommand], ProcessResult],
              recover: bool = False, lifecycle_scope=None,
              context_ref=None, session_control=None,
              request: ProviderRequest | None = None) -> "PinnedDockerTransport":
        if lifecycle_scope is not None:
            from .container_coordinator import _CoordinatorLifecycle
            if type(lifecycle_scope) is not _CoordinatorLifecycle or lifecycle_scope.controller is not controller:
                raise ProviderRequestError("container lifecycle owner is invalid")
        original_input = None
        if recover:
            if request is None or session_control is None:
                raise ProviderRequestError("container recovery lacks original input authority")
            origin, original_input = original_input_launch(controller, request, spec,
                broker=session_control.broker, current_permit=session_control.broker.permit,
                ledger=session_control.ledger, permit_id=session_control.permit_id,
                epoch=session_control.epoch)
            launcher.restore_launch(origin, spec, ContainerHandle(**origin["handle"]), recover=False)
        if lifecycle_scope is not None:
            with lifecycle_scope.guard:
                try:
                    scope = lifecycle_scope.prepare(launcher, spec, context_ref, session_control)
                    return cls._start_guarded(controller, spec, launcher=launcher,
                        vm_attestor=vm_attestor, socket_attestor=socket_attestor,
                        image_attestor=image_attestor, cli_attestor=cli_attestor,
                        process_runner=process_runner, recover=recover, scope=scope,
                        lifecycle_scope=lifecycle_scope, context_ref=context_ref,
                        session_control=session_control, original_input=original_input)
                except BaseException as primary:
                    if not lifecycle_scope.blocked:
                        lifecycle_scope.fail(spec, None, session_control, primary, phase="container_launch")
                    raise
        return cls._start_guarded(controller, spec, launcher=launcher,
            vm_attestor=vm_attestor, socket_attestor=socket_attestor,
            image_attestor=image_attestor, cli_attestor=cli_attestor,
            process_runner=process_runner, recover=recover, original_input=original_input)

    @classmethod
    def _start_guarded(cls, controller, spec, *, launcher, vm_attestor,
                       socket_attestor, image_attestor, cli_attestor,
                       process_runner, recover, scope=None, lifecycle_scope=None,
                       context_ref=None, session_control=None, original_input=None):
        assert_controller(controller)
        if scope is None:
            handle = launcher.recover(spec) if recover else launcher.start(spec)
        else:
            handle = launcher.recover(spec, scope=scope) if recover else launcher.start(spec, scope=scope)
        transport = None
        try:
            record = {
                "spec": asdict(spec), "handle": asdict(handle),
                "route": "recover" if recover else "start",
                "volumes": launcher._volume_expectations[1],
            }
            if recover:
                record["recovery"] = json.loads(canonical_json(asdict(launcher._recovery)))
                record["original_input"] = original_input
            authenticate_launch_record(controller, record, spec, handle)
            origin = write_evidence(controller, _HANDLE_CATEGORY, record)
            transport = cls(controller, spec, handle, launcher, vm_attestor,
                            socket_attestor, image_attestor, cli_attestor,
                            process_runner, origin)
            if lifecycle_scope is not None:
                transport._control = session_control
                transport._lifecycle_scope = lifecycle_scope
                transport._retained_lifecycle_scope = lifecycle_scope
                lifecycle_scope.register(transport, context_ref)
        except BaseException as primary:
            try:
                cls._quarantine_handle(spec, handle, launcher)
            except BaseException as cleanup:
                primary.add_note(_cleanup_failure_note(
                    controller, "launch-receipt", cleanup, primary=primary,
                    spec=spec, handle=handle))
            if lifecycle_scope is not None:
                lifecycle_scope.fail(spec, handle, session_control, primary, phase="container_launch")
            raise
        return transport

    @classmethod
    def start_live(cls, controller: LifecycleController, spec: ContainerSpec, *,
                   ledger: PermitLedger, broker: Broker, permit_id: str,
                   epoch: int, recover: bool = False, lifecycle_scope=None,
                   context_ref=None, request: ProviderRequest | None = None,
                   quality_docker=None) -> "PinnedDockerTransport":
        """Open the owner-pinned relay, then create one inspected seat."""
        ContainerLauncher._validate_spec(spec)
        if (not isinstance(broker, Broker)
                or broker.permit.upstream_route_digest is None):
            raise ProviderRequestError("container live upstream route is absent")
        if quality_docker is not None:
            import types
            if (type(quality_docker) is not types.FunctionType
                    or quality_docker.__code__ not in quality_docker_runner.__code__.co_consts
                    or quality_docker.controller is not controller
                    or not read_evidence(controller, "quality-docker-controls", *quality_docker.controls)):
                raise ProviderRequestError("quality Docker constructor is not the fixed owned helper")
        if recover:
            if request is None:
                raise ProviderRequestError("container recovery lacks original input authority")
            original_input_launch(controller, request, spec, broker=broker,
                current_permit=broker.permit, ledger=ledger, permit_id=permit_id, epoch=epoch)
        if lifecycle_scope is not None:
            from .container_coordinator import _CoordinatorLifecycle
            if type(lifecycle_scope) is not _CoordinatorLifecycle or lifecycle_scope.controller is not controller:
                raise ProviderRequestError("container lifecycle owner is invalid")
            with lifecycle_scope.guard:
                lifecycle_scope.authorize()
                if lifecycle_scope.pending.get((permit_id, epoch)) != (spec, tuple(context_ref), broker.permit, ledger):
                    raise ProviderRequestError("container lifecycle pending permit changed")
        control = PinnedSSHSeatControl(controller, spec, ledger, broker,
                                       permit_id, epoch)
        phase = "relay_open"
        try:
            control.open()
            launcher = ContainerLauncher(quality_docker, guest_socket_attestor=control.attest_socket)
            phase = "vm_attestation"
            control.attest_vm()
            phase = "container_launch"
            transport = cls.start(
                controller, spec, launcher=launcher,
                vm_attestor=control.attest_vm,
                socket_attestor=control.attest_socket,
                image_attestor=lambda: cls._attest_image(launcher, spec),
                cli_attestor=lambda provider: cls._attest_linux_cli(
                    spec, provider, transport.handle),
                process_runner=lambda command: cls._docker_exec(
                    transport.handle, command, transport._model_workdir),
                recover=recover,
                lifecycle_scope=lifecycle_scope, context_ref=context_ref,
                session_control=control, request=request,
            )
            transport._control = control
            transport._production_edge = True
            return transport
        except BaseException as primary:
            if lifecycle_scope is not None and getattr(control, "_turn_failure_ref", None) is None:
                with lifecycle_scope.guard:
                    lifecycle_scope.fail(spec, None, control, primary, phase=phase)
            try:
                control.close()
            except BaseException as cleanup:
                primary.add_note(_cleanup_failure_note(
                    controller, "relay-start", cleanup, primary=primary,
                    spec=spec if isinstance(spec, ContainerSpec) else None,
                    permit_id=permit_id))
            raise

    @staticmethod
    def _attest_image(launcher: ContainerLauncher, spec: ContainerSpec) -> str:
        launcher._image_id(spec)
        return spec.image_digest

    @staticmethod
    def _attest_linux_cli(spec: ContainerSpec, provider: str,
                          handle: ContainerHandle) -> str:
        if provider not in _PROVIDERS:
            raise ProviderRequestError("container CLI provider is invalid")
        script = ("import hashlib,shutil,sys; "
                  "path=shutil.which(sys.argv[1]); "
                  "assert path; "
                  "print(hashlib.sha256(open(path,'rb').read()).hexdigest())")
        argv = (*DOCKER, "exec", "--user", "65532:65532", handle.container_id,
                "python", "-c", script, provider)
        try:
            result = _run_control_command(argv)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProviderRequestError("container Linux CLI byte attestation failed") from error
        try:
            digest = result.stdout.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise ProviderRequestError("container Linux CLI byte attestation failed") from error
        if result.status != ProcessStatus.EXIT or result.returncode != 0 or _SHA.fullmatch(digest) is None:
            raise ProviderRequestError("container Linux CLI byte attestation failed")
        return digest

    @staticmethod
    def _docker_exec(handle: ContainerHandle, command: ProcessCommand,
                     workdir: str, *, before_spawn=None) -> ProcessResult:
        if (not isinstance(workdir, str) or not re.fullmatch(
                r"/workspace/inputs/[0-9a-f]{64}/published/repo", workdir)):
            raise ProviderRequestError("container model workdir is not sealed")
        wrapped = ProcessCommand(
            argv=(*DOCKER, "exec", "-i", "--user", "65532:65532",
                  "--workdir", workdir, handle.container_id, *command.argv),
            stdin=command.stdin, cwd=command.cwd, timeout=command.timeout,
            slot_cap=command.slot_cap, slot_timeout=command.slot_timeout,
            slot_root=command.slot_root, max_depth=command.max_depth,
            term_grace=command.term_grace, reap_timeout=command.reap_timeout,
            max_output_bytes=command.max_output_bytes,
        )
        return (_run_bounded_command(wrapped) if before_spawn is None
                else _run_bounded_command(wrapped, before_spawn=before_spawn))

    @staticmethod
    def _provision_agy_settings(handle: ContainerHandle) -> None:
        argv = (*DOCKER, "exec", "--user", "65532:65532", handle.container_id,
                "python", "-c", _AGY_SETTINGS_SCRIPT, "/session", "65532")
        try:
            result = _run_control_command(argv)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProviderRequestError("container agy settings provision failed") from error
        expected = hashlib.sha256(_AGY_SETTINGS_BYTES).hexdigest().encode("ascii")
        if result.status != ProcessStatus.EXIT or result.returncode != 0 or result.stdout.strip() != expected:
            raise ProviderRequestError("container agy settings are unsafe")

    def attest_vm(self) -> VMProbe:
        return self._vm_attestor()

    def attest_container(self) -> ContainerHandle:
        scope = self._scope()
        if scope is not None:
            with scope.guard:
                snapshot, inventory = scope.snapshot(self)
                handle = self._attest_container(inventory)
                raw = canonical_json(self.runtime_observation)
                with ArtifactStore(controller_directory(self.controller, "quarantine") /
                                   "runtime-proofs") as store:
                    ref = ArtifactRef(f"runtime/{self.handle.container_id}/{hashlib.sha256(raw).hexdigest()}.json",
                                      hashlib.sha256(raw).hexdigest(), len(raw))
                    try:
                        store.write_bytes(ref.path, raw)
                    except ArtifactExistsError:
                        store.read_bytes(ref)
                    self.runtime_proof = ref
                self.lifecycle_snapshot = snapshot
                return handle
        return self._attest_container()

    def _scope(self):
        from .container_coordinator import _CoordinatorLifecycle
        scope = self._lifecycle_scope
        if (scope is not self._retained_lifecycle_scope
                or scope is not None and (type(scope) is not _CoordinatorLifecycle
                                         or scope.controller is not self.controller)):
            raise ProviderRequestError("container lifecycle association changed")
        return scope

    def _attest_container(self, scope=None) -> ContainerHandle:
        if type(self.launcher) is not ContainerLauncher:
            return self.launcher.attest_running(self.spec, self.handle)
        original = self.launcher._run
        observations = RuntimeTranscript(active_peers=0 if scope is None else len(scope.active) - 1)
        def capture(*argv):
            raw = original(*argv)
            observations.append(argv, raw)
            return raw
        self.launcher._run = capture
        try:
            handle = (self.launcher.attest_running(self.spec, self.handle) if scope is None
                      else self.launcher.attest_running(self.spec, self.handle, scope=scope))
        finally:
            self.launcher._run = original
        self.runtime_observation = observations.observations
        return handle

    def attest_socket(self) -> GuestSocketAttestation:
        return self._socket_attestor(self.spec.socket_path)

    def attest_image(self) -> str:
        return self._image_attestor()

    def attest_cli(self, provider: str) -> str:
        return self._cli_attestor(provider)

    def _require_fixed_runner(self) -> None:
        if (self._production_edge is True
                and self._process_runner is not self._retained_process_runner):
            raise ProviderRequestError("container production process runner was replaced")

    def run(self, command: ProcessCommand,
            boundary: "ContainerSeatBoundary") -> ProcessResult:
        # Validation can race with session reservation; repeat immediately
        # before handing any prompt bytes to Docker exec.
        self.process_result = None
        self._require_fixed_runner()
        def final_admission():
            _check_live(boundary)
            admission = boundary.admission
            try:
                admission.ledger.require_permit(admission.permit_id, admission.epoch,
                                                admission.broker_permit)
            except BrokerRejected as error:
                raise ProviderRequestError("container broker permit changed or was fenced") from error
            if command.argv[0] == "agy" and getattr(self, "_control", None) is not None:
                self._provision_agy_settings(self.handle)
                _check_live(boundary)
                try:
                    admission.ledger.require_permit(admission.permit_id, admission.epoch,
                                                    admission.broker_permit)
                except BrokerRejected as error:
                    raise ProviderRequestError("container broker permit changed or was fenced") from error
        self._model_workdir = boundary.input_workdir
        if self._production_edge is True:
            result = PinnedDockerTransport._docker_exec(
                self.handle, command, self._model_workdir, before_spawn=final_admission)
        else:
            final_admission()
            result = self._process_runner(command)
        self.process_result = result
        control = getattr(self, "_control", None)
        worker_error = getattr(control, "_relay_error", None)
        if worker_error is not None:
            raise ProviderRequestError("host relay worker failed") from worker_error
        return result

    def close(self) -> None:
        scope = self._scope()
        if scope is None:
            return self._close()
        primary = None
        try:
            with scope.guard:
                scope.authorize(cleanup=True)
                self._close(retain_control=True)
                scope.complete(self)
        except BaseException as error:
            primary = error
            with scope.guard:
                scope.fail(self.spec, self.handle, getattr(self, "_control", None), error)
            raise
        finally:
            control = getattr(self, "_control", None)
            if control is not None:
                try:
                    control.close()
                except BaseException as error:
                    with scope.guard:
                        scope.fail(self.spec, self.handle, control, error)
                    if primary is None:
                        raise
                    primary.add_note(f"Guest relay cleanup also failed: {type(error).__name__}")
                else:
                    self._control = None

    def _close(self, *, retain_control=False) -> None:
        control = getattr(self, "_control", None)
        shutdown_error: BaseException | None = None
        try:
            if not self._quarantined:
                origin = read_evidence(self.controller, _HANDLE_CATEGORY,
                                       self.handle_evidence_name,
                                       self.handle_evidence_digest)
                authenticate_launch_record(self.controller, origin, self.spec, self.handle)
                self._stopped_inspect = self._quarantine_handle(self.spec, self.handle, self.launcher)
                self._quarantined = True
            if self.stop_evidence is None:
                raw = self._stopped_inspect
                if not isinstance(raw, bytes) or not 0 < len(raw) <= 1024 * 1024:
                    raise ProviderRequestError("container stopped inspect exceeds proof budget")
                with ArtifactStore(controller_directory(self.controller, "quarantine") /
                                   "runtime-proofs") as store:
                    ref = ArtifactRef(f"stopped/{self.handle.container_id}.json",
                                      hashlib.sha256(raw).hexdigest(), len(raw))
                    try:
                        store.write_bytes(ref.path, raw)
                    except ArtifactExistsError:
                        store.read_bytes(ref)
                self.stop_evidence = write_evidence(self.controller, "container-seat-stopped", {
                    "spec": asdict(self.spec), "handle": asdict(self.handle),
                    "launch_evidence_name": self.handle_evidence_name,
                    "launch_evidence_digest": self.handle_evidence_digest,
                    "raw_inspect": asdict(ref),
                })
        except BaseException as error:
            shutdown_error = error
        relay_error: BaseException | None = None
        if control is not None and not retain_control:
            try:
                control.close()
            except BaseException as error:
                relay_error = error
            else:
                self._control = None
        if shutdown_error is not None:
            if relay_error is not None:
                shutdown_error.add_note(
                    f"Guest relay cleanup also failed: {type(relay_error).__name__}")
            raise shutdown_error
        if relay_error is not None:
            raise relay_error

    @staticmethod
    def _quarantine_handle(spec: ContainerSpec, handle: ContainerHandle,
                           launcher: ContainerLauncher) -> bytes:
        ContainerLauncher._validate_spec(spec)
        if (not isinstance(handle, ContainerHandle)
                or re.fullmatch(r"[0-9a-f]{64}", handle.container_id) is None
                or (handle.workspace_volume, handle.session_volume) !=
                (spec.workspace_volume, spec.session_volume)):
            raise ProviderRequestError("container shutdown handle identity differs")

        last_raw = b""
        def owned() -> dict:
            nonlocal last_raw
            try:
                last_raw = launcher._run("inspect", "--type", "container", handle.container_id)
                if not isinstance(last_raw, bytes) or not 0 < len(last_raw) <= 1024 * 1024:
                    raise ProviderRequestError("container shutdown inspect exceeds proof budget")
                items = json.loads(last_raw)
            except (UnicodeDecodeError, ValueError) as error:
                raise ProviderRequestError("container shutdown inspect is invalid") from error
            if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                raise ProviderRequestError("container shutdown inspect is ambiguous")
            item = items[0]
            config = item.get("Config")
            host = item.get("HostConfig")
            if not isinstance(config, dict) or not isinstance(host, dict):
                raise ProviderRequestError("container shutdown ownership differs")
            labels = config.get("Labels")
            mounts = item.get("Mounts")
            by_target = ({mount.get("Destination"): mount for mount in mounts}
                         if isinstance(mounts, list) and
                         all(isinstance(mount, dict) for mount in mounts) else {})
            expected_mounts = {
                "/workspace": ("volume", spec.workspace_volume),
                "/session": ("volume", spec.session_volume),
                "/run/fanout/broker.sock": ("bind", spec.socket_path),
            }
            if (item.get("Id") != handle.container_id
                    or labels != launcher._labels(spec)
                    or config.get("Image") != spec.image_digest
                    or set(by_target) != set(expected_mounts)
                    or any(by_target[target].get("Type") != kind
                           or (by_target[target].get("Name") if kind == "volume"
                               else by_target[target].get("Source")) != source
                           for target, (kind, source) in expected_mounts.items())):
                raise ProviderRequestError("container shutdown ownership differs")
            return item

        item = owned()
        state = item.get("State")
        if not isinstance(state, dict) or type(state.get("Running")) is not bool:
            raise ProviderRequestError("container shutdown state is ambiguous")
        launcher._run("update", "--restart", "no", handle.container_id)
        if state["Running"]:
            launcher._run("stop", "--time", "0", handle.container_id)
        stopped = owned()
        stopped_host = stopped["HostConfig"]
        restart = stopped_host.get("RestartPolicy")
        stopped_state = stopped.get("State")
        if (not isinstance(stopped_state, dict) or stopped_state.get("Running") is not False
                or not isinstance(restart, dict) or restart.get("Name") != "no"):
            raise ProviderRequestError("container shutdown could not verify stopped seat")
        return last_raw


@dataclass(frozen=True, slots=True)
class SessionBinding:
    provider: str
    session_id: str
    volume: str
    turn_digest: str
    upstream_route_digest: str


class SessionStore:
    """Authenticated, fsynced exact-ID and attempted-turn record for one seat."""

    def __init__(self, controller: LifecycleController, run_id: str,
                 task_id: str, target_id: str, seat_id: str, volume: str):
        assert_controller(controller)
        _, expected_volume = private_volume_names(run_id, task_id, target_id,
                                                  seat_id)
        if volume != expected_volume:
            raise ProviderRequestError("container session volume identity differs")
        self.controller = controller
        self.identity = (run_id, task_id, target_id, seat_id)
        self.volume = volume
        stem = _digest({"controller": controller.controller_id,
                        "identity": self.identity, "volume": volume})
        self._record = controller_directory(controller, "transactions") / f"session-{stem}.json"
        self._lock = controller_directory(controller, "locks") / f"session-{stem}.lock"

    def _mac(self, payload: dict[str, object]) -> str:
        import hmac
        return hmac.new(self.controller._secret.encode("ascii"),
                        b"fanout-container-sessions\0" + canonical_json(payload),
                        hashlib.sha256).hexdigest()

    def _empty(self) -> dict[str, object]:
        return {"schema_version": _SESSION_SCHEMA,
                "controller_id": self.controller.controller_id,
                "identity": list(self.identity), "volume": self.volume,
                "bindings": {}, "turns": {}}

    def _read(self) -> dict[str, object]:
        try:
            fd = os.open(self._record, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return self._empty()
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid() or info.st_size > 64 * 1024):
                raise ProviderRequestError("container session record is unsafe")
            data = os.read(fd, 64 * 1024 + 1)
        finally:
            os.close(fd)
        try:
            record = json.loads(data)
        except (UnicodeError, ValueError) as error:
            raise ProviderRequestError("container session record is invalid") from error
        if (not isinstance(record, dict) or set(record) != {"payload", "mac"}
                or not isinstance(record["payload"], dict)
                or canonical_json(record) != data):
            raise ProviderRequestError("container session record is invalid")
        payload = record["payload"]
        if (set(payload) != set(self._empty()) or
                any(payload.get(key) != value for key, value in self._empty().items()
                    if key not in {"bindings", "turns"}) or
                not isinstance(payload["bindings"], dict) or
                not isinstance(payload["turns"], dict)):
            raise ProviderRequestError("container session record identity differs")
        import hmac
        if not isinstance(record["mac"], str) or not hmac.compare_digest(
                record["mac"], self._mac(payload)):
            raise ProviderRequestError("container session record authentication failed")
        return payload

    def _write(self, payload: dict[str, object]) -> None:
        data = canonical_json({"payload": payload, "mac": self._mac(payload)})
        if len(data) > 64 * 1024:
            raise ProviderRequestError("container session record exceeds limit")
        path = self._record.parent / f".session-{secrets.token_hex(8)}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600)
        created = os.fstat(fd)
        try:
            try:
                pending = memoryview(data)
                while pending:
                    written = os.write(fd, pending)
                    if written <= 0:
                        raise OSError("container session write made no progress")
                    pending = pending[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(path, self._record)
            directory_fd = os.open(self._record.parent,
                                   os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException as primary:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) != (created.st_dev, created.st_ino):
                    raise ProviderRequestError("container session temporary identity changed")
                path.unlink()
            except FileNotFoundError:
                pass
            except BaseException as cleanup:
                primary.add_note(f"Session temporary cleanup also failed: {type(cleanup).__name__}")
            raise

    def _update(self, operation):
        assert_controller(self.controller)
        fd = os.open(self._lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid()):
                raise ProviderRequestError("container session lock is unsafe")
            fcntl.flock(fd, fcntl.LOCK_EX)
            state = self._read()
            result, changed = operation(state)
            if changed:
                self._write(state)
            return result
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def read(self, provider: str) -> SessionBinding | None:
        if provider not in _PROVIDERS:
            raise ProviderRequestError("container session provider is invalid")

        def operation(state):
            entry = state["bindings"].get(provider)
            if entry is None:
                return None, False
            if (not isinstance(entry, dict) or set(entry) !=
                    {"provider", "session_id", "volume", "turn_digest",
                     "upstream_route_digest"}):
                raise ProviderRequestError("container session binding is invalid")
            binding = SessionBinding(**entry)
            if (binding.provider != provider or binding.volume != self.volume
                    or not isinstance(binding.session_id, str)
                    or _SESSION_ID.fullmatch(binding.session_id) is None):
                raise ProviderRequestError("container session binding differs")
            _require_sha(binding.turn_digest, "turn")
            _require_sha(binding.upstream_route_digest, "upstream route")
            return binding, False

        return self._update(operation)

    def resume(self, provider: str) -> SessionBinding:
        binding = self.read(provider)
        if binding is None:
            raise ProviderRequestError("container resume requires an exact session binding")
        self.assert_clear(provider)
        return binding

    def assert_clear(self, provider: str) -> None:
        if provider not in _PROVIDERS:
            raise ProviderRequestError("container session provider is invalid")

        def operation(state):
            if any(turn == {"provider": provider, "state": "uncertain"}
                   for turn in state["turns"].values()):
                raise ProviderRequestError("container prior turn is uncertain")
            return None, False

        self._update(operation)

    def require_uncertain(self, provider: str, turn_digest: str) -> None:
        if provider not in _PROVIDERS:
            raise ProviderRequestError("container session provider is invalid")
        _require_sha(turn_digest, "turn")

        def operation(state):
            if state["turns"].get(turn_digest) != {
                    "provider": provider, "state": "uncertain"}:
                raise ProviderRequestError("container export needs the reserved turn")
            return None, False

        self._update(operation)

    def require_quarantine(self, provider: str, permit_id: str,
                           container_id: str) -> None:
        """Durably block this provider when an owned seat could not be fenced."""
        if provider not in _PROVIDERS:
            raise ProviderRequestError("container session provider is invalid")
        _require_sha(permit_id, "permit")
        _require_sha(container_id, "identity")
        key = _digest({"quarantine": container_id, "permit": permit_id})

        def operation(state):
            expected = {"provider": provider, "state": "uncertain"}
            prior = state["turns"].get(key)
            if prior is not None and prior != expected:
                raise ProviderRequestError("container quarantine record differs")
            state["turns"][key] = expected
            return None, prior is None

        self._update(operation)

    def reserve(self, provider: str, turn_digest: str,
                *, initial: bool, upstream_route_digest: str,
                initial_session_id: str | None = None) -> None:
        if provider not in _PROVIDERS:
            raise ProviderRequestError("container session provider is invalid")
        if type(initial) is not bool:
            raise ProviderRequestError("container turn route is invalid")
        _require_sha(turn_digest, "turn")
        _require_sha(upstream_route_digest, "upstream route")
        if initial_session_id is not None and (
                provider != "claude" or not isinstance(initial_session_id, str)
                or _SESSION_ID.fullmatch(initial_session_id) is None):
            raise ProviderRequestError("container initial session identity is invalid")

        def operation(state):
            if turn_digest in state["turns"]:
                raise ProviderRequestError("container uncertain or settled turn replay refused")
            if any(turn == {"provider": provider, "state": "uncertain"}
                   for turn in state["turns"].values()):
                raise ProviderRequestError("container prior turn is uncertain")
            if initial and provider in state["bindings"]:
                raise ProviderRequestError("container initial session already bound")
            if not initial and state["bindings"].get(provider, {}).get(
                    "upstream_route_digest") != upstream_route_digest:
                raise ProviderRequestError("container resume changed upstream route")
            if initial_session_id is not None:
                state["bindings"][provider] = asdict(SessionBinding(
                    provider, initial_session_id, self.volume, turn_digest,
                    upstream_route_digest,
                ))
            state["turns"][turn_digest] = {"provider": provider, "state": "uncertain"}
            return None, True

        self._update(operation)

    def settle(self, provider: str, session_id: str, turn_digest: str,
               *, initial: bool, upstream_route_digest: str) -> None:
        if (provider not in _PROVIDERS or not isinstance(session_id, str)
                or _SESSION_ID.fullmatch(session_id) is None):
            raise ProviderRequestError("container exact session identity is invalid")
        _require_sha(turn_digest, "turn")
        _require_sha(upstream_route_digest, "upstream route")

        def operation(state):
            turn = state["turns"].get(turn_digest)
            if turn != {"provider": provider, "state": "uncertain"}:
                raise ProviderRequestError("container turn has no uncertain reservation")
            prior = state["bindings"].get(provider)
            if initial:
                expected = asdict(SessionBinding(provider, session_id,
                                                 self.volume, turn_digest,
                                                 upstream_route_digest))
                if prior is not None and (provider != "claude" or prior != expected):
                    raise ProviderRequestError("container initial session already bound")
                state["bindings"][provider] = expected
            elif (prior is None or prior.get("session_id") != session_id
                  or prior.get("upstream_route_digest") != upstream_route_digest):
                raise ProviderRequestError("container resume changed exact session identity")
            turn["state"] = "settled"
            return None, True

        self._update(operation)


def _cleanup_failure_note(controller: LifecycleController, phase: str,
                          cleanup: BaseException, *, primary: BaseException | None = None,
                          spec: ContainerSpec | None = None,
                          handle: ContainerHandle | None = None,
                          permit_id: str | None = None,
                          provider: str | None = None,
                          store: SessionStore | None = None) -> str:
    """Keep cleanup evidence independent of a failed turn's terminal record."""
    failures = []
    if (store is not None and provider is not None and permit_id is not None
            and handle is not None):
        try:
            store.require_quarantine(provider, permit_id, handle.container_id)
        except BaseException as error:
            failures.append(f"quarantine record: {type(error).__name__}")
    payload = {
        "phase": phase,
        "spec": (asdict(spec) if isinstance(spec, ContainerSpec) else None),
        "container_id": handle.container_id if isinstance(handle, ContainerHandle) else None,
        "permit_id": permit_id,
        "provider": provider,
        "primary_type": type(primary).__name__ if primary is not None else None,
        "cleanup_type": type(cleanup).__name__,
    }
    try:
        _, digest = write_evidence(controller, _CLEANUP_CATEGORY, payload)
        receipt = digest
    except BaseException as error:
        receipt = "unavailable"
        failures.append(f"cleanup receipt: {type(error).__name__}")
    extra = f"; {'; '.join(failures)}" if failures else ""
    return f"Container cleanup failed; seat requires quarantine; receipt={receipt}{extra}"


class HostSessionLease(AbstractContextManager["HostSessionLease"]):
    """Cross-process, nonblocking permit lease held for an SSH/relay session."""

    def __init__(self, controller: LifecycleController, permit_id: str,
                 epoch: int):
        assert_controller(controller)
        _require_sha(permit_id, "permit")
        if type(epoch) is not int or epoch < 1:
            raise ProviderRequestError("container permit epoch is invalid")
        self.path = controller_directory(controller, "locks") / f"relay-{permit_id}-{epoch}.lock"
        self._fd: int | None = None

    def __enter__(self) -> "HostSessionLease":
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid()):
                raise ProviderRequestError("host relay lease file is unsafe")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ProviderRequestError("host relay lease is already live") from error
            self._fd = fd
            self._identity = info.st_dev, info.st_ino
            return self
        except BaseException:
            os.close(fd)
            raise

    def require_held(self) -> dict:
        if self._fd is None:
            raise ProviderRequestError("host relay lease is not held")
        try:
            info = os.fstat(self._fd)
            path = os.stat(self.path, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid() or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != self._identity
                    or (path.st_dev, path.st_ino) != self._identity):
                raise ProviderRequestError("host relay lease changed association")
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ProviderRequestError("host relay lease is unavailable") from error
        return {"path": str(self.path), "device": info.st_dev, "inode": info.st_ino}

    def __exit__(self, *_: object) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


class PinnedSSHSeatControl:
    """One owner-pinned SSH relay; short control probes use the same host key."""

    def __init__(self, controller: LifecycleController, spec: ContainerSpec,
                 ledger: PermitLedger, broker: Broker, permit_id: str,
                 epoch: int):
        assert_controller(controller)
        if (not isinstance(ledger, PermitLedger) or
                ledger.controller.controller_id != controller.controller_id or
                not isinstance(broker, Broker)):
            raise ProviderRequestError("container host broker differs")
        try:
            ledger.require_permit(permit_id, epoch, broker.permit)
        except BrokerRejected as error:
            raise ProviderRequestError("container relay permit differs") from error
        if (spec.run_id, spec.task_id, spec.target_id, spec.seat_id) != (
                broker.permit.run_id, broker.permit.task_id,
                broker.permit.target_id, broker.permit.seat_id):
            raise ProviderRequestError("container relay target differs")
        self.controller = controller
        self.spec = spec
        self.ledger = ledger
        self.broker = broker
        self.permit_id = permit_id
        self.epoch = epoch
        self._lease: HostSessionLease | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._relay_error: BaseException | None = None
        self._guest_relay_path: str | None = None
        self._socket_at_open: GuestSocketAttestation | None = None
        self._startup_failure: str | None = None
        self._last_ssh_failure: tuple[str, str] | None = None
        self._startup_ssh_failure: tuple[str, str] | None = None
        self._turn_failure_ref: tuple[str, str] | None = None
        from scripts.fanout_container_vm_probe import _read_pinned_host_key
        self._host_key = _read_pinned_host_key(Path.home())

    def _argv(self, guest_argv: tuple[str, ...]) -> tuple[str, ...]:
        from scripts.fanout_container_vm_probe import (
            _read_pinned_host_key, _strict_ssh_argv,
        )
        home = Path.home()
        if _read_pinned_host_key(home) != self._host_key:
            raise ProviderRequestError("dedicated SSH host key changed")
        config = (home / ".colima/_lima/colima-fanout-isolated/ssh.config").read_text()
        return _strict_ssh_argv(config,
                                home / ".colima/.fanout-isolated-known_hosts",
                                guest_argv)

    def _ssh(self, guest_argv: tuple[str, ...], data: bytes = b"", *,
             operation: str, timeout: float = 30) -> bytes:
        if type(operation) is not str or operation not in _SSH_OPERATIONS:
            raise ProviderRequestError("pinned guest SSH operation is invalid")
        def capture(category, *, returncode=None, timed_out=False, stderr=None, stdout_bytes=None):
            text = None; omission = "unavailable"
            if type(stderr) is bytes:
                if len(stderr) > 8192:
                    omission = "stderr_budget"
                else:
                    try:
                        value = stderr.decode("utf-8")
                        _safe_response_body(canonical_json({"stderr": value}), "application/json", self.broker._credential)
                        text = value; omission = None
                    except (BrokerRejected, UnicodeError, ValueError, RecursionError):
                        omission = "unsafe_stderr"
            try:
                source_sha256 = runtime_source_digest(Path(__file__).resolve().parents[3])
                if type(source_sha256) is not str or _SHA.fullmatch(source_sha256) is None:
                    raise ProviderRequestError("pinned guest SSH diagnostic source is invalid")
                self._last_ssh_failure = write_evidence(self.controller, "container-ssh-control-failure", {
                    "schema_version": 1, "operation": operation, "category": category,
                    "returncode": returncode, "timeout": timed_out, "stderr": text,
                    "stderr_omission": omission, "stdout_bytes": stdout_bytes,
                    "permit_id": self.permit_id, "permit_epoch": self.epoch,
                    "permit_digest": _permit_digest(self.broker.permit),
                    "source_sha256": source_sha256,
                    **{key: getattr(self.spec, key) for key in ("run_id", "task_id", "target_id", "seat_id")},
                    "provider": self.broker.permit.provider,
                })
            except BaseException:
                raise ProviderRequestError("pinned guest SSH diagnostic capture failed") from None
        try:
            result = _run_control_command(self._argv(guest_argv), data=data,
                                          timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            if type(error) is subprocess.TimeoutExpired:
                capture("timeout", timed_out=True, stderr=error.stderr)
            else:
                capture("spawn_failure")
            raise ProviderRequestError("pinned guest SSH control failed") from error
        if result.status == ProcessStatus.TIMEOUT:
            capture("timeout", timed_out=True, stderr=result.stderr)
            raise ProviderRequestError("pinned guest SSH control failed")
        if result.status == ProcessStatus.SPAWN_ERROR:
            capture("spawn_failure")
            raise ProviderRequestError("pinned guest SSH control failed")
        if result.status == ProcessStatus.OUTPUT_LIMIT:
            capture(result.error + "_budget", stderr=result.stderr if result.error != "stderr" else None,
                    stdout_bytes=len(result.stdout))
            raise ProviderRequestError("pinned guest SSH control failed")
        if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
            capture("nonzero_exit" if result.returncode != 0 else "stdout_budget",
                    returncode=result.returncode if type(result.returncode) is int else None,
                    stderr=result.stderr, stdout_bytes=min(len(result.stdout), 1024 * 1024 + 1))
            raise ProviderRequestError("pinned guest SSH control failed")
        return result.stdout

    def attest_vm(self) -> VMProbe:
        from scripts.fanout_container_vm_probe import (
            _binding_path, _profile_identity, _read_binding,
            _require_profile_binding,
        )
        home = Path.home()
        try:
            status = _run_control_command(("colima", "status", "-p", PROFILE, "--json"))
            context = _run_control_command((*DOCKER[:1], "context", "inspect", CONTEXT,
                                           "--format", "{{json .}}"))
            info = _run_control_command((*DOCKER, "info", "--format", "{{.ServerVersion}}"))
            if (any(result.status != ProcessStatus.EXIT for result in (status, context, info))
                    or status.returncode != 0 or context.returncode != 0
                    or info.returncode != 0 or not info.stdout.strip()
                    or len(context.stdout) > 1024 * 1024):
                raise ProviderRequestError("dedicated VM control is unavailable")
            observed_context = json.loads(context.stdout)
            identity = _profile_identity(
                home, observed_context,
                hashlib.sha256(self._host_key.encode("ascii")).hexdigest(),
            )
            profile_binding = _read_binding(_binding_path(home))
            _require_profile_binding(profile_binding, identity)
            colima_raw = (home / ".colima/fanout-isolated/colima.yaml").read_bytes()
            lima_raw = (home / ".colima/_lima/colima-fanout-isolated/lima.yaml").read_bytes()
            colima, lima = colima_raw.decode("utf-8"), lima_raw.decode("utf-8")
            config = parse_profile_config(colima, lima, observed_context["Name"])
            mounts_raw = self._ssh(("cat", "/proc/mounts"), operation="vm_mounts")
            mounts = mounts_raw.decode("utf-8").splitlines()
            nonce = secrets.token_bytes(32)
            reply = self._ssh(("python3", "-c",
                               "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"),
                              nonce, operation="vm_nonce")
            probe = require_vm_probe(
                VMProfileSpec(PROFILE, CONTEXT, "x86_64"), config, mounts,
                observed_context["Endpoints"]["docker"]["Host"], nonce, reply,
                ssh_host_key=self._host_key, pinned_host_key=self._host_key,
            )
            observed = {"status": status.stdout, "context": context.stdout,
                        "info": info.stdout, "colima": colima_raw,
                        "lima": lima_raw, "mounts": mounts_raw,
                        "nonce": nonce, "reply": reply}
            if any(not isinstance(raw, bytes) or len(raw) > 1024 * 1024
                   for raw in observed.values()):
                raise ProviderRequestError("VM observation exceeds proof budget")
            self.vm_observation = {name: base64.b64encode(raw).decode("ascii")
                                   for name, raw in observed.items()}
            self.vm_observation.update({"host_key": self._host_key, "probe": asdict(probe)})
            if len(canonical_json(identity)) > 32768 or len(canonical_json(profile_binding)) > 32768:
                raise ProviderRequestError("VM profile identity exceeds proof budget")
            self.vm_observation.update(profile_identity=identity, profile_binding=profile_binding)
            return probe
        except (OSError, ValueError, KeyError, UnicodeError,
                subprocess.TimeoutExpired) as error:
            raise ProviderRequestError("dedicated VM live attestation failed") from error

    def _bootstrap(self) -> None:
        prepare = """import os,stat,sys
p=sys.argv[1]
try:
 os.mkdir(p,0o700); os.chown(p,65532,65532)
except FileExistsError:
 pass
s=os.lstat(p)
if not stat.S_ISDIR(s.st_mode) or s.st_uid!=65532 or stat.S_IMODE(s.st_mode)!=0o700:
 raise SystemExit(1)
"""
        self._ssh(("sudo", "-n", "python3", "-c", prepare, "/run/fanout"), operation="relay_directory_prepare")
        root = Path(__file__).resolve().parents[3]
        sources = (root / "components/fanout-container/relay.py",)
        receive = """import hashlib,os,stat,sys
p=sys.argv[1]; expected=sys.argv[2]; data=sys.stdin.buffer.read()
if hashlib.sha256(data).hexdigest()!=expected: raise SystemExit(1)
try:
 fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400)
except FileExistsError:
 fd=None
if fd is not None:
 try:
  os.write(fd,data); os.fsync(fd)
 finally:
  os.close(fd)
s=os.lstat(p)
if not stat.S_ISREG(s.st_mode) or s.st_uid!=65532 or stat.S_IMODE(s.st_mode)!=0o400:
 raise SystemExit(1)
with open(p,"rb") as f: observed=hashlib.sha256(f.read()).hexdigest()
if observed!=expected: raise SystemExit(1)
print(observed)
"""
        for source in sources:
            payload = source.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            guest = f"/run/fanout/{source.stem}-{digest}.py"
            observed = self._ssh(_as_guest_relay_uid("-c", receive, guest,
                                                     digest), payload, operation="relay_source_transfer")
            if observed.strip() != digest.encode("ascii"):
                raise ProviderRequestError("guest bootstrap byte hash differs")
            if source.name == "relay.py":
                self._guest_relay_path = guest

    def attest_socket(self, path: str, *, timeout: float = 30) -> GuestSocketAttestation:
        if path != self.spec.socket_path:
            raise ProviderRequestError("guest socket path differs")
        script = ("import json,os,sys; s=os.lstat(sys.argv[1]); "
                  "print(json.dumps({'path':sys.argv[1],'uid':s.st_uid,"
                  "'mode':s.st_mode,'device':s.st_dev,'inode':s.st_ino,"
                  "'ctime_ns':s.st_ctime_ns},sort_keys=True))")
        try:
            value = json.loads(self._ssh(_as_guest_relay_uid("-c", script,
                                                             path), operation="socket_attestation", timeout=timeout))
            proof = GuestSocketAttestation(**value)
        except (TypeError, ValueError, KeyError) as error:
            raise ProviderRequestError("guest socket attestation is malformed") from error
        if (proof.path != path or proof.uid != 65532
                or not stat.S_ISSOCK(proof.mode)
                or stat.S_IMODE(proof.mode) != 0o600
                or any(type(value) is not int or value <= 0 for value in
                       (proof.device, proof.inode, proof.ctime_ns))):
            raise ProviderRequestError("guest socket attestation differs")
        return proof

    def open(self) -> None:
        self._startup_failure = None
        self._startup_ssh_failure = None
        self._last_ssh_failure = None
        lease = HostSessionLease(self.controller, self.permit_id, self.epoch)
        lease.__enter__()
        self._lease = lease
        try:
            self._startup_failure = "bootstrap_failure"
            self._bootstrap()
            if self._guest_relay_path is None:
                raise ProviderRequestError("guest relay bootstrap is unavailable")
            permit = self.broker.permit
            guest = _as_guest_relay_uid(
                self._guest_relay_path, "guest-stdio",
                "--seat-id", self.spec.seat_id,
                "--permit-id", self.permit_id, "--epoch", str(self.epoch),
                "--max-requests", str(attempt_limit(permit)),
                "--max-request-bytes", str(permit.max_request_bytes),
                "--max-response-bytes", str(permit.max_response_bytes),
            )
            self._startup_failure = "relay_process_spawn_failure"
            self._process = subprocess.Popen(self._argv(guest), stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL)
            def serve() -> None:
                try:
                    assert self._process is not None
                    assert self._process.stdout is not None
                    assert self._process.stdin is not None
                    HostRelay(self.ledger, self.broker).serve(
                        FrameStream(self._process.stdout, self._process.stdin))
                except BaseException as error:
                    self._relay_error = error
            self._thread = threading.Thread(target=serve, daemon=True)
            self._startup_failure = "relay_worker_start_failure"
            self._thread.start()
            self._startup_failure = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self._relay_error is not None:
                    self._startup_failure = "relay_worker_failure"
                    raise ProviderRequestError("guest relay failed before readiness") from self._relay_error
                if self._process.poll() is not None:
                    self._startup_failure = "relay_process_exit"
                    raise ProviderRequestError("guest relay failed before readiness")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    proof = self.attest_socket(self.spec.socket_path, timeout=remaining)
                except ProviderRequestError:
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(0.1, remaining))
                    continue
                # Retain the genuine socket identity for cleanup, never admission after expiry.
                self._socket_at_open = proof
                if time.monotonic() >= deadline:
                    break
                if self._relay_error is not None:
                    self._startup_failure = "relay_worker_failure"
                    raise ProviderRequestError("guest relay failed before readiness") from self._relay_error
                if self._process.poll() is not None:
                    self._startup_failure = "relay_process_exit"
                    raise ProviderRequestError("guest relay failed before readiness")
                self._startup_failure = None
                return
            self._startup_failure = "readiness_expired"
            raise ProviderRequestError("guest relay readiness expired")
        except BaseException as primary:
            self._startup_ssh_failure = self._last_ssh_failure
            try:
                self.close()
            except BaseException as cleanup:
                primary.add_note(_cleanup_failure_note(
                    self.controller, "relay-open", cleanup, primary=primary,
                    spec=self.spec if isinstance(self.spec, ContainerSpec) else None,
                    permit_id=self.permit_id))
            raise

    def close(self) -> None:
        process = self._process
        errors: list[BaseException] = []
        process_done = process is None
        if process is not None:
            try:
                if process.poll() is None and self._socket_at_open is not None:
                    self._drain_relay(self._socket_at_open)
            except BaseException as error:
                errors.append(ProviderRequestError("guest relay did not close cleanly"))
                errors[-1].__cause__ = error
            try:
                if process.poll() is None:
                    process.terminate()
            except BaseException as error:
                errors.append(error)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=3)
                except BaseException as error:
                    errors.append(error)
            except BaseException as error:
                errors.append(error)
            try:
                process_done = process.poll() is not None
            except BaseException as error:
                errors.append(error)
            if process_done:
                self._process = None
            else:
                errors.append(ProviderRequestError("guest relay process remains live"))
        thread = self._thread
        thread_done = thread is None
        if thread is not None:
            try:
                if thread.ident is not None:
                    thread.join(timeout=3)
                thread_done = not thread.is_alive()
            except BaseException as error:
                errors.append(error)
            if thread_done:
                self._thread = None
            else:
                errors.append(ProviderRequestError("host relay thread remains live"))
        if process_done and thread_done and self._socket_at_open is not None:
            try:
                self._ssh(_as_guest_relay_uid(
                    "-c", "import os,sys;raise SystemExit(os.path.lexists(sys.argv[1]))",
                    self._socket_at_open.path), operation="socket_closed_check")
            except BaseException as error:
                errors.append(ProviderRequestError("guest relay did not close cleanly"))
                errors[-1].__cause__ = error
            else:
                self._socket_at_open = None
        if process_done and thread_done and self._lease is not None:
            try:
                self._lease.__exit__(None, None, None)
            except BaseException as error:
                errors.append(error)
            else:
                self._lease = None
        if self._relay_error is not None:
            error = ProviderRequestError("host relay worker failed")
            error.__cause__ = self._relay_error
            errors.append(error)
        if errors:
            first = errors[0]
            for error in errors[1:]:
                first.add_note(f"Additional relay cleanup failure: {type(error).__name__}")
            raise first

    def _drain_relay(self, proof: GuestSocketAttestation) -> None:
        # Invalid local POSTs consume only guest relay slots. They never form
        # broker frames, letting the reviewed relay close its own socket.
        script = """import os,socket,stat,sys,time
p=sys.argv[1]; device=int(sys.argv[2]); inode=int(sys.argv[3]); ctime=int(sys.argv[4]); limit=int(sys.argv[5])
for _ in range(limit):
 try:
  s=os.lstat(p)
 except FileNotFoundError:
  break
 if not stat.S_ISSOCK(s.st_mode) or (s.st_dev,s.st_ino,s.st_ctime_ns)!=(device,inode,ctime):
  raise SystemExit(1)
 c=socket.socket(socket.AF_UNIX); c.settimeout(2)
 try:
  c.connect(p)
  c.sendall(b"POST http://invalid.invalid/ HTTP/1.1\\r\\nHost: 127.0.0.1:43187\\r\\nContent-Length: 0\\r\\n\\r\\n")
  c.recv(1024)
 except (ConnectionRefusedError,FileNotFoundError,BrokenPipeError,ConnectionResetError):
  break
 finally:
  c.close()
 time.sleep(0.02)
"""
        self._ssh(_as_guest_relay_uid(
            "-c", script, proof.path, str(proof.device), str(proof.inode),
            str(proof.ctime_ns), str(attempt_limit(self.broker.permit) + 1)), operation="relay_drain", timeout=30)


@dataclass(frozen=True, slots=True)
class ContainerAdmission:
    broker_permit: BrokerPermit
    permit_id: str
    epoch: int
    ledger: PermitLedger = field(repr=False, compare=False)
    image_digest: str = ""
    cli_digest: str = ""
    socket_attestation: GuestSocketAttestation | None = None
    transport: ContainerTransport = field(default=None, repr=False, compare=False)
    session_store: SessionStore | None = field(default=None, repr=False, compare=False)
    handle_evidence_name: str = ""
    handle_evidence_digest: str = ""
    input_receipt: InputTransferReceipt | None = field(default=None, repr=False, compare=False)
    quality_provisioning: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ContainerSeatBoundary:
    schema_version: str
    controller_id: str
    run_id: str
    task_id: str
    target_id: str
    seat_id: str
    provider: str
    inputs_digest: str
    profile_digest: str
    request_digest: str
    vm_probe_digest: str
    vm_docker_endpoint: str
    vm_ssh_host_key_sha256: str
    vm_effective_mounts_sha256: str
    vm_guest_mounts_sha256: str
    image_digest: str
    cli_digest: str
    container_id: str
    container_inspect_sha256: str
    workspace_volume: str
    session_volume: str
    socket_path: str
    socket_uid: int
    socket_mode: int
    socket_device: int
    socket_inode: int
    socket_ctime_ns: int
    permit_id: str
    permit_epoch: int
    permit_digest: str
    upstream_route_digest: str
    route: str
    session_id: str | None
    admission_identity: int
    evidence_name: str
    evidence_digest: str
    handle_evidence_name: str
    handle_evidence_digest: str
    input_receipt_name: str
    input_receipt_digest: str
    input_manifest_sha256: str
    input_workdir: str
    controller: LifecycleController = field(repr=False, compare=False)
    admission: ContainerAdmission = field(repr=False, compare=False)
    session_store: SessionStore = field(repr=False, compare=False)


def _payload(boundary: ContainerSeatBoundary) -> dict[str, object]:
    return {name: getattr(boundary, name) for name in (
        "schema_version", "controller_id", "run_id", "task_id", "target_id",
        "seat_id", "provider", "inputs_digest", "profile_digest",
        "request_digest",
        "vm_probe_digest", "vm_docker_endpoint", "vm_ssh_host_key_sha256",
        "vm_effective_mounts_sha256",
        "vm_guest_mounts_sha256", "image_digest", "cli_digest",
        "container_id", "container_inspect_sha256", "workspace_volume",
        "session_volume", "socket_path", "socket_uid", "socket_mode",
        "socket_device", "socket_inode", "socket_ctime_ns", "permit_id",
        "permit_epoch", "permit_digest", "upstream_route_digest",
        "route", "session_id",
        "admission_identity",
        "handle_evidence_name", "handle_evidence_digest",
        "input_receipt_name", "input_receipt_digest",
        "input_manifest_sha256", "input_workdir",
    )}


def _authenticated_launch(controller: LifecycleController,
                          request: ProviderRequest, handle: ContainerHandle,
                          admission: ContainerAdmission) -> None:
    try:
        origin = read_evidence(controller, _HANDLE_CATEGORY,
                               admission.handle_evidence_name,
                               admission.handle_evidence_digest)
    except Exception as error:
        raise ProviderRequestError("container original handle provenance is invalid") from error
    expected = ContainerSpec(
        request.run_id, request.task_id, request.target_id, request.seat_id,
        admission.image_digest, handle.workspace_volume, handle.session_volume,
        f"/run/fanout/{request.seat_id}.sock", admission.cli_digest,
    )
    decode_launch_record(origin, expected, handle, recover=request.resume)
    authenticate_launch_record(controller, origin, expected, handle,
                              inputs_digest=request.inputs_digest)


def _check_association(request: ProviderRequest, admission: ContainerAdmission) -> None:
    if not isinstance(request, ProviderRequest) or request.profile is None:
        raise ProviderRequestError("container request needs a pinned provider profile")
    if (not isinstance(admission, ContainerAdmission)
            or not isinstance(admission.broker_permit, BrokerPermit)
            or not isinstance(admission.ledger, PermitLedger)
            or not isinstance(admission.session_store, SessionStore)):
        raise ProviderRequestError("container admission is invalid")
    permit = admission.broker_permit
    if permit.upstream_route_digest is None:
        raise ProviderRequestError("container upstream route is absent")
    if (permit.provider, permit.run_id, permit.task_id, permit.target_id,
            permit.seat_id, permit.route, permit.model, permit.inputs_digest,
            permit.session_id) != (
            request.executor_id, request.run_id, request.task_id,
            request.target_id, request.seat_id,
            "resume" if request.resume else "initial",
            request.profile.requested_model, request.inputs_digest,
            request.session_id):
        raise ProviderRequestError("container broker permit changed association")
    if request.execution_class != "repo-write" or request.retries != 0:
        raise ProviderRequestError("container diagnostic requires one repo-write attempt")
    if request.resume:
        binding = admission.session_store.resume(request.executor_id)
        if (binding.session_id != request.session_id
                or binding.volume != admission.session_store.volume
                or binding.upstream_route_digest != permit.upstream_route_digest):
            raise ProviderRequestError("container resume requires the exact session and volume")
    else:
        admission.session_store.assert_clear(request.executor_id)
    if not request.resume and request.executor_id == "claude":
        if request.session_id is None:
            raise ProviderRequestError("Claude initial needs a caller-issued session ID")
    elif not request.resume and request.session_id is not None:
        raise ProviderRequestError("container initial session must be unassigned")
    if admission.session_store.identity != (
            request.run_id, request.task_id, request.target_id, request.seat_id):
        raise ProviderRequestError("container session store changed target association")
    try:
        admission.ledger.require_permit(admission.permit_id, admission.epoch, permit)
    except BrokerRejected as error:
        raise ProviderRequestError("container broker permit changed or was fenced") from error


def _check_live(boundary: ContainerSeatBoundary) -> None:
    admission = boundary.admission
    worker_error = getattr(getattr(admission.transport, "_control", None), "_relay_error", None)
    if worker_error is not None:
        raise ProviderRequestError("host relay worker failed") from worker_error
    try:
        vm = admission.transport.attest_vm()
        handle = admission.transport.attest_container()
        socket = admission.transport.attest_socket()
        image = admission.transport.attest_image()
        cli = admission.transport.attest_cli(boundary.provider)
    except Exception as error:
        raise ProviderRequestError("container live attestation failed") from error
    if not isinstance(vm, VMProbe) or (
            vm.profile, vm.docker_endpoint, vm.ssh_host_key_sha256,
            vm.effective_mounts_sha256, vm.guest_mounts_sha256) != (
            "fanout-isolated", boundary.vm_docker_endpoint,
            boundary.vm_ssh_host_key_sha256,
            boundary.vm_effective_mounts_sha256,
            boundary.vm_guest_mounts_sha256):
        raise ProviderRequestError("container VM effective mounts or identity changed")
    if not isinstance(handle, ContainerHandle) or (
            handle.container_id, handle.inspect_sha256,
            handle.workspace_volume, handle.session_volume) != (
            boundary.container_id, boundary.container_inspect_sha256,
            boundary.workspace_volume, boundary.session_volume):
        raise ProviderRequestError("container inspect or private volumes changed")
    if not isinstance(socket, GuestSocketAttestation) or (
            socket.path, socket.uid, socket.mode, socket.device,
            socket.inode, socket.ctime_ns) != (
            boundary.socket_path, boundary.socket_uid, boundary.socket_mode,
            boundary.socket_device, boundary.socket_inode, boundary.socket_ctime_ns):
        raise ProviderRequestError("container guest socket changed")
    if image != boundary.image_digest:
        raise ProviderRequestError("container image digest changed")
    if cli != boundary.cli_digest:
        raise ProviderRequestError("container Linux CLI digest changed")


def _issue_container_boundary(controller: LifecycleController,
                              request: ProviderRequest, vm_probe: VMProbe,
                              handle: ContainerHandle,
                              permit: ContainerAdmission) -> ContainerSeatBoundary:
    """Seal one controller-issued diagnostic turn after exact live admission."""
    assert_controller(controller)
    if isinstance(permit, ContainerAdmission) and type(permit.transport) is PinnedDockerTransport:
        receipt = permit.input_receipt
        if receipt is None and request.resume:
            receipt = permit.transport.reopen_input_receipt(request, permit)
            permit = replace(permit, input_receipt=receipt)
        if receipt is None:
            raise ProviderRequestError("container input receipt is absent")
        validate_input_receipt(controller, request, receipt,
                               permit.transport.handle.workspace_volume)
    _check_association(request, permit)
    if (permit.ledger.controller.controller_id != controller.controller_id
            or permit.session_store.controller.controller_id != controller.controller_id):
        raise ProviderRequestError("container admission belongs to another controller")
    if not isinstance(vm_probe, VMProbe) or vm_probe.profile != "fanout-isolated":
        raise ProviderRequestError("container VM probe is invalid")
    if not isinstance(handle, ContainerHandle):
        raise ProviderRequestError("container handle is invalid")
    _authenticated_launch(controller, request, handle, permit)
    workspace, session = private_volume_names(request.run_id, request.task_id,
                                              request.target_id, request.seat_id)
    if ((handle.workspace_volume, handle.session_volume) != (workspace, session)
            or permit.session_store.volume != session):
        raise ProviderRequestError("container private volume identity differs")
    socket = permit.socket_attestation
    if (not isinstance(socket, GuestSocketAttestation)
            or socket.path != f"/run/fanout/{request.seat_id}.sock"
            or socket.uid != 65532 or not stat.S_ISSOCK(socket.mode)
            or stat.S_IMODE(socket.mode) != 0o600
            or any(type(value) is not int or value <= 0 for value in
                   (socket.device, socket.inode, socket.ctime_ns))):
        raise ProviderRequestError("container guest socket attestation is invalid")
    _require_sha(permit.cli_digest, "CLI")
    _require_sha(handle.inspect_sha256, "inspect")
    _require_sha(request.inputs_digest, "inputs")
    if (not isinstance(permit.image_digest, str)
            or "@sha256:" not in permit.image_digest
            or _SHA.fullmatch(permit.image_digest.rsplit("@sha256:", 1)[-1]) is None):
        raise ProviderRequestError("container image is not pinned by digest")
    boundary = ContainerSeatBoundary(
        schema_version=_SCHEMA, controller_id=controller.controller_id,
        run_id=request.run_id, task_id=request.task_id,
        target_id=request.target_id, seat_id=request.seat_id,
        provider=request.executor_id, inputs_digest=request.inputs_digest,
        profile_digest=request.profile.digest,
        request_digest=admitted_request_digest(request),
        vm_probe_digest=vm_probe.digest,
        vm_docker_endpoint=vm_probe.docker_endpoint,
        vm_ssh_host_key_sha256=vm_probe.ssh_host_key_sha256,
        vm_effective_mounts_sha256=vm_probe.effective_mounts_sha256,
        vm_guest_mounts_sha256=vm_probe.guest_mounts_sha256,
        image_digest=permit.image_digest, cli_digest=permit.cli_digest,
        container_id=handle.container_id,
        container_inspect_sha256=handle.inspect_sha256,
        workspace_volume=workspace, session_volume=session,
        socket_path=socket.path, socket_uid=socket.uid, socket_mode=socket.mode,
        socket_device=socket.device, socket_inode=socket.inode,
        socket_ctime_ns=socket.ctime_ns,
        permit_id=permit.permit_id, permit_epoch=permit.epoch,
        permit_digest=_digest(asdict(permit.broker_permit)),
        upstream_route_digest=permit.broker_permit.upstream_route_digest,
        route="resume" if request.resume else "initial",
        session_id=request.session_id, admission_identity=id(permit),
        evidence_name="", evidence_digest="",
        handle_evidence_name=permit.handle_evidence_name,
        handle_evidence_digest=permit.handle_evidence_digest,
        input_receipt_name=(permit.input_receipt.evidence_name
                            if permit.input_receipt is not None else ""),
        input_receipt_digest=(permit.input_receipt.evidence_digest
                              if permit.input_receipt is not None else ""),
        input_manifest_sha256=(permit.input_receipt.manifest_sha256
                               if permit.input_receipt is not None else ""),
        input_workdir=(permit.input_receipt.workdir
                       if permit.input_receipt is not None else ""),
        controller=controller,
        admission=permit, session_store=permit.session_store,
    )
    _check_live(boundary)
    name, digest = write_evidence(controller, _CATEGORY, _payload(boundary))
    return replace(boundary, evidence_name=name, evidence_digest=digest)


def issue_container_boundary(controller: LifecycleController,
                             request: ProviderRequest, vm_probe: VMProbe,
                             handle: ContainerHandle,
                             permit: ContainerAdmission) -> ContainerSeatBoundary:
    """Seal admission, quarantining a known live seat if issuance fails."""
    try:
        return _issue_container_boundary(controller, request, vm_probe,
                                         handle, permit)
    except BaseException as primary:
        if _issue_cleanup_authorized(controller, request, handle, permit):
            if not request.resume:
                try:
                    abandon_input_claim(controller, request,
                                        handle.workspace_volume)
                except BaseException as abandonment:
                    primary.add_note(
                        f"Container input abandonment failed: {type(abandonment).__name__}")
            try:
                permit.transport.close()
            except BaseException as cleanup:
                primary.add_note(_cleanup_failure_note(
                    controller, "admission", cleanup, primary=primary,
                    spec=permit.transport.spec, handle=permit.transport.handle,
                    permit_id=permit.permit_id,
                    provider=request.executor_id,
                    store=permit.session_store))
        raise


def _issue_cleanup_authorized(controller: LifecycleController,
                              request: ProviderRequest, handle: ContainerHandle,
                              permit: ContainerAdmission) -> bool:
    if (not isinstance(request, ProviderRequest) or request.profile is None
            or request.execution_class != "repo-write" or request.retries != 0
            or not isinstance(handle, ContainerHandle)
            or not isinstance(permit, ContainerAdmission)
            or not isinstance(permit.broker_permit, BrokerPermit)
            or type(permit.transport) is not PinnedDockerTransport):
        return False
    transport = permit.transport
    if (transport.controller is not controller or transport.handle != handle
            or (transport.handle_evidence_name, transport.handle_evidence_digest) !=
            (permit.handle_evidence_name, permit.handle_evidence_digest)
            or (request.run_id, request.task_id, request.target_id, request.seat_id,
                request.executor_id, request.inputs_digest, request.session_id,
                "resume" if request.resume else "initial", request.profile.requested_model) !=
            (transport.spec.run_id, transport.spec.task_id, transport.spec.target_id,
             transport.spec.seat_id, permit.broker_permit.provider,
             permit.broker_permit.inputs_digest, permit.broker_permit.session_id,
             permit.broker_permit.route, permit.broker_permit.model)):
        return False
    try:
        _authenticated_launch(controller, request, handle, permit)
    except Exception:
        return False
    return True


def validate_container_boundary(boundary: ContainerSeatBoundary,
                                request: ProviderRequest,
                                controller: LifecycleController) -> None:
    """Authenticate durable seal and reattest all live components before spend."""
    if not isinstance(boundary, ContainerSeatBoundary):
        raise ProviderRequestError("container boundary type is invalid")
    assert_controller(controller)
    if (boundary.controller_id != controller.controller_id
            or boundary.controller.controller_id != controller.controller_id
            or boundary.admission_identity != id(boundary.admission)):
        raise ProviderRequestError("container boundary controller or admission differs")
    if (not isinstance(boundary.session_store, SessionStore)
            or boundary.session_store.controller.controller_id != controller.controller_id
            or boundary.session_store.identity != (
                boundary.run_id, boundary.task_id, boundary.target_id,
                boundary.seat_id)
            or boundary.session_store.volume != boundary.session_volume):
        raise ProviderRequestError("container boundary session store differs")
    try:
        payload = read_evidence(controller, _CATEGORY,
                                boundary.evidence_name, boundary.evidence_digest)
    except Exception as error:
        raise ProviderRequestError("container boundary seal is invalid") from error
    if payload != _payload(boundary) or boundary.schema_version != _SCHEMA:
        raise ProviderRequestError("container boundary changed after admission")
    if not isinstance(request, ProviderRequest) or request.profile is None or (
            boundary.run_id, boundary.task_id, boundary.target_id,
            boundary.seat_id, boundary.provider, boundary.inputs_digest,
            boundary.profile_digest, boundary.request_digest,
            boundary.route, boundary.session_id) != (
            request.run_id, request.task_id, request.target_id,
            request.seat_id, request.executor_id, request.inputs_digest,
            request.profile.digest, admitted_request_digest(request),
            "resume" if request.resume else "initial",
            request.session_id):
        raise ProviderRequestError("container boundary changed request association")
    _check_association(request, boundary.admission)
    if (boundary.permit_id != boundary.admission.permit_id
            or boundary.permit_epoch != boundary.admission.epoch
            or boundary.permit_digest != _digest(asdict(boundary.admission.broker_permit))
            or boundary.upstream_route_digest !=
            boundary.admission.broker_permit.upstream_route_digest
            or boundary.image_digest != boundary.admission.image_digest
            or boundary.cli_digest != boundary.admission.cli_digest
            or boundary.session_volume != boundary.admission.session_store.volume):
        raise ProviderRequestError("container boundary changed permit or Linux pins")
    if ((boundary.handle_evidence_name, boundary.handle_evidence_digest) !=
            (boundary.admission.handle_evidence_name,
             boundary.admission.handle_evidence_digest)):
        raise ProviderRequestError("container original handle reference changed")
    if type(boundary.admission.transport) is PinnedDockerTransport:
        receipt = boundary.admission.input_receipt
        if (receipt is None or
                (boundary.input_receipt_name, boundary.input_receipt_digest,
                 boundary.input_manifest_sha256, boundary.input_workdir) !=
                (receipt.evidence_name, receipt.evidence_digest,
                 receipt.manifest_sha256, receipt.workdir)):
            raise ProviderRequestError("container input receipt changed")
        validate_input_receipt(controller, request, receipt,
                               boundary.workspace_volume)
    _authenticated_launch(controller, request, ContainerHandle(
        boundary.container_id, boundary.container_inspect_sha256,
        boundary.workspace_volume, boundary.session_volume), boundary.admission)
    _check_live(boundary)


def _turn_cleanup_authorized(request: ProviderRequest,
                             boundary: ContainerSeatBoundary) -> bool:
    if (not isinstance(request, ProviderRequest) or request.profile is None
            or request.execution_class != "repo-write" or request.retries != 0
            or not isinstance(boundary, ContainerSeatBoundary)
            or not isinstance(boundary.admission, ContainerAdmission)
            or not isinstance(boundary.admission.broker_permit, BrokerPermit)
            or not isinstance(boundary.controller, LifecycleController)
            or boundary.admission_identity != id(boundary.admission)
            or boundary.controller_id != boundary.controller.controller_id
            or boundary.schema_version != _SCHEMA):
        return False
    admission = boundary.admission
    if (not isinstance(admission.session_store, SessionStore)
            or admission.session_store is not boundary.session_store
            or admission.session_store.controller is not boundary.controller
            or admission.session_store.identity != (
                boundary.run_id, boundary.task_id, boundary.target_id,
                boundary.seat_id)
            or admission.session_store.volume != boundary.session_volume
            or (boundary.run_id, boundary.task_id, boundary.target_id,
                boundary.seat_id, boundary.provider, boundary.inputs_digest,
                boundary.profile_digest, boundary.route, boundary.session_id) !=
            (request.run_id, request.task_id, request.target_id,
             request.seat_id, request.executor_id, request.inputs_digest,
             request.profile.digest, "resume" if request.resume else "initial",
             request.session_id)
            or (boundary.permit_id, boundary.permit_epoch, boundary.permit_digest,
                boundary.upstream_route_digest,
                boundary.image_digest, boundary.cli_digest,
                boundary.handle_evidence_name, boundary.handle_evidence_digest) !=
            (admission.permit_id, admission.epoch,
             _digest(asdict(admission.broker_permit)),
             admission.broker_permit.upstream_route_digest,
             admission.image_digest,
             admission.cli_digest, admission.handle_evidence_name,
             admission.handle_evidence_digest)):
        return False
    try:
        sealed = read_evidence(boundary.controller, _CATEGORY,
                               boundary.evidence_name, boundary.evidence_digest)
        if sealed != _payload(boundary):
            return False
        handle = ContainerHandle(boundary.container_id,
                                 boundary.container_inspect_sha256,
                                 boundary.workspace_volume, boundary.session_volume)
        if type(admission.transport) is PinnedDockerTransport and (
                admission.transport.controller is not boundary.controller
                or admission.transport.handle != handle):
            return False
        _authenticated_launch(boundary.controller, request, handle, admission)
    except Exception:
        return False
    return True


def _turn_digest(request: ProviderRequest, boundary: ContainerSeatBoundary) -> str:
    return _digest({"run": request.run_id, "task": request.task_id,
                    "target": request.target_id, "seat": request.seat_id,
                    "provider": request.executor_id, "route": boundary.route,
                    "upstream_route_digest": boundary.upstream_route_digest,
                    "permit_id": boundary.permit_id,
                    "permit_epoch": boundary.permit_epoch,
                    "session": request.session_id,
                    "prompt_sha256": hashlib.sha256(request.prompt_bytes).hexdigest(),
                    "inputs": request.inputs_digest})


_CODEX_LOOPBACK_PROVIDER = (
    'model_providers.fanout={name="Fanout",'
    'base_url="http://127.0.0.1:43187/v1",wire_api="responses",'
    'http_headers={x-fanout-seat="disposable-placeholder"},'
    'request_max_retries=0,stream_max_retries=0}'
)


@dataclass(frozen=True, slots=True)
class QualityContainerCommand:
    canonical: ProcessCommand
    guest: ProcessCommand
    workdir: str
    catalog_path: str | None
    request_digest: str
    canonical_digest: str
    guest_digest: str


def quality_agy_agent_command(handle: ContainerHandle, *, verify_only: bool = False) -> ProcessCommand:
    """Fixed no-model create/check operation, to be joined before any guest dispatch."""
    from .eval_provider import AGY_AGENT_TEXT
    if (type(handle) is not ContainerHandle or type(handle.container_id) is not str
            or _SHA.fullmatch(handle.container_id) is None or type(verify_only) is not bool):
        raise ProviderRequestError('quality container agent operation differs')
    return ProcessCommand((*DOCKER, 'exec', '-i', '--user', '65532:65532',
        handle.container_id, 'python', '-c', _AGY_QUALITY_AGENT_SCRIPT,
        '/session', '65532', 'verify' if verify_only else 'create', AGY_AGENT_TEXT.encode().hex()),
        b'', timeout=30, max_output_bytes=65)


def quality_container_command(request: ProviderRequest, command: ProcessCommand, *,
                              workdir: str, catalog_path: str | None = None) -> QualityContainerCommand:
    """Bind one fixed guest adaptation; this constructor grants no execution authority."""
    from .eval_provider import quality_session_command
    from .native_boundary import _command_digest
    from .container_transfer_transport import _identity, _workdir
    if (type(request) is not ProviderRequest or request.execution_class != 'read-only'
            or type(request.profile) is not ExecutorProfile or type(command) is not ProcessCommand
            or command.max_output_bytes is None
            or command.environment or any(getattr(command, name) is not None for name in (
                'native_original', 'native_boundary', 'native_request', 'native_controller', 'native_verification'))):
        raise ProviderRequestError('quality container canonical command authority differs')
    if not request.resume and request.executor_id != 'claude' and request.session_id is not None:
        raise ProviderRequestError('quality container initial session differs')
    try:
        request.prompt_bytes.decode('utf-8')
    except UnicodeDecodeError as error:
        raise ProcessValidationError('quality container prompt must be UTF-8') from error
    pinned = ProviderRegistry.default().select(request.executor_id, 'read-only', request.profile.quality_tier)
    if request.profile.digest != pinned.digest:
        raise ProviderRequestError('quality container profile differs from its pins')
    if type(workdir) is not str or workdir != _workdir(_identity(request)):
        raise ProviderRequestError('quality container workdir differs from original imported identity')
    expected_catalog = workdir.removesuffix('/repo') + '/context/quality-catalog.json'
    if catalog_path != (expected_catalog if request.executor_id == 'codex' else None):
        raise ProviderRequestError('quality container catalog path differs')
    canonical = quality_session_command(request, catalog_path=catalog_path)
    if (command.argv != canonical.argv or command.stdin != canonical.stdin
            or command.cwd != canonical.cwd or command.timeout != canonical.timeout
            or (command.slot_cap, command.slot_timeout, command.slot_root) != (
                request.slot_cap, request.slot_timeout, request.slot_root)):
        raise ProviderRequestError('quality container canonical command grammar or controls differ')
    argv = command.argv
    if request.executor_id == 'codex':
        overrides = ('-c', 'model_provider="fanout"', '-c', _CODEX_LOOPBACK_PROVIDER,
            '-c', 'approval_policy="never"', '-c', 'approvals_reviewer="user"',
            '-c', 'allow_login_shell=false')
        if request.resume:
            if argv[-4:] != ('-c', 'sandbox_mode="read-only"', request.session_id, '-'):
                raise ProviderRequestError('quality container canonical resume sandbox differs')
            argv = (*argv[:3], *overrides, *argv[3:-4],
                    '-c', 'sandbox_mode="danger-full-access"', request.session_id, '-')
        else:
            if argv[-5:] != ('--sandbox', 'read-only', '-C', str(request.cwd), '-'):
                raise ProviderRequestError('quality container canonical initial sandbox differs')
            argv = (*argv[:2], *overrides, *argv[2:-5],
                    '--sandbox', 'danger-full-access', '-C', workdir, '-')
    elif request.executor_id == 'agy':
        argv = ('/usr/local/bin/agy', *argv[1:])
    guest = replace(command, argv=argv)
    return QualityContainerCommand(command, guest, workdir, catalog_path,
        admitted_request_digest(request), _command_digest(command), _command_digest(guest))


def _quality_cli_command(handle, provider):
    if type(handle) is not ContainerHandle or _SHA.fullmatch(handle.container_id) is None or provider not in _PROVIDERS:
        raise ProviderRequestError('quality guest CLI command differs')
    script = ("import hashlib,shutil,sys; path=shutil.which(sys.argv[1]); "
              "assert path; print(hashlib.sha256(open(path,'rb').read()).hexdigest())")
    return ProcessCommand((*DOCKER, 'exec', '-i', '--user', '65532:65532', handle.container_id,
        'python', '-c', script, provider), b'', timeout=30, max_output_bytes=65)


def _quality_container_registration(controller, store, authority, bindings, backend_attachment, *, row=None,
                                    role=None, phase=None, condition=None, epoch=None):
    from .diagnostic import reopen_quality_allocation, _quality_resolved_permit
    from .quality_case import _read_quality_backend_attachment
    from .container_certification import _read_owned_json
    allocation = reopen_quality_allocation(controller, store, authority, bindings=bindings)
    rows = [item for item in allocation['rows'] if item == row] if row is not None else [
        item for item in allocation['rows'] if (item['role'], item['phase'], item['condition'], item['epoch']) ==
        (role, phase, condition, epoch)]
    if len(rows) != 1:
        raise ProviderRequestError('quality container allocated row differs')
    row = rows[0]
    attachment = _read_quality_backend_attachment(controller, store, backend_attachment,
        authority=authority, bindings=bindings, row=row)
    claim = {'allocation': authority[1], 'row': row}
    digest = hashlib.sha256(canonical_json(claim)).hexdigest()
    actual, _ = _read_owned_json(controller.root / 'locks' / ('quality-row-' + digest))
    pointer, _ = _read_owned_json(controller.root / 'locks' / ('quality-registration-' + digest))
    if set(pointer) != {'authority'}:
        raise ProviderRequestError('quality container registration pointer differs')
    registration = read_evidence(controller, 'quality-permit-registration', *pointer['authority'])
    if (set(registration) != {'allocation', 'row', 'permit_id', 'epoch', 'permit', 'initial_receipt', 'backend_attachment'}
            or canonical_json(actual) != canonical_json(claim) or registration['allocation'] != authority[1]
            or registration['row'] != row or type(registration['epoch']) is not int
            or registration['epoch'] != row['epoch'] or registration['backend_attachment'] != list(backend_attachment)):
        raise ProviderRequestError('quality container consumed registration differs')
    permit = BrokerPermit(**registration['permit'])
    resolved = _quality_resolved_permit(controller, store, authority, allocation, row,
        registration['initial_receipt'], backend_attachment=backend_attachment)
    if asdict(resolved) != registration['permit']:
        raise ProviderRequestError('quality container resolved original permit differs')
    ledger = PermitLedger(controller.root / 'locks/spend', controller)
    with ledger._locked() as state:
        entry = state['permits'].get(registration['permit_id'])
        if (entry is None or entry['epoch'] != row['epoch']
                or entry['permit_digest'] != _permit_digest(permit)):
            raise ProviderRequestError('quality container original permit association differs')
    return registration, attachment


def _quality_container_request(request, registration, catalog):
    row, permit = registration['row'], BrokerPermit(**registration['permit'])
    if (type(request) is not ProviderRequest or type(request.profile) is not ExecutorProfile
            or request.execution_class != 'read-only' or request.retries != 0
            or any(getattr(request, key) is not None for key in ('native_boundary', 'native_controller',
                'native_verification', 'container_boundary', 'canary_capture')) or request.certificate_roots != ()
            or request.timeout != row['process_seconds'] or request.profile.digest != row['profile']
            or row['catalog'] != (None if catalog is None else catalog['projected']['digest'])
            or (permit.provider, permit.run_id, permit.task_id, permit.target_id, permit.seat_id,
                permit.inputs_digest, permit.session_id, permit.route, permit.model) !=
               (request.executor_id, request.run_id, request.task_id, request.target_id, request.seat_id,
                request.inputs_digest, request.session_id, 'resume' if request.resume else 'initial', request.profile.requested_model)):
        raise ProviderRequestError('quality container request or registered permit differs')


def _quality_container_controls(controller, allocation, attachment):
    controls = tuple(attachment['container']['docker_controls'])
    proof = read_evidence(controller, 'quality-docker-controls', *controls)
    limits = proof['limits']
    runner = quality_docker_runner(controller, allocation_digest=allocation[1],
        max_commands=limits['max_commands'], command_seconds=limits['command_seconds'],
        outer_seconds=limits['outer_seconds'], original_controls=controls)
    return runner


def _quality_container_scope_request(controller, context_ref, request, registration, backend_attachment):
    context = read_evidence(controller, 'container-coordinator-context', *context_ref)
    if (context['schema_version'] != 'fanout-quality-container-context-v1'
            or context['request_digest'] != admitted_request_digest(request)
            or context['backend_attachment'] != list(backend_attachment)
            or context['row'] != registration['row'] or context['allocation'][1] != registration['allocation']
            or read_evidence(controller, 'quality-permit-registration', *context['registration']) != registration):
        raise ProviderRequestError('quality container actual lifecycle prompt or row differs')
    return context


def _quality_cli_terminal(controller, pointer, controls, handle, provider, digest):
    terminal = read_evidence(controller, 'quality-docker-terminal', *pointer)
    attempt = read_evidence(controller, 'quality-docker-attempt', *terminal['attempt_authority'])
    if (terminal['controls'] != list(controls) or attempt['controls'] != list(controls)
            or attempt['attempt'] != terminal['attempt']
            or attempt['argv'] != list(_quality_cli_command(handle, provider).argv)
            or attempt['stdin_sha256'] != hashlib.sha256(b'').hexdigest()
            or terminal['status'] != 'exit' or terminal['returncode'] != 0
            or terminal['capture_complete'] is not True):
        raise ProviderRequestError('quality original guest CLI observation differs')
    proof = read_evidence(controller, 'quality-docker-controls', *controls)
    with _quality_docker_lock(controller, proof['limits']['allocation'], identity=proof['marker_identity']) as (_, fd):
        if _quality_docker_consumed(controller, fd, proof, controls) < terminal['attempt']:
            raise ProviderRequestError('quality guest CLI original attempt is missing')
    with ArtifactStore.open_existing(controller_directory(controller, 'quarantine') /
            ('quality-docker-' + proof['limits']['allocation'])) as retained:
        stdout, stderr = (retained.read_bytes(ArtifactRef(**terminal[key])) for key in ('stdout', 'stderr'))
    if stdout != (digest + '\n').encode('ascii') or stderr:
        raise ProviderRequestError('quality actual guest CLI bytes differ')


def _quality_container_material(controller, store, request, original, handoff, attachment, catalog):
    from .container_transfer_transport import reopen_quality_handoff_history, _completed_input_receipt, _material, _root_info
    from .container_transfer import snapshot_manifest
    from .diagnostic import _reopen_quality_catalog
    history = reopen_quality_handoff_history(controller, original, handoff)
    receipt = _completed_input_receipt(controller, request,
        private_volume_names(request.run_id, request.task_id, request.target_id, request.seat_id)[0])
    grant = read_evidence(controller, 'container-input-grant', receipt.grant_evidence_name, receipt.grant_evidence_digest)
    if grant['quality_provisioning'].get('backend_attachment') != attachment['pointer']:
        raise ProviderRequestError('quality container imported backend differs')
    source = Path(grant['source_root'])
    info = _root_info(controller, source)
    manifest, _ = _material(controller, receipt.material_evidence_name, receipt.material_evidence_digest,
        receipt.identity, receipt.original_handle, info)
    if snapshot_manifest(source, receipt.identity) != manifest:
        raise ProviderRequestError('quality container original imported material differs')
    expected = {'context/quality-attachment.json': canonical_json(attachment['value'])}
    if request.executor_id == 'codex':
        expected['context/quality-catalog.json'] = _reopen_quality_catalog(store, catalog)
    elif catalog is not None:
        raise ProviderRequestError('quality container unexpected catalog')
    entries = {entry.path: entry for entry in manifest.entries}
    for path, raw in expected.items():
        entry = entries.get(path)
        if (entry is None or entry.kind != 'file' or entry.length != len(raw)
                or entry.sha256 != hashlib.sha256(raw).hexdigest()):
            raise ProviderRequestError('quality container imported attachment or catalog differs')
    return history, receipt


def issue_quality_container_command(controller, store, authority, *, request, admission, backend_attachment,
                                    handoff, original_request, bindings, role, phase, condition,
                                    catalog=None, predecessor=None):
    from .diagnostic import _retain_quality_request, _quality_loaded_sources
    from .eval_provider import quality_session_command
    registration, attachment = _quality_container_registration(controller, store, authority, bindings,
        backend_attachment, role=role, phase=phase, condition=condition, epoch=admission.epoch)
    _quality_container_request(request, registration, catalog)
    if type(admission) is not ContainerAdmission or type(admission.transport) is not PinnedDockerTransport:
        raise ProviderRequestError('quality container admission differs')
    transport = admission.transport
    if (transport.controller is not controller or transport.spec.workspace_readonly is not True
            or admission.ledger.controller is not controller or admission.permit_id != registration['permit_id']
            or asdict(admission.broker_permit) != registration['permit']
            or admission.image_digest != attachment['container']['image_digest']
            or admission.cli_digest != attachment['container']['cli_digests'][request.executor_id]
            or admission.session_store.identity != (request.run_id, request.task_id, request.target_id, request.seat_id)):
        raise ProviderRequestError('quality container actual admission or backend pins differ')
    admission.ledger.require_permit(admission.permit_id, admission.epoch, admission.broker_permit)
    scope = transport._scope()
    if scope is None:
        raise ProviderRequestError('quality container requires its actual lifecycle scope')
    context = scope.active[transport.handle.container_id]['member']['context']
    _quality_container_scope_request(controller, context, request, registration, backend_attachment)
    prefix = f"quality/{role}/{phase}/{admission.epoch}"
    original_ref = _retain_quality_request(store, original_request, prefix + '/original')
    history, receipt = _quality_container_material(controller, store, request, original_request, handoff,
        {'pointer': list(backend_attachment), 'value': attachment}, catalog)
    if admission.input_receipt != receipt:
        raise ProviderRequestError('quality container original receipt differs')
    vm, handle, socket = transport.attest_vm(), transport.attest_container(), transport.attest_socket()
    if (handle != transport.handle or socket != admission.socket_attestation
            or transport.attest_image() != admission.image_digest or transport.attest_cli(request.executor_id) != admission.cli_digest):
        raise ProviderRequestError('quality container live physical pins differ')
    control = getattr(transport, '_control', None)
    if (type(control) is not PinnedSSHSeatControl or control.controller is not controller
            or control.spec != transport.spec or control.ledger is not admission.ledger
            or control.permit_id != admission.permit_id or control.epoch != admission.epoch
            or control.broker.permit != admission.broker_permit or control._relay_error is not None):
        raise ProviderRequestError('quality container actual relay is absent')
    control._lease.require_held()
    runner = _quality_container_controls(controller, authority, attachment)
    cli = runner(_quality_cli_command(handle, request.executor_id).argv, b'')
    _quality_cli_terminal(controller, cli.quality_terminal, runner.controls, handle, request.executor_id, admission.cli_digest)
    workdir = receipt.workdir
    catalog_path = workdir.removesuffix('/repo') + '/context/quality-catalog.json' if request.executor_id == 'codex' else None
    canonical = replace(quality_session_command(request, catalog_path=catalog_path), max_output_bytes=8 * 1024 * 1024)
    command = quality_container_command(request, canonical, workdir=workdir, catalog_path=catalog_path)
    vm_ref = store.write_json(prefix + '/vm.json', control.vm_observation)
    runtime = store.write_json(prefix + '/runtime.json', transport.runtime_observation)
    runtime = transport.runtime_proof
    proof = write_evidence(controller, 'quality-container-command', {
        'schema': 'fanout-quality-container-command-v1', 'allocation': list(authority), 'store': str(store.root),
        'bindings': bindings, 'registration': registration, 'backend_attachment': list(backend_attachment),
        'catalog': catalog, 'loaded': _quality_loaded_sources(), 'request_digest': admitted_request_digest(request),
        'original_request': asdict(original_ref), 'handoff': list(handoff),
        'predecessor': None if predecessor is None else list(predecessor.quality_proof),
        'launch': [transport.handle_evidence_name, transport.handle_evidence_digest],
        'spec': asdict(transport.spec), 'handle': asdict(handle), 'socket': asdict(socket),
        'vm': asdict(vm_ref), 'runtime': asdict(runtime), 'cli_terminal': list(cli.quality_terminal),
        'canonical_digest': command.canonical_digest, 'guest_digest': command.guest_digest,
        'workdir': workdir, 'catalog_path': catalog_path, 'context': context,
        'lifecycle_snapshot': list(transport.lifecycle_snapshot)})
    reopen_quality_container_command(controller, store, request, proof, backend_attachment=backend_attachment)
    return proof


def reopen_quality_container_command(controller, store, request, proof, *, backend_attachment):
    from .diagnostic import _require_quality_loaded_sources, _reopen_quality_request
    from .container_certification import _artifact_json, _reopen_vm
    from .eval_provider import quality_session_command
    try:
        value = read_evidence(controller, 'quality-container-command', *proof)
        if (set(value) != {'schema', 'allocation', 'store', 'bindings', 'registration', 'backend_attachment', 'catalog',
                'loaded', 'request_digest', 'original_request', 'handoff', 'predecessor', 'launch', 'spec', 'handle',
                'socket', 'vm', 'runtime', 'cli_terminal', 'canonical_digest', 'guest_digest', 'workdir', 'catalog_path',
                'context', 'lifecycle_snapshot'} or value['schema'] != 'fanout-quality-container-command-v1'
                or value['store'] != str(store.root) or value['backend_attachment'] != list(backend_attachment)
                or value['request_digest'] != admitted_request_digest(request) or runtime_source_digest() != value['bindings']['source']):
            raise ValueError('exact command source/request/backend')
        _require_quality_loaded_sources(value['loaded'])
        registration, attachment = _quality_container_registration(controller, store, tuple(value['allocation']),
            value['bindings'], backend_attachment, row=value['registration']['row'])
        if registration != value['registration']:
            raise ValueError('original registration')
        _quality_container_scope_request(controller, value['context'], request, registration, backend_attachment)
        _quality_container_request(request, registration, value['catalog'])
        original = _reopen_quality_request(store, ArtifactRef(**value['original_request']), request)
        history, receipt = _quality_container_material(controller, store, request, original, value['handoff'],
            {'pointer': list(backend_attachment), 'value': attachment}, value['catalog'])
        spec, handle, socket = ContainerSpec(**value['spec']), ContainerHandle(**value['handle']), GuestSocketAttestation(**value['socket'])
        ContainerLauncher._validate_spec(spec)
        if (spec.workspace_readonly is not True or spec.image_digest != attachment['container']['image_digest']
                or spec.cli_digest != attachment['container']['cli_digests'][request.executor_id]
                or (spec.run_id, spec.task_id, spec.target_id, spec.seat_id) !=
                   (request.run_id, request.task_id, request.target_id, request.seat_id)
                or socket.path != spec.socket_path or socket.uid != 65532 or not stat.S_ISSOCK(socket.mode)
                or stat.S_IMODE(socket.mode) != 0o600 or any(type(item) is not int or item <= 0
                    for item in (socket.device, socket.inode, socket.ctime_ns))):
            raise ValueError('actual read-only physical policy')
        launch = read_evidence(controller, 'container-seat-launch', *value['launch'])
        authenticate_launch_record(controller, launch, spec, handle, inputs_digest=request.inputs_digest)
        completed = read_evidence(controller, 'quality-container-input-handoff', *value['handoff'])
        if not request.resume and value['launch'] != completed['model_launch']:
            raise ValueError('original RO handoff launch')
        if not request.resume:
            if value['predecessor'] is not None or registration['initial_receipt'] is not None:
                raise ValueError('initial predecessor')
        else:
            if (type(value['predecessor']) is not list or value['predecessor'] != registration['initial_receipt']
                    or launch['route'] != 'recover' or launch['original_input'] != [receipt.evidence_name, receipt.evidence_digest]):
                raise ValueError('original container predecessor')
            previous = validate_quality_container_readonly_turn(controller, store, original,
                value['predecessor'], backend_attachment=backend_attachment)
            previous_command = read_evidence(controller, 'quality-container-command', *previous['command'])
            from .diagnostic import quality_row_identity
            if (previous_command['registration']['row']['initial'] is not None
                    or quality_row_identity(previous_command['registration']['row']) != registration['row']['initial']
                    or previous_command['handoff'] != value['handoff'] or previous_command['catalog'] != value['catalog']
                    or previous['backend_attachment'] != value['backend_attachment']):
                raise ValueError('same original allocation, handoff and catalog')
        vm = _reopen_vm(_artifact_json(store, value['vm']))
        from .container_coordinator import _replay_lifecycle_runtime
        _replay_lifecycle_runtime(controller, value['lifecycle_snapshot'], value['runtime'],
            spec=spec, handle=handle, context_ref=value['context'], socket=socket)
        _quality_cli_terminal(controller, value['cli_terminal'], attachment['container']['docker_controls'],
            handle, request.executor_id, spec.cli_digest)
        canonical = replace(quality_session_command(request, catalog_path=value['catalog_path']), max_output_bytes=8 * 1024 * 1024)
        command = quality_container_command(request, canonical, workdir=value['workdir'], catalog_path=value['catalog_path'])
        if (command.canonical_digest != value['canonical_digest'] or command.guest_digest != value['guest_digest']
                or command.workdir != receipt.workdir):
            raise ValueError('reconstructed original guest grammar')
        return command, registration
    except Exception as error:
        raise ProviderRequestError('quality container command association differs') from error


@dataclass(frozen=True, slots=True)
class QualityContainerTurn:
    result: ProviderResult
    process: ProcessResult
    command: tuple[str, str]
    terminal: tuple[str, str]


def _quality_container_live(controller, store, request, proof, backend_attachment, admission):
    command, registration = reopen_quality_container_command(controller, store, request, proof,
        backend_attachment=backend_attachment)
    transport = admission.transport
    value = read_evidence(controller, 'quality-container-command', *proof)
    if (type(transport) is not PinnedDockerTransport or transport.controller is not controller
            or admission.ledger.controller is not controller or admission.permit_id != registration['permit_id']
            or admission.epoch != registration['epoch'] or asdict(admission.broker_permit) != registration['permit']
            or value['launch'] != [transport.handle_evidence_name, transport.handle_evidence_digest]
            or asdict(transport.spec) != value['spec'] or asdict(transport.handle) != value['handle']
            or transport.spec.workspace_readonly is not True or transport._scope() is None):
        raise ProviderRequestError('quality container live admission differs')
    control = getattr(transport, '_control', None)
    if (type(control) is not PinnedSSHSeatControl or control.controller is not controller
            or control.spec != transport.spec or control.ledger is not admission.ledger
            or control.permit_id != admission.permit_id or control.epoch != admission.epoch
            or control.broker.permit != admission.broker_permit or control._relay_error is not None):
        raise ProviderRequestError('quality container live relay association differs')
    control._lease.require_held()
    admission.ledger.require_permit(admission.permit_id, admission.epoch, admission.broker_permit)
    transport._require_fixed_runner()
    vm = transport.attest_vm()
    from .container_certification import _artifact_json, _reopen_vm
    original_vm = _reopen_vm(_artifact_json(store, value['vm']))
    if (any(getattr(vm, key) != getattr(original_vm, key) for key in ('profile', 'docker_endpoint',
                'ssh_host_key_sha256', 'effective_mounts_sha256', 'guest_mounts_sha256'))
            or transport.attest_container() != transport.handle or asdict(transport.attest_socket()) != value['socket']
            or transport.attest_image() != transport.spec.image_digest
            or transport.attest_cli(request.executor_id) != transport.spec.cli_digest):
        raise ProviderRequestError('quality container live physical policy changed')
    attachment = read_evidence(controller, 'quality-installed-case-attachment', *backend_attachment)
    _quality_container_controls(controller, tuple(value['allocation']), attachment)
    return command, control


def reopen_quality_container_transport(request, *, controller, store, authority, ledger, permit,
                                       permit_id, epoch, session_store, broker, backend_attachment,
                                       handoff, original_request, predecessor, bindings,
                                       lifecycle_scope, context_ref):
    """Recover the registered own-session RO row; historical evidence grants no new row."""
    from .diagnostic import QualityContainerPredecessor, quality_row_identity
    from .container_coordinator import _CoordinatorLifecycle
    from .container_transfer_transport import _completed_input_receipt, reopen_stopped_seat
    if (type(ledger) is not PermitLedger or ledger.controller is not controller
            or ledger.root != controller.root / 'locks/spend' or type(permit) is not BrokerPermit
            or type(broker) is not Broker or broker.permit is not permit
            or type(lifecycle_scope) is not _CoordinatorLifecycle or lifecycle_scope.controller is not controller
            or type(session_store) is not SessionStore):
        raise ProviderRequestError('quality container recovery owner differs')
    context = read_evidence(controller, 'container-coordinator-context', *context_ref)
    registration, attachment = _quality_container_registration(controller, store, authority, bindings,
        backend_attachment, row=context['row'])
    if (registration['permit_id'] != permit_id or registration['epoch'] != epoch
            or registration['permit'] != asdict(permit)):
        raise ProviderRequestError('quality container recovery registration differs')
    control = transport = None
    try:
        if (not request.resume or type(predecessor) is not QualityContainerPredecessor
                or predecessor.backend_attachment != backend_attachment
                or predecessor.session_id != request.session_id
                or predecessor.row_digest != registration['row']['initial']
                or list(predecessor.quality_proof) != registration['initial_receipt']):
            raise ProviderRequestError('quality container recovery original predecessor differs')
        previous = validate_quality_container_readonly_turn(controller, store, original_request,
            predecessor.quality_proof, backend_attachment=backend_attachment)
        prior = read_evidence(controller, 'quality-container-command', *previous['command'])
        _quality_container_request(request, registration, prior['catalog'])
        if (quality_row_identity(prior['registration']['row']) != predecessor.row_digest
                or prior['handoff'] != list(handoff) or prior['registration']['row']['initial'] is not None):
            raise ProviderRequestError('quality container recovery original row differs')
        spec = ContainerSpec(**prior['spec'])
        _quality_container_scope_request(controller, context_ref, request, registration, backend_attachment)
        binding = session_store.resume(request.executor_id)
        if (session_store.controller is not controller
                or session_store.identity != (request.run_id, request.task_id, request.target_id, request.seat_id)
                or session_store.volume != spec.session_volume or binding.session_id != request.session_id
                or binding.upstream_route_digest != permit.upstream_route_digest):
            raise ProviderRequestError('quality container recovery settled own session differs')
        terminal = read_evidence(controller, 'quality-container-terminal', *previous['terminal'])
        handle = ContainerHandle(**prior['handle'])
        reopen_stopped_seat(controller, terminal['stop'], spec=spec, handle=handle)
        receipt = _completed_input_receipt(controller, request, spec.workspace_volume)
        _quality_container_material(controller, store, request, original_request, handoff,
            {'pointer': list(backend_attachment), 'value': attachment}, prior['catalog'])
        ledger.require_permit(permit_id, epoch, permit)
        if ledger.attempt_counts(permit_id, epoch) != (0, 0) or ledger.request_records(permit_id, epoch):
            raise ProviderRequestError('quality container recovery row was already attempted')
        runner = _quality_container_controls(controller, authority, attachment)
        with lifecycle_scope.guard:
            lifecycle_scope.authorize()
            if lifecycle_scope.pending.get((permit_id, epoch)) != (spec, tuple(context_ref), permit, ledger):
                raise ProviderRequestError('quality container recovery pending scope differs')
            control = PinnedSSHSeatControl(controller, spec, ledger, broker, permit_id, epoch)
            control.open()
            control.attest_vm()
            launcher = ContainerLauncher(runner, guest_socket_attestor=control.attest_socket)
            launch = read_evidence(controller, _HANDLE_CATEGORY, *prior['launch'])
            authenticate_launch_record(controller, launch, spec, handle, inputs_digest=request.inputs_digest)
            launcher.restore_launch(launch, spec, handle, recover=launch['route'] == 'recover')
            inventory = lifecycle_scope.prepare(launcher, spec, context_ref, control)
            transport = PinnedDockerTransport._start_guarded(controller, spec, launcher=launcher,
                vm_attestor=control.attest_vm, socket_attestor=control.attest_socket,
                image_attestor=lambda: PinnedDockerTransport._attest_image(launcher, spec),
                cli_attestor=lambda provider: PinnedDockerTransport._attest_linux_cli(spec, provider, transport.handle),
                process_runner=lambda command: PinnedDockerTransport._docker_exec(transport.handle, command, receipt.workdir),
                recover=True, scope=inventory, lifecycle_scope=lifecycle_scope, context_ref=context_ref,
                session_control=control, original_input=[receipt.evidence_name, receipt.evidence_digest])
            transport._production_edge = True
            transport._model_workdir = receipt.workdir
            transport.handoff_evidence = handoff
            return ContainerAdmission(permit, permit_id, epoch, ledger, spec.image_digest, spec.cli_digest,
                transport.attest_socket(), transport, session_store, transport.handle_evidence_name,
                transport.handle_evidence_digest, receipt)
    except BaseException as primary:
        if control is not None:
            try:
                if transport is not None:
                    transport.close()
                else:
                    with lifecycle_scope.guard:
                        if not lifecycle_scope.blocked:
                            lifecycle_scope.fail(ContainerSpec(**prior['spec']), None, control, primary, phase='quality_recovery')
                    control.close()
            except BaseException as cleanup:
                primary.add_note(f"Quality recovery cleanup also failed: {type(cleanup).__name__}")
        try:
            ledger.fence_epoch(permit_id, epoch)
        except BaseException as cleanup:
            primary.add_note(f"Quality recovery permit fence also failed: {type(cleanup).__name__}")
        raise


def run_quality_container_readonly_turn(request, *, controller, store, authority, admission,
                                       backend_attachment, handoff, original_request, bindings,
                                       role, phase, condition, route, credential, catalog=None, predecessor=None):
    from .diagnostic import (_retain_quality_request, _quality_request_material, quality_upstream_sender,
        QualityContainerPredecessor)
    from .container_upstream import HostUpstream
    from scripts.fanout_container_certify import _capture_private_wire
    if (type(admission) is not ContainerAdmission or type(admission.ledger) is not PermitLedger
            or admission.ledger.controller is not controller or admission.ledger.root != controller.root / 'locks/spend'):
        raise ProviderRequestError('quality container original fencing authority differs')
    registration, attachment = _quality_container_registration(controller, store, authority, bindings,
        backend_attachment, role=role, phase=phase, condition=condition, epoch=admission.epoch)
    if admission.permit_id != registration['permit_id'] or asdict(admission.broker_permit) != registration['permit']:
        raise ProviderRequestError('quality container fencing permit differs')
    transport = admission.transport
    primary = cleanup = None
    started = time.monotonic()
    prefix = f"quality/{role}/{phase}/{admission.epoch}"
    wires = {}
    command_proof = process = result = request_ref = None
    cleanup_authorized = False
    try:
        _quality_container_request(request, registration, catalog)
        _quality_request_material(request, prefix + '/current')
        if (request.resume != (predecessor is not None) or predecessor is not None and (
                type(predecessor) is not QualityContainerPredecessor
                or predecessor.backend_attachment != backend_attachment or predecessor.session_id != request.session_id)):
            raise ProviderRequestError('quality container predecessor type or backend differs')
        if predecessor is not None:
            previous = validate_quality_container_readonly_turn(controller, store, original_request,
                predecessor.quality_proof, backend_attachment=backend_attachment)
            previous_command = read_evidence(controller, 'quality-container-command', *previous['command'])
            from .diagnostic import quality_row_identity
            if (previous_command['handoff'] != list(handoff)
                    or predecessor.row_digest != registration['row']['initial']
                    or predecessor.row_digest != quality_row_identity(previous_command['registration']['row'])
                    or list(predecessor.quality_proof) != registration['initial_receipt']):
                raise ProviderRequestError('quality container original handoff differs')
        else:
            reopen_quality_handoff(controller, original_request, handoff,
                ledger=admission.ledger, sessions=admission.session_store)
        if (type(transport) is not PinnedDockerTransport or transport.controller is not controller
                or transport.spec.workspace_readonly is not True
                or [transport.handle_evidence_name, transport.handle_evidence_digest] !=
                   [admission.handle_evidence_name, admission.handle_evidence_digest]):
            raise ProviderRequestError('quality container retained transport differs')
        launch = read_evidence(controller, _HANDLE_CATEGORY, transport.handle_evidence_name, transport.handle_evidence_digest)
        authenticate_launch_record(controller, launch, transport.spec, transport.handle, inputs_digest=request.inputs_digest)
        cleanup_authorized = True
        command_proof = issue_quality_container_command(controller, store, authority, request=request,
            admission=admission, backend_attachment=backend_attachment, handoff=handoff,
            original_request=original_request, bindings=bindings, role=role, phase=phase,
            condition=condition, catalog=catalog, predecessor=predecessor)
        request_ref = _retain_quality_request(store, request, prefix + '/current')
        def success(payload):
            captured = _capture_private_wire(admission.ledger, admission.permit_id, admission.epoch,
                store.root, f'quality-{role}-{phase}-{admission.epoch}', payload)
            wires[captured['request_id']] = captured
        sender = quality_upstream_sender(HostUpstream(admission.broker_permit, route,
            max_response_bytes=admission.broker_permit.max_response_bytes,
            deadline_seconds=admission.broker_permit.response_deadline, success_sink=success),
            store, f'{role}-{phase}-{admission.epoch}')
        command, control = _quality_container_live(controller, store, request, command_proof, backend_attachment, admission)
        with control.broker._lock:
            if (control.broker._credential != credential or control.broker._used != 0
                    or control.broker._effective_limits or control.broker._refused_agy_auxiliary):
                raise ProviderRequestError('quality container original broker was already used or differs')
            if admission.ledger.attempt_counts(admission.permit_id, admission.epoch) != (0, 0):
                raise ProviderRequestError('quality container model permit already attempted')
            control.broker._send = sender
        turn_digest = _digest({'command': list(command_proof), 'request': admitted_request_digest(request)})
        admission.session_store.reserve(request.executor_id, turn_digest, initial=not request.resume,
            upstream_route_digest=admission.broker_permit.upstream_route_digest,
            initial_session_id=request.session_id if request.executor_id == 'claude' and not request.resume else None)
        if request.executor_id == 'agy':
            runner = _quality_container_controls(controller, authority, attachment)
            from .eval_provider import AGY_AGENT_TEXT
            for verify in (False, True):
                observed = runner(quality_agy_agent_command(transport.handle, verify_only=verify).argv, b'')
                if observed.returncode != 0 or observed.stdout != hashlib.sha256(AGY_AGENT_TEXT.encode()).hexdigest().encode() + b'\n' or observed.stderr:
                    raise ProviderRequestError('quality container actual empty-tools agent differs')
        def final_admission():
            _quality_container_live(controller, store, request, command_proof, backend_attachment, admission)
        transport.process_result = None
        if transport._production_edge:
            process = PinnedDockerTransport._docker_exec(transport.handle, command.guest, command.workdir, before_spawn=final_admission)
        else:
            final_admission()
            process = transport._process_runner(command.guest)
        transport.process_result = process
        if type(process) is not ProcessResult or process.capture_complete is not True:
            raise ProviderRequestError('quality container raw process capture differs')
        adapter = ProviderRegistry((request.profile,)).require(request.executor_id)
        parsed = adapter.parse(process.stdout)
        result = _make_result(request, adapter, process, parsed, 1, time.monotonic() - started)
        if not result.valid or process.status is not ProcessStatus.EXIT or process.returncode != 0:
            raise ProviderRequestError('quality container process did not deliver a valid own-session answer')
        if control._relay_error is not None:
            raise ProviderRequestError('quality container relay did not settle') from control._relay_error
    except BaseException as error:
        primary = error
    finally:
        if cleanup_authorized:
            try:
                transport.close()
            except BaseException as error:
                cleanup = error
                (primary or error).add_note(_cleanup_failure_note(controller, 'quality-turn', error,
                    primary=primary, spec=transport.spec, handle=transport.handle,
                    permit_id=admission.permit_id, provider=request.executor_id, store=admission.session_store))
        try:
            admission.ledger.fence_epoch(admission.permit_id, admission.epoch)
        except BaseException as error:
            if primary is not None or cleanup is not None:
                (primary or cleanup).add_note(f"Quality turn permit fence also failed: {type(error).__name__}")
            else:
                cleanup = error
    if primary is not None:
        raise primary.with_traceback(primary.__traceback__)
    if cleanup is not None:
        raise cleanup
    from .container_transfer_transport import reopen_stopped_seat
    reopen_stopped_seat(controller, transport.stop_evidence, spec=transport.spec, handle=transport.handle)
    admission.session_store.settle(request.executor_id, result.session_id, turn_digest,
        initial=not request.resume, upstream_route_digest=admission.broker_permit.upstream_route_digest)
    stdout, stderr = (store.write_bytes(prefix + '/' + key + '.bin', raw) for key, raw in
        (('stdout', process.stdout), ('stderr', process.stderr)))
    final = store.write_bytes(prefix + '/final.txt', result.answer.encode())
    terminal = write_evidence(controller, 'quality-container-terminal', {
        'schema': 'fanout-quality-container-terminal-v1', 'command': list(command_proof),
        'request_digest': admitted_request_digest(request), 'process_status': process.status.value,
        'returncode': process.returncode, 'capture_complete': process.capture_complete,
        'stdout_sha256': stdout.digest, 'stderr_sha256': stderr.digest, 'answer_sha256': final.digest,
        'valid': result.valid, 'session_id': result.session_id, 'observed_model': result.observed_model,
        'turn_digest': turn_digest, 'stop': list(transport.stop_evidence),
        'session_identity': list(admission.session_store.identity), 'session_volume': admission.session_store.volume})
    total, auxiliary = admission.ledger.attempt_counts(admission.permit_id, admission.epoch)
    proof = write_evidence(controller, 'quality-readonly-turn', {
        'schema': 'fanout-quality-container-readonly-turn-v1', 'backend_attachment': list(backend_attachment),
        'command': list(command_proof), 'terminal': list(terminal), 'request': asdict(request_ref),
        'stdout': asdict(stdout), 'stderr': asdict(stderr), 'final': asdict(final),
        'records': list(admission.ledger.request_records(admission.permit_id, admission.epoch)),
        'prepared': [asdict(ref) for ref in sender.captures.values()], 'wires': wires, 'route': asdict(route),
        'main_attempts': total, 'auxiliary_attempts': auxiliary, 'elapsed': time.monotonic() - started,
        'outer_seconds': registration['row']['outer_seconds']})
    validate_quality_container_readonly_turn(controller, store, request, proof, backend_attachment=backend_attachment)
    return QualityContainerTurn(result, process, command_proof, terminal), proof


def validate_quality_container_readonly_turn(controller, store, request, proof, *, backend_attachment):
    from .diagnostic import _quality_request_material, _read_quality_request_json, _read_quality_request_artifact, _quality_empty_tools
    from .container_certification import reopen_quality_wire
    from .container_routes import RouteAdmission, RouteProfile
    from .container_transfer_transport import reopen_stopped_seat
    value = read_evidence(controller, 'quality-readonly-turn', *proof)
    if (set(value) != {'schema', 'backend_attachment', 'command', 'terminal', 'request', 'stdout', 'stderr', 'final',
            'records', 'prepared', 'wires', 'route', 'main_attempts', 'auxiliary_attempts', 'elapsed', 'outer_seconds'}
            or value['schema'] != 'fanout-quality-container-readonly-turn-v1'
            or value['backend_attachment'] != list(backend_attachment)):
        raise ProviderRequestError('quality container readonly receipt differs')
    command, registration = reopen_quality_container_command(controller, store, request, value['command'], backend_attachment=backend_attachment)
    command_value = read_evidence(controller, 'quality-container-command', *value['command'])
    row, permit = registration['row'], BrokerPermit(**registration['permit'])
    request_ref = ArtifactRef(**value['request'])
    retained = _read_quality_request_json(store, request_ref)
    material, prompt, profile = _quality_request_material(request, request_ref.path.removesuffix('/request.json'))
    if (retained != material or _read_quality_request_artifact(store, ArtifactRef(**retained['prompt']), 1024 * 1024) != prompt
            or _read_quality_request_artifact(store, ArtifactRef(**retained['profile']), 64 * 1024) != profile):
        raise ProviderRequestError('quality container retained actual request differs')
    ledger = PermitLedger(controller.root / 'locks/spend', controller)
    records, attempts = ledger.request_records(registration['permit_id'], row['epoch']), ledger.attempt_counts(registration['permit_id'], row['epoch'])
    with ledger._locked() as state:
        entry = state['permits'].get(registration['permit_id'])
        if entry is None or entry['fenced'] is not True:
            raise ProviderRequestError('quality container original permit is not fenced')
    terminal = read_evidence(controller, 'quality-container-terminal', *value['terminal'])
    if (set(terminal) != {'schema', 'command', 'request_digest', 'process_status', 'returncode', 'capture_complete',
            'stdout_sha256', 'stderr_sha256', 'answer_sha256', 'valid', 'session_id', 'observed_model', 'turn_digest',
            'stop', 'session_identity', 'session_volume'} or terminal['schema'] != 'fanout-quality-container-terminal-v1'
            or terminal['command'] != value['command'] or terminal['request_digest'] != admitted_request_digest(request)
            or terminal['process_status'] != 'exit' or type(terminal['returncode']) is not int or terminal['returncode'] != 0
            or terminal['valid'] is not True or terminal['capture_complete'] is not True
            or value['records'] != list(records) or attempts != (value['main_attempts'], value['auxiliary_attempts'])
            or type(value['main_attempts']) is not int or type(value['auxiliary_attempts']) is not int
            or attempts[0] > attempt_limit(permit) or len(records) > permit.max_requests or not records
            or {item['request_id'] for item in records} != set(value['wires'])
            or any(item['state'] != 'settled' for item in records) or value['outer_seconds'] != row['outer_seconds']
            or type(value['elapsed']) not in (int, float) or not math.isfinite(value['elapsed'])
            or not 0 < value['elapsed'] <= row['outer_seconds']):
        raise ProviderRequestError('quality container actual process or finite accounting differs')
    stdout, stderr, final = (store.read_bytes(ArtifactRef(**value[key])) for key in ('stdout', 'stderr', 'final'))
    if any(hashlib.sha256(raw).hexdigest() != terminal[key] for raw, key in
            ((stdout, 'stdout_sha256'), (stderr, 'stderr_sha256'), (final, 'answer_sha256'))):
        raise ProviderRequestError('quality container raw process bytes differ')
    adapter = ProviderRegistry((request.profile,)).require(request.executor_id)
    parsed = adapter.parse(stdout)
    result = _make_result(replace(request, artifact_store=None), adapter,
        ProcessResult(ProcessStatus.EXIT, 0, stdout, stderr), parsed, 1, value['elapsed'])
    if (not result.valid or not final or result.answer.encode() != final or result.session_id != terminal['session_id']
            or result.observed_model != terminal['observed_model']):
        raise ProviderRequestError('quality container raw own-session answer differs')
    spec, handle = ContainerSpec(**command_value['spec']), ContainerHandle(**command_value['handle'])
    reopen_stopped_seat(controller, terminal['stop'], spec=spec, handle=handle)
    stopped = read_evidence(controller, 'container-seat-stopped', *terminal['stop'])
    if [stopped['launch_evidence_name'], stopped['launch_evidence_digest']] != command_value['launch']:
        raise ProviderRequestError('quality container exact model stop differs')
    sessions = SessionStore(controller, request.run_id, request.task_id, request.target_id, request.seat_id, spec.session_volume)
    binding = sessions.read(request.executor_id)
    if binding is None:
        raise ProviderRequestError('quality container original own session is absent')
    expected_turn = _digest({'command': value['command'], 'request': admitted_request_digest(request)})
    def settled(state):
        turn = state['turns'].get(expected_turn)
        if turn != {'provider': request.executor_id, 'state': 'settled'}:
            raise ProviderRequestError('quality container actual session turn is not settled')
        return None, False
    sessions._update(settled)
    if (binding.session_id != result.session_id or binding.upstream_route_digest != permit.upstream_route_digest
            or terminal['turn_digest'] != expected_turn or terminal['session_identity'] != list(sessions.identity)
            or terminal['session_volume'] != sessions.volume or not request.resume and binding.turn_digest != expected_turn):
        raise ProviderRequestError('quality container settled own session differs')
    prepared_set = set()
    if type(value['prepared']) is not list or not 1 <= len(value['prepared']) <= len(records):
        raise ProviderRequestError('quality container prepared request set differs')
    for retained in value['prepared']:
        prepared = store.read_json(ArtifactRef(**retained))
        if set(prepared) != {'method', 'provider_path', 'route', 'origin', 'path', 'model', 'normalized_body', 'effective_body'}:
            raise ProviderRequestError('quality container prepared request fields differ')
        for key in ('normalized_body', 'effective_body'):
            _quality_empty_tools(store.read_bytes(ArtifactRef(**prepared[key])))
        observed = (prepared['normalized_body']['digest'], prepared['effective_body']['digest'])
        matches = [record['identity'] for record in records if
            (record['identity']['body_sha256'], record['identity']['upstream_body_sha256']) == observed]
        if (not matches or observed in prepared_set or any(
                (prepared['method'], prepared['provider_path'], prepared['route'], prepared['origin'], prepared['path'], prepared['model']) !=
                (item['method'], item['path'], item['upstream_route_digest'], item['upstream_origin'], item['upstream_path'], item['upstream_model'])
                for item in matches)):
            raise ProviderRequestError('quality container prepared request association differs')
        prepared_set.add(observed)
    if prepared_set != {(record['identity']['body_sha256'], record['identity']['upstream_body_sha256']) for record in records}:
        raise ProviderRequestError('quality container prepared request set differs')
    profile = {**value['route']['profile'], 'execution_classes': tuple(value['route']['profile']['execution_classes'])}
    route = RouteAdmission(**{**value['route'], 'profile': RouteProfile(**profile)})
    for record in records:
        reopen_quality_wire(store, value['wires'][record['request_id']], permit, record, route)
    return value


def _route_command(request: ProviderRequest, command: ProcessCommand) -> ProcessCommand:
    if request.executor_id != "codex":
        return command
    if request.execution_class != "repo-write" or request.profile is None:
        raise ProviderRequestError("container Codex command needs a repo-write profile")
    template = request.profile.resume_argv if request.resume else request.profile.initial_argv
    if command.argv != _render_profile_argv(template, request.session_id):
        raise ProviderRequestError("container Codex command grammar differs")
    overrides = (
        "-c", 'model_provider="fanout"', "-c", _CODEX_LOOPBACK_PROVIDER,
        "-c", 'approval_policy="never"', "-c", 'approvals_reviewer="user"',
        # Login startup drops Codex's native apply_patch helper from PATH.
        "-c", "allow_login_shell=false",
    )
    # The attested outer container supplies isolation; nested namespaces are unavailable.
    if request.resume:
        if command.argv[:5] != ("codex", "exec", "resume", "-c", 'sandbox_mode="workspace-write"'):
            raise ProviderRequestError("container Codex resume sandbox grammar differs")
        # Resume's overrides must share its command level, not the parent exec level.
        routed = ("resume", *overrides, "-c", 'sandbox_mode="danger-full-access"', *command.argv[5:])
    else:
        if command.argv[:6] != ("codex", "exec", "-", "--json", "--sandbox", "workspace-write"):
            raise ProviderRequestError("container Codex initial sandbox grammar differs")
        routed = (*overrides, "-", "--json", "--sandbox", "danger-full-access", *command.argv[6:])
    return replace(command, argv=(
        "codex", "exec", "--ignore-user-config", "--strict-config",
        "--skip-git-repo-check",
        *routed,
    ))


def _run_container_turn(request: ProviderRequest,
                        boundary: ContainerSeatBoundary) -> tuple[ProviderResult, str]:
    """Run one private diagnostic through the existing adapter and parser."""
    if not isinstance(boundary, ContainerSeatBoundary):
        raise ProviderRequestError("container boundary type is invalid")
    validate_container_boundary(boundary, request, boundary.controller)
    registry = ProviderRegistry.default(version_probe=lambda _: "not-used")
    profile = registry.select(request.executor_id, request.execution_class,
                              request.profile.quality_tier)
    if profile.digest != request.profile.digest:
        raise ProviderRequestError("container executor profile changed")
    adapter = registry.require(request.executor_id)
    if isinstance(adapter, (ClaudeAdapter, AgyAdapter)):
        command = adapter.build_command(request, brokered_diagnostic=True)
    else:
        command = adapter.build_command(request)
    command = _route_command(request, command)
    turn_digest = _turn_digest(request, boundary)
    transport = boundary.admission.transport
    control = (getattr(transport, "_control", None)
               if type(transport) is PinnedDockerTransport else None)
    owner_lease = (control._lease if type(control) is PinnedSSHSeatControl
                   else None)
    live_owner = (owner_lease is not None and owner_lease._fd is not None
                  and control.controller is boundary.controller
                  and control.permit_id == boundary.permit_id
                  and control.epoch == boundary.permit_epoch)
    lease = (nullcontext() if live_owner else
             HostSessionLease(boundary.controller, boundary.permit_id,
                              boundary.permit_epoch))
    with lease:
        # Reserve before process spawn. An interrupted or malformed terminal is
        # intentionally left uncertain across controller recovery.
        boundary.session_store.reserve(
            request.executor_id, turn_digest, initial=not request.resume,
            upstream_route_digest=boundary.upstream_route_digest,
            initial_session_id=(request.session_id if request.executor_id == "claude"
                                and not request.resume else None),
        )
        started = time.monotonic()
        process = boundary.admission.transport.run(command, boundary)
        if not isinstance(process, ProcessResult):
            raise ProviderRequestError("container process returned invalid terminal evidence")
        parsed = None
        if process.status != ProcessStatus.SPAWN_ERROR and process.stdout:
            try:
                parsed = adapter.parse(process.stdout)
            except ProviderProtocolError:
                pass
        result = _make_result(request, adapter, process, parsed, 1,
                              time.monotonic() - started)
        return result, turn_digest


def run_container_turn(request: ProviderRequest,
                       boundary: ContainerSeatBoundary) -> ProviderResult:
    """Accept a terminal only after its exact owned seat is fenced."""
    if not isinstance(boundary, ContainerSeatBoundary):
        raise ProviderRequestError("container boundary type is invalid")
    cleanup_authorized = _turn_cleanup_authorized(request, boundary)
    primary: BaseException | None = None
    terminal: tuple[ProviderResult, str] | None = None
    try:
        if (type(boundary.admission.transport) is PinnedDockerTransport
                and not cleanup_authorized):
            raise ProviderRequestError("container shutdown authority was not retained")
        terminal = _run_container_turn(request, boundary)
        if (terminal[0].valid and
                type(boundary.admission.transport) is PinnedDockerTransport):
            quarantine = controller_directory(boundary.controller, "quarantine")
            export_root = quarantine / f"candidate-{secrets.token_hex(16)}"
            os.mkdir(export_root, 0o700)
            export_receipt = boundary.admission.transport.export_candidate(
                request, boundary, export_root)
            terminal = (replace(terminal[0], candidate_export=export_receipt),
                        terminal[1])
    except BaseException as error:
        primary = error
    cleanup_error: BaseException | None = None
    if cleanup_authorized:
        close = getattr(boundary.admission.transport, "close", None)
        if callable(close):
            try:
                close()
            except BaseException as error:
                cleanup_error = error
    if cleanup_error is not None:
        transport = boundary.admission.transport
        note = _cleanup_failure_note(
            boundary.controller, "turn", cleanup_error, primary=primary,
            spec=transport.spec if type(transport) is PinnedDockerTransport else None,
            handle=transport.handle if type(transport) is PinnedDockerTransport else None,
            permit_id=boundary.permit_id, provider=boundary.provider,
            store=boundary.session_store,
        )
        (primary if primary is not None else cleanup_error).add_note(note)
    if primary is not None:
        raise primary.with_traceback(primary.__traceback__)
    if cleanup_error is not None:
        raise cleanup_error
    if terminal is None:
        raise ProviderRequestError("container terminal evidence is unavailable")
    result, turn_digest = terminal
    if result.valid:
        boundary.session_store.settle(request.executor_id, result.session_id,
                                      turn_digest, initial=not request.resume,
                                      upstream_route_digest=boundary.upstream_route_digest)
    return result
