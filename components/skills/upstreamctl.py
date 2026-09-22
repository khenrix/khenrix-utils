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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


GIT_TIMEOUT_SECONDS = 30
MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024
REPORT_SCHEMA = "khenrix-upstreams/v2"
DEFAULT_CONSUMER = "agentic-setup"


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


@dataclass(frozen=True)
class BundleFile:
    local_path: str
    mode: int
    content: bytes


def notice_path(source: Source) -> Path:
    return source.manifest.parent / "THIRD_PARTY_NOTICES.md"


def license_copy_path(source: Source) -> Path:
    return source.manifest.parent / source.local_license_path


def validated_license_copy(source: Source) -> tuple[Path, bytes]:
    """Read the exact vendored license copy without following a repository symlink."""

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


def checked_git_output(
    returncode: int, stdout: bytes, stderr: bytes, arguments: list[str]
) -> bytes:
    operation = arguments[0] if arguments else "command"
    if returncode:
        detail = (stderr or stdout).decode("utf-8", "replace").strip()
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


def run_git(arguments: list[str], *, cwd: Path | None = None) -> str:
    returncode, stdout, stderr = bounded_git_process(arguments, cwd=cwd)
    return checked_git_output(returncode, stdout, stderr, arguments).decode(
        "utf-8", "replace"
    )


def run_git_bytes(arguments: list[str], *, cwd: Path | None = None) -> bytes:
    returncode, stdout, stderr = bounded_git_process(arguments, cwd=cwd)
    return checked_git_output(returncode, stdout, stderr, arguments)


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
    allowed = required | {"overlays"}
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


def default_web_url(repository: str) -> str:
    if repository.startswith("git@") and ":" in repository:
        host, path = repository[4:].split(":", 1)
        return f"https://{host}/{path.removesuffix('.git')}"
    if repository.startswith(("http://", "https://")):
        return repository.removesuffix(".git")
    return repository


def validate_fetch_url(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise UpstreamError(f"{label} must be a non-empty string")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme in {"http", "https"}:
        if parsed.username is not None or parsed.password is not None:
            raise UpstreamError(f"{label} may not contain credentials")
        if not parsed.hostname or parsed.query or parsed.fragment:
            raise UpstreamError(f"{label} must be a credential-safe repository URL")
    elif parsed.scheme == "ssh" and parsed.password is not None:
        raise UpstreamError(f"{label} may not contain credentials")
    return value


def load_sources(repo_root: Path) -> list[Source]:
    sources: list[Source] = []
    seen: set[str] = set()
    seen_license_copies: set[Path] = set()
    manifest_roots = (
        repo_root / "shared" / "skills",
        repo_root / "shared" / "superpowers",
    )
    manifests = sorted(
        manifest
        for root in manifest_roots
        for manifest in root.glob("*/upstreams.toml")
    )
    for manifest in manifests:
        try:
            with manifest.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise UpstreamError(f"cannot parse {manifest}: {error}") from error
        raw_sources = data.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise UpstreamError(f"{manifest} must contain at least one [[sources]] table")
        for index, raw in enumerate(raw_sources):
            if not isinstance(raw, dict):
                raise UpstreamError(f"{manifest} sources[{index}] is not a table")
            required = {
                "name",
                "repository",
                "ref",
                "commit",
                "paths",
                "path_tree_hash",
                "license",
                "upstream_license_path",
                "local_license_path",
                "adaptation",
            }
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
            upstream_license_path = safe_relative_path(
                raw["upstream_license_path"],
                label=f"{manifest} source {name} upstream_license_path",
            )
            local_license_path = safe_relative_path(
                raw["local_license_path"],
                label=f"{manifest} source {name} local_license_path",
            )
            if PurePosixPath(local_license_path).parts[0] != "licenses":
                raise UpstreamError(
                    f"{manifest} source {name} local_license_path must live under licenses/"
                )
            normalized_paths = tuple(PurePosixPath(path).as_posix() for path in paths)
            if not selected_paths_cover(normalized_paths, upstream_license_path):
                raise UpstreamError(
                    f"{manifest} source {name} upstream license {upstream_license_path!r} "
                    "is outside its reviewed paths"
                )
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
                )
            )
    if not sources:
        raise UpstreamError(
            "no upstream manifests found under shared/skills or shared/superpowers"
        )
    for source in sources:
        validated_notice(source)
        validated_license_copy(source)
    return sources


