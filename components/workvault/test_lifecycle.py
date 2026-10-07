"""Owned lifecycle behavior through real publication and recovery APIs."""
import copy
import json
import os
import select
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import batch
import curation
import sources
import sync
import workvault
from test_source_sync import Adapter, Curator, CUTOFF, config, selection


def event(reference, action='withdrawn'):
    locator = dict(reference, schema_version=sources.LOCATOR_SCHEMA)
    locator.pop('content_hash')
    return {'schema_version': 'workvault-source-lifecycle-v1', 'action': action,
            'reference': locator, 'confirmed': True,
            'evidence': 'access_withdrawn' if action == 'withdrawn' else 'source_removed'}


def interrupt(root, partial=False):
    import lifecycle
    publication = sorted((root / 'vault').glob('Knowledge-*.md'))[0]
    reference = curation.parse_publication(publication.read_bytes())['batch']['cards'][0]['source']
    if partial:
        unlink = workvault._unlink_owned
        def stop(directory, name, identity):
            unlink(directory, name, identity)
            if name.endswith('.md'):
                os._exit(77)
        workvault._unlink_owned = stop
    else:
        lifecycle._erase = lambda *args: os._exit(77)
    lifecycle.apply_source_lifecycle(config(root), selection(Adapter()), event(reference))


