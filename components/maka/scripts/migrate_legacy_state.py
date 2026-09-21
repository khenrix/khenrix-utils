#!/usr/bin/env python3
"""Copy legacy Agentic Setup Maka selectors into the Khenrix namespace safely.

This command never reads OAuth data, Maka sessions, the provider API key, or the
native Maka profile. It leaves the legacy files in place until the separate
runtime/service cutover has passed its doctor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile

LEGACY_CONFIG = pathlib.Path(".config/agentic-setup")
LEGACY_RELAY = pathlib.Path(".config/maka-openai-relay")
NEW_CONFIG = pathlib.Path(".config/khenrix-utils/maka")
NEW_RELAY = NEW_CONFIG / "relay"
MODE = "maka-auth-mode"
ACCOUNT = "maka-openai-keychain-account"
READY = "maka-api-key-relay-ready"
RECEIPT = "legacy-migration-receipt.json"
MODE_VALUES = {b"api-key-relay\n", b"chatgpt-subscription\n"}
COPY_LIMITS = {
    MODE: 64,
    ACCOUNT: 256,
    READY: 256,
    "caller-token": 1024,
    "relay-attestation": 1024,
}


class MigrationError(RuntimeError):
    """Legacy state cannot be copied without weakening its ownership contract."""


def _private_directory(path: pathlib.Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise MigrationError(f"unsafe private directory: {path}")


def _read_private(path: pathlib.Path, limit: int) -> bytes:
    _private_directory(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise MigrationError(f"legacy state is unavailable: {path.name}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise MigrationError(f"unsafe legacy state: {path.name}")
        payload = os.read(descriptor, limit + 1)
        if len(payload) > limit:
            raise MigrationError(f"legacy state is too large: {path.name}")
    finally:
        os.close(descriptor)
    return payload


def _ensure_private_directory(path: pathlib.Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_directory(path)


def _atomic_create(path: pathlib.Path, payload: bytes) -> None:
    _ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        existing = _read_private(path, COPY_LIMITS.get(path.name, 64 * 1024))
        if existing != payload:
            raise MigrationError(f"managed state already differs: {path.name}")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path, follow_symlinks=False)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def migration_payloads(home: pathlib.Path) -> tuple[str, dict[pathlib.Path, bytes]]:
    legacy_config = home / LEGACY_CONFIG
    mode_payload = _read_private(legacy_config / MODE, COPY_LIMITS[MODE])
    if mode_payload not in MODE_VALUES:
        raise MigrationError("legacy auth mode is invalid")
    mode = mode_payload.decode("ascii").strip()
    payloads: dict[pathlib.Path, bytes] = {pathlib.Path(MODE): mode_payload}
    if mode == "api-key-relay":
        for name in (ACCOUNT, READY):
            payloads[pathlib.Path(name)] = _read_private(
                legacy_config / name, COPY_LIMITS[name]
            )
        legacy_relay = home / LEGACY_RELAY
        for name in ("caller-token", "relay-attestation"):
            payloads[pathlib.Path("relay") / name] = _read_private(
                legacy_relay / name, COPY_LIMITS[name]
            )
    return mode, payloads


def plan(home: pathlib.Path) -> dict[str, object]:
    mode, payloads = migration_payloads(home)
    return {
        "schema": "khenrix-maka-legacy-migration-v1",
        "mode": mode,
        "source": str(home / LEGACY_CONFIG),
        "destination": str(home / NEW_CONFIG),
        "files": [
            {"path": relative.as_posix(), "bytes": len(payload)}
            for relative, payload in sorted(payloads.items(), key=lambda item: item[0].as_posix())
        ],
        "copiesOAuth": False,
        "copiesApiKey": False,
        "changesNativeMakaProfile": False,
        "changesService": False,
        "changesWrapper": False,
    }


def apply(home: pathlib.Path) -> pathlib.Path:
    document = plan(home)
    _, payloads = migration_payloads(home)
    document["files"] = [
        {
            "path": relative.as_posix(),
            "sha256": _sha256(payload),
            "bytes": len(payload),
        }
        for relative, payload in sorted(payloads.items(), key=lambda item: item[0].as_posix())
    ]
    destination = home / NEW_CONFIG
    for relative, payload in payloads.items():
        _atomic_create(destination / relative, payload)
    receipt_path = destination / RECEIPT
    receipt = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_create(receipt_path, receipt)
    print(f"Copied non-secret Maka migration state to {destination}")
    return receipt_path


def rollback(home: pathlib.Path) -> None:
    destination = home / NEW_CONFIG
    receipt_path = destination / RECEIPT
    receipt = json.loads(_read_private(receipt_path, 64 * 1024))
    if receipt.get("schema") != "khenrix-maka-legacy-migration-v1":
        raise MigrationError("migration receipt is invalid")
    for entry in reversed(receipt.get("files", [])):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise MigrationError("migration receipt is invalid")
        relative = pathlib.PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise MigrationError("migration receipt is invalid")
        path = destination / pathlib.Path(*relative.parts)
        payload = _read_private(path, COPY_LIMITS.get(path.name, 64 * 1024))
        if _sha256(payload) != entry.get("sha256"):
            raise MigrationError(f"managed state changed after migration: {entry['path']}")
        path.unlink()
    receipt_path.unlink()
    relay = destination / "relay"
    if relay.exists() and not any(relay.iterdir()):
        relay.rmdir()
    if destination.exists() and not any(destination.iterdir()):
        destination.rmdir()
    print("Removed only the unchanged Khenrix migration copies; legacy state is intact.")


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply", "rollback"))
    parser.add_argument("--home", type=pathlib.Path, default=pathlib.Path.home())
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    os.umask(0o077)
    options = parse_args(arguments)
    try:
        home = options.home.resolve(strict=True)
        if options.command == "plan":
            print(json.dumps(plan(home), indent=2, sort_keys=True))
        elif options.command == "apply":
            apply(home)
        else:
            rollback(home)
    except (MigrationError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Maka legacy-state migration failed: {error}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
