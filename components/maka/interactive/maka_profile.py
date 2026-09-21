#!/usr/bin/env python3
"""Resolve and validate Maka's platform-native local profile."""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import stat
import sys
from collections.abc import Mapping


class MakaProfileError(RuntimeError):
    """The local Maka profile does not meet the managed setup contract."""


def resolve_maka_profile(
    home: pathlib.Path | None = None,
    *,
    platform: str | None = None,
    environment: Mapping[str, str] | None = None,
    managed: bool = False,
) -> pathlib.Path:
    """Match upstream's profile roots, with an optional deterministic managed mode."""

    account_home = home or pathlib.Path.home()
    current_platform = platform or sys.platform
    current_environment = os.environ if environment is None else environment
    if current_platform == "darwin":
        return account_home / "Library" / "Application Support" / "Maka"
    if current_platform == "linux":
        configured = None if managed else current_environment.get("XDG_CONFIG_HOME")
        config_home = (
            pathlib.Path(configured)
            if configured and pathlib.PurePosixPath(configured).is_absolute()
            else account_home / ".config"
        )
        return config_home / "Maka"
    raise MakaProfileError(f"managed Maka is unsupported on {current_platform}")


def check_private_tree(path: pathlib.Path, *, owner_uid: int | None = None) -> None:
    """Require an owner-only physical profile tree without reading file contents."""

    expected_uid = os.getuid() if owner_uid is None else owner_uid

    def check_entry(entry: pathlib.Path, *, root: bool = False) -> None:
        metadata = entry.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise MakaProfileError(f"Maka profile contains a symlink: {entry}")
        if root and not stat.S_ISDIR(metadata.st_mode):
            raise MakaProfileError("Maka profile root is not a directory")
        if not root and not (
            stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        ):
            raise MakaProfileError(f"Maka profile contains a special inode: {entry}")
        if metadata.st_uid != expected_uid:
            raise MakaProfileError(f"Maka profile entry has the wrong owner: {entry}")
        if metadata.st_mode & 0o077:
            raise MakaProfileError(f"Maka profile entry is not owner-only: {entry}")

    check_entry(path, root=True)
    for current, directory_names, file_names in os.walk(path, followlinks=False):
        current_path = pathlib.Path(current)
        for name in (*directory_names, *file_names):
            check_entry(current_path / name)


def digest_regular_tree(path: pathlib.Path) -> str:
    """Hash a physical tree deterministically without returning its contents."""

    root_metadata = path.lstat()
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise MakaProfileError("Maka profile root is not a physical directory")
    digest = hashlib.sha256()
    for current, directory_names, file_names in os.walk(path, followlinks=False):
        current_path = pathlib.Path(current)
        directory_names.sort()
        for name in directory_names:
            entry = current_path / name
            if entry.is_symlink():
                raise MakaProfileError(f"Maka profile contains a symlink: {entry}")
            digest.update(b"D\0")
            digest.update(entry.relative_to(path).as_posix().encode("utf-8"))
            digest.update(b"\0")
        for name in sorted(file_names):
            entry = current_path / name
            metadata = entry.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise MakaProfileError(f"Maka profile contains a non-regular file: {entry}")
            digest.update(b"F\0")
            digest.update(entry.relative_to(path).as_posix().encode("utf-8"))
            digest.update(b"\0")
            with entry.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    resolve_command = subcommands.add_parser("resolve")
    resolve_command.add_argument("--managed", action="store_true")
    check_command = subcommands.add_parser("check-private")
    check_command.add_argument("path", type=pathlib.Path)
    digest_command = subcommands.add_parser("digest")
    digest_command.add_argument("path", type=pathlib.Path)
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    options = parse_args(arguments)
    try:
        if options.command == "resolve":
            print(resolve_maka_profile(managed=options.managed))
        elif options.command == "check-private":
            check_private_tree(options.path)
        else:
            print(digest_regular_tree(options.path))
    except (MakaProfileError, OSError) as error:
        print(f"Maka profile check failed: {error}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
