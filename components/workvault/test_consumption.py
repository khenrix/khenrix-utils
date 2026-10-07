"""Offline consumption through real canonical storage and lifecycle fences."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import curation
import sync
import lifecycle
import batch
import retrieval
import workvault
from batch import publication_name, source_publication_name
from test_retrieval import publication, reference, native_publication
from test_source_sync import Adapter, Curator, CUTOFF, config, selection
from test_lifecycle import event


class ConsumptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = sync.SyncConfig('example/wiki', self.root / 'vault',
            self.root / 'history', self.root / 'state', 'example-project', 'eu', 'example-model')
        for path in (self.config.vault, self.config.state):
            path.mkdir(mode=0o700)
        self.paths = ['knowledge/item-1.md', 'knowledge/item-2.md']
        self.state(self.paths)
        self.put(publication())
        self.put(publication(2, commit='2' * 40))
        self.config_file = self.root / 'sync.json'
        self.config_file.write_text(json.dumps({'schema_version': 'workvault-sync-config-v1',
            'repository': self.config.repository, 'vault': str(self.config.vault),
            'snapshots': str(self.config.snapshots), 'state': str(self.config.state),
            'vertex': {'project': self.config.project, 'location': 'eu', 'model': self.config.model}}))
        self.config_file.chmod(0o600)

    def state(self, paths):
        target = self.config.state / sync.STATE
        target.write_text(json.dumps({'schema_version': sync.SCHEMA,
            'repository': self.config.repository, 'target_commit': '2' * 40,
            'discovered_paths': paths, 'indexed_paths': paths, 'tracked_paths': self.paths if hasattr(self, 'paths') else paths,
            'failed_paths': [], 'uncertain_paths': [], 'provider_evidence': {'outcome': 'not_started'},
            'broken_index_references': []}))
        target.chmod(0o600)

    def put(self, record):
        batch = record['batch']
        name = (source_publication_name(batch['cards'][0]['source']) if 'source' in batch['cards'][0]
                else publication_name(batch['repository'], batch['cards'][0]['path']))
        path = self.config.vault / name
        path.write_bytes(curation.render_publication(batch, record['published_at']))
        path.chmod(0o600)
        return path

    def image(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def invoke(self, action, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), \
                patch('collector.GitHubSource', side_effect=AssertionError('upstream forbidden')), \
                patch('collector.VertexCurator', side_effect=AssertionError('models forbidden')):
            try:
                code = sync.main([action, '--config', str(self.config_file), *arguments])
            except SystemExit as error:
                code = error.code
        return code, json.loads(output.getvalue()) if output.getvalue() else None

    def test_real_cli_search_get_and_topic_are_read_only_and_preserve_mixed_revisions(self):
        before = self.image()
        code, rows = self.invoke('search', '--limit', '2', '--', 'carrier capacity')
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['reference']['commit'] for row in rows}, {'1' * 40, '2' * 40})
        self.assertTrue(all('not guaranteed' in row['card']['summary'] for row in rows))
        code, row = self.invoke('get', '--', json.dumps(reference()))
        self.assertEqual(code, 0)
        self.assertEqual(row['reference'], reference())
        code, row = self.invoke('get', '--', self.paths[1])
        self.assertEqual(row['reference'], reference(2, '2' * 40))
        mapping = self.root / 'topics.json'
        mapping.write_text(json.dumps({'schema_version': 'workvault-sync-topics-v1',
            'repository': self.config.repository, 'topics': {'shipping': self.paths[:1]}}))
        mapping.chmod(0o600)
        code, rows = self.invoke('topic', '--topic-map', str(mapping), '--', 'shipping')
        self.assertEqual(code, 0)
        self.assertEqual([row['reference'] for row in rows], [reference()])
        after = self.image()
        del after['topics.json']
        self.assertEqual(after, before)

    def test_follow_is_one_origin_bound_exact_curated_reference(self):
        origin = publication()
        origin['batch']['cards'][0]['source_links'] = [
            'https://github.com/example/wiki/blob/' + '2' * 40 + '/knowledge/item-2.md']
        self.put(origin)
        before = self.image()
        code, row = self.invoke('follow', '--origin', json.dumps(reference()), '--', json.dumps(reference(2, '2' * 40)))
        self.assertEqual(code, 0)
        self.assertEqual(row['reference'], reference(2, '2' * 40))
        for target in (json.dumps(reference(2)), 'https://example.invalid/arbitrary', '../Human.md'):
            self.assertNotEqual(self.invoke('follow', '--origin', json.dumps(reference()), '--', target)[0], 0)
        self.assertEqual(self.image(), before)

    def test_removed_v1_is_not_searchable_and_human_notes_are_preserved(self):
        self.state(self.paths[:1])
        human = self.config.vault / 'Human.md'
        human.write_text('PRIVATE_HUMAN_CANARY')
        before = self.image()
        code, rows = self.invoke('search', '--', 'carrier')
        self.assertEqual(code, 0)
        self.assertEqual([row['reference'] for row in rows], [reference()])
        self.assertEqual(self.invoke('get', '--', self.paths[1]), (0, None))
        self.assertEqual(self.image(), before)

    def test_v2_selection_and_retirement_are_checked_and_maintenance_is_honest(self):
        adapter = Adapter()
        sync.sync_sources(config(self.root), selections=(selection(adapter),), cutoff=CUTOFF, curator=Curator())
        code, rows = self.invoke('search', '--', 'approval')
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        ref = rows[0]['reference']
        code, report = self.invoke('maintain')
        self.assertEqual(code, 0)
        self.assertEqual(report['curated']['uncertain_card_count'], 3)
        self.assertEqual(report['curated']['conflict_check'], 'unavailable_unstructured_cards')
        lifecycle.apply_source_lifecycle(config(self.root), selection(adapter), event(ref, 'retired'))
        before = self.image()
        self.assertEqual(self.invoke('search', '--', 'approval'), (0, []))
        self.assertEqual(self.image(), before)

    def test_storage_refuses_unsafe_paths_symlinks_pending_batches_and_changes(self):
        for argument in ('../Human.md', '/tmp/raw.md', 'sources/raw.md'):
            self.assertNotEqual(self.invoke('get', '--', argument)[0], 0)
        target = self.config.vault / publication_name(self.config.repository, self.paths[0])
        original = target.read_bytes()
        target.unlink()
        target.symlink_to(self.config.vault / publication_name(self.config.repository, self.paths[1]))
        self.assertNotEqual(self.invoke('search', '--', 'carrier')[0], 0)
        target.unlink()
        target.write_bytes(original)
        target.chmod(0o600)
        pending = self.config.vault / '.workvault-batch.json'
        pending.write_text('{}')
        pending.chmod(0o600)
        self.assertNotEqual(self.invoke('search', '--', 'carrier')[0], 0)

    def test_queries_refuse_unbounded_input_and_reads_never_open_for_writing(self):
        for options in (['--limit', '0', '--', 'carrier'], ['--limit', '6', '--', 'carrier'],
                ['--', 'x' * 513], ['--', ' '.join(str(i) for i in range(17))]):
            self.assertNotEqual(self.invoke('search', *options)[0], 0)
        before = self.image()
        original = os.open
        def readonly(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            return original(path, flags, *args, **kwargs)
        with patch('os.open', side_effect=readonly):
            self.assertEqual(self.invoke('search', '--', 'carrier')[0], 0)
        self.assertEqual(self.image(), before)

    def test_lifecycle_becomes_pending_during_search_without_initial_lock(self):
        self.assertFalse((self.config.vault / workvault.LOCK).exists())
        original = retrieval.CuratedReader.search
        def pending(reader, *args, **kwargs):
            result = original(reader, *args, **kwargs)
            value = {'schema_version': 'workvault-lifecycle-state-v2',
                'vault_identity': list(workvault._identity(self.config.vault.stat())),
                'snapshots_identity': [0, 0], 'blocked': {}, 'generations': {},
                'pending': {'files': [], 'artifacts': []}}
            target = self.config.vault / lifecycle.STATE
            target.write_text(json.dumps(value))
            target.chmod(0o600)
            return result
        with patch.object(retrieval.CuratedReader, 'search', pending):
            self.assertEqual(self.invoke('search', '--', 'carrier'), (1, None))

    def test_lifecycle_transition_during_publication_recheck_refuses(self):
        original = batch._record
        calls = 0
        def pending(directory, name):
            nonlocal calls
            result = original(directory, name)
            if name.startswith('Knowledge-'):
                calls += 1
                if calls == 3:
                    value = {'schema_version': 'workvault-lifecycle-state-v2',
                        'vault_identity': list(workvault._identity(self.config.vault.stat())),
                        'snapshots_identity': [0, 0], 'blocked': {}, 'generations': {},
                        'pending': {'files': [], 'artifacts': []}}
                    target = self.config.vault / lifecycle.STATE
                    target.write_text(json.dumps(value))
                    target.chmod(0o600)
            return result
        with patch('batch._record', side_effect=pending):
            self.assertEqual(self.invoke('search', '--', 'carrier'), (1, None))

    def test_new_or_replaced_publication_lock_during_search_refuses(self):
        lock = self.config.vault / workvault.LOCK
        for existing in (False, True):
            if existing:
                lock.write_bytes(b'')
                lock.chmod(0o600)
            original = retrieval.CuratedReader.search
            def changed(reader, *args, **kwargs):
                result = original(reader, *args, **kwargs)
                if lock.exists():
                    lock.rename(self.config.vault / 'detached-lock')
                lock.write_bytes(b'')
                lock.chmod(0o600)
                return result
            with patch.object(retrieval.CuratedReader, 'search', changed):
                self.assertEqual(self.invoke('search', '--', 'carrier'), (1, None))
            lock.unlink()

    def test_aggregate_budget_stops_reads_before_accumulating_the_corpus(self):
        self.paths.append('knowledge/item-3.md')
        self.state(self.paths)
        self.put(publication(3))
        per_checkpoint = len(curation._json(publication()).encode('utf-8'))
        original = batch._record
        publication_reads = []
        def observed(directory, name):
            if name.startswith('Knowledge-'):
                publication_reads.append(name)
            return original(directory, name)
        with patch('retrieval.MAX_COLLECTION_BYTES', per_checkpoint + 1), \
                patch('batch._record', side_effect=observed):
            self.assertEqual(self.invoke('search', '--', 'carrier'), (1, None))
        self.assertEqual(len(publication_reads), 2, 'budget refusal must precede the third publication read')

    def test_v1_status_and_all_retired_status_maintain_keep_curated_metadata(self):
        library = sync.sync_status(self.config)
        self.assertNotIn('curated', library)
        code, report = self.invoke('status')
        self.assertEqual(report['curated']['active_card_count'], 2)
        self.assertEqual(report['curated']['uncertain_card_count'], 2)
        self.assertEqual(report['curated']['conflict_check'], 'unavailable_unstructured_cards')
        self.state([])
        before = self.image()
        for command in ('status', 'maintain'):
            code, report = self.invoke(command)
            self.assertEqual(code, 0)
            self.assertEqual(report['curated']['active_card_count'], 0)
            self.assertEqual(report['curated']['retained_retired_publication_count'], 2)
            self.assertEqual(report['curated']['uncertain_card_count'], 0)
            self.assertEqual(report['curated']['draft_card_count'], 0)
            self.assertEqual(report['curated']['conflict_check'], 'unavailable_unstructured_cards')
        self.assertEqual(self.image(), before)

    def test_present_empty_or_malformed_state_is_not_treated_as_missing(self):
        for name in (sync.STATE, sync.SOURCE_STATE):
            target = self.config.state / name
            original = target.read_bytes() if target.exists() else None
            for raw in (b'', b'RAW_UNTRUSTED_STATE_CANARY'):
                target.write_bytes(raw)
                target.chmod(0o600)
                self.assertNotEqual(self.invoke('search', '--', 'carrier')[0], 0)
            if original is None:
                target.unlink()
            else:
                target.write_bytes(original)

    def test_legacy_aggregate_is_not_silently_omitted_from_the_corpus(self):
        record = publication()
        target = self.config.vault / 'Knowledge.md'
        target.write_bytes(curation.render_publication(record['batch'], record['published_at']))
        target.chmod(0o600)
        before = self.image()
        self.assertNotEqual(self.invoke('search', '--', 'carrier')[0], 0)
        self.assertEqual(self.image(), before)

    def test_source_state_changes_and_detached_roots_refuse_results(self):
        for scope in ('state', 'vault'):
            root = getattr(self.config, scope)
            detached = self.root / (scope + '-detached')
            original = batch._record
            changed = False
            def detach(*args):
                nonlocal changed
                result = original(*args)
                if not changed:
                    changed = True
                    root.rename(detached)
                    root.mkdir(mode=0o700)
                return result
            try:
                with patch('batch._record', side_effect=detach):
                    self.assertNotEqual(self.invoke('search', '--', 'carrier')[0], 0)
            finally:
                root.rmdir()
                detached.rename(root)
        original = sync._read
        calls = 0
        def changed_state(directory, name):
            nonlocal calls
            raw = original(directory, name)
            if name == sync.STATE:
                calls += 1
                if calls == 2:
                    return raw + b' '
            return raw
        with patch('sync._read', side_effect=changed_state):
            self.assertNotEqual(self.invoke('search', '--', 'carrier')[0], 0)

    def test_v2_unselected_binding_and_pending_lifecycle_refuse_content(self):
        adapter = Adapter()
        sync.sync_sources(config(self.root), selections=(selection(adapter),), cutoff=CUTOFF, curator=Curator())
        target = self.config.state / sync.SOURCE_STATE
        original = target.read_bytes()
        value = json.loads(original)
        value['sources']['synthetic']['binding']['container'] = 'different-selection'
        target.write_text(json.dumps(value))
        self.assertNotEqual(self.invoke('search', '--', 'approval')[0], 0)
        target.write_bytes(original)
        with patch('lifecycle.guard', side_effect=RuntimeError('pending lifecycle')):
            self.assertNotEqual(self.invoke('search', '--', 'approval')[0], 0)
        with patch('retrieval.MAX_PUBLICATIONS', 1):
            self.assertNotEqual(self.invoke('search', '--', 'approval')[0], 0)

    def test_complete_corpus_reaches_late_card_and_does_not_silently_truncate(self):
        selected = sync.SourceSelection('synthetic', 'jira', 'company', 'selected', None)
        target = self.config.state / sync.SOURCE_STATE
        target.write_text(json.dumps({'schema_version': 'workvault-multi-source-state-v2',
            'sources': {'synthetic': {'binding': sync._source_binding(selected), 'cutoff': CUTOFF,
                'phase': 'changes', 'cursor': None, 'change_cursor': None, 'pending': None,
                'inflight': None, 'failed': False, 'page_count': 1, 'gaps': []}}}))
        target.chmod(0o600)
        for i in range(1, 4098):
            record = native_publication(summary='Unique late needle.' if i == 4097 else 'Ordinary item.')
            record['batch']['cards'][0]['source'].update(item_id=str(i), citation=f'https://example.invalid/jira/{i}')
            self.put(record)
        code, rows = self.invoke('search', '--', 'needle')
        self.assertEqual(code, 0)
        self.assertEqual([row['reference']['item_id'] for row in rows], ['4097'])


if __name__ == '__main__':
    unittest.main()
