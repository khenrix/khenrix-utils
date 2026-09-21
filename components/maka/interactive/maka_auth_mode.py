#!/usr/bin/env python3
"""Persist the non-secret Maka authentication mode for this machine."""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import re
import stat
import tempfile


ALLOWED_MODES = ("api-key-relay", "chatgpt-subscription")
CONFIG_DIRECTORY = pathlib.Path(".config/khenrix-utils/maka")
MODE_FILE = "maka-auth-mode"
KEYCHAIN_ACCOUNT_FILE = "maka-openai-keychain-account"
RELAY_READY_FILE = "maka-api-key-relay-ready"
RELAY_READY_PREFIX = b"maka-api-key-relay-ready-v2\nkeychain-account-sha256="
_KEYCHAIN_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._+|:-]{0,127}$")


class AuthModeError(RuntimeError):
    """The local selector is missing or does not meet its ownership contract."""


def default_mode_path(home: pathlib.Path | None = None) -> pathlib.Path:
    return (home or pathlib.Path.home()) / CONFIG_DIRECTORY / MODE_FILE


def default_relay_ready_path(home: pathlib.Path | None = None) -> pathlib.Path:
    return (home or pathlib.Path.home()) / CONFIG_DIRECTORY / RELAY_READY_FILE


def default_keychain_account_path(home: pathlib.Path | None = None) -> pathlib.Path:
    return (home or pathlib.Path.home()) / CONFIG_DIRECTORY / KEYCHAIN_ACCOUNT_FILE


