"""Host-side source closure shared by private probes and certificate gates."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import ssl
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


_SOURCE_ROOT = Path(__file__).resolve().parents[3]
_CORE = (
    "broker.py",
    "container_broker.py",
    "container_boundary.py",
    "container_credentials.py",
    "container_routes.py",
    "container_source.py",
    "container_transfer.py",
    "container_transfer_support.py",
    "container_transfer_transport.py",
    "container_upstream.py",
)
_IMAGE = ("Dockerfile", "mise.toml", "mise.lock", "relay.py")
_SKILLS = ("llm-fanout-plan", "llm-fanout-execute")
_CERTIFIER = "scripts/fanout_container_certify.py"
_DOCTOR = "scripts/fanout_route_doctor.py"
_HOST_RUNTIME = ("mise.toml", "mise.lock")
_MAX_FILES = 256
_MAX_FILE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_EXECUTABLE_BYTES = 128 * 1024 * 1024


class SourceClosureError(ValueError):
    """A required source is absent, unsafe, or outside the closure budget."""


class RuntimeIdentityError(ValueError):
    """The host Python executable cannot be attested safely."""


@dataclass(frozen=True, slots=True)
class HostRuntimeIdentity:
    executable_sha256: str
    python_version: str
    openssl_version: str


@dataclass(frozen=True, slots=True)
class ReleaseSourceBinding:
    """Versioned release identity; historical runtime receipts keep their encoding."""

    runtime_sha256: str
    memory_sha256: str

    def __post_init__(self) -> None:
        for value in (self.runtime_sha256, self.memory_sha256):
            if (not isinstance(value, str) or len(value) != 64
                    or any(char not in "0123456789abcdef" for char in value)):
                raise SourceClosureError("release component digest is invalid")

    def _components(self) -> dict[str, object]:
        return {"schema_version": "fanout-release-source-v1",
                "components": {"runtime": self.runtime_sha256,
                               "memory": self.memory_sha256}}

    @property
    def digest(self) -> str:
        raw = json.dumps(self._components(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {**self._components(), "digest": self.digest}

    @classmethod
    def from_dict(cls, value: object) -> ReleaseSourceBinding:
        if (not isinstance(value, dict) or set(value) != {"schema_version", "components", "digest"}
                or value.get("schema_version") != "fanout-release-source-v1"
                or not isinstance(value.get("components"), dict)
                or set(value["components"]) != {"runtime", "memory"}):
            raise SourceClosureError("release source binding is invalid")
        result = cls(value["components"]["runtime"], value["components"]["memory"])
        if value["digest"] != result.digest:
            raise SourceClosureError("release source binding digest differs")
        return result


def _source_paths(root: Path) -> list[str]:
    selected = {
        path.relative_to(root).as_posix()
        for pattern in ("shared/lib/fanout/*.py", "scripts/fanout_*.py")
        for path in root.glob(pattern)
    }
    required = {
        "capabilities.toml",
        "scripts/lib/checks.py",
        *_HOST_RUNTIME,
        *(f"shared/lib/fanout/{name}" for name in _CORE),
        *(f"components/fanout-container/{name}" for name in _IMAGE),
        _CERTIFIER,
        _DOCTOR,
    }
    skill_root = root / "shared/skills"
    skills = {path.name: path for path in skill_root.glob("llm-fanout-*")}
    if not set(_SKILLS) <= skills.keys():
        raise SourceClosureError("required fanout skill is missing")
    for name, skill in skills.items():
        if skill.is_symlink() or not skill.is_dir():
            raise SourceClosureError(f"fanout skill is not a regular directory: {name}")
        metadata = {f"shared/skills/{name}/SKILL.md",
                    f"shared/skills/{name}/agents/openai.yaml"}
        scripts = list(skill.glob("scripts/*.py"))
        if len(scripts) != 1:
            raise SourceClosureError(f"required fanout skill needs exactly one script: {name}")
        required.update(metadata)
        selected.update(metadata)
        selected.add(scripts[0].relative_to(root).as_posix())
    missing = sorted(name for name in required if not (root / name).exists()
                     and not (root / name).is_symlink())
    if missing:
        raise SourceClosureError(f"required source is missing: {missing[0]}")
    selected.update(required)
    if len(selected) > _MAX_FILES:
        raise SourceClosureError("source closure file budget exceeded")
    return sorted(selected)


def _read_source(root: Path, name: str, budget: int) -> bytes:
    directory = root
    for component in Path(name).parts[:-1]:
        directory /= component
        try:
            info = directory.lstat()
        except OSError as error:
            raise SourceClosureError(f"source directory is missing: {directory}") from error
        if not stat.S_ISDIR(info.st_mode):
            raise SourceClosureError(f"source directory is unsafe or a symlink: {directory}")
    path = root / name
    try:
        before = path.lstat()
    except OSError as error:
        raise SourceClosureError(f"source is missing: {path}") from error
    if not stat.S_ISREG(before.st_mode):
        raise SourceClosureError(f"source is not a regular file or is a symlink: {path}")
    if before.st_size > min(_MAX_FILE_BYTES, budget):
        raise SourceClosureError(f"source byte budget exceeded: {path}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or opened.st_size > min(_MAX_FILE_BYTES, budget)):
                raise SourceClosureError(f"source changed or exceeded budget: {path}")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(min(_MAX_FILE_BYTES, budget) + 1)
            after = os.fstat(fd)
            if (len(data) != opened.st_size or len(data) > min(_MAX_FILE_BYTES, budget)
                    or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)):
                raise SourceClosureError(f"source changed or exceeded budget: {path}")
            return data
        finally:
            os.close(fd)
    except OSError as error:
        raise SourceClosureError(f"source could not be read safely: {path}") from error


def _source_root(root: Path | None) -> Path:
    source_root = _SOURCE_ROOT if root is None else Path(root)
    if source_root.is_symlink() or not source_root.is_dir():
        raise SourceClosureError("source root is missing or unsafe")
    hidden = source_root / ".runtime"
    if hidden.exists() or hidden.is_symlink():
        if hidden.is_symlink() or not hidden.is_dir():
            raise SourceClosureError("source root is missing or unsafe")
        return hidden
    return source_root


def runtime_source_digest(root: Path | None = None) -> str:
    """Hash sorted relative path/byte pairs from the bounded runtime closure."""
    source_root = _source_root(root)
    _validate_active_skill(source_root)
    return _digest_paths(source_root, _source_paths(source_root))


def _validate_active_skill(root: Path) -> None:
    """Bind direct-copy entry/metadata duplicates to the canonical release bytes."""
    entries = {"llm-fanout-plan": "scripts/plan.py",
               "llm-fanout-execute": "scripts/execute.py"}
    active_root = root.parent if root.name == ".runtime" else root
    active = [name for name, entry in entries.items()
              if (active_root / entry).exists() or (active_root / entry).is_symlink()]
    metadata = (active_root / "SKILL.md", active_root / "agents/openai.yaml")
    if (root.name != ".runtime" and not active
            and not any(path.exists() or path.is_symlink() for path in metadata)):
        return
    if len(active) != 1:
        raise SourceClosureError("active fanout skill entry is missing or ambiguous")
    name = active[0]
    for relative in ("SKILL.md", "agents/openai.yaml", entries[name]):
        canonical = f"shared/skills/{name}/{relative}"
        if _read_source(active_root, relative, _MAX_TOTAL_BYTES) != _read_source(root, canonical, _MAX_TOTAL_BYTES):
            raise SourceClosureError(f"active fanout skill differs from canonical source: {relative}")


def _digest_paths(source_root: Path, names: list[str], *,
                  selected_controller: bytes | None = None) -> str:
    digest = hashlib.sha256()
    remaining = _MAX_TOTAL_BYTES
    for name in names:
        data = _read_source(source_root, name, remaining)
        if (name == "components/memory/memoryctl.py" and selected_controller is not None
                and data != selected_controller):
            raise SourceClosureError("memory controller manifest changed during selection")
        remaining -= len(data)
        relative = name.encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _memory_paths(root: Path, controller: bytes | None = None) -> list[str]:
    if controller is None:
        controller = _read_source(root, "components/memory/memoryctl.py", _MAX_TOTAL_BYTES)
    try:
        tree = ast.parse(controller)
        declarations = [node for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "CONTROLLER_FILES"
                                for target in node.targets)]
        writes = [node for node in ast.walk(tree) if isinstance(node, ast.Name)
                  and node.id == "CONTROLLER_FILES" and isinstance(node.ctx, ast.Store)]
        if len(declarations) != 1 or len(declarations[0].targets) != 1 or len(writes) != 1:
            raise ValueError
        names = ast.literal_eval(declarations[0].value)
    except (ValueError, SyntaxError, TypeError, RecursionError) as error:
        raise SourceClosureError("memory controller manifest must be one bounded literal") from error
    if (not isinstance(names, tuple) or not names or len(names) > _MAX_FILES
            or any(not isinstance(name, str) or not name or len(name) > 128
                   or Path(name).name != name or name in {".", ".."}
                   or "\\" in name or "\x00" in name for name in names)
            or len(set(names)) != len(names)
            or not {"memoryctl.py", "memory_ingress.py", "mise.toml", "mise.lock",
                    "provenance.json"} <= set(names)):
        raise SourceClosureError("memory controller manifest is unsafe or incomplete")
    return sorted({*(f"components/memory/{name}" for name in names),
                   "components/memory/upstreams.toml"})


def release_source_binding(root: Path | None = None) -> ReleaseSourceBinding:
    """Attest the named runtime and managed-memory closures without importing memory code."""
    source_root = _source_root(root)
    runtime = runtime_source_digest(source_root)
    controller = _read_source(source_root, "components/memory/memoryctl.py", _MAX_TOTAL_BYTES)
    memory = _digest_paths(source_root, _memory_paths(source_root, controller),
                           selected_controller=controller)
    return ReleaseSourceBinding(runtime, memory)


def host_runtime_identity() -> HostRuntimeIdentity:
    """Attest the resolved host interpreter independently of source bytes."""
    try:
        path = Path(sys.executable).resolve(strict=True)
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o111
                or before.st_size > _MAX_EXECUTABLE_BYTES):
            raise RuntimeIdentityError("host executable is not regular or exceeds budget")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or opened.st_size > _MAX_EXECUTABLE_BYTES):
                raise RuntimeIdentityError("host executable changed or exceeds budget")
            digest = hashlib.sha256()
            total = 0
            while chunk := os.read(fd, min(64 * 1024, _MAX_EXECUTABLE_BYTES - total + 1)):
                total += len(chunk)
                if total > _MAX_EXECUTABLE_BYTES:
                    raise RuntimeIdentityError("host executable exceeds budget")
                digest.update(chunk)
            after = os.fstat(fd)
            if (total != opened.st_size or (after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns) != (opened.st_dev, opened.st_ino, opened.st_size,
                    opened.st_mtime_ns)):
                raise RuntimeIdentityError("host executable changed during attestation")
        finally:
            os.close(fd)
    except (OSError, RuntimeError):
        raise RuntimeIdentityError("host executable cannot be attested") from None
    return HostRuntimeIdentity(digest.hexdigest(), sys.version, ssl.OPENSSL_VERSION)
