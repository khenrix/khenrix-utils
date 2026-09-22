#!/usr/bin/env python3
"""Inspect and record reviewed upstreams for composed Khenrix skills.

Each ``shared/skills/*/upstreams.toml`` stores immutable reviewed commits and a
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
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class UpstreamError(RuntimeError):
    """An invalid manifest or Git operation that cannot be safely guessed."""


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


def run_git(arguments: list[str], *, cwd: Path | None = None) -> str:
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


def load_sources(repo_root: Path) -> list[Source]:
    sources: list[Source] = []
    seen: set[str] = set()
    seen_license_copies: set[Path] = set()
    for manifest in sorted((repo_root / "shared" / "skills").glob("*/upstreams.toml")):
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
                )
            )
    if not sources:
        raise UpstreamError("no shared/skills/*/upstreams.toml manifests found")
    for source in sources:
        validated_notice(source)
        validated_license_copy(source)
    return sources


def resolve_remote(source: Source) -> str:
    refs = [source.ref]
    if source.ref.startswith("refs/tags/"):
        refs.append(source.ref + "^{}")
    output = run_git(["ls-remote", source.repository, *refs])
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
            ["fetch", "--quiet", "--no-tags", "--depth=1", self.source.repository, commit],
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


def inspect_source(source: Source) -> dict[str, Any]:
    remote = resolve_remote(source)
    _, local_license = validated_license_copy(source)
    with Checkout(source) as checkout:
        pinned_hash = checkout.path_hash(source.commit)
        remote_hash = checkout.path_hash(remote)
        pinned_license = checkout.file_bytes(source.commit, source.upstream_license_path)
    if pinned_hash != source.path_tree_hash:
        status = "PIN_HASH_MISMATCH"
    elif local_license != pinned_license:
        status = "LICENSE_COPY_MISMATCH"
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
        "paths": list(source.paths),
        "manifest": str(source.manifest),
    }


def status(sources: list[Source], *, as_json: bool) -> int:
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
    failing = {"UPDATE", "PIN_HASH_MISMATCH", "LICENSE_COPY_MISMATCH"}
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
        tree_hash = checkout.path_hash(commit)
        upstream_license = checkout.file_bytes(commit, source.upstream_license_path)
        if upstream_license != pinned_license and not accept_license_change:
            raise UpstreamError(
                f"{source.name}: upstream license bytes changed at {commit}; review the "
                "license diff and the manifest/notice license wording, then re-run record "
                "with --accept-license-change"
            )
    write_provenance(source, commit, tree_hash, upstream_license)
    print(f"recorded {source.name} at {commit}")
    print(f"path_tree_hash = {tree_hash}")
    print(f"updated {notice_path(source)}")
    print(f"updated {license_copy_path(source)} from {source.upstream_license_path}")
    print("composed skill content was not changed; review and update it separately, then run evals")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    commands = result.add_subparsers(dest="command", required=True)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
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
    return result


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    try:
        sources = load_sources(options.repo_root.resolve())
        if options.command == "status":
            return status(sources, as_json=options.json)
        source = find_source(sources, options.source)
        if options.command == "diff":
            return show_diff(source)
        return record(
            source,
            options.commit,
            accept_license_change=options.accept_license_change,
        )
    except UpstreamError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
