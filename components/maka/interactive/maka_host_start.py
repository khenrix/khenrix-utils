"""Read a macOS Host's kernel process start time without wall-clock inference."""

from __future__ import annotations

import ctypes
import json
import os
import sys


class ProcBsdInfo(ctypes.Structure):
    # macOS SDK sys/proc_info.h: proc_bsdinfo, PROC_PIDTBSDINFO.
    _fields_ = [
        ("header", ctypes.c_uint32 * 12),
        ("names", ctypes.c_char * 48),
        ("metadata", ctypes.c_uint32 * 6),
        ("start_seconds", ctypes.c_uint64),
        ("start_microseconds", ctypes.c_uint64),
    ]


def process_start(pid: int) -> dict[str, int]:
    if sys.platform != "darwin" or not 0 < pid < 2**31:
        raise ValueError("process start is unavailable")
    if ctypes.sizeof(ProcBsdInfo) != 136:
        raise ValueError("process structure changed")
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
    ]
    libproc.proc_pidinfo.restype = ctypes.c_int
    info = ProcBsdInfo()
    size = libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if (size != ctypes.sizeof(info) or info.header[3] != pid or
            info.header[5] != os.getuid() or info.start_seconds <= 0 or
            info.start_microseconds >= 1_000_000):
        raise ValueError("process start is unavailable")
    return {
        "pid": pid,
        "uid": os.getuid(),
        "seconds": info.start_seconds,
        "microseconds": info.start_microseconds,
    }


def main(argv: list[str]) -> int:
    try:
        if len(argv) != 1 or not argv[0].isdecimal():
            raise ValueError("process identity is unavailable")
        print(json.dumps(process_start(int(argv[0])), separators=(",", ":")))
    except (OSError, ValueError):
        print("maka host start: unavailable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
