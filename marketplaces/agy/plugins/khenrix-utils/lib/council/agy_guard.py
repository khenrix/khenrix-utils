"""Private native policy for a legacy read-only agy council seat.

The guard has no fanout-controller dependency so the bundled council package can
use it in every rendered plugin. It controls agy's tools, not operating-system
filesystem access; the council's throwaway worktree remains a separate layer.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


_BINARY = Path.home() / ".local/libexec/agy-bin"
_VERSION = "1.2.11"
_HOOK_SOURCE = Path(__file__).with_name("agy_readonly_hook.py")
_HOOK_SHA256 = "dcdc68550144c90ab0f106291eb13deed2cab8665729cae1a167ef6f27fc7df5"
_FILES = (
    "agy_readonly_hook.py", "policy.json",
    ".gemini/config/hooks.json", ".gemini/antigravity-cli/settings.json",
)
_INHERITED_ENV = (
    "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TERM", "TMPDIR", "TZ",
    "LLM_COUNCIL_DEPTH", "LLM_FORGE_DEPTH", "PYTHONDONTWRITEBYTECODE",
)
_WORKSPACE_CUSTOMIZATIONS = (".agents", ".agent", "_agents", "_agent")


class AgyGuardError(RuntimeError):
    """A read-only agy seat cannot be launched under the declared policy."""


@dataclass(frozen=True)
class AgyGuard:
    home: Path
    workspace: Path
    binary: Path
    adc: Path
    binary_sha256: str
    files_sha256: tuple[tuple[str, str], ...]
    environment: Mapping[str, str]
    environment_sha256: str
    argv: tuple[str, ...]
    model: str | None


def _private_directory(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o700)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise AgyGuardError(f"agy guard directory is unsafe: {path}")


def _private_file(path: Path, mode: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != mode
                or info.st_size > 128 * 1024 * 1024):
            raise AgyGuardError(f"agy guard file is unsafe: {path}")
        return os.read(descriptor, info.st_size + 1)
    finally:
        os.close(descriptor)


def _write_private_file(path: Path, content: bytes, mode: int = 0o400) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _expected_files(home: Path, workspace: Path) -> dict[str, bytes]:
    source = _HOOK_SOURCE.read_bytes()
    if hashlib.sha256(source).hexdigest() != _HOOK_SHA256:
        raise AgyGuardError("agy guard script source changed")
    hook = _json({
        "council-readonly-guard": {"PreToolUse": [{
            "matcher": "*", "hooks": [{
                "type": "command",
                "command": f"{shlex.quote(sys.executable)} {shlex.quote(str(home / 'agy_readonly_hook.py'))}",
                "timeout": 10,
            }],
        }]},
    })
    # agy 1.2.10 rewrites compact 0400 settings during --new-project. This
    # measured pretty 0600 form stays byte-stable, including on exact resume.
    settings = (json.dumps({"permissions": {
        "allow": [f"read_file({workspace})"],
        "deny": ["write_file(*)", "command(*)", "unsandboxed(*)", "mcp(*)", "execute_url(*)"],
    }}, indent=2) + "\n").encode("utf-8")
    return dict(zip(_FILES, (
        source, _json({"workspace": str(workspace)}), hook, settings,
    ), strict=True))


def _adc_path() -> Path:
    if os.environ.get("AGY_ADC_AUTH") != "true":
        raise AgyGuardError("agy ADC mode is unavailable")
    path = Path.home() / ".config/gcloud/application_default_credentials.json"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise AgyGuardError("agy default ADC path is unsafe")
    finally:
        os.close(descriptor)
    return path


def _binary_hash(binary: Path) -> str:
    descriptor = os.open(binary, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or not info.st_mode & stat.S_IXUSR
                or info.st_mode & 0o022):
            raise AgyGuardError("agy direct binary is unsafe")
        hasher = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            hasher.update(chunk)
        return hasher.hexdigest()
    finally:
        os.close(descriptor)


def _reject_workspace_customizations(workspace: Path) -> None:
    """agy discovers project hooks from cwd through the repository root."""
    current = workspace
    while True:
        for name in _WORKSPACE_CUSTOMIZATIONS:
            try:
                (current / name).lstat()
            except FileNotFoundError:
                continue
            raise AgyGuardError(f"agy project customization is unsafe: {current / name}")
        try:
            (current / ".git").lstat()
        except FileNotFoundError:
            pass
        else:
            return
        if current.parent == current:
            return
        current = current.parent


def issue(workdir: Path | str, workspace: Path | str,
          base_env: Mapping[str, str] | None = None, *,
          argv: list[str] | tuple[str, ...] | None = None,
          model: str | None = None,
          register_home: Callable[[Path], None] | None = None,
          unregister_home: Callable[[Path], None] | None = None) -> AgyGuard:
    """Prepare one guarded HOME; a failure leaves no child-launchable policy."""
    home = None
    created = False
    try:
        workspace = Path(workspace)
        resolved = workspace.resolve(strict=True)
        if workspace != resolved or not resolved.is_dir():
            raise AgyGuardError("agy workspace must be a real absolute directory")
        _reject_workspace_customizations(resolved)
        adc = _adc_path()
        binary = _BINARY
        binary_sha256 = _binary_hash(binary)
        if argv is not None and (not isinstance(argv, (list, tuple)) or not argv
                                 or any(not isinstance(arg, str) for arg in argv)):
            raise AgyGuardError("agy invocation is invalid")
        bound_argv = (str(binary), *argv[1:]) if argv is not None else ()
        version = subprocess.run(
            (str(binary), "--version"), capture_output=True, timeout=10,
            env={"PATH": os.defpath, "HOME": str(Path.home())}, check=False,
        )
        if version.returncode != 0 or version.stdout.strip() != _VERSION.encode():
            raise AgyGuardError("agy direct binary version is not characterized")
        Path(workdir).mkdir(parents=True, exist_ok=True)
        root = Path(workdir).resolve(strict=True) / "agy-guards"
        root.mkdir(mode=0o700, exist_ok=True)
        _private_directory(root)
        home = root / f"seat-{secrets.token_hex(16)}"
        try:
            home.lstat()
        except FileNotFoundError:
            pass
        else:
            raise AgyGuardError("agy guard HOME already exists")
        # The signal handler can now safely see the exact path before mkdir.
        if register_home is not None:
            register_home(home)
        home.mkdir(mode=0o700)
        created = True
        _private_directory(home)
        for relative in (".config", ".gemini", ".gemini/config", ".gemini/antigravity-cli"):
            _private_directory(home / relative, create=True)
        expected = _expected_files(home, resolved)
        for name, content in expected.items():
            _write_private_file(home / name, content,
                                mode=0o600 if name.endswith("settings.json") else 0o400)
        source = os.environ if base_env is None else base_env
        environment = {name: source[name] for name in _INHERITED_ENV if name in source}
        environment.update({
            "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
            "GOOGLE_APPLICATION_CREDENTIALS": str(adc), "AGY_ADC_AUTH": "true",
            "GOOGLE_CLOUD_LOCATION": "eu", "GOOGLE_CLOUD_REGION": "eu",
        })
        guard = AgyGuard(
            home, resolved, binary, adc, binary_sha256,
            tuple((name, hashlib.sha256(content).hexdigest())
                  for name, content in sorted(expected.items())), environment,
            hashlib.sha256(_json(environment)).hexdigest(),
            bound_argv, model,
        )
        validate(guard)
        return guard
    except (AgyGuardError, OSError, TypeError, ValueError, subprocess.TimeoutExpired) as error:
        if home is not None:
            if created:
                cleanup_home(home)
            if unregister_home is not None and (not created or not home.exists()):
                unregister_home(home)
        raise AgyGuardError("agy read-only guard could not be issued") from error


def validate(guard: AgyGuard) -> None:
    """Recheck the exact policy and auth boundary before each process attempt."""
    try:
        if not isinstance(guard, AgyGuard):
            raise ValueError("missing guard")
        _private_directory(guard.home.parent)
        _private_directory(guard.home)
        for relative in (".config", ".gemini", ".gemini/config", ".gemini/antigravity-cli"):
            _private_directory(guard.home / relative)
        if guard.workspace.resolve(strict=True) != guard.workspace:
            raise ValueError("workspace changed")
        _reject_workspace_customizations(guard.workspace)
        if guard.adc != _adc_path() or guard.binary != _BINARY:
            raise ValueError("auth or binary path changed")
        if _binary_hash(guard.binary) != guard.binary_sha256:
            raise ValueError("binary changed")
        expected = _expected_files(guard.home, guard.workspace)
        if tuple((name, hashlib.sha256(content).hexdigest())
                 for name, content in sorted(expected.items())) != guard.files_sha256:
            raise ValueError("guard source changed")
        for name, content in expected.items():
            mode = 0o600 if name.endswith("settings.json") else 0o400
            if _private_file(guard.home / name, mode) != content:
                raise ValueError(f"guard policy changed: {name}")
        if (hashlib.sha256(_json(dict(guard.environment))).hexdigest() != guard.environment_sha256
                or guard.environment.get("HOME") != str(guard.home)
                or guard.environment.get("GOOGLE_APPLICATION_CREDENTIALS") != str(guard.adc)
                or guard.environment.get("GOOGLE_CLOUD_LOCATION") != "eu"
                or guard.environment.get("GOOGLE_CLOUD_REGION") != "eu"):
            raise ValueError("guard environment changed")
    except (AgyGuardError, OSError, TypeError, ValueError) as error:
        raise AgyGuardError("agy read-only guard validation failed") from error


def cleanup_home(home: Path) -> None:
    """Remove only one owned guard HOME, never a symlink or its target."""
    try:
        if home.name.startswith("seat-") and home.parent.name == "agy-guards":
            _private_directory(home.parent)
            _private_directory(home)
            shutil.rmtree(home)
            try:
                home.parent.rmdir()
            except OSError:
                pass
    except (OSError, AgyGuardError):
        pass


def cleanup(guard: AgyGuard) -> None:
    cleanup_home(guard.home)
