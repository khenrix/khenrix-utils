#!/usr/bin/env python3
"""Check the Council/Forge source baseline and opt-in installed CLI characterization."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable


SCHEMA_VERSION = 2
CHARACTERIZATION_SCHEMA_VERSION = 1
CLI_COMMANDS = ("agy", "claude", "codex")
SOURCE_ROOTS = (
    "shared/lib/council",
    "shared/lib/forge",
    "shared/skills/llm-council",
    "shared/skills/llm-forge",
)
FIXTURE_PATHS = (
    "evals/llm-council/evals.json",
    "evals/llm-forge/evals.json",
    "evals/llm-forge/fixtures",
)
RECEIPT_ROOTS = ("evals/llm-council", "evals/llm-forge")
HISTORICAL_REVISION = "f063e2f4dc6c976b3dd314685642678d8c8ab7bc"
HISTORICAL_LOCK = "evals/llm-fanout/baseline-lock.json"
HISTORICAL_LOCK_SHA256 = "b8b02b98daace4e7d898cd669dcda17f724e3be525088065616f73eac22e397d"


class BaselineError(RuntimeError):
    """The legacy comparison surface could not be measured."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(root: Path, relative: str) -> Iterable[tuple[str, Path]]:
    path = root / relative
    if path.is_file():
        yield relative, path
        return
    if not path.is_dir():
        raise BaselineError(f"baseline input is missing: {relative}")
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        yield child.relative_to(root).as_posix(), child


def _hash_paths(root: Path, paths: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in paths:
        for file_relative, path in _files(root, relative):
            if "__pycache__" not in path.parts:
                result[file_relative] = _sha256(path)
    return dict(sorted(result.items()))


def _receipt_paths(root: Path) -> Iterable[str]:
    for relative in RECEIPT_ROOTS:
        directory = root / relative
        if not directory.is_dir():
            raise BaselineError(f"baseline receipt directory is missing: {relative}")
        for path in sorted(directory.glob("*receipt.json")):
            yield path.relative_to(root).as_posix()


def _installed_output(command: str, flag: str) -> bytes:
    try:
        completed = subprocess.run(
            [command, flag],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            env={**os.environ, "AGY_CLI_DISABLE_AUTO_UPDATE": "true"} if command == "agy" else None,
        )
    except FileNotFoundError as error:
        raise BaselineError(f"installed CLI is unavailable: {command}") from error
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise BaselineError(f"installed CLI {flag} failed: {command}") from error
    output = completed.stdout or completed.stderr
    if not output:
        raise BaselineError(f"installed CLI {flag} returned no output: {command}")
    return output


def installed_help(command: str) -> bytes:
    """Read a CLI help surface only when characterization is requested."""
    return _installed_output(command, "--help")


def installed_version(command: str) -> bytes:
    return _installed_output(command, "--version")


def build_lock(root: Path) -> dict[str, object]:
    """Return hashes for every legacy comparison input, in canonical key order."""
    root = root.resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "sources": _hash_paths(root, SOURCE_ROOTS),
        "fixtures": _hash_paths(root, FIXTURE_PATHS),
        "receipts": _hash_paths(root, _receipt_paths(root)),
    }


def check_lock(expected: dict[str, object], root: Path) -> list[str]:
    """Return sorted category/path mismatches without changing a measured input."""
    actual = build_lock(root)
    if expected.get("schema_version") != SCHEMA_VERSION:
        return ["schema_version"]

    mismatches: list[str] = []
    for category in ("sources", "fixtures", "receipts"):
        expected_hashes = expected.get(category)
        actual_hashes = actual[category]
        if not isinstance(expected_hashes, dict):
            mismatches.append(category)
            continue
        for name in sorted(set(expected_hashes) | set(actual_hashes)):
            if expected_hashes.get(name) != actual_hashes.get(name):
                mismatches.append(f"{category}: {name}")
    return mismatches


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _historical_files(expected: dict[str, object]) -> dict[str, str]:
    if hashlib.sha256(_canonical(expected)).hexdigest() != HISTORICAL_LOCK_SHA256:
        raise BaselineError("historical expected lock differs from the unchanged original lock")
    files = {path: digest for category in ("sources", "fixtures", "receipts")
             for path, digest in expected[category].items()}
    if len(files) != 59:
        raise BaselineError("historical lock must name exactly 59 original blobs")
    return files


def _private_path(path: Path, *, directory: bool) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise BaselineError(f"historical snapshot input is unavailable: {path}") from error
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not valid_type or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or (not directory and info.st_nlink != 1)):
        raise BaselineError(f"historical snapshot input is unsafe: {path}")


def _historical_provenance(root: Path, files: dict[str, str]) -> dict[str, object]:
    blob_ids = {}
    for relative in sorted(files):
        data = (root / relative).read_bytes()
        blob_ids[relative] = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    return {"schema_version": 1, "subject": "historical-source-blobs",
            "revision": HISTORICAL_REVISION, "lock_sha256": HISTORICAL_LOCK_SHA256,
            "blob_count": 59, "snapshot_name": root.name,
            "snapshot_sha256": hashlib.sha256(_canonical(files)).hexdigest(),
            "git_blob_ids": blob_ids}


