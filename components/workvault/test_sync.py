"""Exercise the complete sync using synthetic external-boundary fixtures."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


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

    def test_script_entry_publishes_and_reuses_canonical_lifecycle_configuration(self):
        config = self.root / 'sync.json'
        config.write_text(json.dumps({'schema_version': 'workvault-sync-config-v1',
            'repository': self.config.repository,
            'vault': str(self.config.vault), 'snapshots': str(self.config.snapshots),
            'state': str(self.config.state), 'vertex': {'project': self.config.project,
                'location': self.config.location, 'model': self.config.model}}))
        config.chmod(0o600)
        script = '''
import json, runpy, sys
from pathlib import Path
from unittest.mock import patch
from test_sync import Source, Curator

source = Source(['knowledge/page-0.md'])
curator = Curator()
calls = []
curate = curator.curate
def counted(*args):
    calls.append(1)
    return curate(*args)
curator.curate = counted
entry = str(Path.cwd() / 'sync.py')
with patch('collector.GitHubSource', return_value=source), patch('collector.VertexCurator', return_value=curator):
    for _ in range(2):
        sys.argv = [entry, 'sync', '--config', sys.argv[-1]]
        try:
            runpy.run_path(entry, run_name='__main__')
        except SystemExit as error:
            if error.code:
                raise
print(json.dumps({'curation_calls': len(calls)}))
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(config)],
                                cwd=Path(__file__).parent, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        output = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row['full_source_coverage'] for row in output[:2]], [True, True])
        self.assertEqual(output[2], {'curation_calls': 1})
        self.assertEqual(len(list(self.config.vault.glob('Knowledge-*.md'))), 1)
        self.assertEqual(result.stderr.splitlines(), [b'{"published_count": 1, "remaining_count": 0}'])
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'RAW_SOURCE_CANARY_NEVER_STORED', path.read_bytes())

    def test_actual_second_note_publisher_failure_restores_whole_requested_batch(self):
        paths = self.paths[:2]
        self.sync.sync_all(self.config, source=Source(paths), curator=Curator())
        before = {path: (self.config.vault / self.sync.publication_name(
            self.config.repository, path)).read_bytes() for path in paths}
        human = self.config.vault / 'Human-note.md'
        human.write_bytes(b'Keep this human note unchanged.\n')
        publisher = self.sync.publish_curated_batch
        replace = os.replace
        calls = []

        def actual_publisher(*args, **kwargs):
            calls.append(kwargs['publication'])
            return publisher(*args, **kwargs)

        def second_publication_fault(source, destination, *args, **kwargs):
            if destination == self.sync.publication_name(self.config.repository, paths[1]):
                raise OSError('synthetic second note replacement failure')
            return replace(source, destination, *args, **kwargs)

        with patch.object(self.sync, 'publish_curated_batch', actual_publisher), \
                patch('os.replace', side_effect=second_publication_fault):
            result = self.sync.sync_all(self.config, source=Source(paths, '2' * 40), curator=Curator())
        self.assertEqual(calls, [self.sync.publication_name(self.config.repository, path) for path in paths])
        self.assertFalse(result['full_source_coverage'])
        self.assertEqual({path: (self.config.vault / self.sync.publication_name(
            self.config.repository, path)).read_bytes() for path in paths}, before)
        self.assertEqual(human.read_bytes(), b'Keep this human note unchanged.\n')

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
