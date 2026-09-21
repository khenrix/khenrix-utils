#!/usr/bin/env python3
"""Offline tests for the portable installer and legacy-state migration."""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest

import component_doctor
import install_component as component
import migrate_legacy_state as migration


class ComponentInstallerTests(unittest.TestCase):
    def test_supported_platform_matrix_is_exact(self) -> None:
        self.assertEqual(component.supported_platform("Darwin", "arm64"), "macos-arm64")
        self.assertEqual(component.supported_platform("Linux", "x86_64"), "linux-x64")
        with self.assertRaises(component.ComponentInstallError):
            component.supported_platform("Darwin", "x86_64")
        with self.assertRaises(component.ComponentInstallError):
            component.supported_platform("Linux", "aarch64")
        with self.assertRaises(component.ComponentInstallError):
            component.supported_platform("Windows", "AMD64")

    def test_wrapper_rendering_is_checkout_independent_and_shell_quoted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory) / "home with space"
            layout = component.canonical_layout(home)
            template = (pathlib.Path(__file__).resolve().parent.parent / "wrapper/maka.in").read_text()
            rendered = component.render_wrapper(
                template,
                layout=layout,
                account_name="person with space",
                mise=home / ".local/bin/mise",
                package=home / ".local/share/mise/package with space",
            )
            self.assertNotIn("@@", rendered)
            self.assertNotIn("git/agentic-setup", rendered)
            self.assertIn("dev.khenrix.maka-openai-relay", rendered)
            self.assertIn("--thinking xhigh", rendered)
            self.assertIn("'person with space'", rendered)
            self.assertIn(str(layout.component), rendered)

    def test_manifest_rejects_symlinks_and_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "real").write_text("safe\n")
            (root / "link").symlink_to("real")
            (root / component.MANIFEST_NAME).write_text(
                f"{component.MANIFEST_NAME}\nlink\n", encoding="utf-8"
            )
            with self.assertRaises(component.ComponentInstallError):
                component.read_manifest(root)
            (root / component.MANIFEST_NAME).write_text(
                f"{component.MANIFEST_NAME}\n../outside\n", encoding="utf-8"
            )
            with self.assertRaises(component.ComponentInstallError):
                component.read_manifest(root)

    def test_portable_manifest_covers_every_source_file(self) -> None:
        source = pathlib.Path(__file__).resolve().parent.parent
        self.assertEqual(
            set(component.read_manifest(source)),
            component_doctor.portable_files(source),
        )


class LegacyMigrationTests(unittest.TestCase):
    def private_file(self, path: pathlib.Path, payload: bytes) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        path.write_bytes(payload)
        path.chmod(0o600)

    def test_subscription_migration_copies_only_the_selector_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            legacy = home / migration.LEGACY_CONFIG
            self.private_file(legacy / migration.MODE, b"chatgpt-subscription\n")
            document = migration.plan(home)
            self.assertEqual(document["mode"], "chatgpt-subscription")
            self.assertEqual(document["files"], [{"path": migration.MODE, "bytes": 21}])
            self.assertNotIn("sha256", json.dumps(document))
            with contextlib.redirect_stdout(io.StringIO()):
                migration.apply(home)
            managed = home / migration.NEW_CONFIG
            self.assertEqual((managed / migration.MODE).read_bytes(), b"chatgpt-subscription\n")
            self.assertFalse((managed / "oauth.json").exists())
            with contextlib.redirect_stdout(io.StringIO()):
                migration.rollback(home)
            self.assertTrue((legacy / migration.MODE).exists())
            self.assertFalse(managed.exists())

    def test_api_migration_copies_only_local_decoys_and_bound_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            config = home / migration.LEGACY_CONFIG
            relay = home / migration.LEGACY_RELAY
            self.private_file(config / migration.MODE, b"api-key-relay\n")
            self.private_file(config / migration.ACCOUNT, b"account@example.com\n")
            self.private_file(config / migration.READY, b"ready-fixture\n")
            self.private_file(relay / "caller-token", b"A" * 64 + b"\n")
            self.private_file(relay / "relay-attestation", b"B" * 64 + b"\n")
            with contextlib.redirect_stdout(io.StringIO()):
                migration.apply(home)
            managed = home / migration.NEW_CONFIG
            expected = {
                migration.MODE,
                migration.ACCOUNT,
                migration.READY,
                "relay/caller-token",
                "relay/relay-attestation",
                migration.RECEIPT,
            }
            actual = {
                path.relative_to(managed).as_posix()
                for path in managed.rglob("*")
                if path.is_file()
            }
            self.assertEqual(actual, expected)
            for relative in expected:
                self.assertEqual((managed / relative).stat().st_mode & 0o777, 0o600)
            self.assertFalse((managed / "OPENAI_API_KEY").exists())

    def test_rollback_refuses_to_remove_state_changed_after_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            legacy = home / migration.LEGACY_CONFIG
            self.private_file(legacy / migration.MODE, b"chatgpt-subscription\n")
            with contextlib.redirect_stdout(io.StringIO()):
                migration.apply(home)
            managed_mode = home / migration.NEW_CONFIG / migration.MODE
            managed_mode.write_bytes(b"api-key-relay\n")
            managed_mode.chmod(0o600)
            with self.assertRaises(migration.MigrationError):
                migration.rollback(home)
            self.assertTrue(managed_mode.exists())

    def test_symlinked_legacy_selector_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            legacy = home / migration.LEGACY_CONFIG
            legacy.mkdir(mode=0o700, parents=True)
            real = home / "real"
            real.write_bytes(b"chatgpt-subscription\n")
            real.chmod(0o600)
            (legacy / migration.MODE).symlink_to(real)
            with self.assertRaises(migration.MigrationError):
                migration.plan(home)


if __name__ == "__main__":
    unittest.main()
