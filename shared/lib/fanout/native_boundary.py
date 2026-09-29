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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Sequence

from .artifacts import ArtifactRef, canonical_json
from .controller import LifecycleController, read_evidence, write_evidence
from .errors import ProviderRequestError
from .process import ProcessCommand, ProcessStatus, build_child_environment, run_command
from .repo import SeatWorkspaceVerification, validate_seat_workspace
from .skills import SkillAdmission, SkillLoadEvidence, verify_staged_admission


_CATEGORY = "native-seat-boundary"
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


def issue_native_boundary(
    controller: LifecycleController, verification: SeatWorkspaceVerification, *,
    run_id: str, task_id: str, target_id: str, inputs_digest: str,
    request: object, command: ProcessCommand, other_target_roots: Sequence[Path],
    denied_owner_roots: Sequence[Path],
) -> NativeSeatBoundary:
    """Issue an exact, authenticated descriptor after checking all allowed trees."""
    try:
        validate_seat_workspace(controller, verification)
        _check_identity(run_id, task_id, target_id, inputs_digest, request, verification)
        workspace = verification.workspace.root
        _reject_aliases(workspace)
        _check_command(request, command)
        denied = _denied_roots(other_target_roots, denied_owner_roots, workspace)
        grant = _staged_skill_grant(request, denied, workspace, controller.root)
        profile = request.profile
        executable = _executable(profile.resume_argv[0] if request.resume else profile.initial_argv[0])
        if _executable(command.argv[0]) != executable:
            raise ValueError("native command executable differs from the profile")
        executable_digest = _hash_file(executable)
        resolved_argv = (str(executable), *command.argv[1:])
        route = _route(request)
        state = _private_state(controller.root, run_id, task_id, target_id,
                               verification.workspace.seat_id)
        frozen_environment = _native_environment(state, request, command)
        environment_digest = _environment_digest(frozen_environment)
        policy = _policy(workspace, state, executable, denied,
                         None if grant is None else Path(grant["root"]))
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
    })).hexdigest()


def _native_environment(state: Path, request: object,
                        command: ProcessCommand) -> tuple[tuple[str, str], ...]:
    child = build_child_environment(
        dict(os.environ),
        overrides={"HOME": str(state / "home"), "TMPDIR": str(state / "tmp")},
        max_depth=command.max_depth,
    )
    if request.executor_id == "claude":
        child["CLAUDE_CODE_TMPDIR"] = str(state / "tmp")
    return tuple(sorted(child.items()))


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
    bundle = canonical_json({
        "schema_version": "fanout-seat-skill-bundle-v1",
        "skills": admission.manifest_dict()["skills"],
    })
    if (request.skill_bundle_sha256
            and hashlib.sha256(bundle).hexdigest() != request.skill_bundle_sha256):
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
            denied: Sequence[Path], staged_skill_root: Path | None = None) -> bytes:
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
    rules.extend(f"(allow file-write* (subpath {json.dumps(str(path))}))"
                 for path in (workspace, state))
    rules.append('(allow file-write* (literal "/dev/null"))')
    for path in denied:
        literal = json.dumps(str(path))
        rules.extend((f"(deny file-read* (subpath {literal}))",
                      f"(deny file-write* (subpath {literal}))"))
    rules.extend(("(deny file-link)", "(deny network-outbound)"))
    return ("\n".join(rules) + "\n").encode("utf-8")
