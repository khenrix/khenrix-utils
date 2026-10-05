#!/usr/bin/env python3
"""Guard the additive Undici H2 shutdown correction for staged judge copies."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from apply_maka_hosted_onboarding_compat import digest, regular_target

OVERLAY_ID = "undici-h2-close-v2"
TARGET = Path("node_modules/@maka/runtime/node_modules/undici/lib/dispatcher/client-h2.js")
BASE_SHA256 = "611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7"
PATCHED_SHA256 = "32c2b747c1c1c9c7405e60dc70145f67f9b8f96db0b6264f55e1f0400a375dc4"
OLD = b"""      if (socket[kClosed]) {
        queueMicrotask(callback)
      } else {
        socket.destroy(err).on('close', callback)
      }"""
NEW = b"""      if (socket[kClosed] || session.destroyed) {
        queueMicrotask(callback)
      } else {
        // Node can close the H2 session without emitting socket close.
        let settled = false
        const done = () => {
          if (settled) return
          settled = true
          socket.off('close', done)
          session.off('close', done)
          callback()
        }
        socket.once('close', done)
        session.once('close', done)
        socket.destroy(err)
      }"""


def atomic_replace(target: Path, data: bytes) -> None:
    original = target.stat()
    # Trusted root staging preserves host-owned virtiofs metadata.
    if os.geteuid() not in (0, original.st_uid):
        raise RuntimeError(f"{OVERLAY_ID} requires a caller-owned staged target")
    expected = original.st_uid, original.st_gid, stat.S_IMODE(original.st_mode)
    temporary = target.with_name(f".{target.name}.{OVERLAY_ID}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fchown(stream.fileno(), original.st_uid, original.st_gid)
            os.fchmod(stream.fileno(), expected[2])
            preserved = os.fstat(stream.fileno())
            if (preserved.st_uid, preserved.st_gid, stat.S_IMODE(preserved.st_mode)) != expected:
                raise RuntimeError(f"{OVERLAY_ID} cannot preserve target metadata")
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def apply(package_root: Path) -> Path:
    package_root = package_root.resolve(strict=True)
    target = regular_target(package_root, TARGET, str(TARGET))
    if target.resolve(strict=True) != target:
        raise RuntimeError(f"pinned {TARGET} has a symlink parent")
    source = target.read_bytes()
    if digest(source) != BASE_SHA256:
        raise RuntimeError(f"pinned {TARGET} hash does not match {OVERLAY_ID}")
    if source.count(OLD) != 1:
        raise RuntimeError(f"pinned {TARGET} lacks the exact H2 close target")
    patched = source.replace(OLD, NEW, 1)
    if digest(patched) != PATCHED_SHA256:
        raise RuntimeError(f"{OVERLAY_ID} produced unexpected {TARGET}")
    atomic_replace(target, patched)
    if digest(target.read_bytes()) != PATCHED_SHA256:
        raise RuntimeError(f"{OVERLAY_ID} did not persist exactly")
    return target


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} PACKAGE_ROOT")
    apply(Path(sys.argv[1]))
