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


def relationship_interrupt(root, point):
    adapter, curator = Adapter(), Curator()
    parent = adapter.fetch(adapter.rows[0])['reference']
    dependency = dict(parent, item_id='target', citation='https://example.invalid/target')
    fetch = adapter.fetch
    adapter.fetch = lambda ref: dict(fetch(ref), dependencies=[dependency])
    adapter.change_rows = adapter.rows
    if point == 'uncertain_send':
        curator.curate_sources = lambda refs, records: os._exit(77)
    elif point == 'after_publication':
        sync._ack_source_batch = lambda *args: os._exit(77)
    sync.sync_sources(config(root), selections=(selection(adapter),), cutoff=CUTOFF, curator=curator)


def counted_gap_restart(root, interrupt=False):
    class GapAdapter(Adapter):
        def list_page(self, **kwargs):
            page = super().list_page(**kwargs)
            page['gaps'] = [{'code': 'historical_body_unavailable', 'count': 2},
                            {'code': 'revision_history_incomplete', 'count': None}]
            return page
    if interrupt:
        sync._ack_source_batch = lambda *args: os._exit(77)
    return sync.sync_sources(config(root), selections=(selection(GapAdapter(2)),),
                             cutoff=CUTOFF, curator=Curator())


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

    def relationship_fetch(self, relationships):
        fetch = Adapter.fetch
        self.adapter.fetch = lambda ref: dict(fetch(self.adapter, ref), **relationships)

    def published_card(self):
        pub = next((self.root / 'vault').glob('Knowledge-*.md'))
        return curation.parse_publication(pub.read_bytes())['batch']['cards'][0]

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

    def test_explicit_addition_preserves_prior_checkpoints_and_repeat_reuses_content(self):
        self.run_sync()
        before = self.state()['sources']['synthetic']
        completed = {path.name: path.read_bytes() for path in
                     (self.root / 'state').glob('source-completed-*.json')}
        self.assertEqual(len(completed), 1)
        added = Adapter()
        added.rows[0]['container'] = 'second'
        selected = (selection(self.adapter),
                    sync.SourceSelection('added', 'slack', 'workspace-fixture', 'second', added))
        process = sync._sync_source
        admissions = []

        def observe_admission(*args):
            if not admissions:
                durable = self.state()['sources']
                self.assertEqual(durable['synthetic'], before)
                self.assertEqual(durable['added'], {
                    'binding': {'kind': 'slack', 'account': 'workspace-fixture', 'container': 'second',
                                'route_sha256': None, 'source_sha256': None},
                    'cutoff': CUTOFF, 'phase': 'backfill', 'cursor': None, 'change_cursor': None,
                    'pending': None, 'inflight': None, 'failed': False, 'page_count': 0, 'gaps': []})
                for name, raw in completed.items():
                    self.assertEqual((self.root / 'state' / name).read_bytes(), raw)
                admissions.append(True)
            return process(*args)

        with patch.object(sync, '_sync_source', side_effect=observe_admission):
            result = self.run_sync(selections=selected)
        self.assertEqual(admissions, [True])
        self.assertEqual(result['sources']['synthetic']['covered_count'], 1)
        self.assertEqual(result['sources']['added']['covered_count'], 1)
        self.assertEqual(self.curator.calls, 2)
        self.adapter.change_rows = self.adapter.rows
        added.change_rows = added.rows
        self.run_sync(selections=selected)
        self.assertEqual(self.curator.calls, 2)
        for name, raw in completed.items():
            self.assertEqual((self.root / 'state' / name).read_bytes(), raw)

    def test_addition_cannot_omit_mutate_or_change_prior_cutoff(self):
        self.run_sync()
        before = (self.root / 'state' / sync.SOURCE_STATE).read_bytes()
        added = selection(Adapter(), 'added')
        original = selection(self.adapter)
        cases = [(added,)]
        for field, value in (('kind', 'backstage'), ('account', 'other'), ('container', 'other'),
                             ('route_sha256', 'a' * 64), ('source_sha256', 'b' * 64)):
            changed = dict(vars(original), **{field: value})
            cases.append((sync.SourceSelection(**changed), added))
        for selected in cases:
            with self.subTest(selections=selected), patch.object(sync, '_sync_source') as process:
                with self.assertRaises(sync.SyncError):
                    self.run_sync(selections=selected)
                process.assert_not_called()
                self.assertEqual((self.root / 'state' / sync.SOURCE_STATE).read_bytes(), before)
        with patch.object(sync, '_sync_source') as process:
            with self.assertRaises(sync.SyncError):
                sync.sync_sources(config(self.root), selections=(original, added),
                                  cutoff='2026-10-08T12:00:00Z', curator=self.curator)
            process.assert_not_called()
        self.assertEqual((self.root / 'state' / sync.SOURCE_STATE).read_bytes(), before)
        self.assertEqual(self.curator.calls, 1)

    def test_addition_refuses_malformed_prior_state_without_repair(self):
        self.run_sync()
        valid = self.state()
        path = self.root / 'state' / sync.SOURCE_STATE
        for field, value in (('page_count', -1), ('pending', {'page': {}, 'checkpoint': None, 'offset': 0}),
                             ('inflight', {'references': [], 'end': 1}), ('gaps', [{'code': 'retention_gap', 'count': -1}])):
            malformed = json.loads(json.dumps(valid))
            malformed['sources']['synthetic'][field] = value
            raw = json.dumps(malformed).encode()
            path.write_bytes(raw)
            with self.subTest(field=field), patch.object(sync, '_sync_source') as process:
                with self.assertRaises(sync.SyncError):
                    self.run_sync(selections=(selection(self.adapter), selection(Adapter(), 'added')))
                process.assert_not_called()
                self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.curator.calls, 1)

    def test_addition_refuses_corrupt_completed_revision_before_admission(self):
        self.run_sync()
        before = (self.root / 'state' / sync.SOURCE_STATE).read_bytes()
        completed = next((self.root / 'state').glob('source-completed-*.json'))
        completed.write_bytes(b'{"raw_body":"invalid checkpoint"}')
        with patch.object(sync, '_sync_source') as process:
            with self.assertRaises(sync.SyncError):
                self.run_sync(selections=(selection(self.adapter), selection(Adapter(), 'added')))
            process.assert_not_called()
        self.assertEqual((self.root / 'state' / sync.SOURCE_STATE).read_bytes(), before)
        self.assertEqual(completed.read_bytes(), b'{"raw_body":"invalid checkpoint"}')

    def test_addition_progresses_without_replaying_prior_uncertain_dispatch(self):
        self.interrupt('uncertain_send')
        before = self.state()['sources']['synthetic']
        added = Adapter()
        added.rows[0]['container'] = 'second'
        selected = (selection(self.adapter),
                    sync.SourceSelection('added', 'slack', 'workspace-fixture', 'second', added))
        with patch.object(self.adapter, 'fetch') as old_fetch:
            result = self.run_sync(selections=selected)
            old_fetch.assert_not_called()
        self.assertEqual(self.state()['sources']['synthetic'], before)
        self.assertEqual(result['sources']['synthetic']['state'], 'uncertain')
        self.assertEqual(result['sources']['synthetic']['covered_count'], 0)
        self.assertEqual(result['sources']['added']['covered_count'], 1)
        self.assertEqual(self.curator.calls, 1)
        self.run_sync(selections=selected)
        self.assertEqual(self.curator.calls, 1)
        self.assertEqual(self.state()['sources']['synthetic'], before)

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

    def test_changed_relationship_recurates_stable_parent_and_preserves_body_hash(self):
        for label in ('dependencies', 'supersedes'):
            for revision in ('revision-2', None):
                with self.subTest(label=label, revision=revision), tempfile.TemporaryDirectory() as temporary:
                    self.root = Path(temporary)
                    self.adapter, self.curator = Adapter(), Curator()
                    self.adapter.rows[0]['revision'] = revision
                    parent = self.adapter.fetch(self.adapter.rows[0])['reference']
                    prior = dict(parent, revision='prior-1', content_hash='sha256:' + 'a' * 64)
                    if label == 'dependencies':
                        prior.update(item_id='target', citation='https://example.invalid/target')
                    self.relationship_fetch({label: [prior]})
                    self.run_sync()
                    old_publication = next((self.root / 'vault').glob('Knowledge-*.md')).read_bytes()
                    changed = dict(prior, revision='prior-2', content_hash='sha256:' + 'b' * 64)
                    self.relationship_fetch({label: [changed]})
                    self.adapter.change_rows = self.adapter.rows
                    result = self.run_sync()
                    self.assertEqual(self.curator.calls, 2)
                    self.assertEqual(result['sources']['synthetic']['state'], 'ready')
                    self.assertEqual(self.published_card()[label], [changed])
                    self.assertEqual(self.published_card()['source'], parent)
                    self.assertNotEqual(next((self.root / 'vault').glob('Knowledge-*.md')).read_bytes(), old_publication)

    def test_relationship_observation_and_order_changes_need_zero_curation(self):
        parent = self.adapter.fetch(self.adapter.rows[0])['reference']
        dependencies = [dict(parent, item_id=item, citation=f'https://example.invalid/{item}')
                        for item in ('target-a', 'target-b')]
        supersedes = [dict(parent, revision='prior-1', content_hash='sha256:' + 'a' * 64)]
        self.relationship_fetch({'dependencies': dependencies, 'supersedes': supersedes})
        self.run_sync()
        before = self.published_card()
        self.relationship_fetch({'dependencies': [dict(ref, observed_at='2026-10-07T13:00:00Z')
                                                  for ref in reversed(dependencies)],
                                 'supersedes': [dict(supersedes[0], observed_at='2026-10-07T13:00:00Z')]})
        self.adapter.change_rows = [dict(self.adapter.rows[0], observed_at='2026-10-07T13:00:00Z')]
        self.run_sync()
        self.assertEqual(self.curator.calls, 1)
        self.assertEqual(self.published_card(), before)

    def test_dependency_only_uncertain_curation_cannot_ack_old_parent_checkpoint(self):
        self.run_sync()
        parent = self.published_card()['source']
        dependency = dict(parent, item_id='target', citation='https://example.invalid/target')
        self.relationship_fetch({'dependencies': [dependency]})
        self.adapter.change_rows = self.adapter.rows
        self.curator.fail = True
        self.assertEqual(self.run_sync()['sources']['synthetic']['state'], 'uncertain')
        fenced = self.state()
        self.curator.fail = False
        for _ in range(2):
            report = self.run_sync()['sources']['synthetic']
            self.assertEqual(report['state'], 'uncertain')
            self.assertIsNone(report['change_cursor'])
            self.assertEqual(self.state(), fenced)
        self.assertEqual(self.curator.calls, 2)
        self.assertEqual(self.published_card()['dependencies'], [])
        for path in (self.root / 'state').iterdir():
            if path.is_file():
                self.assertNotIn(RAW, path.read_bytes())

    def test_dependency_only_publication_before_ack_recovers_exact_relationships(self):
        self.run_sync()
        parent = self.published_card()['source']
        dependency = dict(parent, item_id='target', citation='https://example.invalid/target')
        self.relationship_fetch({'dependencies': [dependency]})
        self.adapter.change_rows = self.adapter.rows
        with patch.object(sync, '_ack_source_batch', side_effect=RuntimeError('synthetic ack interruption')):
            self.assertEqual(self.run_sync()['sources']['synthetic']['state'], 'uncertain')
        self.assertEqual(self.published_card()['dependencies'], [dependency])
        for _ in range(2):
            report = self.run_sync()['sources']['synthetic']
            self.assertEqual(report['state'], 'ready')
            self.assertEqual(report['change_cursor'], 'checkpoint-2')
        self.assertEqual(self.curator.calls, 2)
        self.assertIsNone(self.state()['sources']['synthetic']['inflight'])

    def test_dependency_only_process_restart_checks_durable_relationship_fence(self):
        for point in ('uncertain_send', 'after_publication'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary)
                self.adapter, self.curator = Adapter(), Curator()
                self.run_sync()
                script = 'import sys; from pathlib import Path; from test_source_sync import relationship_interrupt; relationship_interrupt(Path(sys.argv[1]), sys.argv[2])'
                interrupted = subprocess.run([sys.executable, '-B', '-c', script, str(self.root), point],
                                             cwd=Path(__file__).parent, capture_output=True)
                self.assertEqual(interrupted.returncode, 77, interrupted.stderr.decode())
                fence = self.state()['sources']['synthetic']['inflight']
                self.assertEqual(fence['relationships'][0]['dependencies'][0]['item_id'], 'target')
                parent = self.published_card()['source']
                dependency = dict(parent, item_id='target', citation='https://example.invalid/target')
                self.relationship_fetch({'dependencies': [dependency]})
                self.adapter.change_rows = self.adapter.rows
                for _ in range(2):
                    report = self.run_sync()['sources']['synthetic']
                    self.assertEqual(report['state'], 'uncertain' if point == 'uncertain_send' else 'ready')
                self.assertEqual(self.curator.calls, 1)
                if point == 'uncertain_send':
                    self.assertEqual(self.state()['sources']['synthetic']['inflight'], fence)
                    self.assertEqual(self.published_card()['dependencies'], [])
                else:
                    self.assertIsNone(self.state()['sources']['synthetic']['inflight'])
                    self.assertEqual(self.published_card()['dependencies'], [dependency])
                for path in self.root.rglob('*'):
                    if path.is_file():
                        self.assertNotIn(RAW, path.read_bytes())

    def test_legacy_parent_only_publication_fence_still_recovers(self):
        self.interrupt('after_publication')
        state = self.state()
        del state['sources']['synthetic']['inflight']['relationships']
        with sync._directory(self.root / 'state') as directory:
            sync._save_state(directory, state, sync.SOURCE_STATE)
        result = self.run_sync()
        self.assertEqual(result['sources']['synthetic']['state'], 'ready')
        self.assertEqual(self.curator.calls, 0)

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

    def test_equal_count_pages_aggregate_once_through_publication_ack_restart(self):
        first = counted_gap_restart(self.root)
        self.assertEqual(first['sources']['synthetic']['gaps'], [
            {'code': 'historical_body_unavailable', 'count': 2},
            {'code': 'revision_history_incomplete', 'count': None}])
        script = 'from pathlib import Path; from test_source_sync import counted_gap_restart; counted_gap_restart(Path(__import__("sys").argv[1]), True)'
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.root)],
                                cwd=Path(__file__).parent, capture_output=True)
        self.assertEqual(result.returncode, 77, result.stderr.decode())
        self.assertEqual(self.state()['sources']['synthetic']['page_count'], 1)
        self.assertEqual(self.state()['sources']['synthetic']['gaps'][0]['count'], 2)
        for _ in range(2):
            report = counted_gap_restart(self.root)
            self.assertEqual(report['sources']['synthetic']['gaps'], [
                {'code': 'historical_body_unavailable', 'count': 4},
                {'code': 'revision_history_incomplete', 'count': None}])
            self.assertEqual(report['sources']['synthetic']['covered_count'], 2)
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(RAW, path.read_bytes())

    def test_unchanged_change_checkpoint_does_not_recount_acknowledged_page(self):
        self.run_sync()
        original = self.adapter.changes
        def changes(**kwargs):
            result = original(**kwargs)
            result['page']['gaps'] = [{'code': 'historical_body_unavailable', 'count': 2}]
            return result
        self.adapter.changes = changes
        for _ in range(2):
            self.assertEqual(self.run_sync()['sources']['synthetic']['gaps'], [
                {'code': 'historical_body_unavailable', 'count': 2}])

    def test_more_than_32_counted_gap_pages_resume_after_process_restart(self):
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
                         [{'code': 'retention_gap', 'count': 820}])
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
