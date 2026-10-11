"""Source companion installs must reject drift and preserve rollback state."""

import hashlib
import importlib.util
import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock


HELPER = pathlib.Path(__file__).with_name("source_runtime.py")


def reference(path):
    metadata = path.stat()
    return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                bytes=metadata.st_size, owner=metadata.st_uid, mode=stat.S_IMODE(metadata.st_mode),
                device=metadata.st_dev, inode=metadata.st_ino)


class SourceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(HELPER.exists(), "source companion installer is missing")
        spec = importlib.util.spec_from_file_location("source_runtime", HELPER)
        self.runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runtime)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name).resolve()
        self.source = self.root / "candidate"
        self.source.mkdir(mode=0o700)
        (self.source / "dist").mkdir(mode=0o700)
        self.payload = self.source / "dist/worker.js"
        self.payload.write_bytes(b"worker")
        self.payload.chmod(0o600)
        self.manifest = self.source / "runtime-manifest.json"
        self.manifest.write_text(json.dumps(dict(schema="validated-runtime-v1", artifact_root=str(self.source),
            commit="a" * 40, capacity=64, files={"dist/worker.js": reference(self.payload)},
            worker=reference(self.payload), entrypoints={"host": str(self.payload)},
            packaging_repairs=[dict(original=dict(path="/original/build/package.json"), staged=reference(self.payload))],
            statistics=dict(files=1, bytes=6, symlinks=0))))
        self.manifest.chmod(0o600)
        self.ref = reference(self.manifest)
        self.component = self.root / "component"
        (self.component / "runtime").mkdir(parents=True, mode=0o700)
        self.state = self.root / "state"

    def plan(self):
        return self.runtime.make_plan(self.ref, self.component, self.state)

    def apply(self, plan=None):
        plan = plan or self.plan()
        return self.runtime.apply_plan(plan, self.runtime.digest_json(plan))

    def test_install_rebinds_paths_and_inodes_and_keeps_original_provenance(self):
        plan = self.plan()
        self.assertFalse(self.state.exists())
        receipt = self.apply(plan)
        installed = self.component / "runtime/source-candidate"
        document = json.loads((installed / "runtime-manifest.json").read_text())
        self.assertEqual(document["worker"], reference(installed / "dist/worker.js"))
        self.assertEqual(document["entrypoints"]["host"], str(installed / "dist/worker.js"))
        self.assertEqual(document["packaging_repairs"][0]["staged"], document["worker"])
        self.assertEqual(document["packaging_repairs"][0]["original"]["path"], "/original/build/package.json")
        self.assertEqual(document["source_manifest"], self.ref)
        self.assertEqual(receipt["source_manifest"], self.ref)
        self.assertEqual(stat.S_IMODE((self.state / "source-runtime/receipt.json").stat().st_mode), 0o600)
        self.assertEqual(self.runtime.doctor(self.component, self.state)["status"], "verified")
        self.assertEqual(reference(self.manifest), self.ref)

    def test_candidate_tampering_cannot_cut_over(self):
        for mutation in ("content", "mode", "extra", "symlink", "inode"):
            with self.subTest(mutation=mutation):
                plan = self.plan()
                if mutation == "content":
                    self.payload.write_bytes(b"tamper")
                elif mutation == "mode":
                    self.payload.chmod(0o644)
                elif mutation == "extra":
                    (self.source / "extra").write_text("extra")
                elif mutation == "symlink":
                    (self.source / "extra").symlink_to(self.payload)
                else:
                    contents = self.payload.read_bytes()
                    self.payload.unlink()
                    self.payload.write_bytes(contents)
                    self.payload.chmod(0o600)
                with self.assertRaises(self.runtime.SourceRuntimeError):
                    self.apply(plan)
                self.assertFalse((self.component / "runtime/source-candidate").exists())
                self.setUp()

    def test_review_digest_and_existing_state_are_compare_and_swap_guards(self):
        plan = self.plan()
        with self.assertRaises(self.runtime.SourceRuntimeError):
            self.runtime.apply_plan(plan, "0" * 64)
        self.apply(plan)
        with self.assertRaises(self.runtime.SourceRuntimeError):
            self.apply(plan)

    def test_doctor_and_rollback_reject_installed_tampering(self):
        self.apply()
        receipt_path = self.state / "source-runtime/receipt.json"
        receipt_sha = reference(receipt_path)["sha256"]
        installed = self.component / "runtime/source-candidate/dist/worker.js"
        installed.write_bytes(b"tamper")
        with self.assertRaises(self.runtime.SourceRuntimeError):
            self.runtime.doctor(self.component, self.state)
        with self.assertRaises(self.runtime.SourceRuntimeError):
            self.runtime.rollback(self.component, self.state, receipt_sha)

    def test_upgrade_and_rollback_restore_exact_previous_tree_and_receipt(self):
        self.apply()
        old_receipt = (self.state / "source-runtime/receipt.json").read_bytes()
        old_inode = (self.component / "runtime/source-candidate/dist/worker.js").stat().st_ino
        self.apply()
        receipt_sha = reference(self.state / "source-runtime/receipt.json")["sha256"]
        self.runtime.rollback(self.component, self.state, receipt_sha)
        self.assertEqual((self.state / "source-runtime/receipt.json").read_bytes(), old_receipt)
        self.assertEqual((self.component / "runtime/source-candidate/dist/worker.js").stat().st_ino, old_inode)
        self.runtime.doctor(self.component, self.state)

    def test_failed_receipt_publication_restores_previous_install(self):
        self.apply()
        old_receipt = (self.state / "source-runtime/receipt.json").read_bytes()
        old_inode = (self.component / "runtime/source-candidate/dist/worker.js").stat().st_ino
        with mock.patch.object(self.runtime, "publish_receipt", side_effect=OSError("publication failed"), create=True):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual((self.state / "source-runtime/receipt.json").read_bytes(), old_receipt)
        self.assertEqual((self.component / "runtime/source-candidate/dist/worker.js").stat().st_ino, old_inode)

    def test_interrupt_immediately_after_exchange_keeps_previous_tree(self):
        self.apply()
        old_inode = (self.component / "runtime/source-candidate/dist/worker.js").stat().st_ino
        exchange = self.runtime.exchange
        interrupted = False
        def interrupt_after_exchange(left, right):
            nonlocal interrupted
            exchange(left, right)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
        with mock.patch.object(self.runtime, "exchange", side_effect=interrupt_after_exchange):
            with self.assertRaises(KeyboardInterrupt):
                self.apply()
        self.assertEqual((self.component / "runtime/source-candidate/dist/worker.js").stat().st_ino, old_inode)
        self.runtime.doctor(self.component, self.state)

    def crash(self, action, operation, predicate):
        self.assertTrue(hasattr(self.runtime, operation), "durable cutover operation is missing")
        process = os.fork()
        if process == 0:
            try:
                original = getattr(self.runtime, operation)
                def interrupted(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if predicate(*args, **kwargs):
                        os._exit(99)
                    return result
                with mock.patch.object(self.runtime, operation, side_effect=interrupted):
                    action()
                os._exit(98)
            except BaseException:
                os._exit(97)
        _, status = os.waitpid(process, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 99)

    def recover(self):
        transaction = self.state / "source-runtime/transaction.json"
        self.assertTrue(transaction.exists())
        self.runtime.recover(self.component, self.state, reference(transaction)["sha256"])
        self.assertFalse(transaction.exists())

    def test_crashed_first_install_can_recover_before_publication(self):
        self.crash(self.apply, "move_new", lambda left, right: pathlib.Path(right).name == "source-candidate")
        self.recover()
        self.assertFalse((self.component / "runtime/source-candidate").exists())
        self.assertFalse((self.state / "source-runtime/receipt.json").exists())

    def test_crashed_upgrade_can_recover_before_publication(self):
        self.apply()
        old = reference(self.component / "runtime/source-candidate/dist/worker.js")
        self.crash(self.apply, "exchange", lambda left, right: pathlib.Path(right).name == "source-candidate")
        self.recover()
        self.assertEqual(reference(self.component / "runtime/source-candidate/dist/worker.js"), old)
        self.runtime.doctor(self.component, self.state)

    def test_crashed_rollback_can_recover_before_publication(self):
        self.apply()
        self.apply()
        current = reference(self.component / "runtime/source-candidate/dist/worker.js")
        receipt_sha = reference(self.state / "source-runtime/receipt.json")["sha256"]
        self.crash(lambda: self.runtime.rollback(self.component, self.state, receipt_sha), "exchange",
                   lambda left, right: pathlib.Path(right).name == "source-candidate")
        self.recover()
        self.assertEqual(reference(self.component / "runtime/source-candidate/dist/worker.js"), current)
        self.runtime.doctor(self.component, self.state)

    def test_receipt_replacement_at_cutover_is_preserved(self):
        self.apply()
        old = reference(self.component / "runtime/source-candidate/dist/worker.js")
        receipt = self.state / "source-runtime/receipt.json"
        exchange = self.runtime.exchange
        raced = False
        replacement = None
        def concurrent_replacement(left, right):
            nonlocal raced, replacement
            if pathlib.Path(right).name == "source-candidate" and not raced:
                raced = True
                contents = receipt.read_bytes()
                receipt.unlink()
                receipt.write_bytes(contents)
                receipt.chmod(0o600)
                replacement = reference(receipt)
            return exchange(left, right)
        with mock.patch.object(self.runtime, "exchange", side_effect=concurrent_replacement):
            with self.assertRaises(self.runtime.SourceRuntimeError):
                self.apply()
        self.assertEqual(reference(receipt), replacement)
        self.assertEqual(reference(self.component / "runtime/source-candidate/dist/worker.js"), old)

    def test_receipt_replacement_at_publication_is_preserved(self):
        self.apply()
        old = reference(self.component / "runtime/source-candidate/dist/worker.js")
        receipt = self.state / "source-runtime/receipt.json"
        publish = self.runtime.publish_receipt
        replacement = None
        def concurrent_replacement(*args):
            nonlocal replacement
            contents = receipt.read_bytes()
            receipt.unlink()
            receipt.write_bytes(contents)
            receipt.chmod(0o600)
            replacement = reference(receipt)
            return publish(*args)
        with mock.patch.object(self.runtime, "publish_receipt", side_effect=concurrent_replacement):
            with self.assertRaises(self.runtime.SourceRuntimeError):
                self.apply()
        self.assertEqual(reference(receipt), replacement)
        self.assertEqual(reference(self.component / "runtime/source-candidate/dist/worker.js"), old)

    def test_crashed_publication_recovers_committed_install(self):
        self.apply()
        self.crash(self.apply, "publish_receipt", lambda *args: True)
        self.recover()
        self.runtime.doctor(self.component, self.state)

    def test_crashed_first_rollback_restores_exact_current_state(self):
        self.apply()
        current = reference(self.component / "runtime/source-candidate/dist/worker.js")
        receipt_sha = reference(self.state / "source-runtime/receipt.json")["sha256"]
        self.crash(lambda: self.runtime.rollback(self.component, self.state, receipt_sha), "move_new",
                   lambda left, right: pathlib.Path(right).name == "rolled-back-candidate")
        self.recover()
        self.assertEqual(reference(self.component / "runtime/source-candidate/dist/worker.js"), current)
        self.runtime.doctor(self.component, self.state)

    def test_explicit_retirement_preserves_candidate_and_clears_installer_guard(self):
        self.apply()
        self.apply()
        current = reference(self.component / "runtime/source-candidate/dist/worker.js")
        receipt_sha = reference(self.state / "source-runtime/receipt.json")["sha256"]
        result = self.runtime.rollback(self.component, self.state, receipt_sha, retire=True)
        self.assertEqual(result, dict(status="retired", installed=False))
        self.assertFalse((self.component / "runtime/source-candidate").exists())
        self.assertFalse((self.state / "source-runtime/receipt.json").exists())
        retained = list((self.state / "source-runtime/backups").glob("*/rolled-back-candidate/dist/worker.js"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(reference(retained[0]) | {"path": current["path"]}, current)

    def test_journal_is_fsynced_before_tree_cutover(self):
        self.apply()
        fsync = self.runtime.os.fsync
        exchange = self.runtime.exchange
        observed = set()
        def sync(descriptor):
            metadata = os.fstat(descriptor)
            result = fsync(descriptor)
            observed.add((metadata.st_dev, metadata.st_ino))
            return result
        def checked_exchange(left, right):
            if pathlib.Path(right).name == "source-candidate":
                journal = self.state / "source-runtime/transaction.json"
                for path in (journal, journal.parent):
                    metadata = path.stat()
                    self.assertIn((metadata.st_dev, metadata.st_ino), observed)
                document = json.loads(journal.read_bytes())
                self.assertEqual(document["before"]["receipt"], reference(self.state / "source-runtime/receipt.json"))
            return exchange(left, right)
        with mock.patch.object(self.runtime.os, "fsync", side_effect=sync):
            with mock.patch.object(self.runtime, "exchange", side_effect=checked_exchange):
                self.apply()

    def test_receipt_replacement_inside_atomic_publication_is_restored(self):
        self.apply()
        old = reference(self.component / "runtime/source-candidate/dist/worker.js")
        receipt = self.state / "source-runtime/receipt.json"
        exchange = self.runtime.exchange
        replacement = None
        raced = False
        def concurrent_replacement(left, right):
            nonlocal replacement, raced
            if pathlib.Path(right) == receipt and not raced:
                raced = True
                contents = receipt.read_bytes()
                receipt.unlink()
                receipt.write_bytes(contents)
                receipt.chmod(0o600)
                replacement = reference(receipt)
            return exchange(left, right)
        with mock.patch.object(self.runtime, "exchange", side_effect=concurrent_replacement):
            with self.assertRaises(self.runtime.SourceRuntimeError):
                self.apply()
        self.assertEqual(reference(receipt), replacement)
        self.assertEqual(reference(self.component / "runtime/source-candidate/dist/worker.js"), old)

    def test_copy_corruption_is_rejected_before_cutover(self):
        copyfile = self.runtime.shutil.copyfile
        def corrupt_copy(source, destination, **kwargs):
            result = copyfile(source, destination, **kwargs)
            if pathlib.Path(source) == self.payload:
                pathlib.Path(destination).write_bytes(b"broken")
            return result
        with mock.patch.object(self.runtime.shutil, "copyfile", side_effect=corrupt_copy):
            with self.assertRaises(self.runtime.SourceRuntimeError):
                self.apply()
        self.assertFalse((self.component / "runtime/source-candidate").exists())

    def test_rollback_rejects_extra_backup_payload_before_cutover(self):
        self.apply()
        receipt = self.apply()
        backup = self.state / "source-runtime/backups" / receipt["backup_id"] / "candidate"
        (backup / "extra").write_bytes(b"injection")
        installed_inode = (self.component / "runtime/source-candidate/dist/worker.js").stat().st_ino
        with mock.patch.object(self.runtime, "exchange", side_effect=AssertionError("drift reached cutover")):
            with self.assertRaises(self.runtime.SourceRuntimeError):
                self.runtime.rollback(self.component, self.state, reference(self.state / "source-runtime/receipt.json")["sha256"])
        self.assertEqual((self.component / "runtime/source-candidate/dist/worker.js").stat().st_ino, installed_inode)

    def test_first_install_rollback_retains_recoverable_candidate(self):
        receipt = self.apply()
        result = self.runtime.rollback(self.component, self.state, reference(self.state / "source-runtime/receipt.json")["sha256"])
        self.assertEqual(result, dict(status="rolled_back", installed=False))
        self.assertFalse((self.component / "runtime/source-candidate").exists())
        self.assertFalse((self.state / "source-runtime/receipt.json").exists())
        self.assertTrue((self.state / "source-runtime/backups" / receipt["backup_id"] / "rolled-back-candidate/dist/worker.js").exists())

    def test_rollback_rejects_backup_receipt_mode_drift(self):
        self.apply()
        receipt = self.apply()
        backup_receipt = self.state / "source-runtime/backups" / receipt["backup_id"] / "receipt.json"
        backup_receipt.chmod(0o644)
        with self.assertRaises(self.runtime.SourceRuntimeError):
            self.runtime.rollback(self.component, self.state, reference(self.state / "source-runtime/receipt.json")["sha256"])

    def test_unsafe_ancestor_is_rejected(self):
        self.source.chmod(0o777)
        with self.assertRaises(self.runtime.SourceRuntimeError):
            self.plan()

    def test_source_cannot_overlap_destination_or_receipt_state(self):
        with self.assertRaises(self.runtime.SourceRuntimeError):
            self.runtime.make_plan(self.ref, self.component, self.source / "state")

    def test_nested_manifest_references_must_match_payload_identity(self):
        document = json.loads(self.manifest.read_text())
        document["worker"]["inode"] += 1
        self.manifest.write_text(json.dumps(document))
        self.ref = reference(self.manifest)
        with self.assertRaises(self.runtime.SourceRuntimeError):
            self.plan()

    def test_traversal_manifest_path_is_rejected(self):
        document = json.loads(self.manifest.read_text())
        document["files"]["../escape"] = document["files"].pop("dist/worker.js")
        self.manifest.write_text(json.dumps(document))
        self.ref = reference(self.manifest)
        with self.assertRaises(self.runtime.SourceRuntimeError):
            self.plan()


if __name__ == "__main__":
    unittest.main()
