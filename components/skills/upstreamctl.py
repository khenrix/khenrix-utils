#!/usr/bin/env python3
"""Inspect and record reviewed upstreams for composed Khenrix skills.

Each manifest beneath a declared shared source root stores immutable reviewed commits and a
hash of only the relevant upstream paths.  This tool distinguishes a repository
moving from those paths changing. It never merges upstream instructions into a
skill; the declared license file is the one exception and is kept as an exact
vendored copy.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import selectors
import signal
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


GIT_TIMEOUT_SECONDS = 30
MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024
REPORT_SCHEMA = "khenrix-upstreams/v2"
DEFAULT_CONSUMER = "agentic-setup"


SOURCE_FIELDS = {
    "name",
    "repository",
    "ref",
    "commit",
    "paths",
    "path_tree_hash",
    "license",
    "upstream_license_path",
    "local_license_path",
    "license_path_policy",
    "license_optional",
    "adaptation",
    "verbatim_bundle",
    "consumers",
    "content_owner",
    "delivery_owner",
    "fetch_url",
    "web_url",
    "relationship",
    "affected_capability_ids",
    "update_mode",
    "review_commands",
    "watch",
    "tag_pattern",
    "package_name",
    "package_version",
    "package_integrity",
    "local_contract",
}


class UpstreamError(RuntimeError):
    """An invalid manifest or Git operation that cannot be safely guessed."""


@dataclass(frozen=True)
class VerbatimOverlay:
    """One reviewed text insertion reapplied to a verbatim upstream file."""

    path: str
    after: str
    insert: str
    reason: str


@dataclass(frozen=True)
class VerbatimBundle:
    """A group of sibling skills copied exactly from one upstream tree."""

    upstream_root: str
    members: tuple[str, ...]
    control_paths: tuple[str, ...]
    additive_files: tuple[str, ...]
    overlays: tuple[VerbatimOverlay, ...]


@dataclass(frozen=True)
class Source:
    skill: str
    manifest: Path
    name: str
    repository: str
    ref: str
    commit: str
    paths: tuple[str, ...]
    path_tree_hash: str
    license: str
    upstream_license_path: str
    local_license_path: str
    adaptation: str
    verbatim_bundle: VerbatimBundle | None
    consumers: tuple[str, ...] = (DEFAULT_CONSUMER,)
    content_owner: str = "khenrix"
    delivery_owner: str = "khenrix"
    fetch_url: str = ""
    web_url: str = ""
    relationship: str = "adapted"
    affected_capability_ids: tuple[str, ...] = ()
    update_mode: str = "manual"
    review_commands: tuple[str, ...] = ()
    repo_root: Path = Path(".")
    license_optional: bool = False
    watch: str = "ref"
    tag_pattern: str = ""
    package_name: str = ""
    package_version: str = ""
    package_integrity: str = ""
    local_contract_paths: tuple[str, ...] = ()
    local_contract_hash: str = ""
    required_text: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class BundleFile:
    local_path: str
    mode: int
    content: bytes


def notice_path(source: Source) -> Path:
    return source.manifest.parent / "THIRD_PARTY_NOTICES.md"


def license_copy_path(source: Source) -> Path:
    if source.license_optional:
        raise UpstreamError(f"{source.name}: optional license has no local copy")
    return source.manifest.parent / source.local_license_path


def validated_license_copy(source: Source) -> tuple[Path | None, bytes]:
    """Read the exact vendored license copy without following a repository symlink."""

    if source.license_optional:
        return None, b""
    path = license_copy_path(source)
    current = source.manifest.parent
    for part in PurePosixPath(source.local_license_path).parts:
        current = current / part
        if current.is_symlink():
            raise UpstreamError(f"{source.skill}: unsafe symlink in local license path {current}")
    if not path.is_file():
        raise UpstreamError(f"{source.skill}: missing local license copy at {path}")
    try:
        return path, path.read_bytes()
    except OSError as error:
        raise UpstreamError(f"cannot read {path}: {error}") from error


def validated_notice(source: Source) -> tuple[Path, str]:
    """Read the notice and require exact pin and license-copy references."""

    path = notice_path(source)
    if not path.is_file() or path.is_symlink():
        raise UpstreamError(f"{source.skill}: missing safe third-party notice at {path}")
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as error:
        raise UpstreamError(f"cannot read {path}: {error}") from error
    occurrences = text.count(source.commit)
    if occurrences != 1:
        raise UpstreamError(
            f"{path} must mention {source.name}'s pinned commit {source.commit} exactly once; "
            f"found {occurrences}"
        )
    if source.license_optional:
        marker = "license_optional = true"
        if text.count(marker) != 1:
            raise UpstreamError(
                f"{path} must mention {source.name}'s explicit {marker!r} exactly once"
            )
    else:
        license_occurrences = text.count(source.local_license_path)
        if license_occurrences != 1:
            raise UpstreamError(
                f"{path} must mention {source.name}'s local license "
                f"{source.local_license_path} exactly once; found {license_occurrences}"
            )
    return path, text


def git_environment() -> dict[str, str]:
    """Return a credential-safe, noninteractive environment for bounded Git calls."""

    environment = os.environ.copy()
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GCM_INTERACTIVE": "Never",
            "LC_ALL": "C",
        }
    )
    return environment


def checked_bounded_git_output(
    returncode: int, stdout: bytes, stderr: bytes, arguments: list[str]
) -> bytes:
    operation = arguments[0] if arguments else "command"
    if returncode:
        detail = redact_report_error(
            (stderr or stdout).decode("utf-8", "replace").strip()
        )
        raise UpstreamError(f"git {operation} failed: {detail[:4096]}")
    return stdout


def kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def bounded_git_process(
    arguments: list[str], *, cwd: Path | None = None
) -> tuple[int, bytes, bytes]:
    operation = arguments[0] if arguments else "command"
    try:
        process = subprocess.Popen(
            ["git", *arguments],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_environment(),
            start_new_session=True,
        )
    except OSError as error:
        raise UpstreamError(f"cannot start git {operation}: {error}") from error
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    total = 0
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                kill_process_group(process)
                raise UpstreamError(
                    f"git {operation} timed out after {GIT_TIMEOUT_SECONDS} seconds"
                )
            events = selector.select(remaining)
            if not events:
                continue
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > MAX_GIT_OUTPUT_BYTES:
                    kill_process_group(process)
                    raise UpstreamError(
                        f"git {operation} output exceeded the "
                        f"{MAX_GIT_OUTPUT_BYTES}-byte limit"
                    )
                chunks[key.data].append(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            kill_process_group(process)
            raise UpstreamError(
                f"git {operation} timed out after {GIT_TIMEOUT_SECONDS} seconds"
            )
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            kill_process_group(process)
            raise UpstreamError(
                f"git {operation} timed out after {GIT_TIMEOUT_SECONDS} seconds"
            ) from error
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return returncode, b"".join(chunks["stdout"]), b"".join(chunks["stderr"])


def run_git_bounded(arguments: list[str], *, cwd: Path | None = None) -> str:
    returncode, stdout, stderr = bounded_git_process(arguments, cwd=cwd)
    return checked_bounded_git_output(returncode, stdout, stderr, arguments).decode(
        "utf-8", "replace"
    )


def run_git_bytes_bounded(arguments: list[str], *, cwd: Path | None = None) -> bytes:
    returncode, stdout, stderr = bounded_git_process(arguments, cwd=cwd)
    return checked_bounded_git_output(returncode, stdout, stderr, arguments)


def run_git(arguments: list[str], *, cwd: Path | None = None) -> str:
    """Run Git with the historical interactive behavior used by maintainer commands."""

    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise UpstreamError(f"git {' '.join(arguments[:3])} failed: {detail}")
    return completed.stdout


def run_git_bytes(arguments: list[str], *, cwd: Path | None = None) -> bytes:
    """Binary counterpart of the historical maintainer-command Git runner."""

    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).decode("utf-8", "replace").strip()
        raise UpstreamError(f"git {' '.join(arguments[:3])} failed: {detail}")
    return completed.stdout


def safe_relative_path(raw: object, *, label: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise UpstreamError(f"{label} must be a non-empty relative POSIX path")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise UpstreamError(f"{label} may not escape its root: {raw!r}")
    return path.as_posix()


def selected_paths_cover(paths: tuple[str, ...], candidate: str) -> bool:
    wanted = PurePosixPath(candidate)
    return any(
        wanted == PurePosixPath(selected) or PurePosixPath(selected) in wanted.parents
        for selected in paths
    )


def parse_verbatim_bundle(
    raw: object,
    *,
    manifest: Path,
    source_name: str,
    source_skill: str,
    selected_paths: tuple[str, ...],
    local_license_path: str,
) -> VerbatimBundle | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise UpstreamError(
            f"{manifest} source {source_name} verbatim_bundle must be a table"
        )
    required = {"upstream_root", "members", "control_paths"}
    allowed = required | {"additive_files", "overlays"}
    missing = sorted(required - set(raw))
    extra = sorted(set(raw) - allowed)
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unknown {extra}")
        raise UpstreamError(
            f"{manifest} source {source_name} has invalid verbatim_bundle: "
            + "; ".join(detail)
        )

    upstream_root = safe_relative_path(
        raw["upstream_root"],
        label=f"{manifest} source {source_name} verbatim_bundle.upstream_root",
    )
    if not selected_paths_cover(selected_paths, upstream_root):
        raise UpstreamError(
            f"{manifest} source {source_name} verbatim bundle root {upstream_root!r} "
            "is outside its reviewed paths"
        )
    raw_members = raw["members"]
    if not isinstance(raw_members, list) or not raw_members:
        raise UpstreamError(
            f"{manifest} source {source_name} verbatim_bundle.members must be non-empty"
        )
    members: list[str] = []
    for member in raw_members:
        if not isinstance(member, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]*", member
        ):
            raise UpstreamError(
                f"{manifest} source {source_name} has invalid verbatim member {member!r}"
            )
        members.append(member)
    if len(set(members)) != len(members):
        raise UpstreamError(
            f"{manifest} source {source_name} has duplicate verbatim bundle members"
        )
    if source_skill not in members:
        raise UpstreamError(
            f"{manifest} source {source_name} verbatim bundle must include its owning "
            f"skill {source_skill!r}"
        )

    raw_controls = raw["control_paths"]
    if not isinstance(raw_controls, list):
        raise UpstreamError(
            f"{manifest} source {source_name} verbatim_bundle.control_paths must be a list"
        )
    controls = tuple(
        safe_relative_path(
            item,
            label=f"{manifest} source {source_name} verbatim bundle control path",
        )
        for item in raw_controls
    )
    if len(set(controls)) != len(controls):
        raise UpstreamError(
            f"{manifest} source {source_name} has duplicate verbatim control paths"
        )
    if any(PurePosixPath(path).parts[0] not in members for path in controls):
        raise UpstreamError(
            f"{manifest} source {source_name} verbatim control paths must live under "
            "declared members"
        )
    required_controls = {
        f"{source_skill}/upstreams.toml",
        f"{source_skill}/THIRD_PARTY_NOTICES.md",
        f"{source_skill}/{local_license_path}",
    }
    if not required_controls.issubset(controls):
        raise UpstreamError(
            f"{manifest} source {source_name} verbatim control_paths must include "
            f"{sorted(required_controls)}"
        )
    raw_additive = raw.get("additive_files", [])
    if not isinstance(raw_additive, list):
        raise UpstreamError(
            f"{manifest} source {source_name} verbatim_bundle.additive_files must be a list"
        )
    additive_files = tuple(
        safe_relative_path(
            item,
            label=f"{manifest} source {source_name} verbatim additive file",
        )
        for item in raw_additive
    )
    if len(set(additive_files)) != len(additive_files):
        raise UpstreamError(
            f"{manifest} source {source_name} has duplicate additive files"
        )
    if any(PurePosixPath(path).parts[0] not in members for path in additive_files):
        raise UpstreamError(
            f"{manifest} source {source_name} verbatim additive files must live under "
            "declared members"
        )
    overlap = set(additive_files).intersection(controls)
    if overlap:
        raise UpstreamError(
            f"{manifest} source {source_name} additive files overlap control paths: "
            f"{sorted(overlap)}"
        )
    raw_overlays = raw.get("overlays", [])
    if not isinstance(raw_overlays, list):
        raise UpstreamError(
            f"{manifest} source {source_name} verbatim_bundle.overlays must be a list"
        )
    overlays: list[VerbatimOverlay] = []
    overlay_paths: set[str] = set()
    for index, raw_overlay in enumerate(raw_overlays):
        if not isinstance(raw_overlay, dict) or set(raw_overlay) != {
            "path",
            "after",
            "insert",
            "reason",
        }:
            raise UpstreamError(
                f"{manifest} source {source_name} verbatim overlay {index} must contain "
                "exactly path, after, insert, and reason"
            )
        path = safe_relative_path(
            raw_overlay["path"],
            label=f"{manifest} source {source_name} verbatim overlay path",
        )
        if PurePosixPath(path).parts[0] not in members:
            raise UpstreamError(
                f"{manifest} source {source_name} verbatim overlay {path!r} must live "
                "under a declared member"
            )
        if path in controls:
            raise UpstreamError(
                f"{manifest} source {source_name} verbatim overlay {path!r} may not be "
                "a control path"
            )
        if path in additive_files:
            raise UpstreamError(
                f"{manifest} source {source_name} verbatim overlay {path!r} may not be "
                "an additive file"
            )
        if path in overlay_paths:
            raise UpstreamError(
                f"{manifest} source {source_name} has duplicate overlay path {path!r}"
            )
        overlay_paths.add(path)
        for field in ("after", "insert", "reason"):
            if not isinstance(raw_overlay[field], str) or not raw_overlay[field]:
                raise UpstreamError(
                    f"{manifest} source {source_name} overlay {path!r} has empty {field}"
                )
        overlays.append(
            VerbatimOverlay(
                path=path,
                after=raw_overlay["after"],
                insert=raw_overlay["insert"],
                reason=raw_overlay["reason"],
            )
        )
    return VerbatimBundle(
        upstream_root=upstream_root,
        members=tuple(members),
        control_paths=controls,
        additive_files=additive_files,
        overlays=tuple(overlays),
    )


def identifier_list(
    raw: object,
    *,
    label: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    if raw is None:
        return default
    if not isinstance(raw, list) or not raw:
        raise UpstreamError(f"{label} must be a non-empty list")
    values: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9._:/-]*", value
        ):
            raise UpstreamError(f"{label} contains invalid identifier {value!r}")
        values.append(value)
    if len(set(values)) != len(values):
        raise UpstreamError(f"{label} contains duplicate identifiers")
    return tuple(values)


def string_list(
    raw: object,
    *,
    label: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    if raw is None:
        return default
    if not isinstance(raw, list) or not raw or not all(
        isinstance(value, str) and value for value in raw
    ):
        raise UpstreamError(f"{label} must be a non-empty list of strings")
    if len(set(raw)) != len(raw):
        raise UpstreamError(f"{label} contains duplicate values")
    return tuple(raw)


def validate_sha512_sri(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha512-"):
        raise UpstreamError(f"{label} must be a canonical SHA-512 integrity")
    encoded = value.removeprefix("sha512-")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise UpstreamError(f"{label} must be a canonical SHA-512 integrity") from error
    if len(decoded) != 64 or base64.b64encode(decoded).decode("ascii") != encoded:
        raise UpstreamError(f"{label} must be a canonical SHA-512 integrity")
    return value


def parse_local_contract(
    raw: object, *, repo_root: Path, manifest: Path, source_name: str
) -> tuple[tuple[str, ...], str, tuple[tuple[str, str], ...]]:
    if raw is None:
        return (), "", ()
    if not isinstance(raw, dict) or set(raw) != {"paths", "hash", "required_text"}:
        raise UpstreamError(
            f"{manifest} source {source_name} local_contract must contain exactly "
            "paths, hash, and required_text"
        )
    paths = tuple(
        safe_relative_path(
            value, label=f"{manifest} source {source_name} local contract path"
        )
        for value in raw["paths"]
    ) if isinstance(raw["paths"], list) else ()
    if not paths or len(set(paths)) != len(paths):
        raise UpstreamError(
            f"{manifest} source {source_name} local_contract.paths must be unique and non-empty"
        )
    digest = raw["hash"]
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise UpstreamError(
            f"{manifest} source {source_name} local_contract.hash is invalid"
        )
    raw_requirements = raw["required_text"]
    if not isinstance(raw_requirements, list):
        raise UpstreamError(
            f"{manifest} source {source_name} local_contract.required_text must be a list"
        )
    requirements: list[tuple[str, str]] = []
    for index, item in enumerate(raw_requirements):
        if not isinstance(item, dict) or set(item) != {"path", "text"}:
            raise UpstreamError(
                f"{manifest} source {source_name} local_contract.required_text[{index}] "
                "must contain exactly path and text"
            )
        path = safe_relative_path(
            item["path"],
            label=f"{manifest} source {source_name} required text path",
        )
        text = item["text"]
        if path not in paths or not isinstance(text, str) or not text:
            raise UpstreamError(
                f"{manifest} source {source_name} has invalid required privacy/review text"
            )
        requirements.append((path, text))
    return paths, digest, tuple(requirements)


def local_contract_digest(repo_root: Path, paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(paths):
        path = repo_root / relative
        try:
            mode = path.lstat().st_mode
        except OSError as error:
            raise UpstreamError(f"cannot inspect local contract path {path}: {error}") from error
        if path.is_symlink() or not stat_module.S_ISREG(mode):
            raise UpstreamError(f"local contract path must be a real file: {path}")
        content = path.read_bytes()
        digest.update(relative.encode() + b"\0" + hashlib.sha256(content).hexdigest().encode() + b"\n")
    return "sha256:" + digest.hexdigest()


def validate_local_contract(source: Source) -> None:
    if not source.local_contract_paths:
        if source.watch == "npm_releases":
            raise UpstreamError(
                f"{source.name}: runtime watch requires non-empty local contract paths"
            )
        return
    if source.watch == "npm_releases" and not source.required_text:
        raise UpstreamError(
            f"{source.name}: runtime watch requires required_text to be non-empty"
        )
    actual = local_contract_digest(source.repo_root, source.local_contract_paths)
    if actual != source.local_contract_hash:
        raise UpstreamError(
            f"{source.name}: local contract hash {source.local_contract_hash} does not "
            f"match protected surfaces {actual}"
        )
    texts: dict[str, str] = {}
    for relative in source.local_contract_paths:
        path = source.repo_root / relative
        try:
            texts[relative] = path.read_text()
        except (OSError, UnicodeDecodeError) as error:
            raise UpstreamError(f"cannot read local contract path {path}: {error}") from error
    for relative, required in source.required_text:
        if required not in texts[relative]:
            raise UpstreamError(
                f"{source.name}: required privacy/review text is missing from {relative}"
            )
    if source.watch == "npm_releases":
        combined = "\n".join(texts.values())
        pins = (source.package_version, source.package_integrity, source.commit)
        if any(pin not in combined for pin in pins):
            raise UpstreamError(
                f"{source.name}: package pin version, SRI, and source commit must all "
                "appear in the protected local contract"
            )


def default_web_url(repository: str) -> str:
    if repository.startswith("git@") and ":" in repository:
        host, path = repository[4:].split(":", 1)
        return f"https://{host}/{path.removesuffix('.git')}"
    if repository.startswith(("http://", "https://")):
        return repository.removesuffix(".git")
    return repository


def validate_source_url(value: object, *, label: str, purpose: str) -> str:
    if not isinstance(value, str) or not value:
        raise UpstreamError(f"{label} must be a non-empty string")
    parsed = urllib.parse.urlsplit(value)
    if not parsed.scheme:
        if value.startswith("git@") and not re.fullmatch(r"git@[^/:\s]+:.+", value):
            raise UpstreamError(f"{label} has an invalid SSH repository URL")
        return value
    allowed_schemes = {"https", "ssh", "file"} if purpose == "fetch" else {"https"}
    if parsed.scheme not in allowed_schemes:
        raise UpstreamError(f"{label} uses unsafe scheme {parsed.scheme!r}")
    if parsed.scheme == "https":
        if parsed.username is not None or parsed.password is not None:
            raise UpstreamError(f"{label} may not contain credentials")
        if not parsed.hostname or parsed.query or parsed.fragment:
            raise UpstreamError(f"{label} must be a safe HTTPS URL")
    elif parsed.scheme == "ssh":
        if parsed.password is not None or parsed.username not in {None, "git"}:
            raise UpstreamError(f"{label} may not contain credentials")
        if not parsed.hostname or parsed.query or parsed.fragment:
            raise UpstreamError(f"{label} must be a safe SSH repository URL")
    elif parsed.username is not None or parsed.password is not None:
        raise UpstreamError(f"{label} may not contain credentials")
    elif parsed.query or parsed.fragment:
        raise UpstreamError(f"{label} must be a safe file URL")
    return value


def validate_fetch_url(value: object, *, label: str) -> str:
    return validate_source_url(value, label=label, purpose="fetch")


def validate_web_url(value: object, *, label: str) -> str:
    return validate_source_url(value, label=label, purpose="web")


def redact_report_error(error: object) -> str:
    """Keep report diagnostics useful without serializing credential-shaped values."""

    text = str(error)

    def redact_url(match: re.Match[str]) -> str:
        value = match.group(0)
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname or ""
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            port = ""
        userinfo = "[REDACTED]@" if parsed.username is not None else ""
        query = "?[REDACTED]" if parsed.query else ""
        fragment = "#[REDACTED]" if parsed.fragment else ""
        return f"{parsed.scheme}://{userinfo}{host}{port}{parsed.path}{query}{fragment}"

    text = re.sub(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"']+", redact_url, text)
    text = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)\b(token|secret|password|passwd|api[_-]?key|credential)"
        r"(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[REDACTED]",
        text,
    )
    text = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|xox[a-z]-[A-Za-z0-9-]{20,}|"
        r"AKIA[0-9A-Z]{16})\b",
        "[REDACTED]",
        text,
    )
    return text[:4096]


def load_sources(repo_root: Path) -> list[Source]:
    sources: list[Source] = []
    seen: set[str] = set()
    seen_license_copies: set[Path] = set()
    manifest_roots = (repo_root / "shared" / "skills", repo_root / "shared" / "superpowers")
    manifests = sorted(
        {
            *(manifest for root in manifest_roots for manifest in root.glob("*/upstreams.toml")),
            *(repo_root / "components").glob("*/upstreams.toml"),
        }
    )
    for manifest in manifests:
        try:
            with manifest.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise UpstreamError(f"cannot parse {manifest}: {error}") from error
        unknown_manifest = sorted(set(data) - {"sources"})
        if unknown_manifest:
            raise UpstreamError(f"{manifest} has unknown fields {unknown_manifest}")
        raw_sources = data.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise UpstreamError(f"{manifest} must contain at least one [[sources]] table")
        for index, raw in enumerate(raw_sources):
            if not isinstance(raw, dict):
                raise UpstreamError(f"{manifest} sources[{index}] is not a table")
            unknown_source = sorted(set(raw) - SOURCE_FIELDS)
            if unknown_source:
                raise UpstreamError(
                    f"{manifest} sources[{index}] has unknown fields {unknown_source}"
                )
            license_optional = raw.get("license_optional", False)
            if not isinstance(license_optional, bool):
                raise UpstreamError(
                    f"{manifest} source {raw.get('name', index)} license_optional must be boolean"
                )
            required = {
                "name",
                "repository",
                "ref",
                "commit",
                "paths",
                "path_tree_hash",
                "license",
                "adaptation",
            }
            if not license_optional:
                required.update({"upstream_license_path", "local_license_path"})
            missing = sorted(required - set(raw))
            if missing:
                raise UpstreamError(f"{manifest} sources[{index}] is missing {missing}")
            name = raw["name"]
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
                raise UpstreamError(f"{manifest} has invalid source name {name!r}")
            if name in seen:
                raise UpstreamError(f"source name {name!r} is duplicated across manifests")
            seen.add(name)
            commit = raw["commit"]
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise UpstreamError(f"{manifest} source {name} must pin a 40-character commit")
            paths = raw["paths"]
            if (
                not isinstance(paths, list)
                or not paths
                or not all(
                    isinstance(path, str) and path and not path.startswith("/")
                    for path in paths
                )
            ):
                raise UpstreamError(f"{manifest} source {name} has invalid paths")
            if any(".." in Path(path).parts for path in paths):
                raise UpstreamError(f"{manifest} source {name} paths may not traverse upward")
            upstream_license_path = ""
            local_license_path = ""
            if license_optional:
                if "upstream_license_path" in raw or "local_license_path" in raw:
                    raise UpstreamError(
                        f"{manifest} source {name} optional license may not declare license paths"
                    )
            else:
                upstream_license_path = safe_relative_path(
                    raw["upstream_license_path"],
                    label=f"{manifest} source {name} upstream_license_path",
                )
                local_license_path = safe_relative_path(
                    raw["local_license_path"],
                    label=f"{manifest} source {name} local_license_path",
                )
                license_path_policy = raw.get("license_path_policy", "licenses")
                if license_path_policy not in {"licenses", "component"}:
                    raise UpstreamError(
                        f"{manifest} source {name} has invalid license_path_policy"
                    )
                if license_path_policy == "component" and "components" not in manifest.parts:
                    raise UpstreamError(
                        f"{manifest} source {name} component license policy is only valid "
                        "for component manifests"
                    )
                if license_path_policy == "licenses" and PurePosixPath(local_license_path).parts[0] != "licenses":
                    raise UpstreamError(
                        f"{manifest} source {name} local_license_path must live under licenses/"
                    )
            normalized_paths = tuple(PurePosixPath(path).as_posix() for path in paths)
            if not license_optional and not selected_paths_cover(normalized_paths, upstream_license_path):
                raise UpstreamError(
                    f"{manifest} source {name} upstream license {upstream_license_path!r} "
                    "is outside its reviewed paths"
                )
            if not license_optional:
                local_license = manifest.parent / local_license_path
                if local_license in seen_license_copies:
                    raise UpstreamError(
                        f"local license copy is shared by more than one source: {local_license}"
                    )
                seen_license_copies.add(local_license)
            tree_hash = raw["path_tree_hash"]
            if not isinstance(tree_hash, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", tree_hash
            ):
                raise UpstreamError(f"{manifest} source {name} has invalid path_tree_hash")
            scalar_keys = ("repository", "ref", "license", "adaptation")
            if not all(isinstance(raw[key], str) and raw[key] for key in scalar_keys):
                raise UpstreamError(f"{manifest} source {name} has an empty string field")
            verbatim_bundle = parse_verbatim_bundle(
                raw.get("verbatim_bundle"),
                manifest=manifest,
                source_name=name,
                source_skill=manifest.parent.name,
                selected_paths=normalized_paths,
                local_license_path=local_license_path,
            )
            consumers = identifier_list(
                raw.get("consumers"),
                label=f"{manifest} source {name} consumers",
                default=(DEFAULT_CONSUMER,),
            )
            affected_capability_ids = identifier_list(
                raw.get("affected_capability_ids"),
                label=f"{manifest} source {name} affected_capability_ids",
                default=verbatim_bundle.members if verbatim_bundle else (manifest.parent.name,),
            )
            relationship = raw.get(
                "relationship", "verbatim" if verbatim_bundle else "adapted"
            )
            if relationship not in {"adapted", "verbatim", "tracks_product", "adapter"}:
                raise UpstreamError(
                    f"{manifest} source {name} has invalid relationship {relationship!r}"
                )
            owners = {
                field: raw.get(field, "khenrix")
                for field in ("content_owner", "delivery_owner")
            }
            if not all(
                isinstance(value, str)
                and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value)
                for value in owners.values()
            ):
                raise UpstreamError(f"{manifest} source {name} has an invalid owner")
            fetch_url = raw.get("fetch_url", raw["repository"])
            web_url = raw.get("web_url", default_web_url(raw["repository"]))
            if not all(isinstance(value, str) and value for value in (fetch_url, web_url)):
                raise UpstreamError(f"{manifest} source {name} has an invalid URL field")
            fetch_url = validate_fetch_url(
                fetch_url, label=f"{manifest} source {name} fetch_url"
            )
            web_url = validate_web_url(
                web_url, label=f"{manifest} source {name} web_url"
            )
            update_mode = raw.get(
                "update_mode", "verbatim_sync" if verbatim_bundle else "manual_adaptation"
            )
            if not isinstance(update_mode, str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9_-]*", update_mode
            ):
                raise UpstreamError(
                    f"{manifest} source {name} has invalid update_mode {update_mode!r}"
                )
            review_commands = string_list(
                raw.get("review_commands"),
                label=f"{manifest} source {name} review_commands",
                default=(f"mise run skills:upstream-diff -- {name}",),
            )
            watch = raw.get("watch", "ref")
            if watch not in {"ref", "stable_tags", "npm_releases"}:
                raise UpstreamError(f"{manifest} source {name} has invalid watch {watch!r}")
            tag_pattern = raw.get("tag_pattern", "")
            if watch != "ref":
                if not isinstance(tag_pattern, str) or not (
                    tag_pattern.startswith("^") and tag_pattern.endswith("$")
                ):
                    raise UpstreamError(
                        f"{manifest} source {name} tag_pattern must be anchored"
                    )
                try:
                    compiled_tag = re.compile(tag_pattern)
                except re.error as error:
                    raise UpstreamError(
                        f"{manifest} source {name} tag_pattern is invalid: {error}"
                    ) from error
                if compiled_tag.groups < 1:
                    raise UpstreamError(
                        f"{manifest} source {name} tag_pattern must capture numeric ordering fields"
                    )
            elif tag_pattern:
                raise UpstreamError(
                    f"{manifest} source {name} tag_pattern requires a release watch"
                )
            package_name = raw.get("package_name", "")
            package_version = raw.get("package_version", "")
            package_integrity = raw.get("package_integrity", "")
            if watch == "npm_releases":
                if not isinstance(package_name, str) or not re.fullmatch(
                    r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+", package_name
                ):
                    raise UpstreamError(f"{manifest} source {name} package_name is invalid")
                if not isinstance(package_version, str) or not re.fullmatch(
                    tag_pattern.removeprefix("^v").removesuffix("$"), package_version
                ):
                    raise UpstreamError(
                        f"{manifest} source {name} package_version does not match tag_pattern"
                    )
                validate_sha512_sri(
                    package_integrity,
                    label=f"{manifest} source {name} package_integrity",
                )
                canonical_selector = f"npm:{package_name}@{package_version}"
                if raw["ref"] != canonical_selector:
                    raise UpstreamError(
                        f"{manifest} source {name} ref must use canonical npm selector "
                        f"{canonical_selector!r}"
                    )
            elif any((package_name, package_version, package_integrity)):
                raise UpstreamError(
                    f"{manifest} source {name} package fields require npm_releases"
                )
            local_paths, local_hash, required_text = parse_local_contract(
                raw.get("local_contract"),
                repo_root=repo_root,
                manifest=manifest,
                source_name=name,
            )
            if watch == "npm_releases" and (not local_paths or not required_text):
                raise UpstreamError(
                    f"{manifest} source {name} runtime watch requires non-empty "
                    "local_contract.paths and required_text guards"
                )
            sources.append(
                Source(
                    skill=manifest.parent.name,
                    manifest=manifest,
                    name=name,
                    repository=raw["repository"],
                    ref=raw["ref"],
                    commit=commit,
                    paths=tuple(paths),
                    path_tree_hash=tree_hash,
                    license=raw["license"],
                    upstream_license_path=upstream_license_path,
                    local_license_path=local_license_path,
                    adaptation=raw["adaptation"],
                    verbatim_bundle=verbatim_bundle,
                    consumers=consumers,
                    content_owner=owners["content_owner"],
                    delivery_owner=owners["delivery_owner"],
                    fetch_url=fetch_url,
                    web_url=web_url,
                    relationship=relationship,
                    affected_capability_ids=affected_capability_ids,
                    update_mode=update_mode,
                    review_commands=review_commands,
                    repo_root=repo_root,
                    license_optional=license_optional,
                    watch=watch,
                    tag_pattern=tag_pattern,
                    package_name=package_name,
                    package_version=package_version,
                    package_integrity=package_integrity,
                    local_contract_paths=local_paths,
                    local_contract_hash=local_hash,
                    required_text=required_text,
                )
            )
    if not sources:
        raise UpstreamError(
            "no upstream manifests found under shared/skills or shared/superpowers"
        )
    for source in sources:
        validated_notice(source)
        validated_license_copy(source)
        validate_local_contract(source)
    return sources


def resolve_remote(source: Source, *, bounded: bool = False) -> str:
    refs = [source.ref]
    if source.ref.startswith("refs/tags/"):
        refs.append(source.ref + "^{}")
    runner = run_git_bounded if bounded else run_git
    output = runner(["ls-remote", source.fetch_url or source.repository, *refs])
    rows = []
    for line in output.splitlines():
        fields = line.split("\t", 1)
        if len(fields) == 2 and re.fullmatch(r"[0-9a-f]{40}", fields[0]):
            rows.append((fields[0], fields[1]))
    if not rows:
        raise UpstreamError(f"{source.name}: tracking ref {source.ref!r} does not exist")
    peeled = [commit for commit, ref in rows if ref.endswith("^{}")]
    return peeled[0] if peeled else rows[0][0]


def numeric_release_rows(source: Source, output: str) -> list[tuple[tuple[int, ...], str, str]]:
    pattern = re.compile(source.tag_pattern)
    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split("\t", 1)
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{40}", fields[0]):
            continue
        ref = fields[1]
        if ref.endswith("^{}"):
            peeled[ref[:-3]] = fields[0]
        else:
            direct[ref] = fields[0]
    rows: list[tuple[tuple[int, ...], str, str]] = []
    for ref, commit in direct.items():
        if not ref.startswith("refs/tags/"):
            continue
        match = pattern.fullmatch(ref.removeprefix("refs/tags/"))
        if match is None or not all(part.isdigit() for part in match.groups()):
            continue
        rows.append((tuple(int(part) for part in match.groups()), ref, peeled.get(ref, commit)))
    return rows


def resolve_stable_tag(source: Source, *, bounded: bool = False) -> tuple[str, str, bool]:
    runner = run_git_bounded if bounded else run_git
    output = runner(["ls-remote", "--tags", source.fetch_url or source.repository, "refs/tags/*"])
    rows = numeric_release_rows(source, output)
    if not rows:
        raise UpstreamError(f"{source.name}: no tag matches {source.tag_pattern!r}")
    _, candidate_ref, candidate_commit = max(rows, key=lambda row: row[0])
    pinned = [commit for _, ref, commit in rows if ref == source.ref]
    if len(pinned) != 1:
        raise UpstreamError(f"{source.name}: pinned release selector {source.ref!r} is missing")
    return candidate_commit, candidate_ref, pinned[0] != source.commit


def fetch_npm_metadata(package: str) -> dict[str, Any]:
    url = "https://registry.npmjs.org/" + urllib.parse.quote(package, safe="@/")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=GIT_TIMEOUT_SECONDS) as response:
            payload = response.read(MAX_GIT_OUTPUT_BYTES + 1)
    except (OSError, TimeoutError) as error:
        raise UpstreamError(f"npm metadata check failed for {package}: {error}") from error
    if len(payload) > MAX_GIT_OUTPUT_BYTES:
        raise UpstreamError(f"npm metadata for {package} exceeded output limit")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpstreamError(f"npm metadata for {package} is malformed") from error
    if not isinstance(value, dict) or not isinstance(value.get("versions"), dict):
        raise UpstreamError(f"npm metadata for {package} lacks versions")
    return value


def npm_release_candidate(
    source: Source, *, bounded: bool = False
) -> tuple[str, str, str, bool]:
    metadata = fetch_npm_metadata(source.package_name)
    pattern = re.compile(source.tag_pattern)
    candidates: list[tuple[tuple[int, ...], str, dict[str, Any]]] = []
    for version, raw in metadata["versions"].items():
        match = pattern.fullmatch("v" + version)
        if match is None or not all(part.isdigit() for part in match.groups()):
            continue
        if isinstance(raw, dict):
            candidates.append((tuple(int(part) for part in match.groups()), version, raw))
    if not candidates:
        raise UpstreamError(
            f"{source.name}: npm has no version matching {source.tag_pattern!r}"
        )
    _, version, candidate = max(candidates, key=lambda row: row[0])
    pinned = metadata["versions"].get(source.package_version)
    if not isinstance(pinned, dict):
        raise UpstreamError(
            f"{source.name}: pinned npm version {source.package_version!r} is missing"
        )
    pinned_integrity = pinned.get("dist", {}).get("integrity")
    candidate_integrity = candidate.get("dist", {}).get("integrity")
    pinned_integrity = validate_sha512_sri(
        pinned_integrity,
        label=f"{source.name}: pinned npm release SHA-512 integrity",
    )
    candidate_integrity = validate_sha512_sri(
        candidate_integrity,
        label=f"{source.name}: candidate npm release SHA-512 integrity",
    )

    def source_commit(release: dict[str, Any], release_version: str) -> str:
        commit = release.get("gitHead")
        if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):
            return commit
        runner = run_git_bounded if bounded else run_git
        ref = f"refs/tags/v{release_version}"
        output = runner(["ls-remote", source.fetch_url or source.repository, ref, ref + "^{}"])
        rows = [
            (fields[0], fields[1])
            for line in output.splitlines()
            if len(fields := line.split("\t", 1)) == 2
            and re.fullmatch(r"[0-9a-f]{40}", fields[0])
        ]
        peeled = [value for value, found in rows if found.endswith("^{}")]
        direct = [value for value, found in rows if found == ref]
        commits = peeled or direct
        if len(commits) != 1:
            raise UpstreamError(f"{source.name}: source tag {ref!r} is missing")
        return commits[0]

    candidate_commit = source_commit(candidate, version)
    pinned_commit = source_commit(pinned, source.package_version)
    moved_or_integrity_changed = (
        pinned_commit != source.commit or pinned_integrity != source.package_integrity
    )
    return candidate_commit, f"npm:{source.package_name}@{version}", candidate_integrity, moved_or_integrity_changed


class Checkout:
    def __init__(self, source: Source, *, bounded: bool = False):
        self.source = source
        self._run_git = run_git_bounded if bounded else run_git
        self._run_git_bytes = run_git_bytes_bounded if bounded else run_git_bytes
        self._temporary = tempfile.TemporaryDirectory(prefix="khenrix-upstream-")
        self.path = Path(self._temporary.name)
        self._run_git(["init", "--quiet"], cwd=self.path)
        self._fetched: set[str] = set()

    def close(self) -> None:
        self._temporary.cleanup()

    def __enter__(self) -> "Checkout":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fetch(self, commit: str) -> None:
        if commit in self._fetched:
            return
        self._run_git(
            [
                "fetch",
                "--quiet",
                "--no-tags",
                "--depth=1",
                self.source.fetch_url or self.source.repository,
                commit,
            ],
            cwd=self.path,
        )
        actual = self._run_git(
            ["rev-parse", "FETCH_HEAD^{commit}"], cwd=self.path
        ).strip()
        if actual != commit:
            raise UpstreamError(f"{self.source.name}: fetched {actual}, expected {commit}")
        self._fetched.add(commit)

    def path_hash(self, commit: str) -> str:
        self.fetch(commit)
        output = self._run_git(
            ["ls-tree", "-r", commit, "--", *self.source.paths], cwd=self.path
        )
        rows = sorted(line for line in output.splitlines() if line)
        if not rows:
            raise UpstreamError(
                f"{self.source.name}: none of {list(self.source.paths)!r} exist at {commit}"
            )
        payload = "".join(row + "\n" for row in rows).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def file_bytes(self, commit: str, path: str) -> bytes:
        self.fetch(commit)
        return self._run_git_bytes(
            ["cat-file", "blob", f"{commit}:{path}"], cwd=self.path
        )

    def bundle_files(self, commit: str, bundle: VerbatimBundle) -> dict[str, BundleFile]:
        """Read a complete portable skill bundle from Git, including executable bits."""

        self.fetch(commit)
        output = self._run_git_bytes(
            ["ls-tree", "-r", "-z", commit, "--", bundle.upstream_root],
            cwd=self.path,
        )
        prefix = PurePosixPath(bundle.upstream_root)
        files: dict[str, BundleFile] = {}
        observed_members: set[str] = set()
        for raw_row in output.split(b"\0"):
            if not raw_row:
                continue
            try:
                header, raw_path = raw_row.split(b"\t", 1)
                raw_mode, raw_kind, raw_object = header.split(b" ", 2)
                path_text = raw_path.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as error:
                raise UpstreamError(
                    f"{self.source.name}: verbatim bundle contains an unsupported Git path"
                ) from error
            path = PurePosixPath(path_text)
            try:
                relative = path.relative_to(prefix)
            except ValueError as error:
                raise UpstreamError(
                    f"{self.source.name}: Git returned {path_text!r} outside bundle root"
                ) from error
            if len(relative.parts) < 2:
                raise UpstreamError(
                    f"{self.source.name}: verbatim bundle root may contain only skill "
                    f"directories, found {path_text!r}"
                )
            member = relative.parts[0]
            observed_members.add(member)
            if raw_kind != b"blob" or raw_mode not in {b"100644", b"100755"}:
                raise UpstreamError(
                    f"{self.source.name}: unsupported entry {path_text!r} with "
                    f"mode/type {raw_mode.decode('ascii', 'replace')} "
                    f"{raw_kind.decode('ascii', 'replace')}"
                )
            local_path = relative.as_posix()
            if local_path in files:
                raise UpstreamError(
                    f"{self.source.name}: duplicate bundle path {local_path!r}"
                )
            object_id = raw_object.decode("ascii")
            content = self._run_git_bytes(
                ["cat-file", "blob", object_id], cwd=self.path
            )
            files[local_path] = BundleFile(
                local_path=local_path,
                mode=0o755 if raw_mode == b"100755" else 0o644,
                content=content,
            )
        expected_members = set(bundle.members)
        if observed_members != expected_members:
            missing = sorted(expected_members - observed_members)
            extra = sorted(observed_members - expected_members)
            raise UpstreamError(
                f"{self.source.name}: pinned verbatim bundle members differ from the "
                f"manifest (missing={missing}, extra={extra})"
            )
        for member in bundle.members:
            if f"{member}/SKILL.md" not in files:
                raise UpstreamError(
                    f"{self.source.name}: verbatim member {member!r} has no SKILL.md"
                )
        for overlay in bundle.overlays:
            item = files.get(overlay.path)
            if item is None:
                raise UpstreamError(
                    f"{self.source.name}: verbatim overlay target {overlay.path!r} "
                    f"does not exist at {commit}"
                )
            after = overlay.after.encode()
            insertion = overlay.insert.encode()
            if item.content.count(after) != 1:
                raise UpstreamError(
                    f"{self.source.name}: overlay anchor for {overlay.path!r} must occur "
                    "exactly once in the pinned upstream file"
                )
            if insertion in item.content:
                raise UpstreamError(
                    f"{self.source.name}: overlay insertion for {overlay.path!r} is already "
                    "present upstream; review and remove or revise the overlay"
                )
            files[overlay.path] = BundleFile(
                local_path=item.local_path,
                mode=item.mode,
                content=item.content.replace(after, after + insertion, 1),
            )
        return files

    def diff(self, old: str, new: str) -> tuple[str, str]:
        self.fetch(old)
        self.fetch(new)
        stat = self._run_git(
            ["diff", "--stat", "--find-renames", old, new, "--", *self.source.paths],
            cwd=self.path,
        )
        # License files are copied byte-for-byte and are not guaranteed to be UTF-8.
        # The review display may replace undecodable bytes, while file_bytes() below
        # remains exact for the provenance update itself.
        patch = self._run_git_bytes(
            ["diff", "--find-renames", old, new, "--", *self.source.paths],
            cwd=self.path,
        ).decode("utf-8", "replace")
        return stat, patch


def bundle_root(source: Source) -> Path:
    return source.manifest.parent.parent


def local_bundle_files(source: Source, *, root: Path | None = None) -> dict[str, Path]:
    bundle = source.verbatim_bundle
    if bundle is None:
        return {}
    root = root or bundle_root(source)
    files: dict[str, Path] = {}
    for member in bundle.members:
        member_path = root / member
        try:
            member_stat = member_path.lstat()
        except OSError as error:
            raise UpstreamError(
                f"{source.name}: cannot inspect local verbatim member {member_path}: {error}"
            ) from error
        if member_path.is_symlink() or not stat_module.S_ISDIR(member_stat.st_mode):
            raise UpstreamError(
                f"{source.name}: local verbatim member must be a real directory: {member_path}"
            )
        for current, directories, names in os.walk(member_path, followlinks=False):
            current_path = Path(current)
            for directory in list(directories):
                path = current_path / directory
                try:
                    mode = path.lstat().st_mode
                except OSError as error:
                    raise UpstreamError(f"{source.name}: cannot inspect {path}: {error}") from error
                if path.is_symlink() or not stat_module.S_ISDIR(mode):
                    raise UpstreamError(
                        f"{source.name}: unsafe entry in local verbatim bundle: {path}"
                    )
            for name in names:
                path = current_path / name
                try:
                    mode = path.lstat().st_mode
                except OSError as error:
                    raise UpstreamError(f"{source.name}: cannot inspect {path}: {error}") from error
                if path.is_symlink() or not stat_module.S_ISREG(mode):
                    raise UpstreamError(
                        f"{source.name}: unsafe entry in local verbatim bundle: {path}"
                    )
                relative = path.relative_to(root).as_posix()
                files[relative] = path
    return files


def verbatim_bundle_mismatches(
    source: Source,
    checkout: Checkout,
    commit: str,
    *,
    root: Path | None = None,
) -> list[str]:
    """Return byte, path, and executable-bit drift for an optional bundle."""

    bundle = source.verbatim_bundle
    if bundle is None:
        return []
    expected = checkout.bundle_files(commit, bundle)
    controls = set(bundle.control_paths)
    additive = set(bundle.additive_files)
    overlap = controls.intersection(expected)
    if overlap:
        raise UpstreamError(
            f"{source.name}: control paths overlap pinned upstream files: {sorted(overlap)}"
        )
    actual_paths = local_bundle_files(source, root=root)
    missing_controls = sorted(controls - set(actual_paths))
    if missing_controls:
        raise UpstreamError(
            f"{source.name}: missing local verbatim control paths: {missing_controls}"
        )
    missing_additive = sorted(additive - set(actual_paths))
    if missing_additive:
        raise UpstreamError(
            f"{source.name}: missing declared additive files: {missing_additive}"
        )
    if additive.intersection(expected):
        raise UpstreamError(
            f"{source.name}: additive files overlap pinned upstream payload: "
            f"{sorted(additive.intersection(expected))}"
        )
    actual = {
        path: file
        for path, file in actual_paths.items()
        if path not in controls and path not in additive
    }
    messages = [f"missing {path}" for path in sorted(set(expected) - set(actual))]
    messages.extend(f"unexpected {path}" for path in sorted(set(actual) - set(expected)))
    for path in sorted(set(expected).intersection(actual)):
        wanted = expected[path]
        local_path = actual[path]
        try:
            content = local_path.read_bytes()
            mode = local_path.stat().st_mode
        except OSError as error:
            raise UpstreamError(f"{source.name}: cannot read {local_path}: {error}") from error
        if content != wanted.content:
            messages.append(f"content differs for {path}")
        expected_executable = bool(wanted.mode & 0o111)
        actual_executable = bool(mode & 0o111)
        if expected_executable != actual_executable:
            messages.append(
                f"executable bit differs for {path} "
                f"(expected={'on' if expected_executable else 'off'}, "
                f"actual={'on' if actual_executable else 'off'})"
            )
    return messages


def require_verbatim_bundle_match(
    source: Source,
    checkout: Checkout,
    commit: str,
    *,
    purpose: str,
) -> None:
    mismatches = verbatim_bundle_mismatches(source, checkout, commit)
    if mismatches:
        preview = "; ".join(mismatches[:8])
        if len(mismatches) > 8:
            preview += f"; and {len(mismatches) - 8} more"
        raise UpstreamError(
            f"{source.name}: local verbatim bundle does not match {commit} before "
            f"{purpose}: {preview}"
        )


def inspect_source(source: Source, *, bounded: bool = False) -> dict[str, Any]:
    candidate_selector = source.ref
    candidate_integrity = ""
    candidate_package_version = ""
    pin_integrity_failure = False
    if source.watch == "stable_tags":
        remote, candidate_selector, pin_integrity_failure = resolve_stable_tag(
            source, bounded=bounded
        )
    elif source.watch == "npm_releases":
        (
            remote,
            candidate_selector,
            candidate_integrity,
            pin_integrity_failure,
        ) = npm_release_candidate(source, bounded=bounded)
        candidate_package_version = candidate_selector.rsplit("@", 1)[1]
    else:
        remote = resolve_remote(source, bounded=bounded)
    _, local_license = validated_license_copy(source)
    with Checkout(source, bounded=bounded) as checkout:
        pinned_hash = checkout.path_hash(source.commit)
        remote_hash = checkout.path_hash(remote)
        pinned_license = (
            b""
            if source.license_optional
            else checkout.file_bytes(source.commit, source.upstream_license_path)
        )
        bundle_mismatches = verbatim_bundle_mismatches(source, checkout, source.commit)
    report_pinned_hash = pinned_hash
    report_remote_hash = remote_hash
    if source.watch == "npm_releases":
        report_pinned_hash = canonical_json_digest(
            {"package_integrity": source.package_integrity, "source_tree": pinned_hash}
        )
        report_remote_hash = canonical_json_digest(
            {"package_integrity": candidate_integrity, "source_tree": remote_hash}
        )
    if pin_integrity_failure or pinned_hash != source.path_tree_hash:
        status = "PIN_HASH_MISMATCH"
    elif local_license != pinned_license:
        status = "LICENSE_COPY_MISMATCH"
    elif bundle_mismatches:
        status = "VERBATIM_BUNDLE_MISMATCH"
    elif candidate_selector == source.ref and remote == source.commit:
        status = "CURRENT"
    elif source.relationship == "tracks_product":
        status = "UPDATE"
    elif source.watch == "npm_releases":
        status = "UPDATE"
    elif remote_hash == source.path_tree_hash:
        status = "REPO_AHEAD"
    else:
        status = "UPDATE"
    return {
        "skill": source.skill,
        "source": source.name,
        "status": status,
        "pinned_commit": source.commit,
        "remote_commit": remote,
        "candidate_selector": candidate_selector,
        "candidate_package_version": candidate_package_version,
        "candidate_package_integrity": candidate_integrity,
        "declared_path_tree_hash": source.path_tree_hash,
        "pinned_path_tree_hash": report_pinned_hash,
        "remote_path_tree_hash": report_remote_hash,
        "upstream_license_path": source.upstream_license_path,
        "local_license_path": source.local_license_path,
        "pinned_license_hash": "sha256:" + hashlib.sha256(pinned_license).hexdigest(),
        "local_license_hash": "sha256:" + hashlib.sha256(local_license).hexdigest(),
        "verbatim_bundle_mismatches": bundle_mismatches,
        "paths": list(source.paths),
        "manifest": str(source.manifest),
    }


def canonical_json_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_source_digest(source: Source) -> str:
    if source.watch != "npm_releases":
        return source.path_tree_hash
    return canonical_json_digest(
        {
            "package_integrity": source.package_integrity,
            "source_tree": source.path_tree_hash,
        }
    )


def owner_source_record(source: Source) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source_id": source.name,
        "content_owner": source.content_owner,
        "delivery_owner": source.delivery_owner,
        "fetch_url": source.fetch_url,
        "web_url": source.web_url,
        "pinned_selector": source.ref,
        "pinned_commit": source.commit,
        "selected_paths": list(source.paths),
        "canonical_digest": canonical_source_digest(source),
        "relationship": source.relationship,
        "affected_capability_ids": list(source.affected_capability_ids),
        "update_mode": source.update_mode,
        "review_commands": list(source.review_commands),
    }
    try:
        inspected = inspect_source(source, bounded=True)
    except UpstreamError as error:
        record.update(
            {
                "status": "CHECK_INCOMPLETE",
                "error": redact_report_error(error),
            }
        )
        return record
    record.update(
        {
            "status": inspected["status"],
            "candidate_selector": inspected["candidate_selector"],
            "candidate_commit": inspected["remote_commit"],
            "pinned_package_version": source.package_version or None,
            "candidate_package_version": inspected["candidate_package_version"] or None,
            "pinned_package_integrity": source.package_integrity or None,
            "candidate_package_integrity": inspected["candidate_package_integrity"] or None,
            "pinned_digest": inspected["pinned_path_tree_hash"],
            "candidate_digest": inspected["remote_path_tree_hash"],
            "pinned_license_digest": inspected["pinned_license_hash"],
            "local_license_digest": inspected["local_license_hash"],
            "verbatim_bundle_mismatches": inspected["verbatim_bundle_mismatches"],
        }
    )
    return record


def validate_owner_report(report: dict[str, Any]) -> None:
    required = {
        "schema",
        "consumer",
        "owner_revision",
        "integrity_ok",
        "check_complete",
        "current",
        "sources",
        "errors",
    }
    missing = sorted(required - set(report))
    unknown = sorted(set(report) - required - {"report_digest"})
    if missing or unknown:
        raise UpstreamError(
            f"invalid owner report fields (missing={missing}, unknown={unknown})"
        )
    if report["schema"] != REPORT_SCHEMA:
        raise UpstreamError(f"invalid owner report schema {report['schema']!r}")
    if not isinstance(report["consumer"], str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9._-]*", report["consumer"]
    ):
        raise UpstreamError("invalid owner report consumer")
    revision = report["owner_revision"]
    if revision is not None and not (
        isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision)
    ):
        raise UpstreamError("invalid owner report owner_revision")
    if revision is None and report["integrity_ok"] is not False:
        raise UpstreamError("a successful owner report requires owner_revision")
    for field in ("integrity_ok", "check_complete", "current"):
        if not isinstance(report[field], bool):
            raise UpstreamError(f"owner report {field} must be boolean")
    errors = report["errors"]
    if not isinstance(errors, list) or not all(
        isinstance(error, str) and error for error in errors
    ):
        raise UpstreamError("owner report errors must be a list of non-empty strings")
    sources = report["sources"]
    if not isinstance(sources, list):
        raise UpstreamError("owner report sources must be a list")
    seen: set[str] = set()
    digest_pattern = r"sha256:[0-9a-f]{64}"
    base_fields = {
        "source_id",
        "content_owner",
        "delivery_owner",
        "fetch_url",
        "web_url",
        "pinned_selector",
        "pinned_commit",
        "selected_paths",
        "canonical_digest",
        "relationship",
        "affected_capability_ids",
        "update_mode",
        "review_commands",
        "status",
    }
    complete_fields = base_fields | {
        "candidate_selector",
        "candidate_commit",
        "pinned_package_version",
        "candidate_package_version",
        "pinned_package_integrity",
        "candidate_package_integrity",
        "pinned_digest",
        "candidate_digest",
        "pinned_license_digest",
        "local_license_digest",
        "verbatim_bundle_mismatches",
    }
    incomplete_fields = base_fields | {"error"}
    allowed_statuses = {
        "CURRENT",
        "REPO_AHEAD",
        "UPDATE",
        "PIN_HASH_MISMATCH",
        "LICENSE_COPY_MISMATCH",
        "VERBATIM_BUNDLE_MISMATCH",
        "CHECK_INCOMPLETE",
    }
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise UpstreamError(f"owner report sources[{index}] must be an object")
        canonical_digest = source.get("canonical_digest")
        if not isinstance(canonical_digest, str) or not re.fullmatch(
            digest_pattern, canonical_digest
        ):
            raise UpstreamError(
                f"owner report sources[{index}].canonical_digest is invalid"
            )
        missing_source = sorted(base_fields - set(source))
        if missing_source:
            raise UpstreamError(
                f"owner report sources[{index}] is missing {missing_source}"
            )
        if source["status"] not in allowed_statuses:
            raise UpstreamError(
                f"owner report sources[{index}] has invalid status {source['status']!r}"
            )
        expected_fields = (
            incomplete_fields
            if source["status"] == "CHECK_INCOMPLETE"
            else complete_fields
        )
        missing_source = sorted(expected_fields - set(source))
        unknown_source = sorted(set(source) - expected_fields)
        if missing_source or unknown_source:
            raise UpstreamError(
                f"invalid owner report sources[{index}] fields "
                f"(missing={missing_source}, unknown={unknown_source})"
            )
        source_id = source["source_id"]
        if not isinstance(source_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9._:/-]*", source_id
        ):
            raise UpstreamError(f"owner report sources[{index}].source_id is invalid")
        if source_id in seen:
            raise UpstreamError(f"owner report has duplicate source_id {source_id!r}")
        seen.add(source_id)
        capabilities = source["affected_capability_ids"]
        identifier_list(
            capabilities,
            label=f"owner report sources[{index}].affected_capability_ids",
            default=(),
        )
        selected_paths = source["selected_paths"]
        if not isinstance(selected_paths, list) or not selected_paths:
            raise UpstreamError(
                f"owner report sources[{index}].selected_paths must be non-empty"
            )
        for path in selected_paths:
            safe_relative_path(
                path, label=f"owner report sources[{index}].selected_paths"
            )
        if len(set(selected_paths)) != len(selected_paths):
            raise UpstreamError(
                f"owner report sources[{index}].selected_paths contains duplicates"
            )
        if not isinstance(source["pinned_commit"], str) or not re.fullmatch(
            r"[0-9a-f]{40}", source["pinned_commit"]
        ):
            raise UpstreamError(
                f"owner report sources[{index}].pinned_commit is invalid"
            )
        for field in ("content_owner", "delivery_owner"):
            if not isinstance(source[field], str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9._-]*", source[field]
            ):
                raise UpstreamError(
                    f"owner report sources[{index}].{field} is invalid"
                )
        validate_fetch_url(
            source["fetch_url"],
            label=f"owner report sources[{index}].fetch_url",
        )
        validate_web_url(
            source["web_url"],
            label=f"owner report sources[{index}].web_url",
        )
        if not isinstance(source["pinned_selector"], str) or not source[
            "pinned_selector"
        ] or re.search(r"[\x00-\x20]", source["pinned_selector"]):
            raise UpstreamError(
                f"owner report sources[{index}].pinned_selector is invalid"
            )
        if source["relationship"] not in {
            "adapted",
            "verbatim",
            "tracks_product",
            "adapter",
        }:
            raise UpstreamError(
                f"owner report sources[{index}].relationship is invalid"
            )
        if source["update_mode"] not in {"manual_adaptation", "verbatim_sync"}:
            raise UpstreamError(
                f"owner report sources[{index}].update_mode is invalid"
            )
        review_commands = string_list(
            source["review_commands"],
            label=f"owner report sources[{index}].review_commands",
            default=(),
        )
        if any("\x00" in command or "\n" in command for command in review_commands):
            raise UpstreamError(
                f"owner report sources[{index}].review_commands contains control characters"
            )
        if source["status"] != "CHECK_INCOMPLETE":
            if not isinstance(source["candidate_selector"], str) or not source[
                "candidate_selector"
            ] or re.search(r"[\x00-\x20]", source["candidate_selector"]):
                raise UpstreamError(
                    f"owner report sources[{index}].candidate_selector is invalid"
                )
            if not isinstance(source["candidate_commit"], str) or not re.fullmatch(
                r"[0-9a-f]{40}", source["candidate_commit"]
            ):
                raise UpstreamError(
                    f"owner report sources[{index}].candidate_commit is invalid"
                )
            package_values = (
                source["pinned_package_version"],
                source["candidate_package_version"],
                source["pinned_package_integrity"],
                source["candidate_package_integrity"],
            )
            if any(value is not None for value in package_values):
                if not all(isinstance(value, str) and value for value in package_values):
                    raise UpstreamError(
                        f"owner report sources[{index}] has incomplete package metadata"
                    )
                for field in ("pinned_package_version", "candidate_package_version"):
                    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+-]*", source[field]):
                        raise UpstreamError(
                            f"owner report sources[{index}].{field} is invalid"
                        )
                for field in ("pinned_package_integrity", "candidate_package_integrity"):
                    validate_sha512_sri(
                        source[field], label=f"owner report sources[{index}].{field}"
                    )
            for field in (
                "pinned_digest",
                "candidate_digest",
                "pinned_license_digest",
                "local_license_digest",
            ):
                if not isinstance(source.get(field), str) or not re.fullmatch(
                    digest_pattern, source[field]
                ):
                    raise UpstreamError(
                        f"owner report sources[{index}].{field} is invalid"
                    )
            mismatches = source["verbatim_bundle_mismatches"]
            if not isinstance(mismatches, list) or not all(
                isinstance(mismatch, str) and mismatch for mismatch in mismatches
            ):
                raise UpstreamError(
                    f"owner report sources[{index}].verbatim_bundle_mismatches is invalid"
                )
        elif not isinstance(source.get("error"), str) or not source["error"]:
            raise UpstreamError(
                f"owner report sources[{index}] incomplete result lacks an error"
            )
    if report["current"] and not (
        report["integrity_ok"] and report["check_complete"]
    ):
        raise UpstreamError("owner report current state contradicts its gate booleans")
    if errors and (
        report["integrity_ok"] or report["check_complete"] or report["current"]
    ):
        raise UpstreamError("owner report errors contradict its gate booleans")
    if not errors:
        statuses = [source["status"] for source in sources]
        expected_integrity = not any(
            status
            in {
                "PIN_HASH_MISMATCH",
                "LICENSE_COPY_MISMATCH",
                "VERBATIM_BUNDLE_MISMATCH",
            }
            for status in statuses
        )
        expected_complete = "CHECK_INCOMPLETE" not in statuses
        expected_current = (
            expected_integrity
            and expected_complete
            and "UPDATE" not in statuses
        )
        observed = (
            report["integrity_ok"],
            report["check_complete"],
            report["current"],
        )
        expected = (expected_integrity, expected_complete, expected_current)
        if observed != expected:
            raise UpstreamError(
                "owner report state booleans do not match its source statuses"
            )
    report_digest = report.get("report_digest")
    if report_digest is not None:
        if not isinstance(report_digest, str) or not re.fullmatch(
            digest_pattern, report_digest
        ):
            raise UpstreamError("owner report report_digest is invalid")
        unsigned = dict(report)
        unsigned.pop("report_digest")
        if report_digest != canonical_json_digest(unsigned):
            raise UpstreamError("owner report report_digest does not match its content")


def emit_owner_report(report: dict[str, Any]) -> None:
    validate_owner_report(report)
    unsigned = dict(report)
    unsigned.pop("report_digest", None)
    report["report_digest"] = canonical_json_digest(unsigned)
    validate_owner_report(report)
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if len(encoded.encode()) > MAX_REPORT_BYTES:
        raise UpstreamError(f"owner report exceeds the {MAX_REPORT_BYTES}-byte limit")
    print(encoded)


def owner_report_status(
    sources: list[Source], *, consumer: str, owner_revision: str
) -> int:
    if not re.fullmatch(r"[0-9a-f]{40}", owner_revision):
        raise UpstreamError("owner revision must be a 40-character lowercase commit")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", consumer):
        raise UpstreamError(f"invalid consumer {consumer!r}")
    records = [
        owner_source_record(source)
        for source in sources
        if consumer in source.consumers
    ]
    records.sort(key=lambda record: record["source_id"])
    integrity_failures = {
        "PIN_HASH_MISMATCH",
        "LICENSE_COPY_MISMATCH",
        "VERBATIM_BUNDLE_MISMATCH",
    }
    integrity_ok = not any(
        record["status"] in integrity_failures for record in records
    )
    check_complete = not any(
        record["status"] == "CHECK_INCOMPLETE" for record in records
    )
    current = (
        integrity_ok
        and check_complete
        and not any(record["status"] == "UPDATE" for record in records)
    )
    report = {
        "schema": REPORT_SCHEMA,
        "consumer": consumer,
        "owner_revision": owner_revision,
        "integrity_ok": integrity_ok,
        "check_complete": check_complete,
        "current": current,
        "sources": records,
        "errors": [],
    }
    emit_owner_report(report)
    if not integrity_ok:
        return 2
    if not check_complete:
        return 3
    if not current:
        return 1
    return 0


def contract_failure_report(
    *, consumer: str, owner_revision: str | None, error: UpstreamError
) -> None:
    report = {
        "schema": REPORT_SCHEMA,
        "consumer": consumer,
        "owner_revision": owner_revision,
        "integrity_ok": False,
        "check_complete": False,
        "current": False,
        "sources": [],
        "errors": [redact_report_error(error)],
    }
    emit_owner_report(report)


def status(
    sources: list[Source],
    *,
    as_json: bool,
    consumer: str | None = None,
    owner_revision: str | None = None,
) -> int:
    if consumer is not None:
        if not as_json:
            raise UpstreamError("--consumer requires --json")
        if owner_revision is None:
            raise UpstreamError("consumer reports require an owner revision")
        return owner_report_status(
            sources, consumer=consumer, owner_revision=owner_revision
        )
    records = [inspect_source(source) for source in sources]
    if as_json:
        print(json.dumps({"schema_version": 1, "sources": records}, indent=2, sort_keys=True))
    else:
        for record in records:
            print(
                f"{record['status']:<18} {record['source']:<18} "
                f"{record['pinned_commit'][:12]} -> {record['remote_commit'][:12]} "
                f"({record['skill']})"
            )
    failing = {
        "UPDATE",
        "PIN_HASH_MISMATCH",
        "LICENSE_COPY_MISMATCH",
        "VERBATIM_BUNDLE_MISMATCH",
    }
    return 1 if any(record["status"] in failing for record in records) else 0


def find_source(sources: list[Source], name: str) -> Source:
    matches = [source for source in sources if source.name == name]
    if not matches:
        choices = ", ".join(x.name for x in sources)
        raise UpstreamError(f"unknown source {name!r}; choose from {choices}")
    return matches[0]


def show_diff(source: Source) -> int:
    if source.watch == "stable_tags":
        remote, _, pin_moved = resolve_stable_tag(source)
        if pin_moved:
            raise UpstreamError(f"{source.name}: pinned release tag moved")
    elif source.watch == "npm_releases":
        remote, _, _, pin_moved = npm_release_candidate(source)
        if pin_moved:
            raise UpstreamError(f"{source.name}: pinned npm source or integrity changed")
    else:
        remote = resolve_remote(source)
    _, local_license = validated_license_copy(source)
    with Checkout(source) as checkout:
        pinned_hash = checkout.path_hash(source.commit)
        if pinned_hash != source.path_tree_hash:
            raise UpstreamError(
                f"{source.name}: manifest hash {source.path_tree_hash} does not match pinned "
                f"tree {pinned_hash}; repair provenance before reviewing a newer commit"
            )
        pinned_license = (
            b""
            if source.license_optional
            else checkout.file_bytes(source.commit, source.upstream_license_path)
        )
        if local_license != pinned_license:
            raise UpstreamError(
                f"{source.name}: local license copy {source.local_license_path} does not "
                "match the pinned upstream license; repair provenance before review"
            )
        require_verbatim_bundle_match(
            source, checkout, source.commit, purpose="reviewing a newer commit"
        )
        remote_hash = checkout.path_hash(remote)
        stat, patch = checkout.diff(source.commit, remote)
    print(f"source: {source.name}")
    print(f"repository: {source.repository}")
    print(f"commits: {source.commit}..{remote}")
    print(f"relevant hash: {source.path_tree_hash} -> {remote_hash}")
    if source.commit == remote:
        print("tracking ref is already pinned")
    elif remote_hash == source.path_tree_hash:
        print("repository moved, but the declared source paths are unchanged")
    else:
        print("\n" + (stat.rstrip() or "(no stat output)"))
        print("\n" + (patch.rstrip() or "(no patch output)"))
    return 0


def updated_manifest_text(
    source: Source, commit: str, tree_hash: str, *, selector: str | None = None
) -> str:
    text = source.manifest.read_text()
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.strip() == "[[sources]]"]
    starts.append(len(lines))
    selected: tuple[int, int] | None = None
    for offset in range(len(starts) - 1):
        start, end = starts[offset], starts[offset + 1]
        block = "".join(lines[start:end])
        try:
            parsed = tomllib.loads(block)
        except tomllib.TOMLDecodeError as error:
            raise UpstreamError(
                f"cannot isolate source table in {source.manifest}: {error}"
            ) from error
        tables = parsed.get("sources", [])
        if len(tables) == 1 and tables[0].get("name") == source.name:
            selected = (start, end)
            break
    if selected is None:
        raise UpstreamError(f"cannot find source {source.name!r} in {source.manifest}")
    start, end = selected
    replaced_commit = False
    replaced_hash = False
    replaced_selector = selector is None
    for index in range(start, end):
        if re.match(r"^\s*commit\s*=", lines[index]):
            newline = "\n" if lines[index].endswith("\n") else ""
            lines[index] = f'commit = "{commit}"{newline}'
            replaced_commit = True
        elif re.match(r"^\s*path_tree_hash\s*=", lines[index]):
            newline = "\n" if lines[index].endswith("\n") else ""
            lines[index] = f'path_tree_hash = "{tree_hash}"{newline}'
            replaced_hash = True
        elif selector is not None and re.match(r"^\s*ref\s*=", lines[index]):
            newline = "\n" if lines[index].endswith("\n") else ""
            lines[index] = f'ref = "{selector}"{newline}'
            replaced_selector = True
    if not replaced_commit or not replaced_hash or not replaced_selector:
        raise UpstreamError(f"source {source.name!r} lacks replaceable commit/hash fields")
    result = "".join(lines)
    try:
        tomllib.loads(result)
    except tomllib.TOMLDecodeError as error:
        raise UpstreamError(f"updated manifest would be invalid: {error}") from error
    return result


def write_provenance(
    source: Source,
    commit: str,
    tree_hash: str,
    upstream_license: bytes,
    *,
    selector: str | None = None,
) -> None:
    """Update the pin, notice, and exact license copy as one reviewed operation."""

    notice, old_notice = validated_notice(source)
    local_license, _ = validated_license_copy(source)
    manifest_text = updated_manifest_text(source, commit, tree_hash, selector=selector)
    notice_text = old_notice.replace(source.commit, commit, 1)
    if selector is not None:
        old_tag = source.ref.removeprefix("refs/tags/")
        new_tag = selector.removeprefix("refs/tags/")
        if old_tag != new_tag and notice_text.count(old_tag) == 1:
            notice_text = notice_text.replace(old_tag, new_tag, 1)
    if notice_text.count(commit) != 1:
        raise UpstreamError(
            f"updating {notice} would not leave exactly one reference to {commit}"
        )

    paths_and_bytes: tuple[tuple[Path, bytes], ...] = (
        (source.manifest, manifest_text.encode()),
        (notice, notice_text.encode()),
    )
    if local_license is not None:
        paths_and_bytes += ((local_license, upstream_license),)
    originals = {
        path: (path.read_bytes(), path.stat().st_mode & 0o777)
        for path, _ in paths_and_bytes
    }
    temporaries: list[tuple[Path, Path]] = []
    replaced: list[Path] = []
    try:
        for path, replacement in paths_and_bytes:
            temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
            temporary.write_bytes(replacement)
            os.chmod(temporary, path.stat().st_mode & 0o777)
            temporaries.append((temporary, path))
        try:
            # All replacements have been prepared and validated before any
            # tracked file changes.
            for temporary, path in temporaries:
                os.replace(temporary, path)
                replaced.append(path)
        except OSError as error:
            rollback_errors: list[str] = []
            for path in reversed(replaced):
                data, mode = originals[path]
                rollback = path.with_name(f".{path.name}.rollback-{os.getpid()}")
                try:
                    rollback.write_bytes(data)
                    os.chmod(rollback, mode)
                    os.replace(rollback, path)
                except OSError as rollback_error:
                    rollback_errors.append(f"{path}: {rollback_error}")
                finally:
                    if rollback.exists():
                        rollback.unlink()
            detail = f"cannot update manifest, notice, and license copy: {error}"
            if rollback_errors:
                detail += "; rollback failed for " + ", ".join(rollback_errors)
            raise UpstreamError(detail) from error
    finally:
        for temporary, _ in temporaries:
            if temporary.exists():
                temporary.unlink()


def record(source: Source, commit: str, *, accept_license_change: bool = False) -> int:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise UpstreamError("record requires an immutable 40-character lowercase commit")
    if source.watch == "npm_releases":
        raise UpstreamError(
            f"{source.name}: npm release updates must revise the package, source, SRI, "
            "local contract, and runtime together before recording"
        )
    selector: str | None = None
    if source.watch == "stable_tags":
        candidate, selector, pin_moved = resolve_stable_tag(source)
        if pin_moved:
            raise UpstreamError(f"{source.name}: pinned release tag moved")
        if candidate != commit:
            raise UpstreamError(
                f"{source.name}: record commit {commit} is not newest stable release {candidate}"
            )
    _, local_license = validated_license_copy(source)
    with Checkout(source) as checkout:
        pinned_hash = checkout.path_hash(source.commit)
        if pinned_hash != source.path_tree_hash:
            raise UpstreamError(
                f"{source.name}: manifest hash {source.path_tree_hash} does not match pinned "
                f"tree {pinned_hash}; repair provenance before recording a newer commit"
            )
        pinned_license = (
            b""
            if source.license_optional
            else checkout.file_bytes(source.commit, source.upstream_license_path)
        )
        if local_license != pinned_license:
            raise UpstreamError(
                f"{source.name}: local license copy {source.local_license_path} does not "
                "match the pinned upstream license; repair provenance before recording"
            )
        require_verbatim_bundle_match(source, checkout, source.commit, purpose="recording")
        tree_hash = checkout.path_hash(commit)
        upstream_license = (
            b""
            if source.license_optional
            else checkout.file_bytes(commit, source.upstream_license_path)
        )
        if upstream_license != pinned_license and not accept_license_change:
            raise UpstreamError(
                f"{source.name}: upstream license bytes changed at {commit}; review the "
                "license diff and the manifest/notice license wording, then re-run record "
                "with --accept-license-change"
            )
        target_mismatches = verbatim_bundle_mismatches(source, checkout, commit)
        if target_mismatches:
            preview = "; ".join(target_mismatches[:8])
            if len(target_mismatches) > 8:
                preview += f"; and {len(target_mismatches) - 8} more"
            raise UpstreamError(
                f"{source.name}: record would pin a commit that does not match the local "
                f"verbatim bundle: {preview}; use the sync command for reviewed bundle updates"
            )
    write_provenance(
        source, commit, tree_hash, upstream_license, selector=selector
    )
    print(f"recorded {source.name} at {commit}")
    print(f"path_tree_hash = {tree_hash}")
    print(f"updated {notice_path(source)}")
    if not source.license_optional:
        print(f"updated {license_copy_path(source)} from {source.upstream_license_path}")
    print("composed skill content was not changed; review and update it separately, then run evals")
    return 0


def updated_notice_text(
    source: Source, commit: str, *, selector: str | None = None
) -> str:
    notice, old_notice = validated_notice(source)
    text = old_notice.replace(source.commit, commit, 1)
    if selector is not None:
        old_tag = source.ref.removeprefix("refs/tags/")
        new_tag = selector.removeprefix("refs/tags/")
        if old_tag != new_tag and text.count(old_tag) == 1:
            text = text.replace(old_tag, new_tag, 1)
    if text.count(commit) != 1:
        raise UpstreamError(
            f"updating {notice} would not leave exactly one reference to {commit}"
        )
    return text


def materialize_bundle(
    source: Source,
    files: dict[str, BundleFile],
    destination: Path,
    *,
    manifest_text: str,
    notice_text: str,
    upstream_license: bytes,
) -> None:
    """Build a complete replacement tree without reading from it while it changes."""

    bundle = source.verbatim_bundle
    if bundle is None:
        raise UpstreamError(f"{source.name}: source does not declare a verbatim bundle")
    destination.mkdir(mode=0o700)
    for member in bundle.members:
        (destination / member).mkdir(mode=0o755)
    for item in files.values():
        path = destination / item.local_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        path.write_bytes(item.content)
        os.chmod(path, item.mode)

    current_root = bundle_root(source)
    for control in bundle.control_paths:
        old_path = current_root / control
        new_path = destination / control
        new_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        try:
            old_mode = old_path.lstat().st_mode
        except OSError as error:
            raise UpstreamError(
                f"{source.name}: cannot stage control path {old_path}: {error}"
            ) from error
        if old_path.is_symlink() or not stat_module.S_ISREG(old_mode):
            raise UpstreamError(f"{source.name}: unsafe control path {old_path}")
        new_path.write_bytes(old_path.read_bytes())
        os.chmod(new_path, old_mode & 0o777)

    for additive in bundle.additive_files:
        old_path = current_root / additive
        new_path = destination / additive
        new_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        try:
            old_mode = old_path.lstat().st_mode
        except OSError as error:
            raise UpstreamError(
                f"{source.name}: cannot stage additive file {old_path}: {error}"
            ) from error
        if old_path.is_symlink() or not stat_module.S_ISREG(old_mode):
            raise UpstreamError(f"{source.name}: unsafe additive file {old_path}")
        new_path.write_bytes(old_path.read_bytes())
        os.chmod(new_path, old_mode & 0o777)

    replacements = {
        f"{source.skill}/upstreams.toml": manifest_text.encode(),
        f"{source.skill}/THIRD_PARTY_NOTICES.md": notice_text.encode(),
        f"{source.skill}/{source.local_license_path}": upstream_license,
    }
    for relative, content in replacements.items():
        path = destination / relative
        path.write_bytes(content)


def replace_bundle_transaction(source: Source, staged_root: Path, transaction: Path) -> None:
    """Replace all bundle members with rollback on an interrupted rename sequence."""

    bundle = source.verbatim_bundle
    if bundle is None:
        raise UpstreamError(f"{source.name}: source does not declare a verbatim bundle")
    local_root = bundle_root(source)
    old_root = transaction / "old"
    old_root.mkdir(mode=0o700)
    moved_old: list[str] = []
    installed: list[str] = []
    try:
        for member in bundle.members:
            os.replace(local_root / member, old_root / member)
            moved_old.append(member)
        for member in bundle.members:
            os.replace(staged_root / member, local_root / member)
            installed.append(member)
    except OSError as error:
        rollback_errors: list[str] = []
        for member in reversed(installed):
            try:
                os.replace(local_root / member, staged_root / member)
            except OSError as rollback_error:
                rollback_errors.append(f"remove replacement {member}: {rollback_error}")
        for member in reversed(moved_old):
            try:
                os.replace(old_root / member, local_root / member)
            except OSError as rollback_error:
                rollback_errors.append(f"restore {member}: {rollback_error}")
        detail = f"cannot replace verbatim bundle: {error}"
        if rollback_errors:
            detail += "; rollback failed: " + "; ".join(rollback_errors)
            detail += f"; recovery files retained at {transaction}"
        else:
            shutil.rmtree(transaction, ignore_errors=True)
        raise UpstreamError(detail) from error


def sync(source: Source, commit: str, *, accept_license_change: bool = False) -> int:
    """Update a reviewed verbatim bundle and its provenance as one transaction."""

    if source.verbatim_bundle is None:
        raise UpstreamError(f"{source.name}: source does not declare a verbatim bundle")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise UpstreamError("sync requires an immutable 40-character lowercase commit")
    selector: str | None = None
    if source.watch == "stable_tags":
        candidate, selector, pin_moved = resolve_stable_tag(source)
        if pin_moved:
            raise UpstreamError(f"{source.name}: pinned release tag moved")
        if candidate != commit:
            raise UpstreamError(
                f"{source.name}: sync commit {commit} is not newest stable release {candidate}"
            )
    _, local_license = validated_license_copy(source)
    with Checkout(source) as checkout:
        pinned_hash = checkout.path_hash(source.commit)
        if pinned_hash != source.path_tree_hash:
            raise UpstreamError(
                f"{source.name}: manifest hash {source.path_tree_hash} does not match pinned "
                f"tree {pinned_hash}; repair provenance before syncing"
            )
        pinned_license = checkout.file_bytes(source.commit, source.upstream_license_path)
        if local_license != pinned_license:
            raise UpstreamError(
                f"{source.name}: local license copy {source.local_license_path} does not "
                "match the pinned upstream license; repair provenance before syncing"
            )
        require_verbatim_bundle_match(source, checkout, source.commit, purpose="syncing")
        tree_hash = checkout.path_hash(commit)
        upstream_license = checkout.file_bytes(commit, source.upstream_license_path)
        if upstream_license != pinned_license and not accept_license_change:
            raise UpstreamError(
                f"{source.name}: upstream license bytes changed at {commit}; review the "
                "license diff and attribution wording, then re-run sync with "
                "--accept-license-change"
            )
        files = checkout.bundle_files(commit, source.verbatim_bundle)

    manifest_text = updated_manifest_text(
        source, commit, tree_hash, selector=selector
    )
    notice_text = updated_notice_text(source, commit, selector=selector)
    local_root = bundle_root(source)
    transaction = Path(
        tempfile.mkdtemp(prefix=".khenrix-upstream-sync-", dir=local_root)
    )
    staged_root = transaction / "new"
    completed = False
    try:
        materialize_bundle(
            source,
            files,
            staged_root,
            manifest_text=manifest_text,
            notice_text=notice_text,
            upstream_license=upstream_license,
        )
        with Checkout(source) as checkout:
            staged_mismatches = verbatim_bundle_mismatches(
                source, checkout, commit, root=staged_root
            )
        if staged_mismatches:
            raise UpstreamError(
                f"{source.name}: staged verbatim bundle failed verification: "
                + "; ".join(staged_mismatches[:8])
            )
        replace_bundle_transaction(source, staged_root, transaction)
        completed = True
    finally:
        if completed and transaction.exists():
            shutil.rmtree(transaction)
        elif transaction.exists() and not (transaction / "old").exists():
            shutil.rmtree(transaction)

    print(f"synced {source.name} verbatim bundle at {commit}")
    print(f"path_tree_hash = {tree_hash}")
    print(f"updated {len(source.verbatim_bundle.members)} skill directories and provenance")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    commands = result.add_subparsers(dest="command", required=True)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    status_parser.add_argument("--consumer")
    diff_parser = commands.add_parser("diff")
    diff_parser.add_argument("source")
    record_parser = commands.add_parser("record")
    record_parser.add_argument("source")
    record_parser.add_argument("commit")
    record_parser.add_argument(
        "--accept-license-change",
        action="store_true",
        help="confirm that changed upstream license bytes and attribution wording were reviewed",
    )
    sync_parser = commands.add_parser("sync")
    sync_parser.add_argument("source")
    sync_parser.add_argument("commit")
    sync_parser.add_argument(
        "--accept-license-change",
        action="store_true",
        help="confirm that changed upstream license bytes and attribution wording were reviewed",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    owner_revision: str | None = None
    try:
        repo_root = options.repo_root.resolve()
        sources = load_sources(repo_root)
        if options.command == "status":
            if options.consumer is not None:
                if not options.json:
                    raise UpstreamError("--consumer requires --json")
                owner_revision = run_git_bounded(
                    ["rev-parse", "HEAD"], cwd=repo_root
                ).strip()
            return status(
                sources,
                as_json=options.json,
                consumer=options.consumer,
                owner_revision=owner_revision,
            )
        source = find_source(sources, options.source)
        if options.command == "diff":
            return show_diff(source)
        if options.command == "sync":
            return sync(
                source,
                options.commit,
                accept_license_change=options.accept_license_change,
            )
        return record(
            source,
            options.commit,
            accept_license_change=options.accept_license_change,
        )
    except UpstreamError as error:
        if (
            options.command == "status"
            and options.json
            and options.consumer is not None
        ):
            contract_failure_report(
                consumer=options.consumer,
                owner_revision=owner_revision,
                error=error,
            )
            return 2
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