def prepare_historical(repo: Path, root: Path, *, revision: str = HISTORICAL_REVISION,
                       source: Path | None = None) -> None:
    """Copy the unchanged original subject from an explicit source or offline Git."""
    if revision != HISTORICAL_REVISION:
        raise BaselineError("historical preparation requires the exact original revision")
    if ".." in root.parts:
        raise BaselineError("historical snapshot path must not contain parent traversal")
    root = root.absolute()
    if root.exists() or root.is_symlink():
        raise BaselineError("historical preparation requires a fresh snapshot root")
    if root.is_relative_to(repo.resolve()):
        raise BaselineError("historical snapshot must be outside the active checkout")
    if any(parent.is_symlink() for parent in root.parents):
        raise BaselineError("historical snapshot ancestors must not be symlinks")

    def git_blob(relative: str) -> bytes:
        try:
            return subprocess.run(["git", "-C", str(repo), "show", f"{revision}:{relative}"],
                                  check=True, capture_output=True).stdout
        except (subprocess.CalledProcessError, FileNotFoundError) as error:
            raise BaselineError(f"original historical Git blob is unavailable: {relative}") from error

    source_files = set()
    source_directories = set()
    if source is not None:
        if ".." in source.parts:
            raise BaselineError("historical source path must not contain parent traversal")
        source = source.absolute()
        if source.is_symlink() or any(parent.is_symlink() for parent in source.parents):
            raise BaselineError("historical source and ancestors must not be symlinks")

        def walk_error(error: OSError) -> None:
            raise error

        try:
            if not stat.S_ISDIR(source.lstat().st_mode):
                raise BaselineError("historical source must be a directory")
            for directory, dirs, names in os.walk(source, onerror=walk_error):
                for name in dirs + names:
                    path = Path(directory) / name
                    info = path.lstat()
                    relative = path.relative_to(source).as_posix()
                    if stat.S_ISDIR(info.st_mode):
                        source_directories.add(relative)
                    elif stat.S_ISREG(info.st_mode):
                        source_files.add(relative)
                    else:
                        raise BaselineError(f"historical source entry is unsafe: {relative}")
        except OSError as error:
            raise BaselineError(f"historical source is unavailable: {source}") from error

    def source_blob(relative: str) -> bytes:
        if source is None:
            return git_blob(relative)
        try:
            return (source / relative).read_bytes()
        except OSError as error:
            raise BaselineError(f"historical source blob is unavailable: {relative}") from error

    lock_bytes = source_blob(HISTORICAL_LOCK)
    if hashlib.sha256(lock_bytes).hexdigest() != HISTORICAL_LOCK_SHA256:
        raise BaselineError("original historical lock digest differs")
    expected = json.loads(lock_bytes)
    files = _historical_files(expected)
    if source is not None:
        paths = set(files) | {HISTORICAL_LOCK}
        directories = {parent.as_posix() for relative in paths
                       for parent in Path(relative).parents if parent != Path(".")}
        if source_files != paths or source_directories != directories:
            raise BaselineError("historical source must contain only the exact 60 original files and their directories")
    blobs = {relative: source_blob(relative) for relative in files}
    if any(hashlib.sha256(blobs[path]).hexdigest() != digest for path, digest in files.items()):
        raise BaselineError("original revision does not match the unchanged historical lock")
    if not root.parent.exists():
        root.parent.mkdir(mode=0o700)
    _private_path(root.parent, directory=True)
    provenance = root.parent / "provenance.json"
    if provenance.exists() or provenance.is_symlink():
        raise BaselineError("historical preparation requires fresh provenance")
    root.mkdir(mode=0o700)
    for relative, data in {**blobs, HISTORICAL_LOCK: lock_bytes}.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # pathlib's intermediate parents use the ambient mode; normalize this owned tree.
        for parent in target.parents:
            if parent == root.parent:
                break
            parent.chmod(0o700)
        with target.open("xb") as handle:
            handle.write(data)
        target.chmod(0o600)
    with provenance.open("xb") as handle:
        handle.write(_canonical(_historical_provenance(root, files)))
    provenance.chmod(0o600)
    mismatches = check_historical(expected, root)
    if mismatches:
        raise BaselineError("prepared historical snapshot differs: " + ", ".join(mismatches))


