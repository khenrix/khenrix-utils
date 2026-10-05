"""The staged runtime's parallel file manifest must match the serial contract."""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


HELPER = pathlib.Path(__file__).resolve().parents[1] / "controller/runtime_manifest_verify.py"


def serial_manifest(root: pathlib.Path) -> bytes:
    found = subprocess.run(
        ["find", ".", "-type", "f", "!", "-path", "./RUNTIME_SHA256SUMS",
         "!", "-path", "./RUNTIME_SYMLINKS", "-print0"],
        cwd=root, check=True, capture_output=True,
    ).stdout
    ordered = subprocess.run(
        ["sort", "-z"], input=found, check=True, capture_output=True,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout
    return subprocess.run(
        ["xargs", "-0", "sha256sum"], cwd=root, input=ordered,
        check=True, capture_output=True,
    ).stdout


def run_helper(root: pathlib.Path, *, hash_command: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HELPER), str(root), hash_command or shutil.which("sha256sum")],
        capture_output=True, timeout=20,
    )


class RuntimeManifestVerifyTests(unittest.TestCase):
    def test_fresh_staging_omits_empty_directories_before_manifest_and_seal(self) -> None:
        entrypoint = HELPER.with_name("entrypoint.sh").read_text()
        staging_steps = entrypoint[entrypoint.index('  test "$(sha256sum "$staging/maka-agent/node_modules/@maka/eval/dist/maka-subject.js"'):
                                  entrypoint.index("  (\n    cd \"$staging\"")]
        # Execute the real staging fragment with the already-authenticated
        # subject check omitted; its pruning runs before the manifest producer.
        staging_steps = staging_steps[staging_steps.index("  printf '%s\\n'"): ]
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "empty/nested").mkdir(parents=True)
            (root / "kept/nested").mkdir(parents=True)
            (root / "kept/nested/file").write_bytes(b"immutable")
            (root / "kept/link").symlink_to("nested/file")
            completed = subprocess.run(
                ["/bin/bash", "-c", "set -eu\n" + staging_steps],
                env={**os.environ, "staging": str(root), "expected_complete": "pinned"},
                capture_output=True, timeout=3,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse((root / "empty").exists())
            self.assertEqual((root / "kept/nested/file").read_bytes(), b"immutable")
            self.assertEqual(os.readlink(root / "kept/link"), "nested/file")
            manifest = run_helper(root)
            self.assertEqual(manifest.returncode, 0, manifest.stderr)
            self.assertEqual(manifest.stdout, serial_manifest(root))

    def test_parallel_manifest_is_byte_identical_and_includes_nested_reserved_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "RUNTIME_SHA256SUMS").write_text("root manifest\n")
            (root / "RUNTIME_SYMLINKS").write_text("root symlinks\n")
            (root / ".complete").write_text("pinned\n")
            nested = root / "nested"
            nested.mkdir()
            (nested / "RUNTIME_SHA256SUMS").write_text("nested file\n")
            (nested / "RUNTIME_SYMLINKS").write_text("another nested file\n")
            (nested / "line\nbreak").write_text("newline name\n")
            (nested / "back\\slash").write_text("backslash name\n")
            (root / "link").symlink_to(".complete")
            for index in range(300):
                (nested / f"file-{index:03d}").write_text(str(index))

            expected = serial_manifest(root)
            completed = run_helper(root)

            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(completed.stdout, expected)
            self.assertIn(b"nested/RUNTIME_SHA256SUMS", completed.stdout)
            self.assertIn(b"nested/RUNTIME_SYMLINKS", completed.stdout)
            self.assertNotIn(b"  ./RUNTIME_SHA256SUMS\n", completed.stdout)
            self.assertNotIn(b"  ./link\n", completed.stdout)

    def test_added_nested_manifest_name_changes_existing_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / ".complete").write_text("pinned\n")
            original = run_helper(root)
            self.assertEqual(original.returncode, 0, original.stderr.decode())
            (root / "RUNTIME_SHA256SUMS").write_bytes(original.stdout)
            (root / "RUNTIME_SYMLINKS").write_text("")
            (root / "nested").mkdir()
            (root / "nested/RUNTIME_SHA256SUMS").write_text("injected\n")

            changed = run_helper(root)

            self.assertEqual(changed.returncode, 0, changed.stderr.decode())
            self.assertNotEqual(changed.stdout, original.stdout)
            self.assertIn(b"nested/RUNTIME_SHA256SUMS", changed.stdout)

    def test_hash_worker_failure_cannot_emit_a_partial_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / ".complete").write_text("pinned\n")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_hash = fake_bin / "sha256sum"
            fake_hash.write_text("#!/bin/sh\nexit 7\n")
            fake_hash.chmod(0o755)

            completed = run_helper(root, hash_command=str(fake_hash))

            self.assertEqual(completed.returncode, 70)
            self.assertEqual(completed.stdout, b"")
            self.assertNotIn(str(root).encode(), completed.stderr)

    def test_special_inode_is_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / ".complete").write_text("pinned\n")
            os.mkfifo(root / "unexpected")

            completed = run_helper(root)

            self.assertEqual(completed.returncode, 70)
            self.assertEqual(completed.stdout, b"")


if __name__ == "__main__":
    unittest.main()
