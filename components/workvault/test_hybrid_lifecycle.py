"""Whole-index ownership and legacy fencing with disposable synthetic bytes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import batch
import curation
import lifecycle
import sync
import workvault
import test_lifecycle
import test_consumption
from test_curated_read import publish
from test_source_sync import Adapter, Curator, CUTOFF, selection
from test_sync import Source, Curator as LegacyCurator


DB = '.obsidian-hybrid-search.db'
BUILD = '.workvault-hybrid-build'
GENERATION = 'a' * 64


def interrupt_inflight(root, save_number):
    from test_source_sync import config
    save = lifecycle._save
    calls = 0
    def interrupted(directory, value, name):
        nonlocal calls
        if name == lifecycle.ARTIFACTS:
            calls += 1
            if calls == save_number:
                os._exit(77)
        save(directory, value, name)
    lifecycle._save = interrupted
    with workvault._publication_lock(config(root).vault, expose_lock=True) as lease:
        lifecycle.register_owned_index(lease[0], GENERATION, 'inflight', [{'scope': 'vault', 'name': DB}])


def interrupt_inventory(root):
    config = sync.SyncConfig('example/wiki', root / 'vault', root / 'history',
                            root / 'state', 'example-project', 'eu', 'example-model')
    lifecycle.purge_owned_index = lambda directory: os._exit(77)
    sync.sync_all(config, source=Source(['knowledge/item-1.md'], '3' * 40), curator=LegacyCurator())


def interrupt_initial(root):
    from test_source_sync import config
    save = lifecycle._save
    def interrupted(directory, value, name):
        if name == lifecycle.ARTIFACTS:
            os._exit(77)
        save(directory, value, name)
    lifecycle._save = interrupted
    with workvault._publication_lock(config(root).vault, expose_lock=True) as lease:
        lifecycle.register_owned_index(lease[0], GENERATION, 'building', [])


def interrupt_allocation(root, boundary):
    from test_source_sync import config
    rename = os.rename
    def interrupted(source, destination, *args, **kwargs):
        if destination == BUILD and boundary % 2 == 0:
            os._exit(77)
        result = rename(source, destination, *args, **kwargs)
        if destination == BUILD:
            os._exit(77)
        return result
    os.rename = interrupted
    initial = boundary >= 2
    with workvault._publication_lock(config(root).vault, expose_lock=True) as lease:
        lifecycle.register_owned_index(lease[0], GENERATION, 'building' if initial else 'inflight',
                                       [] if initial else [{'scope': 'vault', 'name': DB}])


def interrupted_child(test, action, *arguments):
    script = ('import sys; from pathlib import Path; import test_hybrid_lifecycle as tests; '
              f'tests.{action}(Path(sys.argv[1])' + (', int(sys.argv[2]))' if arguments else ')'))
    result = subprocess.run([sys.executable, '-B', '-c', script, str(test.root), *map(str, arguments)],
                            cwd=Path(__file__).parent, capture_output=True, timeout=10)
    test.assertEqual(result.returncode, 77, result.stderr.decode())


class HybridLifecycleTests(unittest.TestCase):
    setUp = test_lifecycle.LifecycleTests.setUp
    publish = test_lifecycle.LifecycleTests.publish
    apply = test_lifecycle.LifecycleTests.apply

    def reserve(self):
        with workvault._publication_lock(self.config.vault, expose_lock=True) as lease:
            lifecycle.register_owned_index(lease[0], GENERATION, 'building', [])

    def build(self, size=32, ready=False):
        self.reserve()
        target = self.config.vault / BUILD / DB
        target.write_bytes(b'SYNTHETIC_INDEX_CANARY' + b'x' * size)
        target.chmod(0o600)
        with workvault._publication_lock(self.config.vault, lifecycle=True, expose_lock=True) as lease:
            lifecycle.register_owned_index(lease[0], GENERATION, 'building', [{'scope': 'build', 'name': DB}])
            if ready:
                target.rename(self.config.vault / DB)
                (self.config.vault / BUILD).rmdir()
                lifecycle.register_owned_index(lease[0], GENERATION, 'ready', [{'scope': 'vault', 'name': DB}])

    def test_owned_index_family_over_publication_limit(self):
        self.build(curation.MAX_PUBLICATION_BYTES + 1, ready=True)
        with workvault._publication_lock(self.config.vault) as directory:
            row = lifecycle._artifacts(directory)[BUILD]
        self.assertGreater(row['members'][0]['info']['bytes'], curation.MAX_PUBLICATION_BYTES)
        self.assertEqual(curation.MAX_METADATA_ITEMS, 32)

    def test_withdrawal_unlinks_db_wal_shm_and_build_copies(self):
        self.build(ready=True)
        with workvault._publication_lock(self.config.vault, expose_lock=True) as lease:
            lifecycle.register_owned_index(lease[0], GENERATION, 'inflight', [{'scope': 'vault', 'name': DB}])
        for suffix in ('-wal', '-shm'):
            self.assertTrue((self.config.vault / (DB + suffix)).exists())
        self.apply()
        self.apply()
        for name in (DB, DB + '-wal', DB + '-shm', BUILD):
            self.assertFalse((self.config.vault / name).exists())
        self.assertEqual(self.note.read_bytes(), b'Human authored note: preserve exact bytes.\n')

    def test_pending_family_recovery_before_read(self):
        self.build()
        copy_path = self.config.vault / BUILD / batch.source_publication_name(self.target['source'])
        copy_path.write_bytes(b'SYNTHETIC_BUILD_CANARY')
        copy_path.chmod(0o600)
        # Unenumerated projections are never adopted, even inside the owned build.
        with self.assertRaises(workvault.WorkVaultError):
            lifecycle.recover_source_lifecycle(self.config)
        copy_path.unlink()
        with self.assertRaises(workvault.WorkVaultError):
            batch.read_source_cards(self.config.vault, [self.other['source']])
        self.assertTrue(lifecycle.recover_source_lifecycle(self.config))
        self.assertFalse((self.config.vault / BUILD).exists())
        self.assertEqual(len(batch.read_source_cards(self.config.vault, [self.other['source']])), 1)

    def test_family_symlink_hardlink_inode_swap_refuses_without_deleting_human_files(self):
        for kind in ('symlink', 'hardlink', 'swap'):
            with self.subTest(kind=kind):
                self.build(ready=True)
                target = self.config.vault / DB
                saved = self.root / ('saved-' + kind)
                target.rename(saved)
                if kind == 'symlink':
                    target.symlink_to(self.note)
                elif kind == 'hardlink':
                    os.link(self.note, target)
                else:
                    target.write_bytes(b'UNOWNED_INDEX')
                    target.chmod(0o600)
                with self.assertRaises(workvault.WorkVaultError):
                    self.apply()
                self.assertTrue(target.exists())
                self.assertIn(b'Human authored', self.note.read_bytes())
                target.unlink()
                saved.rename(target)
                lifecycle.recover_source_lifecycle(self.config)
                with workvault._publication_lock(self.config.vault, lifecycle=True) as directory:
                    lifecycle.purge_owned_index(directory)

    def test_unknown_preexisting_build_and_root_refuse(self):
        unknown = self.config.vault / BUILD
        unknown.mkdir(mode=0o700)
        with self.assertRaises(workvault.WorkVaultError):
            self.reserve()
        unknown.rmdir()
        target = self.config.vault / DB
        target.write_bytes(b'HUMAN_DATABASE')
        target.chmod(0o600)
        with self.assertRaises(workvault.WorkVaultError):
            self.reserve()
        self.assertEqual(target.read_bytes(), b'HUMAN_DATABASE')

    def test_interrupted_withdrawal_recovers_build_copies(self):
        self.build()
        with patch.object(lifecycle, '_erase', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(workvault.WorkVaultError):
                self.apply()
        self.assertFalse((self.config.vault / BUILD / DB).exists())
        self.assertIsNotNone(json.loads((self.config.vault / lifecycle.STATE).read_bytes())['pending'])
        lifecycle.recover_source_lifecycle(self.config)
        self.assertFalse((self.config.vault / BUILD).exists())

    def test_absent_root_sidecar_is_not_later_unrelated_ownership(self):
        self.build(ready=True)
        sidecar = self.config.vault / (DB + '-wal')
        sidecar.write_bytes(b'HUMAN_SIDECAR')
        sidecar.chmod(0o600)
        with self.assertRaises(workvault.WorkVaultError):
            self.apply()
        with self.assertRaises(workvault.WorkVaultError):
            batch.read_source_cards(self.config.vault, [self.other['source']])
        self.assertEqual(sidecar.read_bytes(), b'HUMAN_SIDECAR')

    def test_ready_sealing_cannot_adopt_unrelated_root_database(self):
        self.build()
        target = self.config.vault / DB
        target.write_bytes(b'UNOWNED_ROOT_DB')
        target.chmod(0o600)
        (self.config.vault / BUILD / DB).unlink()
        (self.config.vault / BUILD).rmdir()
        with workvault._publication_lock(self.config.vault, lifecycle=True, expose_lock=True) as lease:
            with self.assertRaises(workvault.WorkVaultError):
                lifecycle.register_owned_index(lease[0], GENERATION, 'ready', [{'scope': 'vault', 'name': DB}])
        self.assertEqual(target.read_bytes(), b'UNOWNED_ROOT_DB')

    def test_interrupted_measured_promotion_recovers_exact_owned_inode(self):
        self.build()
        (self.config.vault / BUILD / DB).rename(self.config.vault / DB)
        lifecycle.recover_source_lifecycle(self.config)
        self.assertFalse((self.config.vault / DB).exists())
        self.assertFalse((self.config.vault / BUILD).exists())

    def test_partial_index_unlink_recovers_remaining_owned_family(self):
        self.build(ready=True)
        unlink = workvault._unlink_owned
        def interrupted(directory, name, identity):
            unlink(directory, name, identity)
            if name == DB:
                raise RuntimeError('interrupted after unlink')
        with patch.object(workvault, '_unlink_owned', side_effect=interrupted):
            with self.assertRaises(workvault.WorkVaultError):
                self.apply()
        lifecycle.recover_source_lifecycle(self.config)
        self.assertFalse((self.config.vault / DB).exists())

    def test_mutable_access_keeps_owned_inodes_and_reseals_changed_bytes(self):
        self.build(ready=True)
        target = self.config.vault / DB
        before = target.stat().st_ino
        with workvault._publication_lock(self.config.vault, expose_lock=True) as lease:
            lifecycle.register_owned_index(lease[0], GENERATION, 'inflight', [{'scope': 'vault', 'name': DB}])
            target.write_bytes(b'UPDATED_OWNED_BYTES')
            for suffix in ('-wal', '-shm'):
                (self.config.vault / (DB + suffix)).unlink()
            lifecycle.register_owned_index(lease[0], GENERATION, 'ready', [{'scope': 'vault', 'name': DB}])
        self.assertEqual(target.stat().st_ino, before)
        with workvault._publication_lock(self.config.vault) as directory:
            self.assertEqual(lifecycle._artifacts(directory)[BUILD]['members'][0]['info']['bytes'], 19)

    def test_process_exit_before_sidecar_receipt_recovers_complete_family(self):
        for save_number in (2, 3, 4, 5, 6):
            with self.subTest(save_number=save_number):
                self.build(ready=True)
                interrupted_child(self, 'interrupt_inflight', save_number)
                if save_number == 2:
                    self.assertFalse((self.config.vault / (DB + '-wal')).exists())
                    self.assertTrue((self.config.vault / BUILD / (DB + '-wal')).exists())
                self.assertTrue(lifecycle.recover_source_lifecycle(self.config))
                for name in (DB, DB + '-wal', DB + '-shm', BUILD):
                    self.assertFalse((self.config.vault / name).exists())

    def test_process_exit_before_first_inflight_receipt_keeps_prior_ready_usable(self):
        self.build(ready=True)
        before = (self.config.vault / lifecycle.ARTIFACTS).read_bytes()
        interrupted_child(self, 'interrupt_inflight', 1)
        orphan, = self.config.vault.glob('.workvault-hybrid-reserve-*')
        self.assertEqual(list(orphan.iterdir()), [])
        self.assertEqual(orphan.stat().st_mode & 0o777, 0o700)
        identity = orphan.stat().st_ino
        canary = orphan / 'Human.md'
        canary.write_bytes(b'UNREGISTERED_ALLOCATION_CANARY')
        lifecycle.recover_source_lifecycle(self.config)
        self.assertEqual((self.config.vault / lifecycle.ARTIFACTS).read_bytes(), before)
        self.assertFalse((self.config.vault / BUILD).exists())
        self.assertEqual(len(batch.read_source_cards(self.config.vault, [self.other['source']])), 1)
        self.apply()
        self.assertFalse((self.config.vault / DB).exists())
        self.assertEqual(orphan.stat().st_ino, identity)
        self.assertEqual(canary.read_bytes(), b'UNREGISTERED_ALLOCATION_CANARY')

    def test_process_exit_before_initial_receipt_keeps_valid_retry_usable(self):
        interrupted_child(self, 'interrupt_initial')
        orphan, = self.config.vault.glob('.workvault-hybrid-reserve-*')
        self.assertEqual(list(orphan.iterdir()), [])
        lifecycle.recover_source_lifecycle(self.config)
        self.assertFalse((self.config.vault / BUILD).exists())
        self.reserve()
        lifecycle.recover_source_lifecycle(self.config)
        self.assertTrue(orphan.exists())

    def test_inflight_envelope_rejection_keeps_prior_ready_usable(self):
        self.build(ready=True)
        before = (self.config.vault / lifecycle.ARTIFACTS).read_bytes()
        names = set(os.listdir(self.config.vault))
        with patch.object(lifecycle, 'MAX_STATE_BYTES', len(before)):
            with workvault._publication_lock(self.config.vault, expose_lock=True) as lease:
                with self.assertRaises(workvault.WorkVaultError):
                    lifecycle.register_owned_index(lease[0], GENERATION, 'inflight', [{'scope': 'vault', 'name': DB}])
        self.assertEqual((self.config.vault / lifecycle.ARTIFACTS).read_bytes(), before)
        self.assertEqual(set(os.listdir(self.config.vault)), names)
        self.assertFalse((self.config.vault / BUILD).exists())
        self.assertEqual(len(batch.read_source_cards(self.config.vault, [self.other['source']])), 1)

    def test_recorded_allocation_and_same_inode_promotion_recover_after_process_exit(self):
        for boundary in (2, 3, 0, 1):
            with self.subTest(boundary=boundary):
                if boundary < 2:
                    self.build(ready=True)
                interrupted_child(self, 'interrupt_allocation', boundary)
                row = json.loads((self.config.vault / lifecycle.ARTIFACTS).read_bytes())[BUILD]
                path = self.config.vault / (BUILD if boundary % 2 else row['reservation'])
                self.assertEqual(path.stat().st_ino, row['build']['identity'][1])
                self.assertEqual(list(path.iterdir()), [])
                self.assertTrue(lifecycle.recover_source_lifecycle(self.config))
                self.assertFalse(path.exists())
                self.assertFalse((self.config.vault / BUILD).exists())
                self.assertFalse((self.config.vault / DB).exists())

    def replaced_allocation_refuses(self, boundary):
        self.build(ready=True)
        interrupted_child(self, 'interrupt_allocation', boundary)
        row = json.loads((self.config.vault / lifecycle.ARTIFACTS).read_bytes())[BUILD]
        target = self.config.vault / (BUILD if boundary else row['reservation'])
        target.rename(self.root / 'saved-owned-allocation')
        target.mkdir(mode=0o700)
        canary = target / 'Human.md'
        canary.write_bytes(b'UNRELATED_DIRECTORY_CANARY')
        before = (self.config.vault / DB).read_bytes()
        with self.assertRaises(workvault.WorkVaultError):
            lifecycle.recover_source_lifecycle(self.config)
        self.assertEqual((self.config.vault / DB).read_bytes(), before)
        self.assertEqual(canary.read_bytes(), b'UNRELATED_DIRECTORY_CANARY')

    def test_recorded_reservation_replacement_refuses_without_erasure(self):
        self.replaced_allocation_refuses(0)

    def test_recorded_fixed_promotion_replacement_refuses_without_erasure(self):
        self.replaced_allocation_refuses(1)

    def test_simultaneous_reservation_and_fixed_build_refuse_without_erasure(self):
        self.build(ready=True)
        interrupted_child(self, 'interrupt_allocation', 0)
        unknown = self.config.vault / BUILD
        unknown.mkdir(mode=0o700)
        canary = unknown / 'Human.md'
        canary.write_bytes(b'UNKNOWN_SIMULTANEOUS_BUILD')
        before = (self.config.vault / DB).read_bytes()
        with self.assertRaises(workvault.WorkVaultError):
            lifecycle.recover_source_lifecycle(self.config)
        self.assertEqual((self.config.vault / DB).read_bytes(), before)
        self.assertEqual(canary.read_bytes(), b'UNKNOWN_SIMULTANEOUS_BUILD')

    def test_process_exit_never_adopts_unrelated_root_sidecar_replacement(self):
        self.build(ready=True)
        interrupted_child(self, 'interrupt_inflight', 3)
        target = self.config.vault / (DB + '-wal')
        owned = self.root / 'owned-wal'
        target.rename(owned)
        target.write_bytes(b'UNRELATED_SIDECAR_CANARY')
        target.chmod(0o600)
        before = (self.config.vault / DB).read_bytes()
        with self.assertRaises(workvault.WorkVaultError):
            lifecycle.recover_source_lifecycle(self.config)
        self.assertEqual(target.read_bytes(), b'UNRELATED_SIDECAR_CANARY')
        self.assertEqual((self.config.vault / DB).read_bytes(), before)
        with self.assertRaises(workvault.WorkVaultError):
            batch.read_source_cards(self.config.vault, [self.other['source']])

    def test_process_exit_does_not_adopt_later_unrelated_root_sidecar(self):
        self.build(ready=True)
        interrupted_child(self, 'interrupt_inflight', 2)
        target = self.config.vault / (DB + '-wal')
        self.assertFalse(target.exists())
        target.write_bytes(b'UNRELATED_LATER_SIDECAR')
        target.chmod(0o600)
        with self.assertRaises(workvault.WorkVaultError):
            lifecycle.recover_source_lifecycle(self.config)
        self.assertEqual(target.read_bytes(), b'UNRELATED_LATER_SIDECAR')
        self.assertTrue((self.config.vault / DB).exists())

    def invalid_reservation(self, members):
        before = {p.name: p.read_bytes() for p in self.config.vault.iterdir() if p.is_file()}
        names = set(os.listdir(self.config.vault))
        with workvault._publication_lock(self.config.vault, expose_lock=True) as lease:
            with self.assertRaises(workvault.WorkVaultError):
                lifecycle.register_owned_index(lease[0], GENERATION, 'building', members)
        self.assertFalse((self.config.vault / BUILD).exists())
        self.assertEqual(set(os.listdir(self.config.vault)), names)
        self.assertEqual({p.name: p.read_bytes() for p in self.config.vault.iterdir() if p.is_file()}, before)

    def test_invalid_initial_projection_count_has_no_storage_effects(self):
        self.invalid_reservation([{'scope': 'build', 'name': f'Knowledge-{number:024x}.md'} for number in range(8193)])
        self.reserve()
        lifecycle.recover_source_lifecycle(self.config)

    def test_invalid_initial_scope_has_no_storage_effects(self):
        self.invalid_reservation([{'scope': 'vault', 'name': DB}])
        self.reserve()
        lifecycle.recover_source_lifecycle(self.config)

    def test_initial_registry_envelope_refusal_has_no_storage_effects(self):
        with patch.object(lifecycle, 'MAX_STATE_BYTES', 4):
            self.invalid_reservation([])
        self.reserve()
        lifecycle.recover_source_lifecycle(self.config)

    def test_initial_registry_count_refusal_has_no_storage_effects(self):
        with patch.object(lifecycle, 'MAX_ARTIFACTS', 0):
            self.invalid_reservation([])
        self.reserve()
        lifecycle.recover_source_lifecycle(self.config)


class LegacyHybridTests(unittest.TestCase):
    setUp = test_consumption.ConsumptionTests.setUp
    state = test_consumption.ConsumptionTests.state
    put = test_consumption.ConsumptionTests.put

    def ready_index(self):
        with workvault._publication_lock(self.config.vault, expose_lock=True) as lease:
            lifecycle.register_owned_index(lease[0], GENERATION, 'building', [])
            build = self.config.vault / BUILD / DB
            build.write_bytes(b'SYNTHETIC_DB')
            build.chmod(0o600)
            lifecycle.register_owned_index(lease[0], GENERATION, 'building', [{'scope': 'build', 'name': DB}])
            build.rename(self.config.vault / DB)
            (self.config.vault / BUILD).rmdir()
            lifecycle.register_owned_index(lease[0], GENERATION, 'ready', [{'scope': 'vault', 'name': DB}])

    def test_inventory_process_exit_before_purge_retries_invalidation(self):
        self.ready_index()
        interrupted_child(self, 'interrupt_inventory')
        result = sync.sync_all(self.config, source=Source(self.paths[:1], '3' * 40), curator=LegacyCurator())
        self.assertEqual(result['state'], 'complete')
        self.assertFalse((self.config.vault / DB).exists())
    def legacy_event(self):
        return {'schema_version': 'workvault-legacy-withdrawal-v1', 'repository': self.config.repository,
                'action': 'withdrawn', 'confirmed': True, 'evidence': 'access_withdrawn'}

    def test_v1_repository_withdrawal_blocks_republish_and_restore(self):
        original = curation.parse_publication(next(self.config.vault.glob('Knowledge-*.md')).read_bytes())['batch']
        publish(self.config.vault, self.config.snapshots, original)
        lifecycle.apply_legacy_repository_withdrawal(self.config, self.legacy_event())
        lifecycle.apply_legacy_repository_withdrawal(self.config, self.legacy_event())
        self.assertEqual(list(self.config.vault.glob('Knowledge-*.md')), [])
        self.assertEqual(list(self.config.snapshots.rglob('snapshot-*.md')), [])
        with self.assertRaises(workvault.WorkVaultError):
            publish(self.config.vault, self.config.snapshots, original)
        with self.assertRaises(workvault.WorkVaultError):
            batch.read_curated_cards(self.config.vault, self.config.repository, self.paths)
        with sync._curated_reader(self.config) as (reader, _):
            self.assertEqual(reader.rows(), [])

    def test_166_v1_and_mixed_v2_collection_needs_no_dependency_cap_raise(self):
        from test_retrieval import publication
        paths = [f'knowledge/item-{number}.md' for number in range(1, 167)]
        self.paths = paths
        self.state(paths)
        for number in range(1, 167):
            self.put(publication(number, commit=('1' if number % 2 else '2') * 40))
        source_config = sync.SourceSyncConfig(self.config.vault, self.config.snapshots, self.config.state)
        adapter = Adapter(2, 2)
        adapter.rows[1]['revision'] = 'mixed-later-r2'
        sync.sync_sources(source_config, selections=(selection(adapter),), cutoff=CUTOFF, curator=Curator())
        before = {p.name: p.read_bytes() for p in self.config.vault.glob('Knowledge-*.md')}
        with workvault._publication_lock(self.config.vault, expose_lock=True) as lease:
            with sync._curated_reader(self.config, publication_lease=lease) as (reader, _):
                self.assertEqual(len(reader.rows()), 168)
                self.assertEqual({row['reference']['commit'] for row in reader.rows() if 'commit' in row['reference']},
                                 {'1' * 40, '2' * 40})
                self.assertEqual({row['reference']['revision'] for row in reader.rows() if 'revision' in row['reference']},
                                 {adapter.rows[0]['revision'], 'mixed-later-r2'})
            lifecycle.register_owned_index(lease[0], GENERATION, 'building', [])
        lifecycle.recover_source_lifecycle(self.config)
        self.assertEqual({p.name: p.read_bytes() for p in self.config.vault.glob('Knowledge-*.md')}, before)
        self.assertEqual(curation.MAX_METADATA_ITEMS, 32)

    def test_v1_inventory_retirement_invalidates_index_without_erasing_human_notes(self):
        human = self.config.vault / 'Human.md'
        human.write_bytes(b'HUMAN_KEEP')
        human.chmod(0o600)
        retired = self.config.vault / batch.publication_name(self.config.repository, self.paths[-1])
        before = retired.read_bytes()
        with workvault._publication_lock(self.config.vault, expose_lock=True) as lease:
            lifecycle.register_owned_index(lease[0], GENERATION, 'building', [])
            build = self.config.vault / BUILD / DB
            build.write_bytes(b'SYNTHETIC_DB')
            build.chmod(0o600)
            lifecycle.register_owned_index(lease[0], GENERATION, 'building', [{'scope': 'build', 'name': DB}])
            build.rename(self.config.vault / DB)
            (self.config.vault / BUILD).rmdir()
            lifecycle.register_owned_index(lease[0], GENERATION, 'ready', [{'scope': 'vault', 'name': DB}])
        sync.sync_all(self.config, source=Source(self.paths[:1], '3' * 40), curator=LegacyCurator())
        self.assertFalse((self.config.vault / DB).exists())
        self.assertEqual(retired.read_bytes(), before)
        self.assertEqual(human.read_bytes(), b'HUMAN_KEEP')
        with sync._curated_reader(self.config) as (reader, _):
            self.assertEqual(len(reader.rows()), 1)

    def test_confirmed_legacy_event_only_and_pending_recovery(self):
        before = {p.name: p.read_bytes() for p in self.config.vault.glob('Knowledge-*.md')}
        for field, value in [('confirmed', False), ('evidence', '403'), ('repository', 'example/other')]:
            bad = dict(self.legacy_event(), **{field: value})
            with self.assertRaises(workvault.WorkVaultError):
                lifecycle.apply_legacy_repository_withdrawal(self.config, bad)
            self.assertEqual({p.name: p.read_bytes() for p in self.config.vault.glob('Knowledge-*.md')}, before)
        with patch.object(lifecycle, '_erase', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(workvault.WorkVaultError):
                lifecycle.apply_legacy_repository_withdrawal(self.config, self.legacy_event())
        with self.assertRaises(workvault.WorkVaultError):
            batch.recover_publication_batch(self.config.vault, self.config.snapshots)
        lifecycle.recover_source_lifecycle(self.config)
        lifecycle.apply_legacy_repository_withdrawal(self.config, self.legacy_event())
        self.assertEqual(list(self.config.vault.glob('Knowledge-*.md')), [])

    def test_legacy_withdrawal_settles_committed_batch_before_erasure(self):
        original = curation.parse_publication(next(self.config.vault.glob('Knowledge-*.md')).read_bytes())['batch']
        with patch.object(batch._Batch, 'cleanup', side_effect=RuntimeError('committed interruption')):
            with self.assertRaises(RuntimeError):
                publish(self.config.vault, self.config.snapshots, original)
        self.assertTrue(batch.publication_pending(self.config.vault))
        lifecycle.apply_legacy_repository_withdrawal(self.config, self.legacy_event())
        self.assertFalse(batch.publication_pending(self.config.vault))
        self.assertEqual(list(self.config.vault.glob('Knowledge-*.md')), [])
        self.assertEqual(list(self.config.snapshots.rglob('snapshot-*.md')), [])

    def test_restored_legacy_bytes_remain_fenced_after_withdrawal(self):
        original = next(self.config.vault.glob('Knowledge-*.md'))
        name, raw = original.name, original.read_bytes()
        lifecycle.apply_legacy_repository_withdrawal(self.config, self.legacy_event())
        restored = self.config.vault / name
        restored.write_bytes(raw)
        restored.chmod(0o600)
        with self.assertRaises(workvault.WorkVaultError):
            workvault.status(self.config.vault, name)
        with self.assertRaises(workvault.WorkVaultError):
            batch.read_curated_cards(self.config.vault, self.config.repository, self.paths[:1])
        with sync._curated_reader(self.config) as (reader, _):
            self.assertEqual(reader.rows(), [])
        lifecycle.apply_legacy_repository_withdrawal(self.config, self.legacy_event())
        self.assertFalse(restored.exists())

    def test_exposed_reader_lease_requires_actual_verified_lock(self):
        with workvault._directory(self.config.vault) as directory:
            with self.assertRaises(workvault.WorkVaultError):
                with sync._curated_reader(self.config, publication_lease=(directory, directory)):
                    pass


if __name__ == '__main__':
    unittest.main()