def _validate_private_directory(path: pathlib.Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise AuthModeError("Maka auth-mode directory is not a regular directory")
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise AuthModeError("Maka auth-mode directory must be owner-only")


def _ensure_private_directory(path: pathlib.Path) -> None:
    if path.exists() or path.is_symlink():
        _validate_private_directory(path)
        return
    path.mkdir(mode=0o700, parents=True)
    os.chmod(path, 0o700)
    _validate_private_directory(path)


def _read_private_payload(target: pathlib.Path) -> bytes:
    try:
        _validate_private_directory(target.parent)
    except OSError as error:
        raise AuthModeError("Maka auth mode is not configured") from error
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as error:
        raise AuthModeError("Maka auth mode is not configured") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AuthModeError("Maka auth-mode selector is not a regular file")
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise AuthModeError("Maka auth-mode selector must be owner-only")
        payload = os.read(descriptor, 256)
        if os.read(descriptor, 1):
            raise AuthModeError("Maka local state file is too large")
    finally:
        os.close(descriptor)
    return payload


def read_mode(path: pathlib.Path | None = None) -> str:
    target = path or default_mode_path()
    try:
        text = _read_private_payload(target).decode("ascii")
    except UnicodeDecodeError as error:
        raise AuthModeError("Maka auth-mode selector is invalid") from error
    if text not in {f"{mode}\n" for mode in ALLOWED_MODES}:
        raise AuthModeError("Maka auth-mode selector is invalid")
    return text.removesuffix("\n")


def _validated_keychain_account_payload(account: str) -> bytes:
    if not _KEYCHAIN_ACCOUNT_RE.fullmatch(account):
        raise AuthModeError("Maka Keychain account selector is invalid")
    return f"{account}\n".encode("ascii")


def read_keychain_account(path: pathlib.Path | None = None) -> str:
    """Read the explicit non-secret account ID without enumerating Keychain."""

    target = path or default_keychain_account_path()
    try:
        payload = _read_private_payload(target)
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise AuthModeError("Maka Keychain account selector is invalid") from error
    if not text.endswith("\n"):
        raise AuthModeError("Maka Keychain account selector is invalid")
    account = text.removesuffix("\n")
    if _validated_keychain_account_payload(account) != payload:
        raise AuthModeError("Maka Keychain account selector is invalid")
    return account


def _atomic_write_private_payload(target: pathlib.Path, payload: bytes) -> pathlib.Path:
    _ensure_private_directory(target.parent)
    if target.is_symlink():
        raise AuthModeError("Maka local state file cannot be a symlink")

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        directory_descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def _atomic_create_private_payload(target: pathlib.Path, payload: bytes) -> pathlib.Path:
    """Publish an initial selector without replacing a concurrent winner."""

    _ensure_private_directory(target.parent)
    if target.is_symlink():
        raise AuthModeError("Maka local state file cannot be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, target, follow_symlinks=False)
        directory_descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def write_mode(mode: str, path: pathlib.Path | None = None) -> pathlib.Path:
    """Make the initial per-machine choice; an established choice is immutable."""

    if mode not in ALLOWED_MODES:
        raise AuthModeError("Unsupported Maka auth mode")
    target = path or default_mode_path()
    payload = f"{mode}\n".encode("ascii")
    if target.exists() or target.is_symlink():
        current = read_mode(target)
        if current != mode:
            raise AuthModeError(
                "Maka auth mode is already selected; cross-mode migration is unsupported"
            )
        return target
    try:
        _atomic_create_private_payload(target, payload)
    except FileExistsError:
        current = read_mode(target)
        if current != mode:
            raise AuthModeError(
                "Maka auth mode is already selected; cross-mode migration is unsupported"
            ) from None
    if read_mode(target) != mode:
        raise AuthModeError("Maka auth-mode selector could not be verified")
    return target


def write_keychain_account(
    account: str,
    path: pathlib.Path | None = None,
    *,
    replace: bool = False,
) -> pathlib.Path:
    """Persist an exact account ID; replacement is reserved for the installer."""

    payload = _validated_keychain_account_payload(account)
    target = path or default_keychain_account_path()
    exists = target.exists() or target.is_symlink()
    if exists:
        current = read_keychain_account(target)
        if current == account:
            return target
        if not replace:
            raise AuthModeError("Maka Keychain account is already selected")
    if exists and replace:
        _atomic_write_private_payload(target, payload)
    else:
        try:
            _atomic_create_private_payload(target, payload)
        except FileExistsError:
            current = read_keychain_account(target)
            if current != account:
                raise AuthModeError("Maka Keychain account is already selected") from None
    if read_keychain_account(target) != account:
        raise AuthModeError("Maka Keychain account selector could not be verified")
    return target


def invalidate_relay_ready(path: pathlib.Path | None = None) -> None:
    """Remove installer readiness before an explicit relay installation attempt."""

    target = path or default_relay_ready_path()
    try:
        _validate_private_directory(target.parent)
    except FileNotFoundError:
        return
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(metadata.st_mode):
        raise AuthModeError("Maka relay readiness path is a directory")
    target.unlink()
    directory_descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def relay_ready_payload(account_path: pathlib.Path | None = None) -> bytes:
    """Bind readiness to the exact private account selector bytes."""

    target = account_path or default_keychain_account_path()
    account = read_keychain_account(target)
    account_payload = _validated_keychain_account_payload(account)
    digest = hashlib.sha256(account_payload).hexdigest().encode("ascii")
    return RELAY_READY_PREFIX + digest + b"\n"


def write_relay_ready(
    path: pathlib.Path | None = None,
    account_path: pathlib.Path | None = None,
) -> pathlib.Path:
    target = path or default_relay_ready_path()
    if target.exists() or target.is_symlink():
        invalidate_relay_ready(target)
    payload = relay_ready_payload(account_path)
    _atomic_write_private_payload(target, payload)
    if _read_private_payload(target) != payload:
        raise AuthModeError("Maka relay readiness marker could not be verified")
    return target


def require_relay_ready(
    mode_path: pathlib.Path | None = None,
    ready_path: pathlib.Path | None = None,
    account_path: pathlib.Path | None = None,
) -> None:
    if read_mode(mode_path) != "api-key-relay":
        raise AuthModeError("Maka API-key relay mode is not selected")
    expected = relay_ready_payload(account_path)
    if _read_private_payload(ready_path or default_relay_ready_path()) != expected:
        raise AuthModeError("Maka API-key relay installation is not ready")


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=(*ALLOWED_MODES, "show", "relay-ready"))
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    os.umask(0o077)
    options = parse_args(arguments)
    try:
        if options.mode == "show":
            print(read_mode())
        elif options.mode == "relay-ready":
            require_relay_ready()
            print("api-key-relay")
        else:
            path = write_mode(options.mode)
            print(f"Maka auth mode: {options.mode} ({path})")
    except (AuthModeError, OSError) as error:
        print(str(error), file=os.sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
