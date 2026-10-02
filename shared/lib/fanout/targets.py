"""Portable target identities bound to exact local Git checkouts."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .errors import PlanValidationError, RepositoryValidationError
from .repo import RepositoryBaseline


_OID = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")
_PATH = re.compile(r"[A-Za-z0-9._/-]+\Z")
_TICKET = re.compile(r"[A-Z][A-Z0-9]*-[1-9][0-9]*\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


def normalize_origin(url: str) -> str:
    """Accept only credential-free SSH or HTTPS repository identities."""
    if not isinstance(url, str):
        raise RepositoryValidationError("origin must be a URL")
    ssh = re.fullmatch(r"git@([A-Za-z0-9.-]+):([A-Za-z0-9._/-]+)", url)
    if ssh:
        host, path = ssh.groups()
    else:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise RepositoryValidationError("origin URL is invalid") from error
        if (
            parsed.scheme != "https" or parsed.username is not None
            or parsed.password is not None or port is not None
            or parsed.query or parsed.fragment or not parsed.hostname
        ):
            raise RepositoryValidationError("origin must be credential-free SSH or HTTPS")
        host, path = parsed.hostname, parsed.path.removeprefix("/")
    if not _HOST.fullmatch(host) or ".." in host or not _PATH.fullmatch(path):
        raise RepositoryValidationError("origin host or path contains unsupported characters")
    path = path.removesuffix(".git")
    parts = path.split("/")
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
        raise RepositoryValidationError("origin path is missing or unsafe")
    return host.lower() + "/" + "/".join(parts)


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """Portable task target, independent of a machine's checkout paths."""

    id: str
    repository: str
    ticket_key: str
    branch_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _ID.fullmatch(self.id):
            raise PlanValidationError("target id must be a simple identity")
        if not isinstance(self.repository, str):
            raise PlanValidationError("target repository must be normalized")
        try:
            normalized = normalize_origin("https://" + self.repository)
        except RepositoryValidationError as error:
            raise PlanValidationError("target repository must be normalized") from error
        if normalized != self.repository:
            raise PlanValidationError("target repository must be normalized")
        if not isinstance(self.ticket_key, str) or not _TICKET.fullmatch(self.ticket_key):
            raise PlanValidationError("target ticket key is invalid")
        if not isinstance(self.branch_ref, str) or not self.branch_ref.startswith("refs/heads/"):
            raise PlanValidationError("target branch must be a full refs/heads ref")
        token = re.compile(r"(?<![A-Za-z0-9])" + re.escape(self.ticket_key) + r"(?![A-Za-z0-9])")
        if not token.search(self.branch_ref):
            raise PlanValidationError("target branch must contain the exact ticket token")
        result = _git_run(None, "check-ref-format", self.branch_ref)
        if result.returncode:
            raise PlanValidationError("target branch is not a valid Git ref")

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id, "repository": self.repository,
            "ticket_key": self.ticket_key, "branch_ref": self.branch_ref,
        }

    @classmethod
    def from_dict(cls, data: object) -> TargetSpec:
        values = _fields(
            data, {"id", "repository", "ticket_key", "branch_ref"},
            "target spec", error_type=PlanValidationError,
        )
        return cls(**values)