def check_historical(expected: dict[str, object], root: Path) -> list[str]:
    """Validate the private source subject, then use check_lock as the content authority."""
    files = _historical_files(expected)
    root = root.absolute()
    if any(parent.is_symlink() for parent in root.parents):
        raise BaselineError("historical snapshot ancestors must not be symlinks")
    _private_path(root.parent, directory=True)
    _private_path(root, directory=True)
    provenance = root.parent / "provenance.json"
    _private_path(provenance, directory=False)
    found = set()
    for path in root.rglob("*"):
        _private_path(path, directory=path.is_dir())
        if path.is_file():
            found.add(path.relative_to(root).as_posix())
    missing = (set(files) | {HISTORICAL_LOCK}) - found
    if missing:
        raise BaselineError("historical snapshot files are missing: " + ", ".join(sorted(missing)))
    if _sha256(root / HISTORICAL_LOCK) != HISTORICAL_LOCK_SHA256:
        raise BaselineError("historical snapshot lock differs from the unchanged original")
    extra = found - (set(files) | {HISTORICAL_LOCK})
    mismatches = sorted(f"extra: {path}" for path in extra) + check_lock(expected, root)
    if mismatches:
        return mismatches
    if _read_lock(provenance) != _historical_provenance(root, files):
        raise BaselineError("historical snapshot provenance differs from the original source subject")
    return []


def build_characterization(
    *,
    help_runner: Callable[[str], bytes] = installed_help,
    version_runner: Callable[[str], bytes] = installed_version,
) -> dict[str, object]:
    """Record volatile installed CLI surfaces outside the deterministic lock."""
    clis = {}
    for command in CLI_COMMANDS:
        version = version_runner(command).decode("utf-8").strip()
        if not version:
            raise BaselineError(f"installed CLI version is empty: {command}")
        clis[command] = {
            "version": version,
            "help_sha256": hashlib.sha256(help_runner(command)).hexdigest(),
        }
    return {"schema_version": CHARACTERIZATION_SCHEMA_VERSION, "clis": clis}


def check_characterization(
    expected: dict[str, object],
    *,
    help_runner: Callable[[str], bytes] = installed_help,
    version_runner: Callable[[str], bytes] = installed_version,
) -> list[str]:
    actual = build_characterization(help_runner=help_runner, version_runner=version_runner)
    if expected.get("schema_version") != CHARACTERIZATION_SCHEMA_VERSION:
        return ["schema_version"]
    expected_clis = expected.get("clis")
    if not isinstance(expected_clis, dict):
        return ["clis"]
    mismatches = []
    for command in sorted(set(expected_clis) | set(actual["clis"])):
        expected_fields = expected_clis.get(command)
        actual_fields = actual["clis"].get(command)
        if not isinstance(expected_fields, dict) or not isinstance(actual_fields, dict):
            mismatches.append(f"clis: {command}")
            continue
        for field in sorted(set(expected_fields) | set(actual_fields)):
            if expected_fields.get(field) != actual_fields.get(field):
                mismatches.append(f"clis: {command}.{field}")
    return mismatches


def _read_lock(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BaselineError(f"cannot read baseline lock: {path}") from error
    if not isinstance(data, dict):
        raise BaselineError(f"baseline lock must be a JSON object: {path}")
    return data


def _write_lock(path: Path, lock: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "check", "characterize", "check-characterization",
                                            "prepare-historical", "check-historical"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--lock", type=Path, default=Path("evals/llm-fanout/baseline-lock.json"))
    parser.add_argument("--receipt", type=Path, default=Path("evals/llm-fanout/cli-characterization-receipt.json"))
    parser.add_argument("--historical-root", type=Path,
                        default=os.environ.get("KHENRIX_HISTORICAL_BASELINE_ROOT"))
    parser.add_argument("--historical-source", type=Path,
                        help="exact original file tree for prepare-historical; otherwise use offline Git")
    parser.add_argument("--revision", default=HISTORICAL_REVISION)
    args = parser.parse_args(argv)
    lock_path = args.lock if args.lock.is_absolute() else args.root / args.lock
    receipt_path = args.receipt if args.receipt.is_absolute() else args.root / args.receipt
    try:
        if args.historical_source is not None and args.command != "prepare-historical":
            raise BaselineError("--historical-source is only valid for prepare-historical")
        if args.command in ("prepare-historical", "check-historical"):
            if args.historical_root is None:
                raise BaselineError("explicit --historical-root or KHENRIX_HISTORICAL_BASELINE_ROOT is required")
            if args.command == "prepare-historical":
                prepare_historical(args.root, args.historical_root, revision=args.revision,
                                   source=args.historical_source)
                return 0
            mismatches = check_historical(_read_lock(lock_path), args.historical_root)
        elif args.command == "generate":
            _write_lock(lock_path, build_lock(args.root))
            return 0
        elif args.command == "characterize":
            _write_lock(receipt_path, build_characterization())
            return 0
        elif args.command == "check-characterization":
            mismatches = check_characterization(_read_lock(receipt_path))
        else:
            mismatches = check_lock(_read_lock(lock_path), args.root)
    except BaselineError as error:
        print(f"fanout baseline: {error}", file=sys.stderr)
        return 2
    if mismatches:
        print("fanout baseline mismatch:", file=sys.stderr)
        print("\n".join(mismatches), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
