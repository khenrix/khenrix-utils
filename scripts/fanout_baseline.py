#!/usr/bin/env python3
"""Check the Council/Forge source baseline and opt-in installed CLI characterization."""
from __future__ import annotations

import argparse
import hashlib
import json
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
    parser.add_argument("command", choices=("generate", "check", "characterize", "check-characterization"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--lock", type=Path, default=Path("evals/llm-fanout/baseline-lock.json"))
    parser.add_argument("--receipt", type=Path, default=Path("evals/llm-fanout/cli-characterization-receipt.json"))
    args = parser.parse_args(argv)
    lock_path = args.lock if args.lock.is_absolute() else args.root / args.lock
    receipt_path = args.receipt if args.receipt.is_absolute() else args.root / args.receipt
    try:
        if args.command == "generate":
            _write_lock(lock_path, build_lock(args.root))
            return 0
        if args.command == "characterize":
            _write_lock(receipt_path, build_characterization())
            return 0
        if args.command == "check-characterization":
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
