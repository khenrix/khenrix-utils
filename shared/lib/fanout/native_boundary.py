"""Controller-authenticated macOS file boundary for disposable seat diagnostics.

Provider write launches stay closed until the live route certificate is issued.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Mapping, Sequence

from .artifacts import ArtifactRef, canonical_json
from .agy_guard import _BINARY as _AGY_DIRECT_BINARY
from .broker import BrokerListener
from .controller import LifecycleController, read_evidence, write_evidence
from .errors import ProviderRequestError
from .process import ProcessCommand, ProcessStatus, build_child_environment, run_command
from .repo import SeatWorkspaceVerification, validate_seat_workspace
from .skills import SkillAdmission, SkillLoadEvidence, verify_staged_admission


_CATEGORY = "native-seat-boundary"
_BROKER_ADMISSION_CATEGORY = "native-broker-diagnostic-admission"
_BROKER_RESULT_FIELDS = frozenset({
    "schema_version", "run_id", "task_id", "target_id", "seat_id", "inputs_digest",
    "profile_digest", "boundary_receipt_digest", "route", "session_id", "valid",
    "reason", "process_status", "exit_code", "stdout_sha256", "stderr_sha256",
    "observed_model", "broker_model", "effective_output_tokens",
    "refused_agy_auxiliary_requests",
})
_BROKER_BOUNDARY_FIELDS = frozenset({
    "schema_version", "controller_id", "run_id", "task_id", "target_id",
    "seat_id", "inputs_digest", "workspace", "workspace_device",
    "workspace_inode", "workspace_receipt", "cwd", "profile_digest",
    "executable", "executable_digest", "resolved_argv", "command_digest",
    "environment_digest", "route", "profile_path", "profile_sha256",
    "state_root", "denied_paths", "broker_port", "broker_binding_id",
    "broker_permit_sha256", "broker_admission_name", "broker_admission_digest",
})
_SANDBOX = Path("/usr/bin/sandbox-exec")
_RUNTIME_ROOTS = (
    "/System", "/usr/lib", "/usr/share", "/Library/Apple",
    "/Library/Developer/CommandLineTools", "/private/var/db/dyld",
    "/private/var/db/timezone", "/private/preboot/Cryptexes", "/private/etc/ssl",
)
_RUNTIME_FILES = (
    "/dev/null", "/dev/random", "/dev/urandom", "/dev/zero",
    "/private/etc/passwd", "/private/etc/services", "/private/etc/protocols",
    "/private/etc/hosts", "/etc/hosts",
)
_MEASURED_WIRE_MODELS = {
    ("agy", "1.2.13", "gemini-3.8-flash-high"): "gemini-3.8-flash",
}
_AGY_GEMINI_SETTINGS = b'{"modelProvider":"gemini"}\n'


@dataclass(frozen=True, slots=True)
class NativeSeatBoundary:
    run_id: str
    task_id: str
    target_id: str
    seat_id: str
    inputs_digest: str
    workspace_root: Path
    workspace_device: int
    workspace_inode: int
    cwd: Path
    profile_digest: str
    executable_digest: str
    command_digest: str
    route: str
    profile_path: Path
    profile_sha256: str
    controller_mac: str
    executable_path: Path
    resolved_argv: tuple[str, ...]
    state_root: Path
    denied_paths: tuple[Path, ...]
    staged_skill_grant_digest: str | None
    receipt_name: str
    receipt_digest: str
    frozen_environment: tuple[tuple[str, str], ...] = field(repr=False)
    environment_digest: str
    broker_port: int | None = None
    broker_binding_id: str | None = None
    broker_permit_sha256: str | None = None
    broker_listener: BrokerListener | None = field(default=None, repr=False, compare=False)
    broker_admission: tuple[str, str] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class NativeProbeReceipt:
    executor_id: str
    route: str
    exit_code: int
    requested_model: str
    observed_model: str | None
    session_id: str | None
    profile_sha256: str
    denied_paths: tuple[str, ...]
    allowed_workspace_write: bool
    credential_blind: bool
    transcript: ArtifactRef


def issue_broker_admission(
    controller: LifecycleController, verification: SeatWorkspaceVerification,
    request: object, command: ProcessCommand,
    listener: BrokerListener, *, initial_receipt: tuple[str, str] | None = None,
    quality_admission: tuple[str, str] | None = None,
) -> tuple[str, str]:
    """Authenticate one read-only command and listener; controller signing is trusted."""
    try:
        validate_seat_workspace(controller, verification)
        _check_identity(request.run_id, request.task_id, request.target_id,
                        request.inputs_digest, request, verification)
        if request.execution_class != "read-only":
            raise ProviderRequestError("broker diagnostic admission is read-only")
        if request.resume != (initial_receipt is not None):
            raise ProviderRequestError("broker diagnostic resume needs initial evidence")
        _check_canonical_broker_command(request, command, listener,
            controller=controller, quality_admission=quality_admission)
        _require_broker_initial(controller, verification, request, initial_receipt)
        binding = _broker_binding(listener, request, quality=quality_admission is not None)
        return write_evidence(
            controller, _BROKER_ADMISSION_CATEGORY,
            _broker_admission_payload(
                request, verification.evidence_digest, _command_digest(command),
                binding, initial_receipt,
                quality_admission=quality_admission,
            ),
        )
    except ProviderRequestError:
        raise
    except Exception as error:
        raise ProviderRequestError("broker diagnostic admission failed") from error


def issue_native_boundary(
    controller: LifecycleController, verification: SeatWorkspaceVerification, *,
    run_id: str, task_id: str, target_id: str, inputs_digest: str,
    request: object, command: ProcessCommand, other_target_roots: Sequence[Path],
    denied_owner_roots: Sequence[Path],
    broker_listener: BrokerListener | None = None,
    broker_admission: tuple[str, str] | None = None,
) -> NativeSeatBoundary:
    """Issue an exact, authenticated descriptor after checking all allowed trees."""
    try:
        validate_seat_workspace(controller, verification)
        _check_identity(run_id, task_id, target_id, inputs_digest, request, verification)
        if request.execution_class == "read-only" and broker_listener is None:
            raise ProviderRequestError("read-only native boundary requires broker admission")
        if (broker_listener is None) != (broker_admission is None):
            raise ProviderRequestError("broker diagnostic admission is required for a listener")
        workspace = verification.workspace.root
        _reject_aliases(workspace)
        _check_command(request, command)
        quality = (read_evidence(controller, _BROKER_ADMISSION_CATEGORY, *broker_admission).get("quality_admission")
                   if broker_admission is not None else None)
        binding = (_broker_binding(broker_listener, request, quality=quality is not None)
                   if broker_listener is not None else None)
        if binding is not None:
            _validate_broker_admission(
                controller, verification, request, command, broker_listener, binding,
                broker_admission,
            )
        broker_port = binding[0] if binding is not None else None
        denied = _denied_roots(other_target_roots, denied_owner_roots, workspace)
        grant = _staged_skill_grant(request, denied, workspace, controller.root)
        profile = request.profile
        profile_executable = profile.resume_argv[0] if request.resume else profile.initial_argv[0]
        if (request.executor_id == "agy" and request.execution_class == "read-only"
                and profile_executable == "agy"):
            profile_executable = str(_AGY_DIRECT_BINARY)
        executable = _executable(profile_executable)
        if _executable(command.argv[0]) != executable:
            raise ValueError("native command executable differs from the profile")
        executable_digest = _hash_file(executable)
        resolved_argv = (str(executable), *command.argv[1:])
        route = _route(request)
        state = _private_state(controller.root, run_id, task_id, target_id,
                               verification.workspace.seat_id)
        frozen_environment = _native_environment(state, request, command, binding)
        environment_digest = _environment_digest(frozen_environment)
        policy = _policy(
            workspace, state, executable, denied,
            None if grant is None else Path(grant["root"]),
            execution_class=request.execution_class, broker_port=broker_port,
        )
        profile_sha256 = hashlib.sha256(policy).hexdigest()
        profile_path = _profile_path(controller.root, profile_sha256)
        _write_immutable(profile_path, policy)
        payload = {
            "schema_version": "fanout-native-seat-boundary-v1",
            "controller_id": controller.controller_id,
            "run_id": run_id, "task_id": task_id, "target_id": target_id,
            "seat_id": verification.workspace.seat_id,
            "inputs_digest": inputs_digest,
            "workspace": str(workspace),
            "workspace_device": verification.root_device,
            "workspace_inode": verification.root_inode,
            "workspace_receipt": verification.evidence_digest,
            "cwd": str(Path(request.cwd)),
            "profile_digest": profile.digest,
            "executable": str(executable),
            "executable_digest": executable_digest,
            "resolved_argv": list(resolved_argv),
            "command_digest": _command_digest(command),
            "environment_digest": environment_digest,
            "route": route,
            "profile_path": str(profile_path),
            "profile_sha256": profile_sha256,
            "state_root": str(state),
            "denied_paths": [str(path) for path in denied],
        }
        if grant is not None:
            payload["staged_skill_grant"] = grant
        if broker_port is not None:
            payload["broker_port"] = broker_port
            payload["broker_binding_id"] = binding[1]
            payload["broker_permit_sha256"] = binding[2]
            payload["broker_admission_name"] = broker_admission[0]
            payload["broker_admission_digest"] = broker_admission[1]
        receipt_name, receipt_digest = write_evidence(controller, _CATEGORY, payload)
        record = json.loads((controller.root / "receipts" / receipt_name).read_bytes())
        return NativeSeatBoundary(
            run_id, task_id, target_id, verification.workspace.seat_id, inputs_digest,
            workspace, verification.root_device, verification.root_inode,
            Path(request.cwd), profile.digest, executable_digest,
            _command_digest(command), route,
            profile_path, profile_sha256, record["mac"], executable, resolved_argv, state,
            denied, None if grant is None else hashlib.sha256(canonical_json(grant)).hexdigest(),
            receipt_name, receipt_digest,
            frozen_environment, environment_digest,
            broker_port,
            None if binding is None else binding[1],
            None if binding is None else binding[2], broker_listener,
            broker_admission,
        )
    except ProviderRequestError:
        raise
    except Exception as error:
        raise ProviderRequestError("native seat boundary issuance failed") from error


def validate_native_boundary(
    boundary: NativeSeatBoundary, request: object, command: ProcessCommand | None,
    controller: LifecycleController, verification: SeatWorkspaceVerification,
) -> None:
    """Authenticate a descriptor; a supplied command is checked for exact launch."""
    try:
        if not isinstance(boundary, NativeSeatBoundary) or (
            command is not None and not isinstance(command, ProcessCommand)
        ):
            raise ValueError("descriptor or command has an invalid type")
        validate_seat_workspace(controller, verification)
        _check_identity(boundary.run_id, boundary.task_id, boundary.target_id,
                        boundary.inputs_digest, request, verification)
        grant = _staged_skill_grant(
            request, boundary.denied_paths, boundary.workspace_root, controller.root,
        )
        grant_digest = None if grant is None else hashlib.sha256(canonical_json(grant)).hexdigest()
        if grant_digest != boundary.staged_skill_grant_digest:
            raise ValueError("staged skill grant changed")
        if request.execution_class == "read-only" and boundary.broker_port is None:
            raise ValueError("read-only native boundary lacks broker admission")
        payload = read_evidence(controller, _CATEGORY,
                                boundary.receipt_name, boundary.receipt_digest)
        record = json.loads((controller.root / "receipts" / boundary.receipt_name).read_bytes())
        if record.get("mac") != boundary.controller_mac:
            raise ValueError("controller MAC changed")
        expected = {
            "schema_version": "fanout-native-seat-boundary-v1",
            "controller_id": controller.controller_id,
            "run_id": boundary.run_id, "task_id": boundary.task_id,
            "target_id": boundary.target_id, "seat_id": boundary.seat_id,
            "inputs_digest": boundary.inputs_digest,
            "workspace": str(boundary.workspace_root),
            "workspace_device": boundary.workspace_device,
            "workspace_inode": boundary.workspace_inode,
            "workspace_receipt": verification.evidence_digest,
            "cwd": str(boundary.cwd),
            "profile_digest": boundary.profile_digest,
            "executable": str(boundary.executable_path),
            "executable_digest": boundary.executable_digest,
            "resolved_argv": list(boundary.resolved_argv),
            "command_digest": boundary.command_digest,
            "environment_digest": boundary.environment_digest,
            "route": boundary.route,
            "profile_path": str(boundary.profile_path),
            "profile_sha256": boundary.profile_sha256,
            "state_root": str(boundary.state_root),
            "denied_paths": [str(path) for path in boundary.denied_paths],
        }
        if grant is not None:
            expected["staged_skill_grant"] = grant
        if boundary.broker_port is not None:
            quality = read_evidence(controller, _BROKER_ADMISSION_CATEGORY,
                                   *boundary.broker_admission).get("quality_admission")
            binding = _broker_binding(boundary.broker_listener, request, quality=quality is not None)
            _validate_broker_admission(
                controller, verification, request, command, boundary.broker_listener, binding,
                boundary.broker_admission,
                command_digest=boundary.command_digest,
            )
            if (boundary.broker_port, boundary.broker_binding_id,
                    boundary.broker_permit_sha256) != binding[:3]:
                raise ValueError("native broker binding changed")
            expected["broker_port"] = boundary.broker_port
            expected["broker_binding_id"] = boundary.broker_binding_id
            expected["broker_permit_sha256"] = boundary.broker_permit_sha256
            expected["broker_admission_name"] = boundary.broker_admission[0]
            expected["broker_admission_digest"] = boundary.broker_admission[1]
            expected_environment = _broker_environment(request.executor_id, binding)
            if any(dict(boundary.frozen_environment).get(key) != value
                   for key, value in expected_environment.items()):
                raise ValueError("native broker endpoint changed")
        elif any(value is not None for value in (boundary.broker_binding_id,
                                                 boundary.broker_permit_sha256,
                                                 boundary.broker_listener,
                                                 boundary.broker_admission)):
            raise ValueError("native broker binding lacks a port")
        if payload != expected:
            raise ValueError("descriptor differs from controller evidence")
        if (_environment_digest(boundary.frozen_environment) != boundary.environment_digest
                or boundary.frozen_environment != tuple(sorted(dict(boundary.frozen_environment).items()))
                or dict(boundary.frozen_environment).get("HOME") != str(boundary.state_root / "home")
                or dict(boundary.frozen_environment).get("TMPDIR") != str(boundary.state_root / "tmp")):
            raise ValueError("frozen native environment changed")
        if (request.executor_id == "claude"
                and dict(boundary.frozen_environment).get("CLAUDE_CODE_TMPDIR") !=
                str(boundary.state_root / "tmp")):
            raise ValueError("Claude private temp changed")
        if (request.executor_id == "codex"
                and dict(boundary.frozen_environment).get("CODEX_HOME") !=
                str(boundary.state_root / "home" / ".codex")):
            raise ValueError("Codex private session home changed")
        if (request.executor_id == "agy"
                and boundary.executable_path == _AGY_DIRECT_BINARY):
            home = boundary.state_root / "home"
            if (dict(boundary.frozen_environment).get("XDG_CONFIG_HOME") !=
                    str(home / ".config")
                    or _hash_file(home / ".gemini" / "antigravity-cli" / "settings.json",
                                  mode=0o600) != hashlib.sha256(_AGY_GEMINI_SETTINGS).hexdigest()):
                raise ValueError("agy private Gemini route changed")
        root_info = boundary.workspace_root.lstat()
        if (root_info.st_dev, root_info.st_ino) != (boundary.workspace_device,
                                                    boundary.workspace_inode):
            raise ValueError("workspace inode changed")
        if (boundary.workspace_root != verification.workspace.root
                or boundary.seat_id != verification.workspace.seat_id
                or boundary.cwd != Path(request.cwd)
                or boundary.profile_digest != request.profile.digest
                or boundary.route != _route(request)):
            raise ValueError("request changed association")
        if command is not None:
            _check_command(request, command)
            if boundary.command_digest != _command_digest(command):
                raise ValueError("native seat command bytes changed")
        if _hash_file(boundary.profile_path, mode=0o400) != boundary.profile_sha256:
            raise ValueError("native profile bytes changed")
        if (not isinstance(boundary.resolved_argv, tuple)
                or not boundary.resolved_argv
                or boundary.resolved_argv[0] != str(boundary.executable_path)):
            raise ValueError("native resolved argv differs from executable")
        if hashlib.sha256(_policy(
                boundary.workspace_root, boundary.state_root, boundary.executable_path,
                boundary.denied_paths, None if grant is None else Path(grant["root"]),
                execution_class=request.execution_class, broker_port=boundary.broker_port,
            )).hexdigest() != boundary.profile_sha256:
            raise ValueError("native profile policy changed")
        if command is not None and _executable(command.argv[0]) != boundary.executable_path:
            raise ValueError("command executable changed")
        if command is not None and boundary.resolved_argv != (
                str(boundary.executable_path), *command.argv[1:]):
            raise ValueError("native resolved argv differs from command")
        if _hash_file(boundary.executable_path) != boundary.executable_digest:
            raise ValueError("command executable bytes changed")
        if sys.platform != "darwin" or not _SANDBOX.is_file():
            raise ValueError("macOS Seatbelt driver is unavailable")
        _reject_aliases(boundary.workspace_root)
        _reject_aliases(boundary.state_root)
    except Exception as error:
        raise ProviderRequestError("native seat boundary validation failed") from error


def wrap_native_command(
    command: ProcessCommand, boundary: NativeSeatBoundary, *,
    request: object, controller: LifecycleController,
    verification: SeatWorkspaceVerification,
) -> ProcessCommand:
    validate_native_boundary(boundary, request, command, controller, verification)
    return replace(command, argv=(str(_SANDBOX), "-f", str(boundary.profile_path),
                                  *boundary.resolved_argv),
                   environment={"HOME": str(boundary.state_root / "home"),
                                "TMPDIR": str(boundary.state_root / "tmp")},
                   native_original=command, native_boundary=boundary,
                   native_request=request, native_controller=controller,
                   native_verification=verification)


def run_native_boundary_probe(
    boundary: NativeSeatBoundary, request: object, command: ProcessCommand, *,
    controller: LifecycleController, verification: SeatWorkspaceVerification,
    oracle_paths: Mapping[str, Path],
) -> NativeProbeReceipt:
    """Verify disposable shell oracles; never certify a production model route."""
    if boundary.executable_path != _executable("/bin/sh"):
        raise ProviderRequestError("native oracle probe requires a pinned disposable shell")
    store = request.artifact_store
    if store is None:
        raise ProviderRequestError("native seat boundary probe needs an artifact store")
    paths = _probe_oracle_paths(boundary, controller, oracle_paths)
    validate_native_boundary(boundary, request, command, controller, verification)
    for path in paths:
        for operation, source in (
            ("read", 'IFS= read -r value < "$1"'),
            ("write", ': >> "$1"'),
        ):
            oracle = ProcessCommand(
                argv=("/bin/sh", "-c", source, "sh", str(path)),
                stdin=b"", cwd=boundary.cwd, timeout=10,
            )
            diagnostic = _diagnostic_boundary(oracle, boundary, request, controller, verification)
            observed = run_command(wrap_native_command(
                oracle, diagnostic, request=request, controller=controller,
                verification=verification,
            ))
            _require_probe_exit(observed)
            if observed.returncode != 1 or b"Operation not permitted" not in observed.stderr:
                raise ProviderRequestError(
                    f"native oracle {operation} was not denied: {path.name}"
                )
    allowed = boundary.workspace_root / f".native-probe-{boundary.receipt_digest[:16]}"
    if allowed.exists() or allowed.is_symlink():
        raise ProviderRequestError("native oracle workspace sentinel already exists")
    write = ProcessCommand(
        argv=("/bin/sh", "-c", 'set -C; printf native-probe > "$1"',
              "sh", str(allowed)),
        stdin=b"", cwd=boundary.cwd, timeout=10,
    )
    diagnostic = _diagnostic_boundary(write, boundary, request, controller, verification)
    observed = run_command(wrap_native_command(
        write, diagnostic, request=request, controller=controller,
        verification=verification,
    ))
    _require_probe_exit(observed)
    if observed.returncode != 0 or allowed.read_bytes() != b"native-probe":
        raise ProviderRequestError("native oracle workspace write failed")
    result = run_command(wrap_native_command(
        command, boundary, request=request, controller=controller,
        verification=verification,
    ))
    _require_probe_exit(result)
    if len(result.stdout) + len(result.stderr) > 64 * 1024:
        raise ProviderRequestError("native oracle transcript exceeds the diagnostic bound")
    transcript = store.write_bytes(
        f"{request.artifact_prefix}/native-probe-{boundary.receipt_digest}.json",
        canonical_json({"exit_code": result.returncode,
                        "stdout": result.stdout.decode("utf-8", "replace"),
                        "stderr": result.stderr.decode("utf-8", "replace"),
                        "denied_paths": [str(path) for path in paths],
                        "allowed_workspace_write": str(allowed)}),
    )
    return NativeProbeReceipt(
        request.executor_id, boundary.route, result.returncode,
        request.profile.requested_model, None, request.session_id,
        boundary.profile_sha256, tuple(str(path) for path in paths),
        True, False, transcript,
    )


def _diagnostic_boundary(command: ProcessCommand, boundary: NativeSeatBoundary,
                         request: object, controller: LifecycleController,
                         verification: SeatWorkspaceVerification) -> NativeSeatBoundary:
    # Every oracle gets its own controller-authenticated exact command receipt.
    return issue_native_boundary(
        controller, verification, run_id=boundary.run_id, task_id=boundary.task_id,
        target_id=boundary.target_id, inputs_digest=boundary.inputs_digest,
        request=request, command=command, other_target_roots=(),
        denied_owner_roots=boundary.denied_paths,
    )


def _probe_oracle_paths(boundary: NativeSeatBoundary, controller: LifecycleController,
                        paths: Mapping[str, Path]) -> tuple[Path, ...]:
    if not isinstance(paths, Mapping) or not paths:
        raise ProviderRequestError("native oracle paths are required")
    result = []
    disposable_root = Path("/private/tmp")
    if disposable_root not in controller.root.parents:
        raise ProviderRequestError("native oracle controller must be disposable")
    for name, raw in paths.items():
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name) is None:
            raise ProviderRequestError("native oracle name is invalid")
        path = Path(raw)
        try:
            info = path.lstat()
            parent_info = path.parent.lstat()
            canonical = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ProviderRequestError("native oracle path is unavailable") from error
        admitted = (path == boundary.profile_path
                    or path == controller.root / "controller.json"
                    or any(root in path.parents for root in boundary.denied_paths))
        if (not path.is_absolute() or path != canonical
                or disposable_root not in path.parents or not admitted
                or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_size > 64 * 1024
                or not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid != os.getuid()
                or stat.S_IMODE(parent_info.st_mode) != 0o700):
            raise ProviderRequestError("native oracle path is not a disposable protected file")
        result.append(path)
    if len(set(result)) != len(result):
        raise ProviderRequestError("native oracle paths must be distinct")
    return tuple(result)


def _require_probe_exit(result: object) -> None:
    if result.returncode == 71 and b"sandbox_apply: Operation not permitted" in result.stderr:
        raise ProviderRequestError("native oracle sandbox was denied by enclosing environment")
    if result.status is not ProcessStatus.EXIT or result.returncode is None:
        raise ProviderRequestError("native oracle did not finish")


def _check_identity(run_id: str, task_id: str, target_id: str, digest: str,
                    request: object, verification: SeatWorkspaceVerification) -> None:
    if any(not isinstance(value, str) or not value or "\x00" in value
           for value in (run_id, task_id, target_id)):
        raise ValueError("native descriptor identity is invalid")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("native descriptor inputs digest is invalid")
    if (getattr(request, "run_id", None) != run_id
            or getattr(request, "task_id", None) != task_id
            or getattr(request, "target_id", None) != target_id
            or getattr(request, "inputs_digest", None) != digest
            or getattr(request, "seat_id", None) != verification.workspace.seat_id
            or getattr(request, "profile", None) is None
            or Path(request.cwd) != verification.workspace.root):
        raise ValueError("native descriptor request association differs")
    if request.executor_id == "claude":
        session_id = request.session_id
        if not isinstance(session_id, str):
            raise ValueError("Claude native route requires a canonical preassigned session UUID")
        try:
            canonical = str(uuid.UUID(session_id))
        except ValueError as error:
            raise ValueError("Claude native route requires a canonical preassigned session UUID") from error
        if canonical != session_id:
            raise ValueError("Claude native route requires a canonical preassigned session UUID")


def _route(request: object) -> str:
    if request.resume:
        return f"resume:{request.session_id}"
    if request.session_id is None:
        return "initial:unassigned"
    return f"initial:assigned:{request.session_id}"


def _check_command(request: object, command: ProcessCommand) -> None:
    if not isinstance(command, ProcessCommand) or Path(command.cwd) != Path(request.cwd):
        raise ValueError("native seat command cwd differs")
    if command.environment:
        raise ValueError("native seat command may not carry environment overrides")


def _command_digest(command: ProcessCommand) -> str:
    return hashlib.sha256(canonical_json({
        "argv": list(command.argv), "stdin_sha256": hashlib.sha256(command.stdin).hexdigest(),
        "environment": dict(command.environment), "cwd": str(command.cwd),
        "timeout": command.timeout, "slot_cap": command.slot_cap,
        "slot_timeout": command.slot_timeout,
        "slot_root": None if command.slot_root is None else str(command.slot_root),
        "max_depth": command.max_depth, "term_grace": command.term_grace,
        "reap_timeout": command.reap_timeout,
        "max_output_bytes": command.max_output_bytes,
    })).hexdigest()


def _native_environment(state: Path, request: object,
                        command: ProcessCommand,
                        binding: tuple[int, str, str, str] | None = None,
                        ) -> tuple[tuple[str, str], ...]:
    overrides = {"HOME": str(state / "home"), "TMPDIR": str(state / "tmp")}
    if request.executor_id == "agy" and command.argv[0] == str(_AGY_DIRECT_BINARY):
        _agy_private_settings(state / "home")
        if "--agent" in command.argv and command.argv[command.argv.index("--agent") + 1] == "eval-no-tools":
            _quality_agy_agent(state / "home")
        overrides["XDG_CONFIG_HOME"] = str(state / "home" / ".config")
    child = build_child_environment(
        dict(os.environ),
        overrides=overrides,
        max_depth=command.max_depth,
    )
    if request.executor_id == "claude":
        child["CLAUDE_CODE_TMPDIR"] = str(state / "tmp")
    elif request.executor_id == "codex":
        codex_home = state / "home" / ".codex"
        codex_home.mkdir(mode=0o700, exist_ok=True)
        info = codex_home.lstat()
        if (codex_home.is_symlink() or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("Codex private session home is unsafe")
        child["CODEX_HOME"] = str(codex_home)
    if binding is not None:
        child.update(_broker_environment(request.executor_id, binding))
    return tuple(sorted(child.items()))


def _quality_agy_agent(home):
    from .eval_provider import AGY_AGENT_TEXT
    directory = home / ".gemini" / "config" / "agents"
    for path in (home / ".gemini", home / ".gemini" / "config", directory):
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if (path.is_symlink() or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ProviderRequestError("quality agy private agent directory differs")
    path = directory / "eval-no-tools.md"
    data = AGY_AGENT_TEXT.encode()
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
    if _hash_file(path, mode=0o600) != hashlib.sha256(data).hexdigest():
        raise ProviderRequestError("quality agy private empty-tools agent differs")


def _agy_private_settings(home: Path) -> None:
    for path in (home / ".config", home / ".gemini",
                 home / ".gemini" / "antigravity-cli"):
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if (path.is_symlink() or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("agy private settings directory is unsafe")
    path = home / ".gemini" / "antigravity-cli" / "settings.json"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            if os.write(fd, _AGY_GEMINI_SETTINGS) != len(_AGY_GEMINI_SETTINGS):
                raise OSError("short agy settings write")
            os.fsync(fd)
        finally:
            os.close(fd)
    if (_hash_file(path, mode=0o600) !=
            hashlib.sha256(_AGY_GEMINI_SETTINGS).hexdigest()):
        raise ValueError("agy private Gemini settings changed")


def _broker_binding(listener: BrokerListener, request: object, *, quality=False) -> tuple[int, str, str, str]:
    if not isinstance(listener, BrokerListener):
        raise ValueError("native broker listener is invalid")
    host, port, binding_id, permit = listener.active_binding()
    wire_model = _MEASURED_WIRE_MODELS.get(
        (request.executor_id, request.profile.cli_version, request.profile.requested_model),
        request.profile.requested_model,
    )
    if quality:
        wire_model = request.profile.requested_model
    if (host != "127.0.0.1" or type(port) is not int or not 1 <= port <= 65535
            or permit.provider != request.executor_id
            or permit.run_id != request.run_id or permit.task_id != request.task_id
            or permit.target_id != request.target_id or permit.seat_id != request.seat_id
            or permit.inputs_digest != request.inputs_digest
            or permit.session_id != request.session_id
            or permit.route != ("resume" if request.resume else "initial")
            or permit.model != wire_model):
        raise ValueError("native broker permit differs from the seat")
    permit_sha256 = hashlib.sha256(canonical_json(asdict(permit))).hexdigest()
    return port, binding_id, permit_sha256, permit.placeholder_key


def _check_canonical_broker_command(
    request: object, command: ProcessCommand | None, listener: BrokerListener,
    *, command_digest: str | None = None,
    controller=None, quality_admission=None,
) -> None:
    from .providers import AgyAdapter, ClaudeAdapter, CodexAdapter, ProviderRegistry

    if request.execution_class != "read-only" or request.profile is None:
        raise ProviderRequestError("broker diagnostic is read-only")
    registry = ProviderRegistry.for_installed((request.executor_id,))
    profile = registry.admit(
        request.executor_id, request.execution_class, request.profile.quality_tier,
    )
    if profile.cli_version != request.profile.cli_version:
        raise ProviderRequestError("CLI version drifted from the pinned executor profile")
    if profile.digest != request.profile.digest:
        raise ProviderRequestError("broker diagnostic profile differs from the canonical contract")
    registry = ProviderRegistry((profile,), version_probe=registry._version_probe)
    registry.assert_installed_version(profile)
    adapter = registry.require(request.executor_id)
    if quality_admission is not None:
        from functools import partial
        from .diagnostic import reopen_quality_native_command, _codex_broker_command, _quality_send, _quality_preview
        from .container_upstream import HostUpstream
        expected, registration = reopen_quality_native_command(controller, request, quality_admission)
        if (listener._ledger_binding is None
                or listener._ledger_binding[0].controller is not controller
                or listener._ledger_binding[1:] != (registration["permit_id"], registration["epoch"])
                or asdict(listener.active_binding()[3]) != registration["permit"]):
            raise ProviderRequestError("quality native consumed permit differs from listener")
        sender = listener._broker._send
        if (not isinstance(sender, partial) or sender.func is not _quality_send
                or len(sender.args) != 4 or type(sender.args[0]) is not HostUpstream
                or asdict(sender.args[0]._permit) != registration["permit"]
                or not isinstance(getattr(sender, "preview", None), partial)
                or sender.preview.func is not _quality_preview
                or sender.preview.args != sender.args or sender.captures is not sender.args[3]):
            raise ProviderRequestError("quality native sender lacks the genuine empty-tools guard")
        evidence = read_evidence(controller, "quality-native-command", *quality_admission)
        if str(sender.args[1].root) != evidence["store"]:
            raise ProviderRequestError("quality native wire store differs from allocation")
        if request.executor_id == "codex":
            expected = _codex_broker_command(request, expected, listener)
    elif type(adapter) is ClaudeAdapter:
        expected = adapter.build_command(request, brokered_diagnostic=True)
    elif type(adapter) is CodexAdapter:
        from .diagnostic import _codex_broker_command

        expected = _codex_broker_command(request, adapter.build_command(request), listener)
    elif type(adapter) is AgyAdapter:
        expected = adapter.build_command(request, brokered_diagnostic=True)
    else:
        raise ProviderRequestError("broker diagnostic provider is unsupported")
    if expected.environment or (
        command is not None and command != expected
    ) or (
        command_digest is not None and _command_digest(expected) != command_digest
    ):
        raise ProviderRequestError("broker diagnostic command differs from the installed profile")


def _broker_admission_payload(
    request: object, workspace_receipt: str, command_digest: str,
    binding: tuple[int, str, str, str],
    initial_receipt: tuple[str, str] | None,
    *, quality_admission=None,
) -> dict[str, object]:
    payload = {
        "schema_version": "fanout-native-broker-diagnostic-admission-v1",
        "run_id": request.run_id, "task_id": request.task_id,
        "target_id": request.target_id, "seat_id": request.seat_id,
        "inputs_digest": request.inputs_digest,
        "executor_id": request.executor_id, "execution_class": request.execution_class,
        "route": _route(request), "session_id": request.session_id,
        "cwd": str(request.cwd), "profile_digest": request.profile.digest,
        "workspace_receipt": workspace_receipt,
        "command_digest": command_digest,
        "broker_port": binding[0], "broker_binding_id": binding[1],
        "broker_permit_sha256": binding[2],
        "staged_skill_root": (None if request.staged_skill_root is None
                              else str(request.staged_skill_root)),
        "skill_bundle_sha256": request.skill_bundle_sha256,
        "skill_delivery_sha256": request.skill_delivery_sha256,
        "initial_receipt_name": None if initial_receipt is None else initial_receipt[0],
        "initial_receipt_digest": None if initial_receipt is None else initial_receipt[1],
    }
    if quality_admission is not None:
        payload["quality_admission"] = list(quality_admission)
    return payload


def _require_broker_initial(
    controller: LifecycleController, verification: SeatWorkspaceVerification,
    request: object,
    initial_receipt: tuple[str, str] | None,
) -> None:
    if not request.resume:
        if initial_receipt is not None:
            raise ProviderRequestError("initial broker diagnostic cannot reuse initial evidence")
        return
    if (not isinstance(initial_receipt, tuple) or len(initial_receipt) != 2
            or any(not isinstance(value, str) for value in initial_receipt)):
        raise ProviderRequestError("broker diagnostic resume needs initial evidence")
    try:
        prior = read_evidence(controller, "brokered-provider-diagnostic", *initial_receipt)
    except Exception as error:
        raise ProviderRequestError("broker diagnostic initial evidence is unavailable") from error
    expected = {
        "schema_version": "fanout-brokered-diagnostic-v1",
        "run_id": request.run_id, "task_id": request.task_id,
        "target_id": request.target_id, "seat_id": request.seat_id,
        "inputs_digest": request.inputs_digest,
        "profile_digest": request.profile.digest,
        "route": (f"initial:assigned:{request.session_id}"
                  if request.executor_id == "claude" else "initial:unassigned"),
        "session_id": request.session_id,
        "valid": True, "reason": "ok",
        "process_status": ProcessStatus.EXIT.value, "exit_code": 0,
    }
    wire_model = _MEASURED_WIRE_MODELS.get(
        (request.executor_id, request.profile.cli_version, request.profile.requested_model),
        request.profile.requested_model,
    )
    if (set(prior) != _BROKER_RESULT_FIELDS
            or any(prior.get(key) != value for key, value in expected.items())
            or prior["broker_model"] not in {wire_model, request.profile.requested_model}
            or prior["observed_model"] not in (None, request.profile.requested_model)
            or any(not isinstance(prior[name], str)
                   or re.fullmatch(r"[0-9a-f]{64}", prior[name]) is None
                   for name in ("boundary_receipt_digest", "stdout_sha256", "stderr_sha256"))
            or not isinstance(prior["effective_output_tokens"], list)
            or not prior["effective_output_tokens"]
            or len(prior["effective_output_tokens"]) > 32
            or any(type(value) is not int or not 1 <= value <= 8192
                   for value in prior["effective_output_tokens"])
            or type(prior["refused_agy_auxiliary_requests"]) is not int
            or prior["refused_agy_auxiliary_requests"] < 0):
        raise ProviderRequestError("broker diagnostic initial evidence differs from resume")
    digest = prior["boundary_receipt_digest"]
    try:
        boundary = read_evidence(controller, _CATEGORY, f"{digest}.json", digest)
    except Exception as error:
        raise ProviderRequestError("broker diagnostic initial boundary is unavailable") from error
    _require_initial_boundary_chain(controller, verification, request, boundary)
    prior_admission = read_evidence(controller, _BROKER_ADMISSION_CATEGORY,
        boundary["broker_admission_name"], boundary["broker_admission_digest"])
    expected_model = (request.profile.requested_model
        if prior_admission.get("quality_admission") is not None else wire_model)
    if prior["broker_model"] != expected_model:
        raise ProviderRequestError("initial native permit model differs from its exact route")


def _require_initial_boundary_chain(
    controller: LifecycleController, verification: SeatWorkspaceVerification,
    request: object, boundary: dict[str, object],
) -> None:
    initial = replace(
        request, resume=False,
        session_id=request.session_id if request.executor_id == "claude" else None,
    )
    workspace = Path(request.cwd)
    info = workspace.lstat()
    identity = hashlib.sha256(canonical_json((
        request.run_id, request.task_id, request.target_id, request.seat_id,
    ))).hexdigest()
    state = controller.root / "native-seat-state" / identity
    expected = {
        "schema_version": "fanout-native-seat-boundary-v1",
        "controller_id": controller.controller_id,
        "run_id": request.run_id, "task_id": request.task_id,
        "target_id": request.target_id, "seat_id": request.seat_id,
        "inputs_digest": request.inputs_digest,
        "workspace": str(workspace), "workspace_device": info.st_dev,
        "workspace_inode": info.st_ino,
        "workspace_receipt": verification.evidence_digest,
        "cwd": str(workspace), "profile_digest": request.profile.digest,
        "route": _route(initial), "state_root": str(state),
    }
    has_grant = (request.staged_skill_root is not None
                 or request.staged_skill_admission is not None)
    expected_fields = (_BROKER_BOUNDARY_FIELDS | {"staged_skill_grant"}
                       if has_grant else _BROKER_BOUNDARY_FIELDS)
    if set(boundary) != expected_fields:
        raise ProviderRequestError("broker diagnostic initial boundary lacks current admission chain")
    if boundary["workspace_receipt"] != verification.evidence_digest:
        raise ProviderRequestError("broker diagnostic initial boundary workspace evidence differs")
    if (any(boundary.get(key) != value for key, value in expected.items())
            or any(not isinstance(boundary[name], str)
                   or re.fullmatch(r"[0-9a-f]{64}", boundary[name]) is None
                   for name in ("workspace_receipt", "executable_digest",
                                "command_digest", "environment_digest",
                                "profile_sha256", "broker_permit_sha256",
                                "broker_admission_digest"))
            or not isinstance(boundary["broker_admission_name"], str)
            or type(boundary["broker_port"]) is not int
            or not 1 <= boundary["broker_port"] <= 65535
            or not isinstance(boundary["broker_binding_id"], str)
            or not boundary["broker_binding_id"]
            or not isinstance(boundary["denied_paths"], list)
            or not boundary["denied_paths"]
            or any(not isinstance(value, str) or not Path(value).is_absolute()
                   for value in boundary["denied_paths"])
            or not isinstance(boundary["resolved_argv"], list)
            or not boundary["resolved_argv"]
                or any(not isinstance(value, str) for value in boundary["resolved_argv"])):
        raise ProviderRequestError("broker diagnostic initial boundary lacks current admission chain")
    denied = tuple(Path(value) for value in boundary["denied_paths"])
    grant = _staged_skill_grant(initial, denied, workspace, controller.root)
    if grant != boundary.get("staged_skill_grant"):
        raise ProviderRequestError("broker diagnostic initial skill grant differs")
    admission_ref = (boundary["broker_admission_name"],
                     boundary["broker_admission_digest"])
    try:
        admission = read_evidence(controller, _BROKER_ADMISSION_CATEGORY, *admission_ref)
    except Exception as error:
        raise ProviderRequestError("broker diagnostic initial boundary admission is unavailable") from error
    binding = (boundary["broker_port"], boundary["broker_binding_id"],
               boundary["broker_permit_sha256"], "")
    quality = admission.get("quality_admission")
    if quality is not None:
        from .diagnostic import validate_quality_prior_command
        validate_quality_prior_command(controller, request, tuple(quality))
    if admission != _broker_admission_payload(
        initial, verification.evidence_digest, boundary["command_digest"], binding, None,
        quality_admission=quality,
    ):
        raise ProviderRequestError("broker diagnostic initial boundary admission differs")
    executable_name = request.profile.initial_argv[0]
    if request.executor_id == "agy" and executable_name == "agy":
        executable_name = str(_AGY_DIRECT_BINARY)
    executable = _executable(executable_name)
    if (boundary["executable"] != str(executable)
            or boundary["executable_digest"] != _hash_file(executable)
            or boundary["resolved_argv"][0] != str(executable)):
        raise ProviderRequestError("broker diagnostic initial boundary command differs")
    policy = _policy(
        workspace, state, executable, denied,
        None if grant is None else Path(grant["root"]),
        execution_class="read-only", broker_port=boundary["broker_port"],
    )
    profile_sha256 = hashlib.sha256(policy).hexdigest()
    profile_path = controller.root / "native-profiles" / f"{profile_sha256}.sb"
    if (boundary["profile_sha256"] != profile_sha256
            or boundary["profile_path"] != str(profile_path)
            or _hash_file(profile_path, mode=0o400) != profile_sha256):
        raise ProviderRequestError("broker diagnostic initial boundary policy differs")


def _validate_broker_admission(
    controller: LifecycleController, verification: SeatWorkspaceVerification,
    request: object, command: ProcessCommand | None,
    listener: BrokerListener, binding: tuple[int, str, str, str],
    admission: tuple[str, str] | None,
    *, command_digest: str | None = None,
) -> None:
    if (not isinstance(admission, tuple) or len(admission) != 2
            or any(not isinstance(value, str) for value in admission)):
        raise ProviderRequestError("broker diagnostic admission is invalid")
    payload = read_evidence(controller, _BROKER_ADMISSION_CATEGORY, *admission)
    quality = payload.get("quality_admission")
    _check_canonical_broker_command(
        request, command, listener, command_digest=command_digest,
        controller=controller, quality_admission=quality,
    )
    initial_name = payload.get("initial_receipt_name")
    initial_digest = payload.get("initial_receipt_digest")
    initial = (initial_name, initial_digest) if initial_name is not None else None
    if request.resume != (initial is not None):
        raise ProviderRequestError("broker diagnostic admission changed route")
    _require_broker_initial(controller, verification, request, initial)
    if payload != _broker_admission_payload(
        request, verification.evidence_digest,
        _command_digest(command) if command is not None else command_digest,
        binding, initial,
        quality_admission=quality,
    ):
        raise ProviderRequestError("broker diagnostic admission changed association")


def _broker_environment(executor_id: str, binding: tuple[int, str, str, str]) -> dict[str, str]:
    port, _, _, placeholder = binding
    endpoint = f"http://127.0.0.1:{port}"
    environment = {"LLM_FANOUT_BROKER_URL": endpoint}
    if executor_id == "claude":
        environment.update({"ANTHROPIC_BASE_URL": endpoint,
                            "ANTHROPIC_API_KEY": placeholder})
    elif executor_id == "agy":
        environment.update({"GOOGLE_GEMINI_BASE_URL": endpoint,
                            "GEMINI_API_KEY": placeholder})
    return environment


def _environment_digest(environment: tuple[tuple[str, str], ...]) -> str:
    return hashlib.sha256(canonical_json(dict(environment))).hexdigest()


def _executable(value: str) -> Path:
    found = shutil.which(value)
    if found is None:
        raise ValueError("provider executable is unavailable")
    path = Path(found).resolve(strict=True)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise ValueError("provider executable is unsafe")
    with path.open("rb") as source:
        if source.read(2) == b"#!":
            raise ValueError("native boundary requires a direct executable, not a script wrapper")
    return path


def _hash_file(path: Path, *, mode: int | None = None) -> str:
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode)
            or (mode is not None and (stat.S_IMODE(info.st_mode) != mode
                                      or info.st_nlink != 1))):
        raise ValueError("boundary file is unsafe")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reject_aliases(root: Path) -> None:
    if not root.is_absolute() or root != root.resolve(strict=True) or not root.is_dir():
        raise ProviderRequestError("native boundary allowed root has an alias")
    for base, directories, files in os.walk(root, followlinks=False):
        for name in (*directories, *files):
            path = Path(base) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                raise ProviderRequestError("native boundary allowed tree contains an alias")


def _denied_roots(other: Sequence[Path], owner: Sequence[Path], workspace: Path) -> tuple[Path, ...]:
    if not owner:
        raise ValueError("owner roots are required")
    roots = []
    for raw in (*other, *owner):
        path = Path(raw).resolve(strict=True)
        if not path.is_dir() or path == workspace or path in workspace.parents:
            raise ValueError("denied root overlaps the workspace")
        roots.append(path)
    return tuple(sorted(set(roots)))


def _private_state(root: Path, run_id: str, task_id: str, target_id: str,
                   seat_id: str) -> Path:
    identity = hashlib.sha256(canonical_json((run_id, task_id, target_id,
                                              seat_id))).hexdigest()
    parent = root / "native-seat-state"
    parent.mkdir(mode=0o700, exist_ok=True)
    state = parent / identity
    state.mkdir(mode=0o700, exist_ok=True)
    for path in (parent, state):
        info = path.lstat()
        if (path.is_symlink() or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("native seat state is unsafe")
    for path in (state / "home", state / "tmp"):
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if (path.is_symlink() or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("native seat private HOME or tmp is unsafe")
    return state


def _profile_path(controller_root: Path, digest: str) -> Path:
    directory = controller_root / "native-profiles"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (directory.is_symlink() or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("native profile directory is unsafe")
    return directory / f"{digest}.sb"


def _write_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if _hash_file(path, mode=0o400) != hashlib.sha256(content).hexdigest():
            raise ValueError("native profile changed")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    try:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short native profile write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _staged_skill_grant(request: object, denied_roots: Sequence[Path],
                        workspace: Path, controller_root: Path) -> dict[str, object] | None:
    root = getattr(request, "staged_skill_root", None)
    admission = getattr(request, "staged_skill_admission", None)
    if root is None and admission is None:
        return None
    if root is None or not isinstance(admission, SkillAdmission):
        raise ProviderRequestError("staged skill read grant lacks authenticated admission")
    root = Path(root)
    if (root != admission.staged_root or root.name != "ready"
            or root == workspace or root in workspace.parents or workspace in root.parents
            or root == controller_root or root in controller_root.parents
            or controller_root in root.parents
            or any(root == denied or root in denied.parents or denied in root.parents
                   for denied in denied_roots)):
        raise ProviderRequestError("staged skill read grant changed association")
    if (not admission.engine_delivered or admission.task_id != request.task_id
            or admission.seat_id != request.seat_id
            or admission.provider != request.executor_id):
        raise ProviderRequestError("staged skill read grant changed association")
    verify_staged_admission(admission)
    _reject_aliases(root)
    if root.lstat().st_uid != os.getuid():
        raise ProviderRequestError("staged skill read grant is not privately owned")
    expected = {skill.name: skill for skill in admission.skills}
    observed: set[str] = set()
    for event in admission.engine_evidence:
        if (not isinstance(event, SkillLoadEvidence) or event.kind != "engine"
                or event.truncated or event.skill not in expected
                or event.skill in observed or event.provider != admission.provider
                or event.session_id != admission.session_id
                or event.seat_id != admission.seat_id
                or event.tree_hash != expected[event.skill].tree_hash
                or event.source != expected[event.skill].source.identity):
            raise ProviderRequestError("staged skill engine evidence changed")
        observed.add(event.skill)
    if observed != set(expected):
        raise ProviderRequestError("staged skill engine evidence is incomplete")
    delivery = canonical_json([
        {
            "kind": event.kind, "provider": event.provider, "seat_id": event.seat_id,
            "session_id": event.session_id, "skill": event.skill,
            "source": event.source, "tree_hash": event.tree_hash,
            "truncated": event.truncated,
        }
        for event in admission.engine_evidence
    ])
    delivery_digest = hashlib.sha256(delivery).hexdigest()
    if delivery_digest != request.skill_delivery_sha256:
        raise ProviderRequestError("staged skill delivery digest changed")
    if not request.skill_bundle_sha256:
        raise ProviderRequestError("staged skill bundle digest is required")
    bundle = canonical_json({
        "schema_version": "fanout-seat-skill-bundle-v1",
        "skills": admission.manifest_dict()["skills"],
    })
    if hashlib.sha256(bundle).hexdigest() != request.skill_bundle_sha256:
        raise ProviderRequestError("staged skill bundle digest changed")
    info = root.lstat()
    return {
        "root": str(root), "device": info.st_dev, "inode": info.st_ino,
        "manifest_sha256": hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
        "admission_session_id": admission.session_id,
        "delivery_sha256": delivery_digest,
        "skill_bundle_sha256": request.skill_bundle_sha256,
    }


def _policy(workspace: Path, state: Path, executable: Path,
            denied: Sequence[Path], staged_skill_root: Path | None = None,
            *, execution_class: str, broker_port: int | None = None) -> bytes:
    if execution_class not in {"read-only", "repo-write"}:
        raise ValueError("native policy execution class is invalid")
    read_roots = (*_RUNTIME_ROOTS, str(workspace), str(state))
    if staged_skill_root is not None:
        read_roots += (str(staged_skill_root),)
    read_files = (*_RUNTIME_FILES, str(executable))
    ancestors = {str(parent) for path in (*read_roots, *read_files)
                 for parent in Path(path).parents}
    read_filters = [*(f"(subpath {json.dumps(path)})" for path in read_roots),
                    *(f"(literal {json.dumps(path)})" for path in read_files)]
    metadata_filters = [*read_filters,
                        *(f"(literal {json.dumps(path)})" for path in sorted(ancestors))]
    controller_root = state.parent.parent
    sealed_ancestors = sorted({path for allowed in (workspace, state)
                               for path in allowed.parents
                               if path == controller_root or controller_root in path.parents})
    rules = [
        "(version 1)", "(allow default)",
        "(deny file-read* (require-not (require-any " + " ".join(metadata_filters) + ")))",
        "(deny file-write*)",
    ]
    rules.extend(f"(deny file-read-data (literal {json.dumps(str(path))}))"
                 for path in sealed_ancestors)
    write_roots = (workspace, state) if execution_class == "repo-write" else (state,)
    rules.extend(f"(allow file-write* (subpath {json.dumps(str(path))}))"
                 for path in write_roots)
    rules.append('(allow file-write* (literal "/dev/null"))')
    for path in denied:
        literal = json.dumps(str(path))
        rules.extend((f"(deny file-read* (subpath {literal}))",
                      f"(deny file-write* (subpath {literal}))"))
    rules.extend(("(deny file-link)", "(deny network-bind)",
                  "(deny network-inbound)", "(deny network-outbound)",
                  "(deny mach-lookup)", "(deny ipc-posix-shm)"))
    if broker_port is not None:
        rules.append(f'(allow network-outbound (remote tcp "localhost:{broker_port}"))')
    return ("\n".join(rules) + "\n").encode("utf-8")
