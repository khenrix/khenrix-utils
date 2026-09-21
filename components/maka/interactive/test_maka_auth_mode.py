#!/usr/bin/env python3
"""Offline tests for the per-machine Maka authentication selector."""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from unittest import mock

import maka_auth_mode as selector


class AuthModeTests(unittest.TestCase):
    def test_both_modes_round_trip_with_owner_only_permissions(self) -> None:
        for mode in selector.ALLOWED_MODES:
            with tempfile.TemporaryDirectory() as directory:
                path = pathlib.Path(directory) / "private" / "maka-auth-mode"
                selector.write_mode(mode, path)
                self.assertEqual(selector.read_mode(path), mode)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_invalid_mode_does_not_replace_valid_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "private" / "maka-auth-mode"
            selector.write_mode("api-key-relay", path)
            with self.assertRaises(selector.AuthModeError):
                selector.write_mode("automatic", path)
            self.assertEqual(selector.read_mode(path), "api-key-relay")

    def test_selector_is_idempotent_but_refuses_cross_mode_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "private" / "maka-auth-mode"
            selector.write_mode("chatgpt-subscription", path)
            before = path.stat().st_ino
            selector.write_mode("chatgpt-subscription", path)
            self.assertEqual(path.stat().st_ino, before)
            with self.assertRaisesRegex(selector.AuthModeError, "migration is unsupported"):
                selector.write_mode("api-key-relay", path)
            self.assertEqual(selector.read_mode(path), "chatgpt-subscription")

    def test_concurrent_initial_selectors_cannot_replace_the_first_published_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private = pathlib.Path(directory) / "private"
            mode_path = private / "maka-auth-mode"

            def publish_competing_mode(_source, target, **_options):
                pathlib.Path(target).write_text("chatgpt-subscription\n", encoding="ascii")
                pathlib.Path(target).chmod(0o600)
                raise FileExistsError

            with mock.patch.object(selector.os, "link", side_effect=publish_competing_mode):
                with self.assertRaisesRegex(selector.AuthModeError, "migration is unsupported"):
                    selector.write_mode("api-key-relay", mode_path)
            self.assertEqual(selector.read_mode(mode_path), "chatgpt-subscription")

            account_path = private / "maka-openai-keychain-account"

            def publish_competing_account(_source, target, **_options):
                pathlib.Path(target).write_text("first@example.com\n", encoding="ascii")
                pathlib.Path(target).chmod(0o600)
                raise FileExistsError

            with mock.patch.object(selector.os, "link", side_effect=publish_competing_account):
                with self.assertRaisesRegex(selector.AuthModeError, "already selected"):
                    selector.write_keychain_account("second@example.com", account_path)
            self.assertEqual(selector.read_keychain_account(account_path), "first@example.com")

    def test_symlink_is_rejected_for_read_and_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            private = root / "private"
            private.mkdir(mode=0o700)
            real = root / "real"
            real.write_text("api-key-relay\n", encoding="ascii")
            os.chmod(real, 0o600)
            path = private / "maka-auth-mode"
            path.symlink_to(real)
            with self.assertRaises(selector.AuthModeError):
                selector.read_mode(path)
            with self.assertRaises(selector.AuthModeError):
                selector.write_mode("chatgpt-subscription", path)

    def test_loose_file_or_directory_permissions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "private" / "maka-auth-mode"
            selector.write_mode("api-key-relay", path)
            os.chmod(path, 0o644)
            with self.assertRaises(selector.AuthModeError):
                selector.read_mode(path)
            os.chmod(path, 0o600)
            os.chmod(path.parent, 0o755)
            with self.assertRaises(selector.AuthModeError):
                selector.read_mode(path)
            with self.assertRaises(selector.AuthModeError):
                selector.write_mode("chatgpt-subscription", path)

    def test_malformed_selector_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "private" / "maka-auth-mode"
            path.parent.mkdir(mode=0o700)
            path.write_text("chatgpt-subscription extra\n", encoding="ascii")
            os.chmod(path, 0o600)
            with self.assertRaises(selector.AuthModeError):
                selector.read_mode(path)

    def test_keychain_account_is_private_bounded_and_explicitly_replaceable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "private" / "maka-openai-keychain-account"
            selector.write_keychain_account("cli|first-account", path)
            self.assertEqual(selector.read_keychain_account(path), "cli|first-account")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(selector.AuthModeError):
                selector.write_keychain_account("cli|second-account", path)
            selector.write_keychain_account("cli|second-account", path, replace=True)
            self.assertEqual(selector.read_keychain_account(path), "cli|second-account")
            selector.write_keychain_account(
                "developer@example.com", path, replace=True
            )
            self.assertEqual(selector.read_keychain_account(path), "developer@example.com")
            for invalid in (
                "",
                "-leading-dash",
                "a" * 129,
                "cli|bad account",
                "cli|bad\naccount",
            ):
                with self.subTest(invalid=invalid), self.assertRaises(selector.AuthModeError):
                    selector.write_keychain_account(invalid, path, replace=True)

    def test_keychain_account_rejects_symlinks_and_loose_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            private = root / "private"
            private.mkdir(mode=0o700)
            real = root / "account"
            real.write_text("cli|selected\n", encoding="ascii")
            real.chmod(0o600)
            symlink = private / "maka-openai-keychain-account"
            symlink.symlink_to(real)
            with self.assertRaises(selector.AuthModeError):
                selector.read_keychain_account(symlink)
            symlink.unlink()
            selector.write_keychain_account("cli|selected", symlink)
            symlink.chmod(0o644)
            with self.assertRaises(selector.AuthModeError):
                selector.read_keychain_account(symlink)

    def test_relay_readiness_is_private_explicit_and_invalidatable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private = pathlib.Path(directory) / "private"
            mode_path = private / "maka-auth-mode"
            account_path = private / "maka-openai-keychain-account"
            ready_path = private / "maka-api-key-relay-ready"
            selector.write_mode("api-key-relay", mode_path)
            selector.write_keychain_account("cli|first-account", account_path)
            with self.assertRaises(selector.AuthModeError):
                selector.require_relay_ready(mode_path, ready_path, account_path)
            selector.write_relay_ready(ready_path, account_path)
            selector.require_relay_ready(mode_path, ready_path, account_path)
            self.assertEqual(ready_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(b"first-account", ready_path.read_bytes())
            selector.write_keychain_account("cli|second-account", account_path, replace=True)
            with self.assertRaises(selector.AuthModeError):
                selector.require_relay_ready(mode_path, ready_path, account_path)
            selector.invalidate_relay_ready(ready_path)
            self.assertFalse(ready_path.exists())
            with self.assertRaises(selector.AuthModeError):
                selector.require_relay_ready(mode_path, ready_path, account_path)

    def test_subscription_mode_never_accepts_a_relay_readiness_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private = pathlib.Path(directory) / "private"
            mode_path = private / "maka-auth-mode"
            account_path = private / "maka-openai-keychain-account"
            ready_path = private / "maka-api-key-relay-ready"
            selector.write_mode("chatgpt-subscription", mode_path)
            selector.write_keychain_account("cli|selected", account_path)
            selector.write_relay_ready(ready_path, account_path)
            with self.assertRaises(selector.AuthModeError):
                selector.require_relay_ready(mode_path, ready_path, account_path)


if __name__ == "__main__":
    unittest.main()
