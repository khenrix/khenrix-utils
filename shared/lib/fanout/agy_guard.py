"""Controller-authenticated native read-only guard for agy seats."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .artifacts import canonical_json
from .controller import (
    LifecycleController, assert_controller, controller_directory, read_evidence, write_evidence,
)
from .errors import LifecycleError, ProviderRequestError
from .process import build_child_environment, default_claude_adc_path
from .repo import SeatWorkspaceVerification, validate_seat_workspace


_CATEGORY = "agy-readonly-guard"
_BINARY = Path.home() / ".local" / "libexec" / "agy-bin"
_HOOK_SOURCE = Path(__file__).with_name("agy_readonly_hook.py")
_HOOK_SHA256 = "d7b49ab6020b96e91e07e34e9949132b61f8fe3a9d637a7413115279526d69da"
_FILES = (
    "fanout_guard.py",
    "policy.json",
    ".gemini/config/hooks.json",
    ".gemini/antigravity-cli/settings.json",
)
_PROJECT_CUSTOMIZATION_ROOTS = (".agents", ".agent", "_agents", "_agent")


@dataclass(frozen=True, slots=True)
class AgyReadOnlyGuard:
    """One immutable receipt binding a seat workspace to a private agy HOME."""

    controller: LifecycleController = field(repr=False, compare=False)
    verification: SeatWorkspaceVerification = field(repr=False, compare=False)
    profile_sha256: str
    home: Path
    binary: Path
    adc_path: Path
    receipt_name: str
    receipt_sha256: str

    @property
    def environment(self) -> Mapping[str, str]:
        return {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "GOOGLE_APPLICATION_CREDENTIALS": str(self.adc_path),
            "AGY_ADC_AUTH": "true",
            "GOOGLE_CLOUD_LOCATION": "eu",
            "GOOGLE_CLOUD_REGION": "eu",
        }


def issue_agy_readonly_guard(
    controller: LifecycleController,
    verification: SeatWorkspaceVerification,
    profile: object,
) -> AgyReadOnlyGuard:
    """Create an idempotent private policy and its authenticated controller receipt."""
    try:
        validate_seat_workspace(controller, verification)
        _reject_project_customizations(
            verification.workspace.root, verification.workspace.root,
        )
        if (
            getattr(profile, "executor_id", None) != "agy"
            or getattr(profile, "execution_class", None) != "read-only"
            or not isinstance(getattr(profile, "digest", None), str)
        ):
            raise ValueError("profile is not a pinned agy read-only profile")
        if os.environ.get("AGY_ADC_AUTH") != "true":
            raise ValueError("agy ADC mode is not enabled")
        adc = default_claude_adc_path()
        if adc is None:
            raise ValueError("private default ADC path is unavailable")
        binary_sha256 = _hash_private_file(_BINARY, executable=True)
        version = subprocess.run(
            (str(_BINARY), "--version"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=build_child_environment(), timeout=10, check=False,
        )
        if (
            version.returncode != 0
            or re.fullmatch(rb"[0-9]+\.[0-9]+\.[0-9]+\n?", version.stdout) is None
            or version.stdout.strip().decode("ascii") != profile.cli_version
        ):
            raise ValueError("direct agy binary version differs from profile")
        source = _HOOK_SOURCE.read_bytes()
        if hashlib.sha256(source).hexdigest() != _HOOK_SHA256:
            raise ValueError("guard script source changed")
        workspace = verification.workspace.root
        home = _guard_home(controller, verification, profile.digest)
        _ensure_private_directory(controller.root / "agy-homes")
        prior_receipts = _authenticated_receipts_for_home(controller, home)
        if prior_receipts:
            _ensure_private_directory(home, create=False)
            created_home = False
        else:
            created_home = _ensure_private_directory(home)
        for directory in (
            home / ".config", home / ".gemini", home / ".gemini/config",
            home / ".gemini/antigravity-cli",
        ):
            _ensure_private_directory(directory, create=created_home)
        if created_home:
            policy = canonical_json({"workspace": str(workspace)})
            hook = canonical_json({
                "fanout-readonly-guard": {"PreToolUse": [{
                    "matcher": "*", "hooks": [{
                        "type": "command",
                        "command": f"{shlex.quote(sys.executable)} {shlex.quote(str(home / 'fanout_guard.py'))}",
                        "timeout": 10,
                    }],
                }]},
            })
            settings = (json.dumps({"permissions": {
                "allow": [f"read_file({workspace})"],
                "deny": ["write_file(*)", "command(*)", "unsandboxed(*)", "mcp(*)", "execute_url(*)"],
            }}, indent=2) + "\n").encode("utf-8")
            files = dict(zip(_FILES, (source, policy, hook, settings), strict=True))
            for file_name, content in files.items():
                _write_or_verify(
                    home / file_name, content,
                    mode=0o600 if file_name.endswith("settings.json") else 0o400,
                )
        else:
            files = {
                file_name: _read_private_file(
                    home / file_name,
                    mode=0o600 if file_name.endswith("settings.json") else 0o400,
                )
                for file_name in _FILES
            }
        payload = _receipt_payload(
            controller, verification, profile.digest, home, adc, binary_sha256, files,
        )
        name, digest = (
            write_evidence(controller, _CATEGORY, payload) if created_home
            else _find_authenticated_receipt(prior_receipts, payload)
        )
        guard = AgyReadOnlyGuard(
            controller, verification, profile.digest, home, _BINARY, adc, name, digest,
        )
        validate_agy_readonly_guard(guard, workspace, profile)
        return guard
    except ProviderRequestError:
        raise
    except Exception as error:
        raise ProviderRequestError("agy read-only guard could not be issued") from error


def validate_agy_readonly_guard(guard: AgyReadOnlyGuard, cwd: Path | str,
                                profile: object) -> None:
    """Fail closed if any policy byte, path, seat, auth path, or binary changed."""
    try:
        if not isinstance(guard, AgyReadOnlyGuard):
            raise ValueError("guard is absent")
        assert_controller(guard.controller)
        validate_seat_workspace(guard.controller, guard.verification)
        if (
            Path(cwd) != guard.verification.workspace.root
            or getattr(profile, "executor_id", None) != "agy"
            or getattr(profile, "execution_class", None) != "read-only"
            or getattr(profile, "digest", None) != guard.profile_sha256
            or guard.binary != _BINARY
            or guard.home.parent != guard.controller.root / "agy-homes"
            or guard.adc_path != default_claude_adc_path()
            or os.environ.get("AGY_ADC_AUTH") != "true"
        ):
            raise ValueError("guard binding changed")
        _reject_project_customizations(guard.verification.workspace.root, Path(cwd))
        _ensure_private_directory(guard.controller.root / "agy-homes", create=False)
        for directory in (
            guard.home, guard.home / ".config", guard.home / ".gemini",
            guard.home / ".gemini/config", guard.home / ".gemini/antigravity-cli",
        ):
            _ensure_private_directory(directory, create=False)
        files = {
            name: _read_private_file(
                guard.home / name, mode=0o600 if name.endswith("settings.json") else 0o400,
            )
            for name in _FILES
        }
        binary_sha256 = _hash_private_file(guard.binary, executable=True)
        persisted = read_evidence(guard.controller, _CATEGORY,
                                  guard.receipt_name, guard.receipt_sha256)
        expected = _receipt_payload(
            guard.controller, guard.verification, guard.profile_sha256,
            guard.home, guard.adc_path, binary_sha256, files,
        )
        if persisted != expected:
            raise ValueError("guard receipt changed")
        expected_script = _HOOK_SOURCE.read_bytes()
        if hashlib.sha256(expected_script).hexdigest() != _HOOK_SHA256:
            raise ValueError("guard script source changed")
        if files["fanout_guard.py"] != expected_script:
            raise ValueError("guard script changed")
        expected_policy = canonical_json({"workspace": str(guard.verification.workspace.root)})
        if files["policy.json"] != expected_policy:
            raise ValueError("guard workspace policy changed")
        expected_settings = (json.dumps({"permissions": {
            "allow": [f"read_file({guard.verification.workspace.root})"],
            "deny": ["write_file(*)", "command(*)", "unsandboxed(*)", "mcp(*)", "execute_url(*)"],
        }}, indent=2) + "\n").encode("utf-8")
        if files[".gemini/antigravity-cli/settings.json"] != expected_settings:
            raise ValueError("guard native settings changed")
    except ProviderRequestError:
        raise
    except Exception as error:
        raise ProviderRequestError("agy read-only guard validation failed") from error


def agy_readonly_guard_receipt_sha256(
    controller: LifecycleController,
    verification: SeatWorkspaceVerification,
    profile: object,
) -> str | None:
    """Recover a guard binding for no-spend status without probing a changed binary."""
    try:
        validate_seat_workspace(controller, verification)
        if (getattr(profile, "executor_id", None) != "agy"
                or getattr(profile, "execution_class", None) != "read-only"
                or not isinstance(getattr(profile, "digest", None), str)):
            raise ValueError("profile is not a pinned agy read-only profile")
        home = _guard_home(controller, verification, profile.digest)
        receipts = _authenticated_receipts_for_home(controller, home)
        if not receipts:
            return None
        expected = {
            "schema_version": "fanout-agy-readonly-guard-v1",
            "controller_id": controller.controller_id,
            "seat_id": verification.workspace.seat_id,
            "workspace": str(verification.workspace.root),
            "workspace_evidence_sha256": verification.evidence_digest,
            "profile_sha256": profile.digest,
            "home": str(home),
            "binary": str(_BINARY),
        }
        if len(receipts) != 1 or any(
            any(payload.get(key) != value for key, value in expected.items())
            for _, _, payload in receipts
        ):
            raise ValueError("agy guard receipt changed association")
        return receipts[0][1]
    except ProviderRequestError:
        raise
    except Exception as error:
        raise ProviderRequestError("agy read-only guard receipt could not be recovered") from error


def _guard_home(
    controller: LifecycleController,
    verification: SeatWorkspaceVerification,
    profile_sha256: str,
) -> Path:
    identity = hashlib.sha256(canonical_json({
        "controller_id": controller.controller_id,
        "profile_sha256": profile_sha256,
        "seat_id": verification.workspace.seat_id,
        "workspace_evidence_sha256": verification.evidence_digest,
    })).hexdigest()[:32]
    return controller.root / "agy-homes" / identity


def _receipt_payload(
    controller: LifecycleController, verification: SeatWorkspaceVerification,
    profile_sha256: str, home: Path, adc: Path, binary_sha256: str,
    files: Mapping[str, bytes],
) -> dict[str, object]:
    return {
        "schema_version": "fanout-agy-readonly-guard-v1",
        "controller_id": controller.controller_id,
        "seat_id": verification.workspace.seat_id,
        "workspace": str(verification.workspace.root),
        "workspace_evidence_sha256": verification.evidence_digest,
        "profile_sha256": profile_sha256,
        "home": str(home),
        "binary": str(_BINARY),
        "binary_sha256": binary_sha256,
        "adc_path": str(adc),
        "region": "eu",
        "files": {name: hashlib.sha256(content).hexdigest()
                  for name, content in sorted(files.items())},
    }


def _authenticated_receipts_for_home(
    controller: LifecycleController, home: Path,
) -> list[tuple[str, str, dict[str, object]]]:
    matches = []
    for path in controller_directory(controller, "receipts").iterdir():
        name = path.name
        if re.fullmatch(r"[0-9a-f]{64}\.json", name) is None:
            continue
        digest = name[:-5]
        try:
            persisted = read_evidence(controller, _CATEGORY, name, digest)
        except LifecycleError:
            continue
        if persisted.get("home") == str(home):
            matches.append((name, digest, persisted))
    return matches


def _find_authenticated_receipt(
    receipts: list[tuple[str, str, dict[str, object]]], payload: dict[str, object],
) -> tuple[str, str]:
    for name, digest, persisted in receipts:
        if persisted == payload:
            return name, digest
    raise ValueError("existing guard has no authenticated receipt")


def _reject_project_customizations(repository: Path, cwd: Path) -> None:
    """Guarded cwd is the repo root, so upward discovery has one directory."""
    if cwd != repository or cwd.is_symlink():
        raise ValueError("guarded agy cwd must be the real repository root")
    for name in _PROJECT_CUSTOMIZATION_ROOTS:
        try:
            (repository / name).lstat()
        except FileNotFoundError:
            continue
        # These roots can carry hooks, MCPs, and plugin manifests; reject any
        # file, directory, or symlink under a discovered name.
        raise ValueError("agy project customization root is present")


def _ensure_private_directory(path: Path, *, create: bool = True) -> bool:
    created = False
    if create:
        try:
            path.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("guard directory is unsafe")
    return created


def _write_or_verify(path: Path, content: bytes, *, mode: int = 0o400) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    except FileExistsError:
        if _read_private_file(path, mode=mode) != content:
            raise ValueError("existing guard file changed")
        return
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        raise


def _read_private_file(path: Path, *, mode: int = 0o400) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != mode
                or info.st_size > 128 * 1024):
            raise ValueError("guard file is unsafe")
        return os.read(descriptor, info.st_size + 1)
    finally:
        os.close(descriptor)


def _hash_private_file(path: Path, *, executable: bool = False) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or (executable and not info.st_mode & stat.S_IXUSR)):
            raise ValueError("guard binary is unsafe")
        hasher = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            hasher.update(chunk)
        return hasher.hexdigest()
    finally:
        os.close(descriptor)