@dataclass(frozen=True, slots=True)
class TargetBinding:
    """A checked-out repository with captured physical and Git identity."""

    spec: TargetSpec
    root: Path
    common_dir: Path
    common_device: int
    common_inode: int
    base_oid: str
    branch_oid: str | None
    baseline_sha256: str | None
    head_ref: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.spec, TargetSpec):
            raise RepositoryValidationError("target binding spec is invalid")
        if any(not isinstance(path, Path) or not path.is_absolute() for path in (self.root, self.common_dir)):
            raise RepositoryValidationError("target binding paths must be absolute")
        if any(type(value) is not int or value < 0 for value in (self.common_device, self.common_inode)):
            raise RepositoryValidationError("target common directory identity is invalid")
        if not isinstance(self.base_oid, str) or not _OID.fullmatch(self.base_oid):
            raise RepositoryValidationError("target base OID must be SHA-1")
        if self.branch_oid is not None and (not isinstance(self.branch_oid, str) or not _OID.fullmatch(self.branch_oid)):
            raise RepositoryValidationError("target branch OID must be SHA-1")
        if self.baseline_sha256 is not None and (
            not isinstance(self.baseline_sha256, str) or not _DIGEST.fullmatch(self.baseline_sha256)
        ):
            raise RepositoryValidationError("target baseline digest is invalid")
        if self.head_ref is not None and (
            not isinstance(self.head_ref, str) or not self.head_ref.startswith("refs/heads/")
            or _git_run(None, "check-ref-format", self.head_ref).returncode
        ):
            raise RepositoryValidationError("target direct HEAD ref is invalid")

    def to_dict(self) -> dict[str, object]:
        if self.baseline_sha256 is None:
            raise RepositoryValidationError("target baseline must be captured before serialization")
        return {
            "spec": self.spec.to_dict(), "root": str(self.root),
            "common_dir": str(self.common_dir), "common_device": self.common_device,
            "common_inode": self.common_inode, "base_oid": self.base_oid,
            "branch_oid": self.branch_oid, "baseline_sha256": self.baseline_sha256,
            "head_ref": self.head_ref,
        }

    @classmethod
    def from_dict(cls, data: object) -> TargetBinding:
        values = _fields(data, {
            "spec", "root", "common_dir", "common_device", "common_inode",
            "base_oid", "branch_oid", "baseline_sha256", "head_ref",
        }, "target binding")
        if not isinstance(values["root"], str) or not isinstance(values["common_dir"], str):
            raise RepositoryValidationError("target binding paths must be strings")
        if values["baseline_sha256"] is None:
            raise RepositoryValidationError("serialized target baseline digest is required")
        return cls(
            spec=TargetSpec.from_dict(values["spec"]),
            root=Path(values["root"]), common_dir=Path(values["common_dir"]),
            common_device=values["common_device"], common_inode=values["common_inode"],
            base_oid=values["base_oid"], branch_oid=values["branch_oid"],
            baseline_sha256=values["baseline_sha256"], head_ref=values["head_ref"],
        )


def resolve_target(spec: TargetSpec, root: Path) -> TargetBinding:
    """Bind a portable target to one local SHA-1 checkout without network access."""
    if not isinstance(spec, TargetSpec):
        raise RepositoryValidationError("target spec is invalid")
    root = Path(root)
    _reject_symlink_path_components(root)
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise RepositoryValidationError("target root does not exist") from error
    top = Path(_git_local(root, "rev-parse", "--show-toplevel"))
    if top.resolve(strict=True) != root:
        raise RepositoryValidationError("target root must be the Git top level")
    if _git_local(root, "rev-parse", "--show-object-format") != "sha1":
        raise RepositoryValidationError("target Git object format must be SHA-1")
    if _git_local_optional(root, "config", "--includes", "--get-regexp", r"^url\..*\.insteadof$") is not None:
        raise RepositoryValidationError("target origin has an effective URL rewrite")
    urls = _git_local_optional(root, "config", "--local", "--no-includes", "--get-all", "remote.origin.url")
    if urls is None:
        raise RepositoryValidationError("target origin is missing")
    values = urls.split("\n")
    if len(values) != 1 or normalize_origin(values[0]) != spec.repository:
        raise RepositoryValidationError("target origin differs from admitted repository identity")
    effective = _git_local_optional(root, "config", "--includes", "--get-all", "remote.origin.url")
    if effective is None or effective.split("\n") != values:
        raise RepositoryValidationError("target origin has multiple effective identities")
    common = Path(_git_local(root, "rev-parse", "--git-common-dir"))
    try:
        common = (common if common.is_absolute() else root / common).resolve(strict=True)
        info = common.stat()
    except OSError as error:
        raise RepositoryValidationError("target common Git directory is missing") from error
    base = _git_local(root, "rev-parse", "HEAD")
    head_ref = _git_local_optional(root, "symbolic-ref", "-q", "--no-recurse", "HEAD")
    if _git_local_optional(root, "symbolic-ref", "-q", "--no-recurse", spec.branch_ref) is not None:
        raise RepositoryValidationError("target ticket branch is a symbolic ref")
    branch = _git_local_optional(root, "rev-parse", "-q", "--verify", spec.branch_ref)
    return TargetBinding(spec, root, common, info.st_dev, info.st_ino, base, branch, None, head_ref)


