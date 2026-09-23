#!/usr/bin/env python3
"""Offline tests for the portable installer and legacy-state migration."""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import component_doctor
import install_component as component
import migrate_legacy_state as migration
import apply_gpt6_compat as compat


class ComponentInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = pathlib.Path(__file__).resolve().parent.parent
        layout = component.canonical_layout()
        mise = component.resolve_mise(layout.home)
        account = component.pwd.getpwuid(component.os.getuid())
        cls.upstream_package = component.discover_package_root(
            mise, source, component.clean_environment(layout, mise, account.pw_name)
        )

    def _fake_candidate(self, source: pathlib.Path, layout: component.InstallLayout) -> pathlib.Path:
        candidate = component.candidate_path(source, layout)
        candidate.mkdir(mode=0o700, parents=True)
        component.copy_manifest(source, candidate)
        package = candidate / "runtime/package"
        package.mkdir(mode=0o700, parents=True)
        shutil.copy2(self.upstream_package / "package.json", package / "package.json")
        executable = package / "dist/cli.js"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"reviewed fake Maka executable\n")
        for relative in compat.PATCHES:
            target = package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.upstream_package / relative, target)
        hashes = compat.apply_overlay(package)
        receipt = {
            "schema": "khenrix-maka-candidate-v1",
            "version": component.PACKAGE_VERSION,
            "source_digest": component.source_digest(source),
            "package": component.reviewed_package_identity(source),
            "overlay_hashes": hashes,
            "runtime_package_digest": component.runtime_package_digest(package),
        }
        receipt_path = candidate / "candidate-receipt.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        receipt_path.chmod(0o600)
        return candidate

    def _install_with_fakes(
        self,
        home: pathlib.Path,
        *,
        activate: bool = True,
        fail_after_cutover: bool = False,
        fail_receipt_publish: bool = False,
    ) -> pathlib.Path:
        source = pathlib.Path(__file__).resolve().parent.parent
        layout = component.canonical_layout(home)
        candidate = self._fake_candidate(source, layout)
        package = (
            home
            / ".local/share/mise/installs/npm-maka-agent"
            / component.PACKAGE_VERSION
            / "package"
        )
        private_state = (
            mock.Mock(side_effect=component.ComponentInstallError("state finalization failed"))
            if fail_after_cutover
            else component.ensure_private_state_directory
        )
        original_replace = component.os.replace

        def replace(source: pathlib.Path | str, destination: pathlib.Path | str) -> None:
            if fail_receipt_publish and pathlib.Path(destination) == layout.state / "install-receipt.json":
                raise OSError("receipt publish failed")
            original_replace(source, destination)
        with (
            mock.patch.object(component, "canonical_layout", return_value=layout),
            mock.patch.object(component, "resolve_mise", return_value=home / ".local/bin/mise"),
            mock.patch.object(component, "supported_platform", return_value="macos-arm64"),
            mock.patch.object(
                component.pwd,
                "getpwuid",
                return_value=SimpleNamespace(pw_name="fixture-user"),
            ),
            mock.patch.object(component.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
            mock.patch.object(component, "discover_package_root", return_value=package),
            mock.patch.object(component, "stage_candidate", return_value=candidate),
            mock.patch.object(component, "ensure_private_state_directory", private_state),
            mock.patch.object(component.os, "replace", side_effect=replace),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return component.install(source, activate=activate)

    def _inspect_with_fakes(self, home: pathlib.Path) -> dict[str, object]:
        source = pathlib.Path(__file__).resolve().parent.parent
        layout = component.canonical_layout(home)
        package = (
            home
            / ".local/share/mise/installs/npm-maka-agent"
            / component.PACKAGE_VERSION
            / "package"
        )
        with (
            mock.patch.object(component_doctor, "canonical_layout", return_value=layout),
            mock.patch.object(
                component_doctor,
                "resolve_mise",
                return_value=home / ".local/bin/mise",
            ),
            mock.patch.object(component_doctor, "supported_platform", return_value="macos-arm64"),
            mock.patch.object(
                component_doctor.pwd,
                "getpwuid",
                return_value=SimpleNamespace(pw_name="fixture-user"),
            ),
            mock.patch.object(component_doctor, "discover_package_root", return_value=package),
        ):
            return component_doctor.inspect_component(source)

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

    def test_package_discovery_rejects_tampered_integrity_with_same_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            component_root = root / "component"
            lock_root = (
                component_root
                / ".mise/locks/npm-maka-agent"
                / component.PACKAGE_VERSION
            )
            lock_root.mkdir(parents=True)
            (lock_root / "aube-lock.yaml").write_text(
                "packages:\n"
                f"  {component.PACKAGE_NAME}@{component.PACKAGE_VERSION}:\n"
                "    resolution: {integrity: sha512-tampered}\n",
                encoding="utf-8",
            )
            package = root / "package"
            (package / "bin").mkdir(parents=True)
            (package / "bin/maka.js").write_text("", encoding="utf-8")
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": component.PACKAGE_NAME,
                        "version": component.PACKAGE_VERSION,
                    }
                ),
                encoding="utf-8",
            )
            shim = root / "bin/maka"
            shim.parent.mkdir()
            shim.write_text("#!/bin/sh\n# aube-bin-shim v2 target=../package/bin/maka.js\n")
            completed = SimpleNamespace(returncode=0, stdout=f"{shim}\n")
            with mock.patch.object(component.subprocess, "run", return_value=completed):
                with self.assertRaisesRegex(component.ComponentInstallError, "integrity"):
                    component.discover_package_root(
                        root / "mise",
                        component_root,
                        {},
                    )

    def test_package_discovery_accepts_reviewed_lock_with_snapshot_heading(self) -> None:
        source = pathlib.Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            package = root / "package"
            (package / "bin").mkdir(parents=True)
            (package / "bin/maka.js").write_text("", encoding="utf-8")
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": component.PACKAGE_NAME,
                        "version": component.PACKAGE_VERSION,
                    }
                ),
                encoding="utf-8",
            )
            shim = root / "bin/maka"
            shim.parent.mkdir()
            shim.write_text("#!/bin/sh\n# aube-bin-shim v2 target=../package/bin/maka.js\n")
            completed = SimpleNamespace(returncode=0, stdout=f"{shim}\n")
            with mock.patch.object(component.subprocess, "run", return_value=completed):
                self.assertEqual(
                    component.discover_package_root(root / "mise", source, {}),
                    package.resolve(),
                )

    def test_portable_manifest_covers_every_source_file(self) -> None:
        source = pathlib.Path(__file__).resolve().parent.parent
        self.assertEqual(
            set(component.read_manifest(source)),
            component_doctor.portable_files(source),
        )

    def test_successful_install_writes_private_reviewed_identity_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            self._install_with_fakes(home)
            layout = component.canonical_layout(home)
            receipt_path = layout.state / "install-receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

            self.assertEqual(receipt["schema"], "khenrix-maka-install-v2")
            self.assertEqual(receipt["package"], "maka-agent")
            self.assertEqual(receipt["version"], component.PACKAGE_VERSION)
            self.assertEqual(receipt["integrity"], component.PACKAGE_INTEGRITY)
            self.assertEqual(receipt["source_commit"], component.SOURCE_COMMIT)
            self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(layout.state.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                component_doctor.inspect_install_receipt(layout),
                receipt,
            )
            receipt["integrity"] = "sha512-invalid"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            receipt_path.chmod(0o600)
            with self.assertRaisesRegex(component_doctor.DoctorError, "integrity mismatch"):
                component_doctor.inspect_install_receipt(layout)

    def test_candidate_and_doctor_reject_unpatched_executable_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            source = pathlib.Path(__file__).resolve().parent.parent
            self._install_with_fakes(home)
            layout = component.canonical_layout(home)
            executable = layout.component / "runtime/package/dist/cli.js"
            executable.write_bytes(b"modified fake Maka executable\n")
            with self.assertRaisesRegex(component.ComponentInstallError, "runtime package"):
                component.candidate_receipt(layout.component, source)
            with self.assertRaisesRegex(component.ComponentInstallError, "runtime package"):
                self._inspect_with_fakes(home)

    def test_component_doctor_rejects_receipt_platform_backup_and_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            self._install_with_fakes(home)
            layout = component.canonical_layout(home)
            receipt_path = layout.state / "install-receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            other_platform = (
                "linux-x64"
                if component.supported_platform() != "linux-x64"
                else "macos-arm64"
            )
            cases = (
                ("platform", other_platform, "platform mismatch"),
                ("backup", "not-a-backup-id", "backup id is invalid"),
                ("wrapper", None, "wrapper path mismatch"),
            )
            for field, value, message in cases:
                with self.subTest(field=field):
                    candidate = dict(receipt)
                    candidate[field] = value
                    receipt_path.write_text(json.dumps(candidate), encoding="utf-8")
                    receipt_path.chmod(0o600)
                    with self.assertRaisesRegex(component_doctor.DoctorError, message):
                        component_doctor.inspect_install_receipt(layout)

            encoded = json.dumps(receipt)
            needle = f'"version": "{component.PACKAGE_VERSION}"'
            receipt_path.write_text(
                encoded.replace(needle, f'{needle}, {needle}', 1),
                encoding="utf-8",
            )
            receipt_path.chmod(0o600)
            with self.assertRaisesRegex(component_doctor.DoctorError, "duplicate field"):
                component_doctor.inspect_install_receipt(layout)

    def test_failed_first_install_does_not_publish_a_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            with self.assertRaisesRegex(component.ComponentInstallError, "state finalization failed"):
                self._install_with_fakes(home, fail_after_cutover=True)
            self.assertFalse(
                (component.canonical_layout(home).state / "install-receipt.json").exists()
            )

    def test_stage_only_first_install_does_not_publish_final_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            candidate = self._install_with_fakes(home, activate=False)
            layout = component.canonical_layout(home)
            self.assertTrue(candidate.is_dir())
            self.assertFalse(layout.component.exists())
            self.assertFalse(layout.wrapper.exists())
            self.assertFalse((layout.state / "install-receipt.json").exists())

    def test_stage_only_upgrade_preserves_previous_receipt_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            layout = component.canonical_layout(home)
            layout.component.mkdir(mode=0o700, parents=True)
            (layout.component / "old").write_bytes(b"old component\n")
            layout.wrapper.parent.mkdir(mode=0o700, parents=True)
            layout.wrapper.write_bytes(b"#!/bin/sh\nold-wrapper\n")
            layout.wrapper.chmod(0o755)
            layout.state.mkdir(mode=0o700, parents=True)
            receipt_path = layout.state / "install-receipt.json"
            previous = b'{"schema":"previous-install"}\n'
            receipt_path.write_bytes(previous)
            receipt_path.chmod(0o600)

            self._install_with_fakes(home, activate=False)

            self.assertEqual(receipt_path.read_bytes(), previous)
            self.assertEqual((layout.component / "old").read_bytes(), b"old component\n")
            self.assertEqual(layout.wrapper.read_bytes(), b"#!/bin/sh\nold-wrapper\n")

    def test_failed_upgrade_preserves_previous_receipt_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            layout = component.canonical_layout(home)
            layout.component.mkdir(mode=0o700, parents=True)
            (layout.component / "old").write_bytes(b"old component\n")
            layout.wrapper.parent.mkdir(mode=0o700, parents=True)
            layout.wrapper.write_bytes(b"#!/bin/sh\nexit 0\n")
            layout.wrapper.chmod(0o755)
            layout.state.mkdir(mode=0o700, parents=True)
            receipt_path = layout.state / "install-receipt.json"
            previous = b'{"schema":"previous-install"}\n'
            receipt_path.write_bytes(previous)
            receipt_path.chmod(0o600)

            with self.assertRaisesRegex(component.ComponentInstallError, "state finalization failed"):
                self._install_with_fakes(home, fail_after_cutover=True)

            self.assertEqual(receipt_path.read_bytes(), previous)
            self.assertEqual((layout.component / "old").read_bytes(), b"old component\n")

    def test_receipt_publish_failure_restores_component_wrapper_and_previous_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            layout = component.canonical_layout(home)
            layout.component.mkdir(mode=0o700, parents=True)
            (layout.component / "old").write_bytes(b"old component\n")
            layout.wrapper.parent.mkdir(mode=0o700, parents=True)
            old_wrapper = b"#!/bin/sh\nold-wrapper\n"
            layout.wrapper.write_bytes(old_wrapper)
            layout.wrapper.chmod(0o755)
            layout.state.mkdir(mode=0o700, parents=True)
            receipt_path = layout.state / "install-receipt.json"
            previous = b'{"schema":"previous-install"}\n'
            receipt_path.write_bytes(previous)
            receipt_path.chmod(0o600)

            with self.assertRaisesRegex(OSError, "receipt publish failed"):
                self._install_with_fakes(home, fail_receipt_publish=True)

            self.assertEqual(receipt_path.read_bytes(), previous)
            self.assertEqual((layout.component / "old").read_bytes(), b"old component\n")
            self.assertEqual(layout.wrapper.read_bytes(), old_wrapper)

    def test_explicit_rollback_restores_previous_receipt_with_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            layout = component.canonical_layout(home)
            layout.component.mkdir(mode=0o700, parents=True)
            (layout.component / "old").write_bytes(b"old component\n")
            layout.wrapper.parent.mkdir(mode=0o700, parents=True)
            old_wrapper = b"#!/bin/sh\nold-wrapper\n"
            layout.wrapper.write_bytes(old_wrapper)
            layout.wrapper.chmod(0o755)
            layout.state.mkdir(mode=0o700, parents=True)
            receipt_path = layout.state / "install-receipt.json"
            previous = b'{"schema":"previous-install"}\n'
            receipt_path.write_bytes(previous)
            receipt_path.chmod(0o600)

            self._install_with_fakes(home)
            backup = json.loads(receipt_path.read_text())["backup"]
            with (
                mock.patch.object(component, "canonical_layout", return_value=layout),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                component.rollback(backup)

            self.assertEqual(receipt_path.read_bytes(), previous)
            self.assertEqual((layout.component / "old").read_bytes(), b"old component\n")
            self.assertEqual(layout.wrapper.read_bytes(), old_wrapper)

    def test_explicit_rollback_removes_receipt_when_previous_install_had_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            layout = component.canonical_layout(home)
            layout.component.mkdir(mode=0o700, parents=True)
            (layout.component / "old").write_bytes(b"old component\n")
            layout.wrapper.parent.mkdir(mode=0o700, parents=True)
            layout.wrapper.write_bytes(b"#!/bin/sh\nold-wrapper\n")
            layout.wrapper.chmod(0o755)

            self._install_with_fakes(home)
            receipt_path = layout.state / "install-receipt.json"
            backup = json.loads(receipt_path.read_text())["backup"]
            with (
                mock.patch.object(component, "canonical_layout", return_value=layout),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                component.rollback(backup)

            self.assertFalse(receipt_path.exists())
            self.assertEqual((layout.component / "old").read_bytes(), b"old component\n")

    def test_first_install_rollback_removes_component_and_restores_preexisting_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            layout = component.canonical_layout(home)
            layout.wrapper.parent.mkdir(mode=0o700, parents=True)
            old_wrapper = b"#!/bin/sh\npreexisting-wrapper\n"
            layout.wrapper.write_bytes(old_wrapper)
            layout.wrapper.chmod(0o755)

            self._install_with_fakes(home)
            receipt_path = layout.state / "install-receipt.json"
            backup = json.loads(receipt_path.read_text())["backup"]
            with (
                mock.patch.object(component, "canonical_layout", return_value=layout),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                component.rollback(backup)

            self.assertFalse(layout.component.exists())
            self.assertFalse(receipt_path.exists())
            self.assertEqual(layout.wrapper.read_bytes(), old_wrapper)

    def test_rollback_receipt_restore_failure_leaves_no_false_current_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            layout = component.canonical_layout(home)
            layout.component.mkdir(mode=0o700, parents=True)
            (layout.component / "old").write_bytes(b"old component\n")
            layout.wrapper.parent.mkdir(mode=0o700, parents=True)
            old_wrapper = b"#!/bin/sh\nold-wrapper\n"
            layout.wrapper.write_bytes(old_wrapper)
            layout.wrapper.chmod(0o755)
            layout.state.mkdir(mode=0o700, parents=True)
            receipt_path = layout.state / "install-receipt.json"
            receipt_path.write_bytes(b'{"schema":"previous-install"}\n')
            receipt_path.chmod(0o600)

            self._install_with_fakes(home)
            backup = json.loads(receipt_path.read_text())["backup"]
            original_atomic_write = component.atomic_write

            def fail_receipt_restore(path: pathlib.Path, payload: bytes, mode: int) -> None:
                if path == receipt_path:
                    raise OSError("receipt restore failed")
                original_atomic_write(path, payload, mode)

            with (
                mock.patch.object(component, "canonical_layout", return_value=layout),
                mock.patch.object(component, "atomic_write", side_effect=fail_receipt_restore),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(OSError, "receipt restore failed"),
            ):
                component.rollback(backup)

            self.assertFalse(receipt_path.exists())
            self.assertEqual((layout.component / "old").read_bytes(), b"old component\n")
            self.assertEqual(layout.wrapper.read_bytes(), old_wrapper)

    def test_component_doctor_fails_closed_on_component_and_managed_wrapper_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            self._install_with_fakes(home)
            layout = component.canonical_layout(home)
            source = pathlib.Path(__file__).resolve().parent.parent
            installed_readme = layout.component / "README.md"
            original_readme = installed_readme.read_bytes()
            installed_readme.write_bytes(original_readme + b"drift\n")
            with self.assertRaisesRegex(component_doctor.DoctorError, "installed component drift"):
                self._inspect_with_fakes(home)

            installed_readme.write_bytes((source / "README.md").read_bytes())
            layout.wrapper.write_bytes(layout.wrapper.read_bytes() + b"# appended drift\n")
            layout.wrapper.chmod(0o755)
            with self.assertRaisesRegex(component_doctor.DoctorError, "managed wrapper drift"):
                self._inspect_with_fakes(home)

    def test_atomic_write_has_no_fallible_chmod_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "receipt.json"
            path.write_bytes(b"old\n")
            path.chmod(0o600)
            original_chmod = component.os.chmod

            def chmod(
                candidate: pathlib.Path | str,
                mode: int,
                **kwargs: object,
            ) -> None:
                if pathlib.Path(candidate) == path:
                    raise OSError("post-replace chmod called")
                original_chmod(candidate, mode, **kwargs)

            with mock.patch.object(component.os, "chmod", side_effect=chmod):
                component.atomic_write(path, b"new\n", 0o600)
            self.assertEqual(path.read_bytes(), b"new\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


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