def resolve_remote(source: Source) -> str:
    refs = [source.ref]
    if source.ref.startswith("refs/tags/"):
        refs.append(source.ref + "^{}")
    output = run_git(["ls-remote", source.fetch_url or source.repository, *refs])
    rows = []
    for line in output.splitlines():
        fields = line.split("\t", 1)
        if len(fields) == 2 and re.fullmatch(r"[0-9a-f]{40}", fields[0]):
            rows.append((fields[0], fields[1]))
    if not rows:
        raise UpstreamError(f"{source.name}: tracking ref {source.ref!r} does not exist")
    peeled = [commit for commit, ref in rows if ref.endswith("^{}")]
    return peeled[0] if peeled else rows[0][0]


class Checkout:
    def __init__(self, source: Source):
        self.source = source
        self._temporary = tempfile.TemporaryDirectory(prefix="khenrix-upstream-")
        self.path = Path(self._temporary.name)
        run_git(["init", "--quiet"], cwd=self.path)
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
        run_git(
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
        actual = run_git(["rev-parse", "FETCH_HEAD^{commit}"], cwd=self.path).strip()
        if actual != commit:
            raise UpstreamError(f"{self.source.name}: fetched {actual}, expected {commit}")
        self._fetched.add(commit)

    def path_hash(self, commit: str) -> str:
        self.fetch(commit)
        output = run_git(["ls-tree", "-r", commit, "--", *self.source.paths], cwd=self.path)
        rows = sorted(line for line in output.splitlines() if line)
        if not rows:
            raise UpstreamError(
                f"{self.source.name}: none of {list(self.source.paths)!r} exist at {commit}"
            )
        payload = "".join(row + "\n" for row in rows).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def file_bytes(self, commit: str, path: str) -> bytes:
        self.fetch(commit)
        return run_git_bytes(["cat-file", "blob", f"{commit}:{path}"], cwd=self.path)

    def bundle_files(self, commit: str, bundle: VerbatimBundle) -> dict[str, BundleFile]:
        """Read a complete portable skill bundle from Git, including executable bits."""

        self.fetch(commit)
        output = run_git_bytes(
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
            content = run_git_bytes(["cat-file", "blob", object_id], cwd=self.path)
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
        stat = run_git(
            ["diff", "--stat", "--find-renames", old, new, "--", *self.source.paths],
            cwd=self.path,
        )
        # License files are copied byte-for-byte and are not guaranteed to be UTF-8.
        # The review display may replace undecodable bytes, while file_bytes() below
        # remains exact for the provenance update itself.
        patch = run_git_bytes(
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
    actual = {path: file for path, file in actual_paths.items() if path not in controls}
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


def inspect_source(source: Source) -> dict[str, Any]:
    remote = resolve_remote(source)
    _, local_license = validated_license_copy(source)
    with Checkout(source) as checkout:
        pinned_hash = checkout.path_hash(source.commit)
        remote_hash = checkout.path_hash(remote)
        pinned_license = checkout.file_bytes(source.commit, source.upstream_license_path)
        bundle_mismatches = verbatim_bundle_mismatches(source, checkout, source.commit)
    if pinned_hash != source.path_tree_hash:
        status = "PIN_HASH_MISMATCH"
    elif local_license != pinned_license:
        status = "LICENSE_COPY_MISMATCH"
    elif bundle_mismatches:
        status = "VERBATIM_BUNDLE_MISMATCH"
    elif remote == source.commit:
        status = "CURRENT"
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
        "declared_path_tree_hash": source.path_tree_hash,
        "pinned_path_tree_hash": pinned_hash,
        "remote_path_tree_hash": remote_hash,
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
        "canonical_digest": source.path_tree_hash,
        "relationship": source.relationship,
        "affected_capability_ids": list(source.affected_capability_ids),
        "update_mode": source.update_mode,
        "review_commands": list(source.review_commands),
    }
    try:
        inspected = inspect_source(source)
    except UpstreamError as error:
        record.update(
            {
                "status": "CHECK_INCOMPLETE",
                "error": str(error)[:4096],
            }
        )
        return record
    record.update(
        {
            "status": inspected["status"],
            "candidate_commit": inspected["remote_commit"],
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
        if source["status"] not in allowed_statuses:
            raise UpstreamError(
                f"owner report sources[{index}] has invalid status {source['status']!r}"
            )
        if not isinstance(source["pinned_commit"], str) or not re.fullmatch(
            r"[0-9a-f]{40}", source["pinned_commit"]
        ):
            raise UpstreamError(
                f"owner report sources[{index}].pinned_commit is invalid"
            )
        for field in ("content_owner", "delivery_owner", "web_url"):
            if not isinstance(source[field], str) or not source[field]:
                raise UpstreamError(
                    f"owner report sources[{index}].{field} must be non-empty"
                )
        validate_fetch_url(
            source["fetch_url"],
            label=f"owner report sources[{index}].fetch_url",
        )
        if source["status"] != "CHECK_INCOMPLETE":
            for field in ("pinned_digest", "candidate_digest"):
                if not isinstance(source.get(field), str) or not re.fullmatch(
                    digest_pattern, source[field]
                ):
                    raise UpstreamError(
                        f"owner report sources[{index}].{field} is invalid"
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
        "errors": [str(error)[:4096]],
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
    remote = resolve_remote(source)
    _, local_license = validated_license_copy(source)
    with Checkout(source) as checkout:
        pinned_hash = checkout.path_hash(source.commit)
        if pinned_hash != source.path_tree_hash:
            raise UpstreamError(
                f"{source.name}: manifest hash {source.path_tree_hash} does not match pinned "
                f"tree {pinned_hash}; repair provenance before reviewing a newer commit"
            )
        pinned_license = checkout.file_bytes(source.commit, source.upstream_license_path)
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


def updated_manifest_text(source: Source, commit: str, tree_hash: str) -> str:
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
    for index in range(start, end):
        if re.match(r"^\s*commit\s*=", lines[index]):
            newline = "\n" if lines[index].endswith("\n") else ""
            lines[index] = f'commit = "{commit}"{newline}'
            replaced_commit = True
        elif re.match(r"^\s*path_tree_hash\s*=", lines[index]):
            newline = "\n" if lines[index].endswith("\n") else ""
            lines[index] = f'path_tree_hash = "{tree_hash}"{newline}'
            replaced_hash = True
    if not replaced_commit or not replaced_hash:
        raise UpstreamError(f"source {source.name!r} lacks replaceable commit/hash fields")
    result = "".join(lines)
    try:
        tomllib.loads(result)
    except tomllib.TOMLDecodeError as error:
        raise UpstreamError(f"updated manifest would be invalid: {error}") from error
    return result


def write_provenance(
    source: Source, commit: str, tree_hash: str, upstream_license: bytes
) -> None:
    """Update the pin, notice, and exact license copy as one reviewed operation."""

    notice, old_notice = validated_notice(source)
    local_license, _ = validated_license_copy(source)
    manifest_text = updated_manifest_text(source, commit, tree_hash)
    notice_text = old_notice.replace(source.commit, commit, 1)
    if notice_text.count(commit) != 1:
        raise UpstreamError(
            f"updating {notice} would not leave exactly one reference to {commit}"
        )

    paths_and_bytes = (
        (source.manifest, manifest_text.encode()),
        (notice, notice_text.encode()),
        (local_license, upstream_license),
    )
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
    _, local_license = validated_license_copy(source)
    with Checkout(source) as checkout:
        pinned_hash = checkout.path_hash(source.commit)
        if pinned_hash != source.path_tree_hash:
            raise UpstreamError(
                f"{source.name}: manifest hash {source.path_tree_hash} does not match pinned "
                f"tree {pinned_hash}; repair provenance before recording a newer commit"
            )
        pinned_license = checkout.file_bytes(source.commit, source.upstream_license_path)
        if local_license != pinned_license:
            raise UpstreamError(
                f"{source.name}: local license copy {source.local_license_path} does not "
                "match the pinned upstream license; repair provenance before recording"
            )
        require_verbatim_bundle_match(source, checkout, source.commit, purpose="recording")
        tree_hash = checkout.path_hash(commit)
        upstream_license = checkout.file_bytes(commit, source.upstream_license_path)
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
    write_provenance(source, commit, tree_hash, upstream_license)
    print(f"recorded {source.name} at {commit}")
    print(f"path_tree_hash = {tree_hash}")
    print(f"updated {notice_path(source)}")
    print(f"updated {license_copy_path(source)} from {source.upstream_license_path}")
    print("composed skill content was not changed; review and update it separately, then run evals")
    return 0


def updated_notice_text(source: Source, commit: str) -> str:
    notice, old_notice = validated_notice(source)
    text = old_notice.replace(source.commit, commit, 1)
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

    manifest_text = updated_manifest_text(source, commit, tree_hash)
    notice_text = updated_notice_text(source, commit)
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
                owner_revision = run_git(["rev-parse", "HEAD"], cwd=repo_root).strip()
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