def interrupt_publication(root):
    publication = sorted((root / 'vault').glob('Knowledge-*.md'))[0]
    clean = curation.parse_publication(publication.read_bytes())['batch']
    clean['cards'][0]['summary'] = 'Uncommitted synthetic generation.'
    with batch.publication_batch(root / 'vault', root / 'history', clean):
        workvault.publish_curated_batch(root / 'vault', root / 'history' / publication.stem,
                                       clean, publication=publication.name)
        os._exit(77)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = config(self.root)
        self.adapter = Adapter(2, 2)
        sync.sync_sources(self.config, selections=(selection(self.adapter),), cutoff=CUTOFF, curator=Curator())
        self.cards = [curation.parse_publication(path.read_bytes())['batch']['cards'][0]
                      for path in sorted(self.config.vault.glob('Knowledge-*.md'))]
        self.target, self.other = self.cards
        self.note = self.config.vault / 'Human.md'
        self.note.write_bytes(b'Human authored note: preserve exact bytes.\n')
        self.note.chmod(0o600)
        self.publish(self.target)

    def publish(self, card):
        clean = {'schema_version': curation.SOURCE_BATCH_SCHEMA, 'cards': [card]}
        name = batch.source_publication_name(card['source'])
        with batch.publication_batch(self.config.vault, self.config.snapshots, clean):
            workvault.publish_curated_batch(self.config.vault, self.config.snapshots / name[:-3], clean, publication=name)

    def apply(self, action='withdrawn', value=None):
        import lifecycle
        return lifecycle.apply_source_lifecycle(self.config, selection(self.adapter), value or event(self.target['source'], action))

    def unaffected(self):
        name = batch.source_publication_name(self.other['source'])
        return self.note.read_bytes(), (self.config.vault / name).read_bytes()

    def test_retirement_excludes_active_read_and_preserves_private_history(self):
        for number in range(10):
            changed = copy.deepcopy(self.target)
            changed['summary'] = f'Approval checkpoint number {number}.'
            self.publish(changed)
        before = self.unaffected()
        history = {str(path): path.read_bytes() for path in self.config.snapshots.rglob('snapshot-*.md')}
        self.assertEqual(len(history), 7)
        self.apply('retired')
        self.assertEqual(batch.read_source_cards(self.config.vault, [self.target['source']]), [])
        self.assertEqual({str(path): path.read_bytes() for path in self.config.snapshots.rglob('snapshot-*.md')}, history)
        self.assertEqual(self.unaffected(), before)

    def test_replaced_history_directory_refuses_before_any_erasure(self):
        import lifecycle
        before = self.unaffected()
        with patch.object(lifecycle, '_erase', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(workvault.WorkVaultError):
                self.apply()
        name = batch.source_publication_name(self.target['source'])
        folder = self.config.snapshots / name[:-3]
        moved = self.config.snapshots / 'saved-history'
        folder.rename(moved)
        folder.mkdir(mode=0o700)
        for path in moved.iterdir():
            path.rename(folder / path.name)
        with self.assertRaises(workvault.WorkVaultError):
            lifecycle.recover_source_lifecycle(self.config)
        self.assertTrue((self.config.vault / name).exists())
        self.assertEqual(self.unaffected(), before)

    def test_withdrawn_changed_source_is_fenced_before_model_dispatch(self):
        self.apply()
        self.adapter.change_rows = [dict(row, revision='new-revision') for row in self.adapter.rows]
        curator = Curator()
        report = sync.sync_sources(self.config, selections=(selection(self.adapter),), cutoff=CUTOFF, curator=curator)
        self.assertEqual(curator.calls, 0)
        self.assertEqual(report['sources']['synthetic']['state'], 'failed')

    def test_blocked_fetched_dependency_refuses_before_dispatch_and_inflight(self):
        self.apply()
        row = next(row for row in self.adapter.rows if row['item_id'] == self.other['source']['item_id'])
        self.adapter.change_rows = [dict(row, revision='new-dependent')]
        fetch = self.adapter.fetch
        def dependent(locator):
            record = fetch(locator)
            record['dependencies'] = [self.target['source']]
            return record
        self.adapter.fetch = dependent
        curator = Curator()
        result = sync.sync_sources(self.config, selections=(selection(self.adapter),), cutoff=CUTOFF, curator=curator)
        self.assertEqual(curator.calls, 0)
        self.assertEqual(result['sources']['synthetic']['state'], 'failed')
        state = json.loads((self.config.state / sync.SOURCE_STATE).read_bytes())
        self.assertIsNone(state['sources']['synthetic']['inflight'])

    def test_withdrawal_settled_during_fetch_refuses_before_dispatch(self):
        row = next(row for row in self.adapter.rows if row['item_id'] == self.target['source']['item_id'])
        self.adapter.change_rows = [dict(row, revision='new-after-withdrawal')]
        fetch = self.adapter.fetch
        def withdrawing(locator):
            record = fetch(locator)
            self.apply()
            return record
        self.adapter.fetch = withdrawing
        curator = Curator()
        result = sync.sync_sources(self.config, selections=(selection(self.adapter),), cutoff=CUTOFF, curator=curator)
        self.assertEqual(curator.calls, 0)
        self.assertEqual(result['sources']['synthetic']['state'], 'failed')

    def test_dispatch_lock_serializes_other_process_and_refuses_recursive_lifecycle(self):
        row = next(row for row in self.adapter.rows if row['item_id'] == self.target['source']['item_id'])
        self.adapter.change_rows = [dict(row, revision='dispatch-revision')]
        child = []
        outer = self
        class SerialCurator(Curator):
            def curate_sources(self, locators, records):
                with outer.assertRaises(workvault.WorkVaultError):
                    outer.apply()
                script = ('import sys; from pathlib import Path; import lifecycle; '
                    'from test_source_sync import config, selection, Adapter; '
                    'from test_lifecycle import event; import json; '
                    'print("before withdrawal", flush=True); '
                    'lifecycle.apply_source_lifecycle(config(Path(sys.argv[1])), selection(Adapter()), event(json.loads(sys.argv[2]))); '
                    'print("withdrawal settled", flush=True)')
                process = subprocess.Popen([sys.executable, '-B', '-c', script, str(outer.root), json.dumps(outer.target['source'])],
                    cwd=Path(__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                child.append(process)
                outer.assertTrue(select.select([process.stdout], [], [], 5)[0])
                outer.assertEqual(process.stdout.readline(), b'before withdrawal\n')
                outer.assertFalse(select.select([process.stdout], [], [], 0.2)[0], 'withdrawal settled during dispatch lock')
                return super().curate_sources(locators, records)
        curator = SerialCurator()
        try:
            sync.sync_sources(self.config, selections=(selection(self.adapter),), cutoff=CUTOFF, curator=curator)
            self.assertEqual(curator.calls, 1)
            out, err = child[0].communicate(timeout=5)
            self.assertEqual(child[0].returncode, 0, err.decode())
            self.assertEqual(out, b'withdrawal settled\n')
        finally:
            for process in child:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def test_retirement_then_withdrawal_erases_retained_history(self):
        self.apply('retired')
        self.apply()
        name = batch.source_publication_name(self.target['source'])
        self.assertEqual(list((self.config.snapshots / name[:-3]).glob('*.md')), [])

    def test_historical_dependency_erases_only_tainted_generations_and_transitive_dependents(self):
        import lifecycle
        dependent = copy.deepcopy(self.other)
        dependent['source']['revision'] = 'tainted-B'
        dependent['dependencies'] = [self.target['source']]
        self.publish(dependent)
        descendant = copy.deepcopy(dependent)
        descendant['source'] = dict(descendant['source'], item_id='transitive-summary', revision='tainted-C')
        descendant['dependencies'] = [dependent['source']]
        self.publish(descendant)
        independent = copy.deepcopy(self.other)
        independent['source']['revision'] = 'independent-B'
        independent['dependencies'] = []
        self.publish(independent)
        self.publish(independent)
        for filename, ref in [('tainted-memory.json', dependent['source']), ('independent-memory.json', independent['source'])]:
            artifact = self.config.vault / filename
            artifact.write_bytes(b'Exact synthetic memory bytes.')
            artifact.chmod(0o600)
            lifecycle.register_owned_artifact(self.config, filename, 'memory', [ref])
        name = batch.source_publication_name(independent['source'])
        current = (self.config.vault / name).read_bytes()
        independent_history = {str(path): path.read_bytes() for path in (self.config.snapshots / name[:-3]).glob('*.md')
                               if curation.parse_publication(path.read_bytes())['batch']['cards'][0]['dependencies'] == []}
        self.apply()
        self.assertEqual((self.config.vault / name).read_bytes(), current)
        retained = {str(path): path.read_bytes() for path in (self.config.snapshots / name[:-3]).glob('*.md')}
        self.assertEqual(retained, independent_history)
        self.assertFalse((self.config.vault / 'tainted-memory.json').exists())
        self.assertEqual((self.config.vault / 'independent-memory.json').read_bytes(), b'Exact synthetic memory bytes.')
        self.assertEqual(batch.read_source_cards(self.config.vault, [independent['source']])[0]['batch']['cards'], [independent])
        self.assertEqual(batch.read_source_cards(self.config.vault, [descendant['source']]), [])
        for card in [dependent, descendant]:
            with self.assertRaises(workvault.WorkVaultError):
                self.publish(card)
        later = copy.deepcopy(independent)
        later['source']['revision'] = 'new-independent-B'
        self.publish(later)
        self.assertEqual(batch.read_source_cards(self.config.vault, [later['source']])[0]['batch']['cards'], [later])

    def test_root_limit_refuses_new_event_without_poisoning_readable_state(self):
        import lifecycle
        self.apply('retired')
        with workvault._publication_lock(self.config.vault) as directory:
            state = lifecycle._load(directory)
            for number in range(8191):
                reference = dict(self.target['source'], item_id=f'previous-{number}')
                state['blocked'][lifecycle._key(reference)] = 'retired'
            sync._save_state(directory, state, lifecycle.STATE)
            self.assertEqual(len(lifecycle._load(directory)['blocked']), 8192)
        before = (self.config.vault / lifecycle.STATE).read_bytes()
        with self.assertRaises(workvault.WorkVaultError):
            self.apply(value=event(dict(self.target['source'], item_id='overflow'), 'retired'))
        self.assertEqual((self.config.vault / lifecycle.STATE).read_bytes(), before)
        self.assertEqual(len(batch.read_source_cards(self.config.vault, [self.other['source']])), 1)
        lifecycle.recover_source_lifecycle(self.config)

    def test_pending_count_and_serialized_size_refuse_before_state_write_or_erasure(self):
        import lifecycle
        self.apply(value=event(dict(self.target['source'], item_id='no-files'), 'retired'))
        before = (self.config.vault / lifecycle.STATE).read_bytes()
        current = self.unaffected()
        target = self.config.vault / batch.source_publication_name(self.target['source'])
        original = target.read_bytes()
        for name, limit in [('MAX_PENDING_FILES', 1), ('MAX_STATE_BYTES', len(before))]:
            with self.subTest(bound=name), patch.object(lifecycle, name, limit, create=True):
                with self.assertRaises(workvault.WorkVaultError):
                    self.apply()
            self.assertEqual((self.config.vault / lifecycle.STATE).read_bytes(), before)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(self.unaffected(), current)
            self.assertEqual(len(batch.read_source_cards(self.config.vault, [self.other['source']])), 1)
            lifecycle.recover_source_lifecycle(self.config)

    def test_artifact_count_and_size_refuse_before_registry_replacement(self):
        import lifecycle
        for name in ['first-index.json', 'next-index.json']:
            path = self.config.vault / name
            path.write_bytes(b'Synthetic index bytes.')
            path.chmod(0o600)
        lifecycle.register_owned_artifact(self.config, 'first-index.json', 'index', [self.other['source']])
        before = (self.config.vault / lifecycle.ARTIFACTS).read_bytes()
        for name, limit in [('MAX_ARTIFACTS', 1), ('MAX_STATE_BYTES', len(before))]:
            with self.subTest(bound=name), patch.object(lifecycle, name, limit, create=True):
                with self.assertRaises(workvault.WorkVaultError):
                    lifecycle.register_owned_artifact(self.config, 'next-index.json', 'index', [self.other['source']])
            self.assertEqual((self.config.vault / lifecycle.ARTIFACTS).read_bytes(), before)
            self.assertEqual((self.config.vault / 'first-index.json').read_bytes(), b'Synthetic index bytes.')
            self.assertEqual(len(batch.read_source_cards(self.config.vault, [self.other['source']])), 1)

    def test_legacy_aggregate_native_layout_refuses_without_partial_erasure(self):
        workvault.publish_curated_batch(self.config.vault, self.config.snapshots / 'aggregate-history',
            {'schema_version': curation.SOURCE_BATCH_SCHEMA, 'cards': self.cards})
        before = {str(path): path.read_bytes() for path in self.config.vault.glob('*.md')}
        with self.assertRaises(workvault.WorkVaultError):
            self.apply()
        self.assertEqual({str(path): path.read_bytes() for path in self.config.vault.glob('*.md')}, before)

    def test_existing_publication_rollback_settles_before_withdrawal_plan(self):
        script = 'from pathlib import Path; from test_lifecycle import interrupt_publication; interrupt_publication(Path(__import__("sys").argv[1]))'
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.root)], cwd=Path(__file__).parent, capture_output=True)
        self.assertEqual(result.returncode, 77, result.stderr.decode())
        self.assertTrue(batch.publication_pending(self.config.vault))
        self.apply()
        self.assertFalse(batch.publication_pending(self.config.vault))
        self.assertFalse(batch.recover_publication_batch(self.config.vault, self.config.snapshots))
        self.assertEqual(batch.read_source_cards(self.config.vault, [self.target['source']]), [])

    def test_partial_erasure_restarts_without_restoring_deleted_current(self):
        import lifecycle
        script = 'from pathlib import Path; from test_lifecycle import interrupt; interrupt(Path(__import__("sys").argv[1]), True)'
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.root)], cwd=Path(__file__).parent, capture_output=True)
        self.assertEqual(result.returncode, 77, result.stderr.decode())
        with self.assertRaises(workvault.WorkVaultError):
            batch.read_source_cards(self.config.vault, [self.other['source']])
        lifecycle.recover_source_lifecycle(self.config)
        name = batch.source_publication_name(self.target['source'])
        self.assertFalse((self.config.vault / name).exists())
        self.assertEqual(list((self.config.snapshots / name[:-3]).glob('*.md')), [])

    def test_withdrawal_erases_dependent_history_index_memory_and_blocks_restore(self):
        import lifecycle
        dependent = copy.deepcopy(self.target)
        dependent['source'] = dict(dependent['source'], item_id='summary')
        dependent['dependencies'] = [self.target['source']]
        self.publish(dependent)
        self.publish(dependent)
        old = curation.parse_publication(next(self.config.snapshots.rglob('snapshot-*.md')).read_bytes())['batch']
        for name, kind in [('derived-index.json', 'index'), ('derived-memory.json', 'memory')]:
            path = self.config.vault / name
            path.write_bytes(b'Owned synthetic derived payload')
            path.chmod(0o600)
            lifecycle.register_owned_artifact(self.config, name, kind, [self.target['source']])
        before = self.unaffected()
        self.apply()
        self.apply()
        lifecycle.recover_source_lifecycle(self.config)
        for card in [self.target, dependent]:
            name = batch.source_publication_name(card['source'])
            self.assertFalse((self.config.vault / name).exists())
            self.assertEqual(list((self.config.snapshots / name[:-3]).glob('*.md')), [])
            self.assertEqual(batch.read_source_cards(self.config.vault, [card['source']]), [])
        self.assertFalse((self.config.vault / 'derived-index.json').exists())
        self.assertFalse((self.config.vault / 'derived-memory.json').exists())
        self.assertEqual(self.unaffected(), before)
        with self.assertRaises(workvault.WorkVaultError):
            with batch.publication_batch(self.config.vault, self.config.snapshots, old):
                pass
        with self.assertRaises(workvault.WorkVaultError):
            self.publish(self.target)

    def test_auth_denial_missing_credentials_and_incomplete_pages_do_not_remove(self):
        before = {str(path): path.read_bytes() for path in self.config.vault.glob('*.md')}
        for failure in [RuntimeError('401'), RuntimeError('403'), RuntimeError('absent credentials')]:
            with patch.object(self.adapter, 'changes', side_effect=failure):
                sync.sync_sources(self.config, selections=(selection(self.adapter),), cutoff=CUTOFF, curator=Curator())
        self.assertEqual({str(path): path.read_bytes() for path in self.config.vault.glob('*.md')}, before)
        page = {'page': {'items': [], 'complete': False, 'next_cursor': 'more', 'gaps': []}, 'checkpoint': None}
        with patch.object(self.adapter, 'changes', return_value=page):
            sync.sync_sources(self.config, selections=(selection(self.adapter),), cutoff=CUTOFF, curator=Curator())
        self.assertEqual({str(path): path.read_bytes() for path in self.config.vault.glob('*.md')}, before)

    def test_identity_and_symlink_refuse_before_erasure(self):
        before = self.unaffected()
        bad = event(self.target['source'])
        bad['reference']['account'] = 'wrong-workspace'
        with self.assertRaises(workvault.WorkVaultError):
            self.apply(value=bad)
        name = batch.source_publication_name(self.target['source'])
        path = self.config.vault / name
        original = path.read_bytes()
        path.unlink()
        path.symlink_to(self.note)
        with self.assertRaises(workvault.WorkVaultError):
            self.apply()
        self.assertEqual(self.unaffected(), before)
        path.unlink()
        path.write_bytes(original)
        path.chmod(0o600)
        bad = event(self.target['source'])
        bad['confirmed'] = False
        with self.assertRaises(workvault.WorkVaultError):
            self.apply(value=bad)
        bad = event(self.target['source'])
        bad['evidence'] = '403'
        with self.assertRaises(workvault.WorkVaultError):
            self.apply(value=bad)
        import lifecycle
        with self.assertRaises(workvault.WorkVaultError):
            lifecycle.register_owned_artifact(self.config, '../Human.md', 'memory', [self.target['source']])

    def test_interrupted_withdrawal_fences_reads_and_batch_recovery_until_restart(self):
        import lifecycle
        script = 'from pathlib import Path; from test_lifecycle import interrupt; interrupt(Path(__import__("sys").argv[1]))'
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.root)], cwd=Path(__file__).parent, capture_output=True)
        self.assertEqual(result.returncode, 77, result.stderr.decode())
        with self.assertRaises(workvault.WorkVaultError):
            batch.read_source_cards(self.config.vault, [self.other['source']])
        with self.assertRaises(workvault.WorkVaultError):
            batch.recover_publication_batch(self.config.vault, self.config.snapshots)
        lifecycle.recover_source_lifecycle(self.config)
        lifecycle.recover_source_lifecycle(self.config)
        self.assertEqual(batch.read_source_cards(self.config.vault, [self.target['source']]), [])
        evidence = (self.config.vault / lifecycle.STATE).read_bytes()
        self.assertNotIn(self.target['summary'].encode(), evidence)
        self.assertNotIn(self.target['source']['citation'].encode(), evidence)


if __name__ == '__main__':
    unittest.main()