def bind_captured_baseline(binding: TargetBinding, baseline: RepositoryBaseline) -> TargetBinding:
    if not isinstance(binding, TargetBinding) or not isinstance(baseline, RepositoryBaseline):
        raise RepositoryValidationError("target baseline binding inputs are invalid")
    if baseline.repository.resolve(strict=True) != binding.root or baseline.head != binding.base_oid:
        raise RepositoryValidationError("target baseline belongs to another checkout or HEAD")
    return replace(binding, baseline_sha256=baseline.digest)


def validate_target_bindings(
    bindings: Mapping[str, TargetBinding], *, writable_ids: set[str],
) -> None:
    """Reject aliases and concurrent writers to one repository identity."""
    if not isinstance(bindings, Mapping) or not isinstance(writable_ids, set):
        raise RepositoryValidationError("target bindings or writable ids are invalid")
    if not writable_ids <= bindings.keys():
        raise RepositoryValidationError("writable target is missing a binding")
    roots: set[Path] = set()
    common_paths: set[Path] = set()
    common_inodes: set[tuple[int, int]] = set()
    writable_origins: set[str] = set()
    for target_id, binding in bindings.items():
        if not isinstance(binding, TargetBinding) or target_id != binding.spec.id:
            raise RepositoryValidationError("target binding id differs from its spec")
        _reject_symlink_path_components(binding.root)
        try:
            root = binding.root.resolve(strict=True)
            common = binding.common_dir.resolve(strict=True)
            info = common.stat()
        except OSError as error:
            raise RepositoryValidationError("target checkout or common directory is missing") from error
        if (info.st_dev, info.st_ino) != (binding.common_device, binding.common_inode):
            raise RepositoryValidationError("target common directory identity changed")
        if root in roots:
            raise RepositoryValidationError("duplicate target root")
        if common in common_paths or (info.st_dev, info.st_ino) in common_inodes:
            raise RepositoryValidationError("duplicate target common directory")
        roots.add(root)
        common_paths.add(common)
        common_inodes.add((info.st_dev, info.st_ino))
        if target_id in writable_ids:
            if binding.head_ref is None:
                raise RepositoryValidationError("writable target requires a direct symbolic HEAD")
            if binding.spec.repository in writable_origins:
                raise RepositoryValidationError("duplicate writable repository identity")
            writable_origins.add(binding.spec.repository)


def _fields(
    data: object, names: set[str], label: str,
    *, error_type: type[Exception] = RepositoryValidationError,
) -> dict[str, object]:
    if not isinstance(data, dict) or set(data) != names:
        raise error_type(f"{label} must contain exactly its canonical fields")
    return dict(data)


def _reject_symlink_path_components(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise RepositoryValidationError("target root must be an absolute, direct path")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise RepositoryValidationError("target root has a symlink component")


def _git_environment() -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_") and name != "HOME"}
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_COUNT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    })
    return environment


def _git_run(root: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    argv = ("git", "-C", str(root), *args) if root is not None else ("git", *args)
    return subprocess.run(argv, capture_output=True, text=True, env=_git_environment(), check=False)


def _git_local(root: Path, *args: str) -> str:
    result = _git_run(root, *args)
    if result.returncode:
        raise RepositoryValidationError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_local_optional(root: Path, *args: str) -> str | None:
    result = _git_run(root, *args)
    if result.returncode == 1 and not result.stderr.strip():
        return None
    if result.returncode:
        raise RepositoryValidationError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout.removesuffix("\n")
