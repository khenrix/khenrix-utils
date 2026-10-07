"""Synthetic coordinator recovery through real private state and publications."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import curation
import sources
import sync
from test_sources import locator


RAW = b'RAW_COORDINATOR_CANARY: a native body that must never survive serialization.'
CUTOFF = '2026-10-07T12:00:00Z'


class Adapter:
    def __init__(self, count=1, page_size=1):
        self.rows = [dict(locator(), item_id=str(i), citation=f'https://example.invalid/source/{i}')
                     for i in range(count)]
        self.page_size = page_size
        self.fail = False
        self.change_rows = []

    def list_page(self, *, cursor, cutoff):
        if self.fail:
            raise RuntimeError(RAW.decode())
        if cutoff != CUTOFF:
            raise AssertionError('cutoff changed')
        start = int(cursor or 0)
        end = min(start + self.page_size, len(self.rows))
        return {'items': self.rows[start:end], 'next_cursor': str(end) if end < len(self.rows) else None,
                'complete': end == len(self.rows), 'gaps': []}

    def changes(self, *, cursor):
        return {'page': {'items': self.change_rows, 'next_cursor': None, 'complete': True,
                         'gaps': []}, 'checkpoint': 'checkpoint-2'}

    def fetch(self, ref):
        return {'reference': dict(ref, schema_version=sources.SOURCE_SCHEMA,
                                 content_hash='sha256:' + hashlib.sha256(RAW).hexdigest()),
                'body': RAW, 'draft': False, 'supersedes': [], 'dependencies': []}


class Curator:
    def __init__(self):
        self.calls = 0
        self.sizes = []
        self.fail = False

    def curate_sources(self, locators, records):
        self.calls += 1
        self.sizes.append(len(records))
        if self.fail:
            raise RuntimeError(RAW.decode())
        return [{'source_key': sources.source_key(record['reference']), 'title': 'Synthetic card',
                 'summary': 'Approval is pending.', 'details': 'Owner confirmation is required.',
                 'date': '2026-10-05', 'draft': False, 'uncertainty': ['Not independently verified.'],
                 'source_links': [], 'evidence_kind': 'owner_synthesis'} for record in records]


def config(root):
    return sync.SourceSyncConfig(root / 'vault', root / 'history', root / 'state')


def selection(adapter, source_id='synthetic'):
    return sync.SourceSelection(source_id, 'slack', 'workspace-fixture', 'C123', adapter)


def interrupt_run(root, point):
    adapter, curator = Adapter(), Curator()
    if point == 'before_send':
        adapter.fetch = lambda ref: os._exit(77)
    elif point == 'uncertain_send':
        curator.curate_sources = lambda refs, records: os._exit(77)
    elif point == 'after_publication':
        sync._ack_source_batch = lambda *args: os._exit(77)
    sync.sync_sources(config(root), selections=(selection(adapter),), cutoff=CUTOFF, curator=curator)


def gap_restart(root):
    class GapAdapter(Adapter):
        def list_page(self, *, cursor, cutoff):
            page = int(cursor or 0) + 1
            return {'items': [], 'next_cursor': str(page) if page < 40 else None,
                    'complete': page == 40, 'gaps': [{'code': 'retention_gap', 'count': page}]}
    healthy = Adapter()
    healthy.rows[0]['container'] = 'second'
    return sync.sync_sources(config(root), selections=(selection(GapAdapter(), 'gaps'),
            sync.SourceSelection('healthy', 'slack', 'workspace-fixture', 'second', healthy)),
        cutoff=CUTOFF, curator=Curator())


def metadata_interrupt(root):
    adapter = Adapter()
    adapter.change_rows = [dict(adapter.rows[0], citation='https://example.invalid/updated/0')]
    sync._ack_source_batch = lambda *args: os._exit(77)
    sync.sync_sources(config(root), selections=(selection(adapter),), cutoff=CUTOFF, curator=Curator())


class SourceSyncTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(sync, 'sync_sources'), 'multi-source coordinator is missing')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adapter, self.curator = Adapter(), Curator()

    def run_sync(self, **kwargs):
        return sync.sync_sources(config(self.root), selections=kwargs.pop('selections', (selection(self.adapter),)),
                                 cutoff=CUTOFF, curator=kwargs.pop('curator', self.curator), **kwargs)

    def state(self):
        return json.loads((self.root / 'state' / sync.SOURCE_STATE).read_bytes())

    def interrupt(self, point):
        script = 'from pathlib import Path; from test_source_sync import interrupt_run; interrupt_run(Path(__import__("sys").argv[1]), __import__("sys").argv[2])'
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.root), point],
                                cwd=Path(__file__).parent, capture_output=True)
        self.assertEqual(result.returncode, 77, result.stderr.decode())

    def test_backfill_publishes_then_unchanged_delta_needs_zero_curation(self):
        first = self.run_sync()
        self.assertEqual(first['sources']['synthetic']['covered_count'], 1)
        self.assertEqual(self.curator.calls, 1)
        self.adapter.change_rows = self.adapter.rows
        again = self.run_sync()
        self.assertEqual(self.curator.calls, 1)
        self.assertEqual(again['sources']['synthetic']['change_cursor'], 'checkpoint-2')
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(RAW, path.read_bytes())
        pub = next((self.root / 'vault').glob('Knowledge-*.md'))
        ref = curation.parse_publication(pub.read_bytes())['batch']['cards'][0]['source']
        self.assertEqual(ref['content_hash'], 'sha256:' + hashlib.sha256(RAW).hexdigest())
        self.assertEqual(ref['revision'], 'revision-2')

    def test_all_pages_resume_with_five_item_paid_bound(self):
        self.adapter = Adapter(23, 11)
        for _ in range(3):
            result = self.run_sync()
        self.assertEqual(result['sources']['synthetic']['covered_count'], 23)
        self.assertEqual(result['sources']['synthetic']['page_count'], 3)
        self.assertEqual(result['sources']['synthetic']['phase'], 'changes')
        self.assertTrue(all(1 <= size <= 5 for size in self.curator.sizes))
        self.assertEqual(len(list((self.root / 'vault').glob('Knowledge-*.md'))), 23)

    def test_process_interruption_before_send_resumes_unpaid_page(self):
        self.interrupt('before_send')
        result = self.run_sync()
        self.assertEqual(result['sources']['synthetic']['covered_count'], 1)
        self.assertEqual(self.curator.calls, 1)

    def test_process_interruption_during_send_is_fenced_without_replay(self):
        self.interrupt('uncertain_send')
        result = self.run_sync()
        self.assertEqual(result['sources']['synthetic']['state'], 'uncertain')
        self.assertEqual(self.curator.calls, 0)
        self.assertEqual(result['sources']['synthetic']['covered_count'], 0)

    def test_process_interruption_after_publication_reuses_committed_cards(self):
        self.interrupt('after_publication')
        result = self.run_sync()
        self.assertEqual(result['sources']['synthetic']['covered_count'], 1)
        self.assertEqual(self.curator.calls, 0)
        self.assertEqual(result['sources']['synthetic']['phase'], 'changes')

    def test_terminal_change_checkpoint_waits_for_publication(self):
        self.run_sync()
        self.adapter.change_rows = [dict(self.adapter.rows[0], revision='revision-3')]
        self.curator.fail = True
        result = self.run_sync()
        self.assertIsNone(result['sources']['synthetic']['change_cursor'])
        self.assertEqual(result['sources']['synthetic']['state'], 'uncertain')
        self.assertNotIn(RAW.decode(), json.dumps(result))

    def test_failed_source_does_not_prevent_independent_progress(self):
        failed, healthy = Adapter(), Adapter()
        failed.fail = True
        healthy.rows = [dict(healthy.rows[0], container='second')]
        result = self.run_sync(selections=(selection(failed, 'failed'),
                    sync.SourceSelection('healthy', 'slack', 'workspace-fixture', 'second', healthy)))
        self.assertEqual(result['sources']['failed']['state'], 'failed')
        self.assertEqual(result['sources']['healthy']['covered_count'], 1)
        self.assertNotIn(RAW.decode(), json.dumps(result))

    def test_frozen_binding_and_adapter_identity_drift_refuse(self):
        self.run_sync()
        before = self.state()
        with self.assertRaises(sync.SyncError) as caught:
            self.run_sync(selections=(sync.SourceSelection('synthetic', 'slack', 'other', 'C123', self.adapter),))
        self.assertNotIn(RAW.decode(), str(caught.exception))
        self.assertEqual(self.state(), before)
        self.adapter.change_rows = [dict(self.adapter.rows[0], account='other')]
        result = self.run_sync()
        self.assertEqual(result['sources']['synthetic']['state'], 'failed')
        self.assertEqual(self.curator.calls, 1)

    def test_listing_without_revision_fetches_hash_before_skipping_unchanged_content(self):
        self.adapter.rows[0]['revision'] = None
        self.run_sync()
        self.adapter.change_rows = self.adapter.rows
        self.run_sync()
        self.assertEqual(self.curator.calls, 1)

    def test_changed_body_with_no_revision_requires_curation(self):
        self.adapter.rows[0]['revision'] = None
        self.run_sync()
        self.adapter.change_rows = self.adapter.rows
        fetch = self.adapter.fetch
        def changed(ref):
            record = fetch(ref)
            record['body'] = RAW + b' Changed observation.'
            record['reference']['content_hash'] = 'sha256:' + hashlib.sha256(record['body']).hexdigest()
            return record
        self.adapter.fetch = changed
        result = self.run_sync()
        self.assertEqual(self.curator.calls, 2)
        self.assertEqual(result['sources']['synthetic']['covered_count'], 2)

    def test_invalid_fetched_body_refuses_before_paid_dispatch(self):
        fetch = self.adapter.fetch
        def invalid(ref):
            record = fetch(ref)
            record['body'] += b' Hash conflict.'
            return record
        self.adapter.fetch = invalid
        result = self.run_sync()
        self.assertEqual(self.curator.calls, 0)
        self.assertEqual(result['sources']['synthetic']['state'], 'failed')
        self.assertEqual(result['sources']['synthetic']['covered_count'], 0)

    def test_new_observation_time_does_not_recurate_unchanged_revision_or_hash(self):
        for revision in ('revision-2', None):
            with self.subTest(revision=revision), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary)
                self.adapter, self.curator = Adapter(), Curator()
                self.adapter.rows[0]['revision'] = revision
                self.run_sync()
                self.adapter.change_rows = [dict(self.adapter.rows[0], observed_at='2026-10-07T13:00:00Z')]
                self.run_sync()
                self.assertEqual(self.curator.calls, 1)

    def test_completion_metadata_corruption_cannot_inflate_coverage(self):
        self.run_sync()
        ledger = next((self.root / 'state').glob('source-completed-*.json'))
        with sync._directory(self.root / 'state') as directory:
            sync._save_state(directory, {'raw_body': 'RAW_COORDINATOR_CANARY'}, ledger.name)
        with self.assertRaises(sync.SyncError):
            self.run_sync()

    def test_mixed_delta_batch_curates_only_changed_items(self):
        self.adapter = Adapter(3, 3)
        for row in self.adapter.rows:
            row['revision'] = None
        self.run_sync()
        self.adapter.change_rows = self.adapter.rows
        fetch = self.adapter.fetch
        def mixed(ref):
            record = fetch(ref)
            if ref['item_id'] == '1':
                record['body'] += b' Changed only this item.'
                record['reference']['content_hash'] = 'sha256:' + hashlib.sha256(record['body']).hexdigest()
            return record
        self.adapter.fetch = mixed
        self.run_sync()
        self.assertEqual(self.curator.sizes, [3, 1])

    def test_curator_cannot_mutate_fetched_provenance_after_dispatch_fence(self):
        class MutatingCurator(Curator):
            def curate_sources(self, locators, records):
                cards = super().curate_sources(locators, records)
                records[0]['body'] += b' Mutated during curation.'
                records[0]['reference']['content_hash'] = 'sha256:' + hashlib.sha256(records[0]['body']).hexdigest()
                return cards
        result = self.run_sync(curator=MutatingCurator())
        self.assertEqual(result['sources']['synthetic']['covered_count'], 0)
        self.assertEqual(list((self.root / 'vault').glob('Knowledge-*.md')), [])

    def test_v1_state_status_and_repeat_coexist_with_native_state(self):
        from test_sync import Source, Curator as LegacyCurator
        native = config(self.root)
        legacy = sync.SyncConfig('example/team-wiki', native.vault, native.snapshots, native.state,
                                 'example-project', 'eu', 'example-model')
        sync.sync_all(legacy, source=Source(['knowledge/guide.md']), curator=LegacyCurator())
        before = (native.state / sync.STATE).read_bytes()
        report = sync.sync_status(legacy)
        self.run_sync()
        self.assertEqual((native.state / sync.STATE).read_bytes(), before)
        self.assertEqual(sync.sync_status(legacy), report)
        self.assertTrue(sync.sync_all(legacy, source=Source(['knowledge/guide.md']),
                                     curator=LegacyCurator(True))['full_source_coverage'])

    def test_failed_page_reports_native_gap_without_acknowledging_it(self):
        original = self.adapter.list_page
        def page_with_gap(**kwargs):
            page = original(**kwargs)
            page['gaps'] = [{'code': 'retention_gap', 'count': None}]
            return page
        self.adapter.list_page = page_with_gap
        self.curator.fail = True
        result = self.run_sync()
        self.assertEqual(result['sources']['synthetic']['gaps'], [{'code': 'retention_gap', 'count': None}])
        self.assertEqual(result['sources']['synthetic']['page_count'], 0)
        self.assertFalse(result['full_source_coverage'])

    def test_more_than_32_cumulative_gaps_resume_after_process_restart(self):
        for _ in range(33):
            gap_restart(self.root)
        script = 'import json,sys; from pathlib import Path; from test_source_sync import gap_restart; print(json.dumps(gap_restart(Path(sys.argv[1]))))'
        for _ in range(7):
            result = subprocess.run([sys.executable, '-B', '-c', script, str(self.root)],
                                    cwd=Path(__file__).parent, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            report = json.loads(result.stdout)
        self.assertEqual(report['sources']['gaps']['page_count'], 40)
        self.assertEqual(report['sources']['gaps']['gaps'],
                         [{'code': 'retention_gap', 'count': count} for count in range(1, 41)])
        self.assertEqual(report['sources']['healthy']['page_count'], 40)
        self.assertEqual(report['sources']['healthy']['covered_count'], 1)

    def test_acknowledged_history_replay_does_not_pay_after_snapshot_retention(self):
        for count in (2, 10):
            with self.subTest(revisions=count), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary)
                self.adapter, self.curator = Adapter(), Curator()
                original = self.adapter.rows[0]
                self.adapter.rows = [dict(original, revision=f'revision-{index}') for index in range(count)]
                self.adapter.page_size = count
                self.run_sync()
                self.assertEqual(self.curator.calls, count)
                before = {str(path.relative_to(self.root)): path.read_bytes()
                          for folder in ('vault', 'history') for path in (self.root / folder).rglob('*') if path.is_file()}
                retained = [curation.parse_publication(path.read_bytes())['batch']['cards'][0]['source']['revision']
                            for path in (self.root / 'history').rglob('*.md')]
                if count == 10:
                    self.assertNotIn('revision-0', retained)
                    self.assertEqual(len(retained), 7)
                self.adapter.change_rows = [dict(row, observed_at='2026-10-07T13:00:00Z')
                                            for row in self.adapter.rows]
                report = self.run_sync()
                self.assertEqual(self.curator.calls, count)
                self.assertEqual(report['sources']['synthetic']['covered_count'], count)
                self.assertEqual(report['sources']['synthetic']['change_cursor'], 'checkpoint-2')
                after = {str(path.relative_to(self.root)): path.read_bytes()
                         for folder in ('vault', 'history') for path in (self.root / folder).rglob('*') if path.is_file()}
                self.assertEqual(after, before)

    def test_old_checkpoint_cannot_mask_exact_committed_metadata_on_restart(self):
        self.run_sync()
        script = 'import sys; from pathlib import Path; from test_source_sync import metadata_interrupt; metadata_interrupt(Path(sys.argv[1]))'
        interrupted = subprocess.run([sys.executable, '-B', '-c', script, str(self.root)],
                                     cwd=Path(__file__).parent, capture_output=True)
        self.assertEqual(interrupted.returncode, 77, interrupted.stderr.decode())
        pub = next((self.root / 'vault').glob('Knowledge-*.md'))
        reference = curation.parse_publication(pub.read_bytes())['batch']['cards'][0]['source']
        self.assertEqual(reference['citation'], 'https://example.invalid/updated/0')
        self.assertEqual(self.state()['sources']['synthetic']['inflight']['references'], [reference])
        self.adapter.change_rows = [dict(self.adapter.rows[0], citation='https://example.invalid/updated/0')]
        for _ in range(2):
            result = self.run_sync()
            self.assertEqual(result['sources']['synthetic']['state'], 'ready')
            self.assertEqual(result['sources']['synthetic']['change_cursor'], 'checkpoint-2')
        self.assertEqual(self.curator.calls, 1)
        self.assertIsNone(self.state()['sources']['synthetic']['inflight'])



if __name__ == '__main__':
    unittest.main()
