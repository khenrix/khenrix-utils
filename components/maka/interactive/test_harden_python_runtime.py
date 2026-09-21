#!/usr/bin/env python3
"""Credential-free tests for the pinned Python tree hardener."""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
os.sys.path.insert(0, str(SCRIPTS))

import harden_python_runtime as hardener


class RuntimeHardenerTests(unittest.TestCase):
    def _fixture(self, parent: pathlib.Path) -> pathlib.Path:
        root = parent / "runtime"
        bin_dir = root / "bin"
        lib_dir = root / "lib/python3.12"
        bin_dir.mkdir(parents=True, mode=0o775)
        lib_dir.mkdir(parents=True, mode=0o775)
        root.chmod(0o775)
        bin_dir.chmod(0o775)
        lib_dir.chmod(0o775)
        executable = bin_dir / "python3.12"
        executable.write_bytes(b"fixture")
        executable.chmod(0o775)
        (bin_dir / "python3").symlink_to("python3.12")
        (bin_dir / "python").symlink_to("python3.12")
        module = lib_dir / "module.py"
        module.write_text("fixture = True\n", encoding="ascii")
        module.chmod(0o664)
        return root

    def test_acl_parser_accepts_effect_field_deny_and_rejects_principal_named_deny(self) -> None:
        self.assertTrue(
            hardener._acl_document_is_deny_only(
                "!#acl 1\ngroup:ABCDEFAB-CDEF-ABCD-EFAB-CDEF0000000C:everyone:12:deny:delete\n"
            )
        )
        self.assertFalse(
            hardener._acl_document_is_deny_only(
                "!#acl 1\nuser:ABCDEFAB-CDEF-ABCD-EFAB-CDEF0000000C:deny:12:allow:write\n"
            )
        )
        self.assertFalse(hardener._acl_document_is_deny_only("!#acl 1\nmalformed:deny\n"))

    def test_hardener_clears_go_write_and_verifies_every_physical_entry(self) -> None:
        with tempfile.TemporaryDirectory(dir=hardener.canonical_home()) as directory:
            root = self._fixture(pathlib.Path(directory))
            hardener.harden_runtime(root)
            hardener.verify_runtime(root)
            for base, directories, files in os.walk(root, followlinks=False):
                for name in (*directories, *files):
                    path = pathlib.Path(base) / name
                    if not path.is_symlink():
                        self.assertEqual(path.lstat().st_mode & 0o022, 0)

    def test_hardener_rejects_symlink_escape_without_changing_target(self) -> None:
        with tempfile.TemporaryDirectory(dir=hardener.canonical_home()) as directory:
            parent = pathlib.Path(directory)
            root = self._fixture(parent)
            outside = parent / "outside"
            outside.write_text("untouched\n", encoding="ascii")
            outside.chmod(0o664)
            (root / "escape").symlink_to(outside)
            with self.assertRaises(hardener.RuntimeHardeningError):
                hardener.harden_runtime(root)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o664)

    def test_hardener_removes_extended_acl_then_verifies_none_remains(self) -> None:
        with tempfile.TemporaryDirectory(dir=hardener.canonical_home()) as directory:
            root = self._fixture(pathlib.Path(directory))
            module = root / "lib/python3.12/module.py"
            subprocess.run(
                ["/bin/chmod", "+a", "everyone allow write", str(module)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with self.assertRaises(hardener.RuntimeHardeningError):
                hardener.verify_runtime(root)
            hardener.harden_runtime(root)
            hardener.verify_runtime(root)

    def test_hardener_rejects_regular_file_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory(dir=hardener.canonical_home()) as directory:
            root = self._fixture(pathlib.Path(directory))
            module = root / "lib/python3.12/module.py"
            os.link(module, root / "lib/python3.12/module-copy.py")
            with self.assertRaises(hardener.RuntimeHardeningError):
                hardener.harden_runtime(root)

    def test_mise_bootstrap_executes_an_owner_only_copy_of_validated_inode(self) -> None:
        with tempfile.TemporaryDirectory(dir=hardener.canonical_home()) as directory:
            root = pathlib.Path(directory)
            source = root / "mise-source"
            source.write_bytes(b"validated-mise-inode")
            source.chmod(0o500)
            staging = root / "private-staging"
            with hardener._staged_mise_binary((source,), staging) as copied:
                self.assertEqual(copied.read_bytes(), b"validated-mise-inode")
                self.assertEqual(copied.stat().st_mode & 0o777, 0o500)
                self.assertEqual(copied.parent.stat().st_mode & 0o077, 0)
                source.chmod(0o700)
                source.write_bytes(b"changed-after-validation")
                self.assertEqual(copied.read_bytes(), b"validated-mise-inode")

    def test_mise_bootstrap_rejects_source_with_extended_acl(self) -> None:
        with tempfile.TemporaryDirectory(dir=hardener.canonical_home()) as directory:
            root = pathlib.Path(directory)
            source = root / "mise-source"
            source.write_bytes(b"mise-with-write-acl")
            source.chmod(0o500)
            subprocess.run(
                ["/bin/chmod", "+a", "everyone allow write", str(source)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with self.assertRaises(hardener.RuntimeHardeningError):
                with hardener._staged_mise_binary((source,), root / "staging"):
                    self.fail("ACL-bearing mise source was staged")


if __name__ == "__main__":
    unittest.main()
