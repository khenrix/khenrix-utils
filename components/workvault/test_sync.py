"""Exercise the complete sync using synthetic external-boundary fixtures."""
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest


class Source:
    def __init__(self, paths, commit='1' * 40):
        self.paths, self.commit = paths, commit
        self.raw = 'RAW_SOURCE_CANARY_NEVER_STORED'

    def snapshot(self, repository, ref=None):
        return SimpleNamespace(repository=repository, commit=self.commit,
            discovered_paths=tuple(self.paths), indexed_paths=tuple(self.paths[:-1]),
            broken_index_references=(), schema='synthetic schema', index='synthetic index')

    def read_pages(self, snapshot, paths):
        return {path: self.raw for path in paths}


class Curator:
    def __init__(self, fail=False):
        self.fail = fail

    def curate(self, snapshot, pages):
        if self.fail:
            raise ValueError('EXTERNAL_RAW_ERROR_MUST_NOT_LEAK')
        if not 1 <= len(pages) <= 5:
            raise AssertionError('unbounded provider batch')
        return {'schema_version': 'workvault-curated-batch-v1',
            'repository': snapshot.repository, 'commit': snapshot.commit,
            'inventory_complete': True, 'discovered_paths': list(snapshot.discovered_paths),
            'indexed_paths': list(snapshot.indexed_paths), 'requested_paths': list(pages),
            'cards': [{'path': p, 'title': 'Synthetic ' + p, 'summary': 'A curated fact.',
                'details': '', 'date': '2026-10-05', 'draft': False,
                'uncertainty': ['Source is not independently verified.'],
                'source_links': [], 'cross_references': []} for p in pages]}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(Path(__file__).with_name('sync.py').exists(), 'functional sync module missing')
        self.sync = importlib.import_module('sync')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.sync.SyncConfig('example/team-wiki', self.root / 'vault',
            self.root / 'snapshots', self.root / 'state', 'example-project', 'eu', 'example-model')
        self.paths = ['knowledge/page-' + str(i) + '.md' for i in range(6)]

    def test_all_pages_publish_in_bounded_batches_and_raw_bodies_never_persist(self):
        source = Source(self.paths)
        state = self.sync.sync_all(self.config, source=source, curator=Curator())
        self.assertTrue(state['full_source_coverage'])
        self.assertEqual(state['covered_paths'], self.paths)
        self.assertEqual(state['index_gaps'], ['knowledge/page-5.md'])
        self.assertEqual(len(list(self.config.vault.glob('Knowledge-*.md'))), 6)
        self.assertEqual(self.sync.sync_status(self.config), state)
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(source.raw.encode(), path.read_bytes())
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_failed_refresh_retains_previous_notes_and_reports_current_commit_uncovered(self):
        self.sync.sync_all(self.config, source=Source(self.paths), curator=Curator())
        before = {p.name: p.read_bytes() for p in self.config.vault.glob('*.md')}
        state = self.sync.sync_all(self.config, source=Source(self.paths, '2' * 40), curator=Curator(True))
        self.assertFalse(state['full_source_coverage'])
        self.assertEqual(state['uncovered_paths'], self.paths)
        self.assertEqual(state['stale_paths'], self.paths)
        self.assertEqual(state['failed_paths'], self.paths[:5])
        self.assertEqual(state['not_attempted_paths'], self.paths[5:])
        self.assertEqual({p.name: p.read_bytes() for p in self.config.vault.glob('*.md')}, before)
        self.assertNotIn('EXTERNAL_RAW_ERROR', json.dumps(state))

    def test_current_commit_is_not_recurated_or_snapshotted_again(self):
        self.sync.sync_all(self.config, source=Source(self.paths), curator=Curator())
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        state = self.sync.sync_all(self.config, source=Source(self.paths), curator=Curator(True))
        self.assertTrue(state['full_source_coverage'])
        self.assertEqual({str(p): p.read_bytes() for p in self.config.vault.glob('*.md')},
            {p: data for p, data in before.items() if Path(p).parent == self.config.vault and Path(p).suffix == '.md'})
        self.assertFalse(any(self.config.snapshots.rglob('snapshot-*.md')))

    def test_source_removal_is_reported_but_does_not_delete_notes(self):
        self.sync.sync_all(self.config, source=Source(self.paths), curator=Curator())
        removed = self.config.vault / self.sync.publication_name(self.config.repository, self.paths[-1])
        before = removed.read_bytes()
        state = self.sync.sync_all(self.config, source=Source(self.paths[:-1], '2' * 40), curator=Curator())
        self.assertTrue(state['full_source_coverage'])
        self.assertEqual(state['retained_removed_paths'], [self.paths[-1]])
        self.assertEqual(removed.read_bytes(), before)

    def test_uncertain_provider_work_is_not_implicitly_replayed_on_resume(self):
        first = self.sync.sync_all(self.config, source=Source(self.paths), curator=Curator(True))
        self.assertEqual(first['uncertain_paths'], self.paths[:5])
        class ForbiddenSource:
            def snapshot(self, *args, **kwargs):
                raise AssertionError('uncertain work must be fenced before source access')
        with self.assertRaises(self.sync.SyncError):
            self.sync.sync_all(self.config, source=ForbiddenSource(), curator=Curator())
        self.assertEqual(self.sync.sync_status(self.config)['uncertain_paths'], self.paths[:5])

    def test_status_without_state_is_read_only(self):
        self.assertEqual(self.sync.sync_status(self.config),
            {'schema_version': 'workvault-sync-status-v1', 'state': 'empty'})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_overlap_is_rejected_before_source_access(self):
        config = self.sync.SyncConfig(self.config.repository, self.config.vault,
            self.config.vault / 'snapshots', self.config.state, self.config.project,
            self.config.location, self.config.model)
        class ForbiddenSource:
            def snapshot(self, *args, **kwargs):
                raise AssertionError('source access must not happen')
        with self.assertRaises(self.sync.SyncError):
            self.sync.sync_all(config, source=ForbiddenSource(), curator=Curator())
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
