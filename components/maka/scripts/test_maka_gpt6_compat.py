#!/usr/bin/env python3
"""The pinned package overlay is source-guarded and leaves old models intact."""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import unittest

import apply_gpt6_compat as compat
import install_component as component


class PinnedOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = pathlib.Path(__file__).resolve().parent.parent
        layout = component.canonical_layout()
        mise = component.resolve_mise(layout.home)
        account = component.pwd.getpwuid(component.os.getuid())
        environment = component.clean_environment(layout, mise, account.pw_name)
        cls.package = component.discover_package_root(mise, source, environment)

    def copied_sources(self, destination: pathlib.Path) -> None:
        shutil.copy2(self.package / "package.json", destination / "package.json")
        for relative in compat.PATCHES:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.package / relative, target)

    def test_exact_pinned_sources_gain_gpt6_without_losing_gpt56(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = pathlib.Path(directory)
            self.copied_sources(package)
            receipt = compat.apply_overlay(package)
            self.assertEqual(set(receipt), set(compat.PATCHES))
            for relative, hashes in receipt.items():
                self.assertEqual(
                    compat.sha256((package / relative).read_bytes()), hashes["patched"]
                )
                self.assertNotEqual(hashes["source"], hashes["patched"])
            metadata = (package / "node_modules/@maka/core/dist/model-metadata.js").read_text()
            registry = (package / "node_modules/@maka/core/dist/provider-registry.js").read_text()
            self.assertIn("'gpt-6-sol'", metadata)
            self.assertIn("'gpt-5.6-sol'", metadata)
            self.assertIn("'gpt-6-sol'", registry)
            self.assertIn("'gpt-5.6-sol'", registry)

    def test_any_mismatched_source_blocks_every_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = pathlib.Path(directory)
            self.copied_sources(package)
            paths = [package / relative for relative in compat.PATCHES]
            originals = [path.read_bytes() for path in paths]
            paths[-1].write_bytes(originals[-1] + b"\n// changed\n")
            with self.assertRaisesRegex(compat.OverlayError, "source hash differs"):
                compat.apply_overlay(package)
            self.assertEqual([path.read_bytes() for path in paths[:-1]], originals[:-1])


if __name__ == "__main__":
    unittest.main()
