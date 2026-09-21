#!/usr/bin/env python3
"""Harden and verify the exact pinned Python used on secret-bearing Maka paths."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import os
import pathlib
import pwd
import stat
import subprocess
import sys
import tempfile
from typing import List, Optional


PYTHON_VERSION = "3.12.14"
_ACL_TYPE_EXTENDED = 0x00000100
_CACHED_ACL_LIBRARY: Optional[ctypes.CDLL] = None
_OPEN_DIRECTORY = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_OPEN_REGULAR = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class RuntimeHardeningError(RuntimeError):
    """The pinned runtime is missing or outside its local integrity contract."""


def _acl_library() -> ctypes.CDLL:
    global _CACHED_ACL_LIBRARY
    if _CACHED_ACL_LIBRARY is not None:
        return _CACHED_ACL_LIBRARY
    if sys.platform != "darwin":
        raise RuntimeHardeningError("the API relay Python hardener requires macOS")
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.acl_init.argtypes = [ctypes.c_int]
    library.acl_init.restype = ctypes.c_void_p
    library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    library.acl_get_fd_np.restype = ctypes.c_void_p
    library.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
    library.acl_to_text.restype = ctypes.c_void_p
    library.acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    library.acl_set_fd_np.restype = ctypes.c_int
    library.acl_free.argtypes = [ctypes.c_void_p]
    library.acl_free.restype = ctypes.c_int
    _CACHED_ACL_LIBRARY = library
    return library


def _clear_extended_acl(descriptor: int) -> None:
    library = _acl_library()
    acl = library.acl_init(0)
    if not acl:
        raise RuntimeHardeningError("could not allocate an empty ACL")
    try:
        ctypes.set_errno(0)
        if library.acl_set_fd_np(descriptor, acl, _ACL_TYPE_EXTENDED) != 0:
            raise RuntimeHardeningError("could not clear a pinned Python ACL")
    finally:
        library.acl_free(acl)


def _acl_document_is_deny_only(document: str) -> bool:
    entries = [line for line in document.splitlines() if line and not line.startswith("!#acl")]
    if not entries:
        return False
    for line in entries:
        fields = line.rsplit(":", 2)
        if len(fields) != 3 or fields[-2] != "deny" or not fields[-1]:
            return False
    return True


def _assert_no_extended_acl(descriptor: int, *, allow_deny_only: bool = False) -> None:
    library = _acl_library()
    ctypes.set_errno(0)
    acl = library.acl_get_fd_np(descriptor, _ACL_TYPE_EXTENDED)
    if not acl:
        if ctypes.get_errno() in (0, errno.ENOENT):
            return
        raise RuntimeHardeningError("could not inspect a pinned Python ACL")
    try:
        if allow_deny_only:
            length = ctypes.c_ssize_t()
            text_pointer = library.acl_to_text(acl, ctypes.byref(length))
            if not text_pointer:
                raise RuntimeHardeningError("could not inspect a pinned Python ACL")
            try:
                document = ctypes.string_at(text_pointer, length.value).decode("ascii")
            except (UnicodeDecodeError, ValueError) as error:
                raise RuntimeHardeningError("could not inspect a pinned Python ACL") from error
            finally:
                library.acl_free(text_pointer)
            if _acl_document_is_deny_only(document):
                return
        raise RuntimeHardeningError("pinned Python contains an extended ACL")
    finally:
        library.acl_free(acl)


def canonical_home() -> pathlib.Path:
    return pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)


def default_runtime_root() -> pathlib.Path:
    return canonical_home() / ".local/share/mise/installs/python" / PYTHON_VERSION


def _within(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_ancestors(root: pathlib.Path, uid: int, *, hardened_root: bool) -> None:
    """Reject a replaceable path before opening the runtime root."""

    home = canonical_home()
    if not _within(root, home):
        raise RuntimeHardeningError("pinned Python root is outside the canonical home")
    current = root
    while True:
        metadata = current.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != uid
            or ((current != root or hardened_root) and metadata.st_mode & 0o022)
        ):
            raise RuntimeHardeningError("pinned Python ancestor is unsafe")
        if current != root or hardened_root:
            descriptor = os.open(current, _OPEN_DIRECTORY)
            try:
                _assert_no_extended_acl(descriptor, allow_deny_only=True)
            finally:
                os.close(descriptor)
        if current == home:
            return
        current = current.parent


def _validate_metadata(
    metadata: os.stat_result,
    *,
    uid: int,
    device: int,
    expected: str,
    hardened: bool,
) -> None:
    if metadata.st_uid != uid:
        raise RuntimeHardeningError("pinned Python contains an entry with the wrong owner")
    if metadata.st_dev != device:
        raise RuntimeHardeningError("pinned Python crosses a filesystem boundary")
    if getattr(metadata, "st_flags", 0):
        raise RuntimeHardeningError("pinned Python contains an entry with file flags")
    if expected == "directory" and not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeHardeningError("pinned Python directory changed during traversal")
    if expected == "regular":
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeHardeningError("pinned Python file changed during traversal")
        if metadata.st_nlink != 1:
            raise RuntimeHardeningError("pinned Python contains a multiply linked file")
    if hardened and metadata.st_mode & 0o022:
        raise RuntimeHardeningError("pinned Python contains a group/world-writable entry")


def _same_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _secure_descriptor(
    descriptor: int,
    *,
    uid: int,
    device: int,
    expected: str,
    harden: bool,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    _validate_metadata(
        metadata,
        uid=uid,
        device=device,
        expected=expected,
        hardened=False,
    )
    if harden:
        _clear_extended_acl(descriptor)
        os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode) & ~0o022)
        metadata = os.fstat(descriptor)
    _assert_no_extended_acl(descriptor)
    _validate_metadata(
        metadata,
        uid=uid,
        device=device,
        expected=expected,
        hardened=True,
    )
    return metadata


def _validate_internal_symlink(
    directory_fd: int,
    name: str,
    metadata: os.stat_result,
    *,
    uid: int,
    device: int,
    hardened: bool,
) -> None:
    if metadata.st_uid != uid or metadata.st_dev != device or getattr(metadata, "st_flags", 0):
        raise RuntimeHardeningError("pinned Python contains an unsafe symlink")
    try:
        target = os.readlink(name, dir_fd=directory_fd)
    except (OSError, TypeError) as error:
        raise RuntimeHardeningError("pinned Python symlink changed during traversal") from error
    # The pinned distribution uses only sibling links. Requiring one plain
    # component prevents both direct and chained escapes from the runtime.
    if not target or target in {".", ".."} or "/" in target or "\x00" in target:
        raise RuntimeHardeningError("pinned Python contains a symlink escape")
    after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not _same_entry(metadata, after):
        raise RuntimeHardeningError("pinned Python symlink changed during traversal")
    destination = os.stat(target, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(destination.st_mode):
        raise RuntimeHardeningError("pinned Python symlink target is not a regular sibling")
    _validate_metadata(
        destination,
        uid=uid,
        device=device,
        expected="regular",
        hardened=hardened,
    )


def _walk_descriptor(
    directory_fd: int,
    *,
    uid: int,
    device: int,
    harden: bool,
) -> None:
    """Walk with openat-style operations so path replacement cannot redirect writes."""

    try:
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError as error:
        raise RuntimeHardeningError("pinned Python directory could not be read") from error
    for name in names:
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise RuntimeHardeningError("pinned Python entry could not be inspected") from error
        if stat.S_ISLNK(metadata.st_mode):
            _validate_internal_symlink(
                directory_fd,
                name,
                metadata,
                uid=uid,
                device=device,
                hardened=not harden,
            )
            continue
        if stat.S_ISDIR(metadata.st_mode):
            flags = _OPEN_DIRECTORY
            expected = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            flags = _OPEN_REGULAR
            expected = "regular"
        else:
            raise RuntimeHardeningError("pinned Python contains an unsupported inode")
        try:
            child_fd = os.open(name, flags, dir_fd=directory_fd)
        except OSError as error:
            raise RuntimeHardeningError("pinned Python entry changed during traversal") from error
        try:
            opened = _secure_descriptor(
                child_fd,
                uid=uid,
                device=device,
                expected=expected,
                harden=harden,
            )
            if not _same_entry(metadata, opened):
                raise RuntimeHardeningError("pinned Python entry changed during traversal")
            if expected == "directory":
                _walk_descriptor(
                    child_fd,
                    uid=uid,
                    device=device,
                    harden=harden,
                )
        finally:
            os.close(child_fd)


def _open_and_walk(root: pathlib.Path, *, harden: bool) -> None:
    uid = os.getuid()
    _validate_ancestors(root, uid, hardened_root=not harden)
    try:
        root_fd = os.open(root, _OPEN_DIRECTORY)
    except OSError as error:
        raise RuntimeHardeningError("pinned Python root is unavailable") from error
    try:
        initial = os.fstat(root_fd)
        device = initial.st_dev
        _secure_descriptor(
            root_fd,
            uid=uid,
            device=device,
            expected="directory",
            harden=harden,
        )
        _walk_descriptor(root_fd, uid=uid, device=device, harden=harden)
    finally:
        os.close(root_fd)


def _validate_launcher_chain(root: pathlib.Path, uid: int) -> pathlib.Path:
    python = root / "bin/python"
    python3 = root / "bin/python3"
    target = root / "bin/python3.12"
    if not python.is_symlink() or os.readlink(python) != "python3.12":
        raise RuntimeHardeningError("pinned Python launcher chain is invalid")
    if not python3.is_symlink() or os.readlink(python3) != "python3.12":
        raise RuntimeHardeningError("pinned Python launcher chain is invalid")
    metadata = target.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o022
        or not os.access(target, os.X_OK)
    ):
        raise RuntimeHardeningError("pinned Python executable is invalid")
    return target


def verify_runtime(root: Optional[pathlib.Path] = None) -> pathlib.Path:
    target_root = root or default_runtime_root()
    _open_and_walk(target_root, harden=False)
    return _validate_launcher_chain(target_root, os.getuid())


def harden_runtime(root: Optional[pathlib.Path] = None) -> pathlib.Path:
    target_root = root or default_runtime_root()
    _open_and_walk(target_root, harden=True)
    # A second, mutation-free traversal proves the final state.
    return verify_runtime(target_root)


def _open_admitted_mise(
    candidates: Optional[tuple[pathlib.Path, ...]] = None,
) -> int:
    home = canonical_home()
    admitted = candidates or (
        home / ".local/bin/mise",
        pathlib.Path("/opt/homebrew/bin/mise"),
        pathlib.Path("/usr/local/bin/mise"),
    )
    for candidate in admitted:
        try:
            entry = candidate.lstat()
            target = candidate.resolve(strict=True)
            descriptor = os.open(
                target,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except (OSError, RuntimeError):
            continue
        metadata = os.fstat(descriptor)
        if not (
            (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode))
            and stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid in {0, os.getuid()}
            and not metadata.st_mode & 0o022
            and metadata.st_mode & 0o111
            and not getattr(metadata, "st_flags", 0)
            and 0 < metadata.st_size <= 512 * 1024 * 1024
        ):
            os.close(descriptor)
            continue
        try:
            _assert_no_extended_acl(descriptor)
        except RuntimeHardeningError:
            os.close(descriptor)
            continue
        return descriptor
    raise RuntimeHardeningError("mise is unavailable at an admitted standard path")


def _private_staging_parent(requested: Optional[pathlib.Path] = None) -> pathlib.Path:
    home = canonical_home()
    parent = requested or home / ".config/khenrix-utils/maka"
    if not _within(parent, home):
        raise RuntimeHardeningError("mise staging directory is outside the canonical home")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = parent
    while True:
        metadata = current.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022
        ):
            raise RuntimeHardeningError("mise staging directory is unsafe")
        descriptor = os.open(current, _OPEN_DIRECTORY)
        try:
            _assert_no_extended_acl(descriptor, allow_deny_only=True)
        finally:
            os.close(descriptor)
        if current == home:
            break
        current = current.parent
    parent.chmod(0o700)
    return parent


@contextlib.contextmanager
def _staged_mise_binary(
    candidates: Optional[tuple[pathlib.Path, ...]] = None,
    staging_parent: Optional[pathlib.Path] = None,
):
    """Copy one validated inode into an owner-only path before executing it."""

    source = _open_admitted_mise(candidates)
    try:
        parent = _private_staging_parent(staging_parent)
        with tempfile.TemporaryDirectory(prefix=".maka-mise-", dir=parent) as directory:
            staged = pathlib.Path(directory) / "mise"
            destination = os.open(
                staged,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o500,
            )
            try:
                while True:
                    block = os.read(source, 1024 * 1024)
                    if not block:
                        break
                    view = memoryview(block)
                    while view:
                        written = os.write(destination, view)
                        view = view[written:]
                os.fchmod(destination, 0o500)
                _assert_no_extended_acl(destination)
                os.fsync(destination)
                copied = os.fstat(destination)
                original = os.fstat(source)
                if copied.st_size != original.st_size:
                    raise RuntimeHardeningError("staged mise copy is incomplete")
            finally:
                os.close(destination)
            yield staged
    finally:
        os.close(source)


def prepare_runtime() -> pathlib.Path:
    """Reinstall from the lock before establishing the future-write boundary."""

    account = pwd.getpwuid(os.getuid())
    component = pathlib.Path(__file__).resolve().parent.parent
    with _staged_mise_binary() as mise:
        environment = {
            "HOME": account.pw_dir,
            "USER": account.pw_name,
            "LOGNAME": account.pw_name,
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
        }
        completed = subprocess.run(
            [
                str(mise),
                "-C",
                str(component),
                "install",
                "--force",
                "--locked",
                "--yes",
                f"python@{PYTHON_VERSION}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            check=False,
            close_fds=True,
            env=environment,
            timeout=900,
        )
    if completed.returncode != 0:
        raise RuntimeHardeningError("locked Python reinstall failed")
    return harden_runtime()


def assert_current_runtime() -> pathlib.Path:
    required_flags = (
        sys.flags.isolated,
        sys.flags.no_site,
        sys.flags.dont_write_bytecode,
        sys.flags.ignore_environment,
        sys.flags.no_user_site,
        sys.flags.safe_path,
    )
    if not all(required_flags):
        raise RuntimeHardeningError("secret-bearing Python must run with -I -S -B")
    if sys.version_info[:3] != (3, 12, 14):
        raise RuntimeHardeningError("secret-bearing Python version is not pinned")
    root = default_runtime_root()
    target = verify_runtime(root)
    try:
        resolved_root = root.resolve(strict=True)
        executable = pathlib.Path(sys.executable).resolve(strict=True)
        prefix = pathlib.Path(sys.prefix).resolve(strict=True)
        base_prefix = pathlib.Path(sys.base_prefix).resolve(strict=True)
        resolved_target = target.resolve(strict=True)
        search_paths = [pathlib.Path(value).resolve(strict=False) for value in sys.path]
    except (OSError, RuntimeError) as error:
        raise RuntimeHardeningError("secret-bearing Python runtime resolution failed") from error
    if executable != resolved_target or prefix != resolved_root or base_prefix != resolved_root:
        raise RuntimeHardeningError("secret-bearing Python runtime is not the pinned tree")
    if not search_paths or any(
        not value.is_absolute() or not _within(value, resolved_root) for value in search_paths
    ):
        raise RuntimeHardeningError("secret-bearing Python search path escapes the pinned tree")
    return target


def parse_args(arguments: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "harden", "verify"))
    return parser.parse_args(arguments)


def main(arguments: Optional[List[str]] = None) -> int:
    options = parse_args(arguments)
    try:
        if options.command == "prepare":
            target = prepare_runtime()
        elif options.command == "harden":
            target = harden_runtime()
        else:
            target = verify_runtime()
    except (OSError, RuntimeHardeningError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 78
    print(f"Pinned Python runtime {options.command}: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
