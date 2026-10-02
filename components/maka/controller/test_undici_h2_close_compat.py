#!/usr/bin/env python3
"""Offline pinned-source lifecycle and guarded-overlay regressions."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TARGET = Path("node_modules/@maka/runtime/node_modules/undici/lib/dispatcher/client-h2.js")
BASE_SHA256 = "611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7"
PATCHED_SHA256 = "521fef59ccb84909a3c68d52c15e972f61836e0d386093d5d4f4e64ba7d85087"


def inode_metadata(path: Path) -> tuple[int, int, int, int, int]:
    details = path.stat()
    return details.st_dev, details.st_ino, details.st_uid, details.st_gid, stat.S_IMODE(details.st_mode)


def lifecycle(source: bytes) -> subprocess.CompletedProcess[str]:
    # Characterization seam: execute the actual pinned H2 context method with
    # EventEmitter sockets so synchronous and asynchronous close are deterministic.
    # The real TLS/CONNECT trigger is covered by the private Docker diagnosis.
    return subprocess.run(["node", "--input-type=module", "-"], input=f"""
import assert from 'node:assert/strict';
import {{ EventEmitter }} from 'node:events';
import {{ runInNewContext }} from 'node:vm';
const source = {json.dumps(source.decode())};
const start = source.indexOf('    destroy (err, callback) {{');
assert.ok(start >= 0);
const end = source.indexOf('    /**', start);
assert.ok(end > start);
const method = source.slice(start, end);
const kClosed = Symbol('closed');
for (const mode of ['synchronous', 'already-closed', 'asynchronous']) {{
  const socket = new EventEmitter();
  socket[kClosed] = mode === 'already-closed';
  let destroys = 0;
  const error = new Error('test teardown');
  socket.destroy = (observed) => {{
    assert.equal(observed, error);
    destroys++;
    if (mode === 'synchronous') socket.emit('close');
    else setImmediate(() => socket.emit('close'));
    return socket;
  }};
  const context = runInNewContext('({{' + method + '}})', {{ socket, kClosed, queueMicrotask }});
  let callbacks = 0;
  context.destroy(error, () => callbacks++);
  await new Promise(setImmediate);
  assert.equal(callbacks, 1, mode + ' close must complete exactly once');
  assert.equal(destroys, mode === 'already-closed' ? 0 : 1);
}}
console.log('Pinned H2 synchronous/already-closed/asynchronous lifecycle passed');
""", text=True, capture_output=True, timeout=10)


def main() -> None:
    package = Path(sys.argv[1]).resolve(strict=True)
    original = (package / TARGET).read_bytes()
    if sys.argv[2:] == ["--original"]:
        result = lifecycle(original)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)

    import apply_undici_h2_close_compat as compat
    from apply_maka_hosted_onboarding_compat import digest

    assert digest(original) == BASE_SHA256
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / TARGET
        target.parent.mkdir(parents=True)
        target.write_bytes(original)
        creation_group = target.stat().st_gid
        other_groups = sorted(set(os.getgroups()) - {creation_group})
        if other_groups:
            os.chown(target, -1, other_groups[0])
        target.chmod(0o640)
        before = target.stat()
        compat.apply(root)
        assert digest(target.read_bytes()) == PATCHED_SHA256
        after = target.stat()
        assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
            before.st_uid, before.st_gid, 0o640
        ), f"H2 replacement lost distinct target metadata: {(after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode))} != {(before.st_uid, before.st_gid, 0o640)}"
        result = lifecycle(target.read_bytes())
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""

        # This private data file is never executed. A post-check write clears
        # supported special mode bits on the actual host.
        target.write_bytes(original)
        target.chmod(0o4750)
        before = target.stat()
        assert stat.S_IMODE(before.st_mode) == 0o4750
        compat.apply(root)
        after = target.stat()
        actual = after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)
        expected = before.st_uid, before.st_gid, 0o4750
        assert actual == expected, f"H2 replacement lost final special mode: {actual} != {expected}"
        assert digest(target.read_bytes()) == PATCHED_SHA256

        target.chmod(0o640)
        target.write_bytes(original)
        if os.geteuid() == 0:
            # Already-root test contexts can use a genuinely foreign scratch
            # owner; this never changes a shared or installed runtime inode.
            os.chown(target, 502, 20)
        else:
            os.chown(target, -1, os.getegid())
        target.chmod(0o444)
        before = target.stat()
        assert before.st_uid != 0
        # The ordinary host cannot create a foreign UID without new privilege.
        # Inject only the root caller; preservation uses real OS operations.
        with patch.object(compat.os, "geteuid", return_value=0):
            compat.apply(root)
        after = target.stat()
        assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
            before.st_uid, before.st_gid, 0o444
        ), "Root staging changed host-owned target metadata"
        assert digest(target.read_bytes()) == PATCHED_SHA256
        target.chmod(0o640)

        # Foreign ownership needs privileges to create. Inject only the caller
        # identity policy; target metadata and all filesystem operations are real.
        target.write_bytes(original)
        before = target.stat()
        unchanged = inode_metadata(target)
        with (
            patch.object(compat.os, "geteuid", return_value=before.st_uid + 1),
            patch.object(compat.os, "open", side_effect=AssertionError("foreign-owner temporary creation")),
        ):
            try:
                compat.apply(root)
            except RuntimeError as error:
                assert "caller-owned" in str(error)
            else:
                raise AssertionError("H2 overlay accepted foreign target ownership")
        assert target.read_bytes() == original and inode_metadata(target) == unchanged
        assert not list(target.parent.glob(f".{target.name}.*.tmp"))

        # Inject OS pre-publication failures: cleanup and the unchanged
        # original inode/metadata are observed on the real private filesystem.
        for caller in (before.st_uid, 0):
            for operation in ("fchown", "fchmod", "fsync"):
                unchanged = inode_metadata(target)
                with (
                    patch.object(compat.os, "geteuid", return_value=caller),
                    patch.object(compat.os, operation, side_effect=PermissionError("metadata denied")),
                ):
                    try:
                        compat.apply(root)
                    except PermissionError:
                        pass
                    else:
                        raise AssertionError(f"H2 overlay ignored {operation} failure")
                assert target.read_bytes() == original and inode_metadata(target) == unchanged
                assert not list(target.parent.glob(f".{target.name}.*.tmp"))
        compat.apply(root)

        patched = target.read_bytes()
        try:
            compat.apply(root)
        except RuntimeError as error:
            assert "hash does not match" in str(error)
        else:
            raise AssertionError("H2 overlay accepted double application")
        assert target.read_bytes() == patched
        target.write_bytes(original + b"\n// drift\n")
        drifted = target.read_bytes()
        try:
            compat.apply(root)
        except RuntimeError as error:
            assert "hash does not match" in str(error)
        else:
            raise AssertionError("H2 overlay accepted unpinned input")
        assert target.read_bytes() == drifted
        target.unlink()
        outside = root / "outside.js"
        outside.write_bytes(original)
        target.symlink_to(outside)
        try:
            compat.apply(root)
        except RuntimeError as error:
            assert "regular file" in str(error)
        else:
            raise AssertionError("H2 overlay accepted a symlink target")
        assert outside.read_bytes() == original
        target.unlink()
        parent = target.parent
        parent.rmdir()
        external = root / "external"
        external.mkdir()
        (external / target.name).write_bytes(original)
        parent.symlink_to(external, target_is_directory=True)
        try:
            compat.apply(root)
        except RuntimeError as error:
            assert "symlink" in str(error)
        else:
            raise AssertionError("H2 overlay accepted a symlink parent")
        assert (external / target.name).read_bytes() == original
    print("Pinned Undici H2 guarded overlay and lifecycle tests passed")


if __name__ == "__main__":
    main()
