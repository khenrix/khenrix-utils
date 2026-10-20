"""Source companion installs must reject drift and preserve rollback state."""

import hashlib
import importlib.util
import json
import os
import pathlib
import stat
import tempfile
import types
import unittest
from contextlib import contextmanager
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

    @contextmanager
    def remounted(self):
        original = os.fstat
        def new_device(descriptor):
            value = original(descriptor)
            fields = {key: getattr(value, key) for key in dir(value) if key.startswith("st_")}
            return types.SimpleNamespace(**(fields | {"st_dev": value.st_dev + 1}))
        with mock.patch.object(self.runtime.os, "fstat", side_effect=new_device):
            yield

    def remount_plan(self):
        self.assertTrue(hasattr(self.runtime, "make_remount_retire_plan"), "explicit remount retirement is missing")
        return self.runtime.make_remount_retire_plan(self.component, self.state, self.root / "reconciled-source")

    def test_remount_retirement_preserves_originals_and_makes_strict_source_snapshot(self):
        self.apply()
        original_source = self.manifest.read_bytes()
        receipt = self.state / "source-runtime/receipt.json"
        original_receipt = receipt.read_bytes()
        installed = self.component / "runtime/source-candidate"
        original_installed = (installed / "runtime-manifest.json").read_bytes()
        payload_inode = (installed / "dist/worker.js").stat().st_ino
        with self.remounted():
            with self.assertRaises(self.runtime.SourceRuntimeError):
                self.runtime.doctor(self.component, self.state)
            plan = self.remount_plan()
            result = self.runtime.apply_remount_retire_plan(plan, self.runtime.digest_json(plan))
            self.assertEqual(result["status"], "retired")
            self.assertFalse(installed.exists())
            self.assertFalse(receipt.exists())
            backup = pathlib.Path(result["backup"])
            self.assertEqual((backup / "rolled-back-receipt.json").read_bytes(), original_receipt)
            self.assertEqual((backup / "rolled-back-candidate/runtime-manifest.json").read_bytes(), original_installed)
            self.assertEqual((backup / "rolled-back-candidate/dist/worker.js").stat().st_ino, payload_inode)
            self.assertEqual(self.manifest.read_bytes(), original_source)
            fresh = self.runtime.verify_manifest(result["source_manifest"])
            self.assertEqual(fresh["commit"], "a" * 40)
            self.assertEqual(fresh["packaging_repairs"][0]["original"]["path"], "/original/build/package.json")
            self.assertEqual(fresh["source_manifest"], self.ref)
            normal = self.runtime.make_plan(result["source_manifest"], self.component, self.state)
            self.apply(normal)
            self.runtime.doctor(self.component, self.state)

    def test_remount_plan_rejects_nondevice_drift_and_pending_transaction(self):
        for mutation in ("content", "mode", "inode", "extra", "symlink", "hardlink", "worker", "mixed-device", "transaction"):
            with self.subTest(mutation=mutation):
                self.setUp()
                self.apply()
                installed = self.component / "runtime/source-candidate"
                payload = installed / "dist/worker.js"
                with self.remounted():
                    if mutation == "content":
                        payload.write_bytes(b"tamper")
                    elif mutation == "mode":
                        payload.chmod(0o644)
                    elif mutation == "inode":
                        moved = payload.with_suffix(".old")
                        payload.rename(moved)
                        payload.write_bytes(moved.read_bytes())
                        payload.chmod(0o600)
                        moved.unlink()
                    elif mutation == "extra":
                        (installed / "extra").write_bytes(b"extra")
                    elif mutation == "symlink":
                        (installed / "extra").symlink_to(payload)
                    elif mutation == "hardlink":
                        os.link(payload, self.root / "linked")
                    elif mutation == "worker":
                        self.payload.write_bytes(b"tamper")
                    elif mutation == "transaction":
                        (self.state / "source-runtime/transaction.json").write_text("{}")
                    if mutation == "mixed-device":
                        current = self.runtime.file_ref
                        def mixed(path):
                            value = current(path)
                            return value | {"device": value["device"] + (pathlib.Path(path) == payload)}
                        with mock.patch.object(self.runtime, "file_ref", side_effect=mixed):
                            with self.assertRaises(self.runtime.SourceRuntimeError):
                                self.remount_plan()
                    else:
                        with self.assertRaises(self.runtime.SourceRuntimeError):
                            self.remount_plan()

    def test_remount_apply_rejects_wrong_digest_and_receipt_replacement(self):
        self.apply()
        with self.remounted():
            plan = self.remount_plan()
            with self.assertRaises(self.runtime.SourceRuntimeError):
                self.runtime.apply_remount_retire_plan(plan, "0" * 64)
            receipt = self.state / "source-runtime/receipt.json"
            old = receipt.with_suffix(".old")
            receipt.rename(old)
            receipt.write_bytes(old.read_bytes())
            receipt.chmod(0o600)
            replacement = self.runtime.file_ref(receipt)
            with self.assertRaises(self.runtime.SourceRuntimeError):
                self.runtime.apply_remount_retire_plan(plan, self.runtime.digest_json(plan))
            self.assertEqual(self.runtime.file_ref(receipt), replacement)
            self.assertTrue((self.component / "runtime/source-candidate").exists())

    def test_remount_retirement_recovers_crashes_before_and_after_publication(self):
        for publication in (False, True):
            with self.subTest(publication=publication):
                self.setUp()
                self.apply()
                receipt = self.state / "source-runtime/receipt.json"
                original = receipt.read_bytes()
                with self.remounted():
                    plan = self.remount_plan()
                    self.crash(lambda: self.runtime.apply_remount_retire_plan(plan, self.runtime.digest_json(plan)),
                               "move_new", lambda left, right: pathlib.Path(right).name ==
                               ("rolled-back-receipt.json" if publication else "rolled-back-candidate"))
                    self.recover()
                    if publication:
                        self.assertFalse(receipt.exists())
                        self.assertFalse((self.component / "runtime/source-candidate").exists())
                    else:
                        self.assertEqual(receipt.read_bytes(), original)
                        self.assertTrue((self.component / "runtime/source-candidate").exists())
                        with self.assertRaises(self.runtime.SourceRuntimeError):
                            self.runtime.doctor(self.component, self.state)

    def test_remount_apply_revalidates_closures_and_rejects_corrupt_snapshot(self):
        for mutation in ("source", "installed", "copy"):
            with self.subTest(mutation=mutation):
                self.setUp()
                self.apply()
                with self.remounted():
                    plan = self.remount_plan()
                    if mutation == "source":
                        self.payload.write_bytes(b"tamper")
                    elif mutation == "installed":
                        (self.component / "runtime/source-candidate/dist/worker.js").write_bytes(b"tamper")
                    original = self.runtime.shutil.copyfileobj
                    def copy(source, destination, **kwargs):
                        value = original(source, destination, **kwargs)
                        if mutation == "copy":
                            destination.seek(0)
                            destination.write(b"broken")
                        return value
                    with mock.patch.object(self.runtime.shutil, "copyfileobj", side_effect=copy):
                        with self.assertRaises(self.runtime.SourceRuntimeError):
                            self.runtime.apply_remount_retire_plan(plan, self.runtime.digest_json(plan))
                    self.assertTrue((self.state / "source-runtime/receipt.json").exists())
                    self.assertTrue((self.component / "runtime/source-candidate").exists())
                    self.assertFalse((self.root / "reconciled-source").exists())

    def test_remount_retirement_retains_concurrent_receipt_and_restores_tree(self):
        self.apply()
        receipt = self.state / "source-runtime/receipt.json"
        installed = self.component / "runtime/source-candidate"
        inode = (installed / "dist/worker.js").stat().st_ino
        move = self.runtime.move_new
        with self.remounted():
            plan = self.remount_plan()
            replacement = None
            def replace(left, right):
                nonlocal replacement
                value = move(left, right)
                if pathlib.Path(right).name == "rolled-back-candidate":
                    old = receipt.with_suffix(".raced")
                    receipt.rename(old)
                    receipt.write_bytes(old.read_bytes())
                    receipt.chmod(0o600)
                    replacement = self.runtime.file_ref(receipt)
                return value
            with mock.patch.object(self.runtime, "move_new", side_effect=replace):
                with self.assertRaises(self.runtime.SourceRuntimeError):
                    self.runtime.apply_remount_retire_plan(plan, self.runtime.digest_json(plan))
            self.assertEqual(self.runtime.file_ref(receipt), replacement)
            self.assertEqual((installed / "dist/worker.js").stat().st_ino, inode)

    def test_source_symlink_race_never_chmods_unrelated_private_file(self):
        self.payload.chmod(0o644)
        document = json.loads(self.manifest.read_text())
        payload = reference(self.payload)
        document["files"]["dist/worker.js"] = payload
        document["worker"] = payload
        document["packaging_repairs"][0]["staged"] = payload
        self.manifest.write_text(json.dumps(document))
        self.ref = reference(self.manifest)
        self.apply()
        victim = self.root / "unrelated-private-file"
        victim.write_bytes(b"unrelated fixture data")
        victim.chmod(0o600)
        injected = False
        def inject():
            nonlocal injected
            if not injected:
                injected = True
                self.payload.rename(self.payload.with_suffix(".saved"))
                self.payload.symlink_to(victim)
        copyfile, copyfileobj = self.runtime.shutil.copyfile, self.runtime.shutil.copyfileobj
        def raced_copy(source, destination, **kwargs):
            inject()
            return copyfile(source, destination, **kwargs)
        def raced_descriptors(source, destination, **kwargs):
            inject()
            return copyfileobj(source, destination, **kwargs)
        with self.remounted():
            plan = self.remount_plan()
            with mock.patch.object(self.runtime.shutil, "copyfile", side_effect=raced_copy):
                with mock.patch.object(self.runtime.shutil, "copyfileobj", side_effect=raced_descriptors):
                    with self.assertRaises(self.runtime.SourceRuntimeError):
                        self.runtime.apply_remount_retire_plan(plan, self.runtime.digest_json(plan))
        self.assertTrue(injected)
        self.assertEqual(victim.read_bytes(), b"unrelated fixture data")
        self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o600)
        self.assertTrue((self.state / "source-runtime/receipt.json").exists())

    def test_remount_retirement_rejects_snapshot_drift_after_reference_publication(self):
        self.apply()
        with self.remounted():
            plan = self.remount_plan()
            write = self.runtime.write_private
            def corrupt(path, value):
                result = write(path, value)
                if pathlib.Path(path).name == "source-reference.json":
                    (pathlib.Path(plan["source_snapshot"]) / "dist/worker.js").write_bytes(b"broken")
                return result
            with mock.patch.object(self.runtime, "write_private", side_effect=corrupt):
                with self.assertRaises(self.runtime.SourceRuntimeError):
                    self.runtime.apply_remount_retire_plan(plan, self.runtime.digest_json(plan))
            self.assertTrue((self.state / "source-runtime/receipt.json").exists())
            self.assertTrue((self.component / "runtime/source-candidate").exists())

    def test_remount_cutover_and_recovery_require_unchanged_published_snapshot(self):
        for point in ("tree", "receipt", "recover"):
            with self.subTest(point=point):
                self.setUp()
                self.apply()
                with self.remounted():
                    plan = self.remount_plan()
                    action = lambda: self.runtime.apply_remount_retire_plan(plan, self.runtime.digest_json(plan))
                    snapshot_payload = pathlib.Path(plan["source_snapshot"]) / "dist/worker.js"
                    if point == "recover":
                        self.crash(action, "move_new", lambda left, right: pathlib.Path(right).name == "rolled-back-receipt.json")
                        snapshot_payload.write_bytes(b"broken")
                    else:
                        move = self.runtime.move_new
                        def corrupt(left, right):
                            result = move(left, right)
                            if pathlib.Path(right).name == ("rolled-back-candidate" if point == "tree" else "rolled-back-receipt.json"):
                                snapshot_payload.write_bytes(b"broken")
                            return result
                        with mock.patch.object(self.runtime, "move_new", side_effect=corrupt):
                            with self.assertRaises(self.runtime.SourceRuntimeError):
                                action()
                    journal = self.state / "source-runtime/transaction.json"
                    self.assertTrue(journal.exists())
                    with self.assertRaises(self.runtime.SourceRuntimeError):
                        self.runtime.recover(self.component, self.state, self.runtime.file_ref(journal)["sha256"])
                    self.assertTrue(journal.exists())

    def test_remount_finish_rechecks_snapshot_before_removing_journal(self):
        self.apply()
        with self.remounted():
            plan = self.remount_plan()
            matches = self.runtime.state_matches
            def corrupt_after_committed_state(bound, slot, receipt):
                result = matches(bound, slot, receipt)
                if bound is None and result:
                    (pathlib.Path(plan["source_snapshot"]) / "dist/worker.js").write_bytes(b"broken")
                return result
            with mock.patch.object(self.runtime, "state_matches", side_effect=corrupt_after_committed_state):
                with self.assertRaises(self.runtime.SourceRuntimeError):
                    self.runtime.apply_remount_retire_plan(plan, self.runtime.digest_json(plan))
            self.assertTrue((self.state / "source-runtime/transaction.json").exists())

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

    def test_source_repair_is_rebound_and_provenance_is_preserved(self):
        document = json.loads(self.manifest.read_text())
        repair = dict(kind="reviewed-source-repair", original=dict(path="/original/source.js"),
                      source=dict(path="/reviewed/source.ts"), runtime=dict(path="/reviewed/source.js"),
                      staged=reference(self.payload))
        document["source_repairs"] = [repair]
        self.manifest.write_text(json.dumps(document))
        self.ref = reference(self.manifest)
        self.apply()
        installed = self.component / "runtime/source-candidate"
        document = json.loads((installed / "runtime-manifest.json").read_text())
        self.assertEqual(document["source_repairs"][0], repair | {"staged": reference(installed / "dist/worker.js")})
        self.assertEqual(document["source_schema"], "validated-runtime-v1")
        self.assertEqual(self.runtime.doctor(self.component, self.state)["status"], "verified")

    def test_source_repair_outside_closure_refuses_before_delivery(self):
        for defect in ("outside", "identity", "not-list", "not-record"):
            with self.subTest(defect=defect):
                document = json.loads(self.manifest.read_text())
                repaired = reference(self.payload)
                if defect == "outside":
                    repaired["path"] = str(self.root / "outside.js")
                elif defect == "identity":
                    repaired["inode"] += 1
                document["source_repairs"] = [dict(kind="reviewed-source-repair", staged=repaired)]
                if defect == "not-list":
                    document["source_repairs"] = {}
                elif defect == "not-record":
                    document["source_repairs"] = ["invalid"]
                self.manifest.write_text(json.dumps(document))
                self.ref = reference(self.manifest)
                with self.assertRaises(self.runtime.SourceRuntimeError):
                    self.plan()
                self.assertFalse(self.state.exists())
                self.assertFalse((self.component / "runtime/source-candidate").exists())

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
        copyfile = self.runtime.shutil.copyfileobj
        def corrupt_copy(source, destination, **kwargs):
            result = copyfile(source, destination, **kwargs)
            destination.seek(0)
            destination.write(b"broken")
            return result
        with mock.patch.object(self.runtime.shutil, "copyfileobj", side_effect=corrupt_copy):
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
