"""Standard-library security primitives shared by host and guest transfer code."""
from __future__ import annotations

import ctypes
import errno
import json
import os
import sys
from typing import Any


def canonical_json(value: Any) -> bytes:
    """Encode a JSON value in the one spelling used for durable artifacts."""
    return (json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n").encode("utf-8")


def _rename_no_replace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> bool:
    """Move one entry only if its destination spelling remains unclaimed.

    Unsupported platforms fail closed; no check-then-rename fallback can
    overwrite a peer that claims the destination concurrently.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            rename = libc.renameatx_np
            flags = 0x00000004  # RENAME_EXCL
        elif sys.platform.startswith("linux"):
            rename = libc.renameat2
            flags = 1  # RENAME_NOREPLACE
        else:
            return False
    except (AttributeError, OSError):
        return False
    rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = rename(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        flags,
    )
    if result == 0:
        return True
    failure = ctypes.get_errno()
    if failure in {errno.EEXIST, errno.ENOTEMPTY, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL}:
        return False
    raise OSError(failure, os.strerror(failure))
