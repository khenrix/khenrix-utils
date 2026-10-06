"""Real publication/topic state; source and provider boundaries are synthetic."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sync
from test_sync import Source, Curator


class ObservedSource(Source):
    def __init__(self, paths, commit='1' * 40):
        super().__init__(paths, commit)
        self.snapshots, self.reads = [], []

    def snapshot(self, repository, ref=None):
        self.snapshots.append((repository, ref))
        return super().snapshot(repository, ref)

    def read_pages(self, snapshot, paths):
        self.reads.append(tuple(paths))
        return super().read_pages(snapshot, paths)


class SettledCurator(Curator):
    last_evidence = {'outcome': 'settled'}


class TopicSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = sync.SyncConfig('example/team-wiki', self.root / 'vault',
            self.root / 'snapshots', self.root / 'state', 'example-project', 'eu', 'example-model')
        self.paths = ['knowledge/alpha.md', 'knowledge/beta.md', 'knowledge/unindexed.md']
        self.source = ObservedSource(self.paths)

    def envelope(self, topics=None, repository='example/team-wiki'):
        return {'schema_version': 'workvault-sync-topics-v1', 'repository': repository,
            'topics': {'shipping': ['knowledge/alpha.md'], 'empty': []} if topics is None else topics}

    def topics(self, value=None):
        parser = getattr(sync, 'parse_topics', None)
        self.assertTrue(callable(parser), 'versioned topic mapping ingress is missing')
        return parser(json.dumps(self.envelope() if value is None else value).encode())

    def run_topic(self, topic='shipping', *, source=None, curator=None, topics=None, ref=None):
        try:
            return sync.sync_all(self.config, source=self.source if source is None else source,
                curator=SettledCurator() if curator is None else curator, topic=topic,
                topics=self.topics() if topics is None else topics, ref=ref)
        except TypeError as error:
            self.fail('sync lacks optional topic selection: ' + str(error))

    def files(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob('*') if path.is_file()}

    def note(self, path):
        return (self.config.vault / sync.publication_name(self.config.repository, path)).read_bytes()

    def config_file(self):
        parent = self.root / 'configuration'
        parent.mkdir(mode=0o700)
        path = parent / 'sync.json'
        path.write_text(json.dumps({'schema_version': 'workvault-sync-config-v1',
            'repository': 'example/team-wiki', 'vault': str(self.config.vault),
            'snapshots': str(self.config.snapshots), 'state': str(self.config.state),
            'vertex': {'project': 'example-project', 'location': 'eu', 'model': 'example-model'}}))
        path.chmod(0o600)
        mapping = parent / 'topics.json'
        mapping.write_text(json.dumps(self.envelope()))
        mapping.chmod(0o600)
        return path, mapping

    def run_main(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('collector.GitHubSource', return_value=self.source), \
                patch('collector.VertexCurator', return_value=SettledCurator()), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = sync.main(argv)
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_real_cli_optional_topic_publishes_only_declared_page(self):
        config, mapping = self.config_file()
        code, stdout, stderr = self.run_main(['sync', 'shipping', '--config', str(config),
            '--topic-map', str(mapping)])
        self.assertEqual(code, 0, stderr)
        result = json.loads(stdout)
        self.assertEqual(result['covered_paths'], ['knowledge/alpha.md'])
        self.assertFalse(result['full_source_coverage'])
        self.assertTrue(result['topic_selection']['full_topic_coverage'])
        self.assertEqual(self.source.reads, [('knowledge/alpha.md',)])
        self.assertEqual(len(list(self.config.vault.glob('Knowledge-*.md'))), 1)

    def test_subset_preserves_full_inventory_and_uncovered_accounting(self):
        result = self.run_topic(ref='explicit-ref')
        self.assertEqual(result['target_commit'], '1' * 40)
        self.assertEqual(result['covered_paths'], ['knowledge/alpha.md'])
        self.assertEqual(result['uncovered_paths'], ['knowledge/beta.md', 'knowledge/unindexed.md'])
        self.assertEqual(result['not_attempted_paths'], ['knowledge/beta.md', 'knowledge/unindexed.md'])
        self.assertEqual(result['index_gaps'], ['knowledge/unindexed.md'])
        self.assertFalse(result['full_source_coverage'])
        self.assertEqual(self.source.snapshots, [('example/team-wiki', 'explicit-ref')])
        self.assertEqual(sync.sync_status(self.config),
            {key: value for key, value in result.items() if key != 'topic_selection'})
        state = json.loads((self.config.state / 'source-state.json').read_bytes())
        self.assertEqual(state['discovered_paths'], self.paths)
        self.assertEqual(state['tracked_paths'], self.paths)

    def test_refresh_keeps_nonselected_cards_and_checkpoints_byte_identical(self):
        sync.sync_all(self.config, source=self.source, curator=SettledCurator())
        other = self.note('knowledge/beta.md'), self.note('knowledge/unindexed.md')
        old = self.note('knowledge/alpha.md')
        fresh = ObservedSource(self.paths, '2' * 40)
        result = self.run_topic(source=fresh)
        self.assertNotEqual(self.note('knowledge/alpha.md'), old)
        self.assertEqual((self.note('knowledge/beta.md'), self.note('knowledge/unindexed.md')), other)
        self.assertEqual(result['stale_paths'], ['knowledge/beta.md', 'knowledge/unindexed.md'])
        self.assertFalse(result['full_source_coverage'])
        self.assertEqual(fresh.reads, [('knowledge/alpha.md',)])

    def test_topic_success_does_not_recurate_or_snapshot_same_revision(self):
        self.run_topic()
        before = self.files()
        source = ObservedSource(self.paths)
        result = self.run_topic(source=source, curator=Curator(True))
        self.assertEqual(source.reads, [])
        self.assertTrue(result['topic_selection']['full_topic_coverage'])
        self.assertEqual(self.files(), before)

    def test_omitted_topic_still_syncs_all_uncovered_paths_in_five_page_batches(self):
        paths = ['knowledge/page-' + str(i) + '.md' for i in range(6)]
        source = ObservedSource(paths)
        result = sync.sync_all(self.config, source=source, curator=SettledCurator())
        self.assertTrue(result['full_source_coverage'])
        self.assertEqual(source.reads, [tuple(paths[:5]), tuple(paths[5:])])
        self.assertNotIn('topic_selection', result)

    def test_topic_itself_uses_bounded_batches_without_reading_other_pages(self):
        paths = ['knowledge/page-' + str(i) + '.md' for i in range(8)]
        source = ObservedSource(paths)
        result = self.run_topic(source=source, topics=self.topics(self.envelope({'shipping': paths[:6]})))
        self.assertEqual(source.reads, [tuple(paths[:5]), tuple(paths[5:6])])
        self.assertEqual(result['uncovered_paths'], paths[6:])
        self.assertTrue(result['topic_selection']['full_topic_coverage'])
        self.assertFalse(result['full_source_coverage'])

    def test_declared_empty_topic_reports_no_match_without_state_or_body_changes(self):
        sync.sync_all(self.config, source=self.source, curator=SettledCurator())
        before = self.files()
        fresh = ObservedSource(self.paths, '2' * 40)
        result = self.run_topic('empty', source=fresh)
        self.assertEqual(result['topic_selection']['outcome'], 'no_match')
        self.assertEqual(result['topic_selection']['requested_paths'], [])
        self.assertEqual(result['topic_selection']['target_commit'], '2' * 40)
        self.assertFalse(result['topic_selection']['full_topic_coverage'])
        self.assertEqual(fresh.reads, [])
        self.assertEqual(result['target_commit'], '1' * 40)
        self.assertEqual(self.files(), before)

    def test_unknown_topic_is_exact_and_never_falls_back_to_collection(self):
        for topic in ('unknown', 'Shipping', '', ' shipping', 'shipping\n'):
            with self.subTest(topic=repr(topic)), self.assertRaises(sync.SyncError):
                self.run_topic(topic)
        self.assertEqual(self.source.snapshots, [])
        self.assertEqual(self.source.reads, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_topic_requires_mapping_before_any_source_access(self):
        try:
            with self.assertRaises(sync.SyncError):
                sync.sync_all(self.config, source=self.source, curator=SettledCurator(), topic='shipping')
        except TypeError as error:
            self.fail('missing topic admission: ' + str(error))
        self.assertEqual(self.source.snapshots, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_mapping_repository_cannot_extend_configured_source_authority(self):
        topics = self.topics(self.envelope(repository='other/team-wiki'))
        with self.assertRaises(sync.SyncError):
            self.run_topic(topics=topics)
        self.assertEqual(self.source.snapshots, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_map_paths_must_belong_to_exact_current_inventory_even_for_other_topics(self):
        sync.sync_all(self.config, source=self.source, curator=SettledCurator())
        before = self.files()
        topics = self.topics(self.envelope({'shipping': ['knowledge/alpha.md'],
            'other': ['knowledge/missing.md']}))
        with self.assertRaises(sync.SyncError):
            self.run_topic(topics=topics)
        self.assertEqual(self.files(), before)

    def test_versioned_map_rejects_duplicate_unknown_unsafe_or_unbounded_fields(self):
        parser = getattr(sync, 'parse_topics', None)
        self.assertTrue(callable(parser), 'strict versioned map parsing is missing')
        good = json.dumps(self.envelope())
        bad = [good.replace('workvault-sync-topics-v1', 'unknown'),
            good.replace('"repository":', '"repository":"other/repo", "repository":'),
            good.replace('"empty": []', '"empty": [], "empty": []'),
            json.dumps(dict(self.envelope(), raw='RAW_CANARY')),
            json.dumps(self.envelope({'shipping': ['knowledge/../outside.md']})),
            json.dumps(self.envelope({'shipping': ['sources/raw.md']})),
            json.dumps(self.envelope({'shipping': ['knowledge/alpha.md', 'knowledge/alpha.md']})),
            json.dumps(self.envelope({'shipping': 'knowledge/alpha.md'})),
            json.dumps(self.envelope({' shipping': ['knowledge/alpha.md']})),
            json.dumps(self.envelope({'shipping\n': []})),
            ' ' * (1024 * 1024 + 1)]
        for value in bad:
            with self.subTest(bytes=len(value)), self.assertRaises(sync.SyncError):
                parser(value.encode())

    def test_parsed_mapping_cannot_be_changed_by_mutating_caller_envelope(self):
        value = self.envelope()
        topics = self.topics(value)
        value['topics']['shipping'].append('knowledge/beta.md')
        result = self.run_topic(topics=topics)
        self.assertEqual(result['covered_paths'], ['knowledge/alpha.md'])
        self.assertEqual(self.source.reads, [('knowledge/alpha.md',)])

    def test_failed_other_topic_is_preserved_when_selected_topic_succeeds(self):
        topics = self.topics(self.envelope({'shipping': ['knowledge/alpha.md'],
            'billing': ['knowledge/beta.md']}))
        first = self.run_topic('billing', topics=topics, curator=SettledCurator(True))
        self.assertEqual(first['failed_paths'], ['knowledge/beta.md'])
        second = self.run_topic(topics=topics)
        self.assertEqual(second['failed_paths'], ['knowledge/beta.md'])
        self.assertEqual(second['not_attempted_paths'], ['knowledge/unindexed.md'])
        self.assertEqual(second['uncertain_paths'], [])
        self.assertEqual(second['covered_paths'], ['knowledge/alpha.md'])

    def test_failed_selected_topic_merges_failure_without_dropping_other_failures(self):
        topics = self.topics(self.envelope({'shipping': ['knowledge/alpha.md'],
            'billing': ['knowledge/beta.md']}))
        self.run_topic('billing', topics=topics, curator=SettledCurator(True))
        second = self.run_topic(topics=topics, curator=SettledCurator(True))
        self.assertEqual(second['failed_paths'], ['knowledge/alpha.md', 'knowledge/beta.md'])
        recovered = self.run_topic(topics=topics)
        self.assertEqual(recovered['failed_paths'], ['knowledge/beta.md'])

    def test_uncertain_other_topic_is_fenced_before_even_new_inventory(self):
        topics = self.topics(self.envelope({'shipping': ['knowledge/alpha.md'],
            'billing': ['knowledge/beta.md'], 'empty': []}))
        first = self.run_topic('billing', topics=topics, curator=Curator(True))
        self.assertEqual(first['uncertain_paths'], ['knowledge/beta.md'])
        before = self.files()
        for topic in ('shipping', 'empty'):
            source = ObservedSource(self.paths, '2' * 40)
            with self.assertRaises(sync.SyncError):
                self.run_topic(topic, topics=topics, source=source)
            self.assertEqual(source.snapshots, [])
            self.assertEqual(source.reads, [])
            self.assertEqual(self.files(), before)

    def test_topic_publisher_failure_restores_batch_and_preserves_nonselected_note(self):
        sync.sync_all(self.config, source=self.source, curator=SettledCurator())
        before = {path: self.note(path) for path in self.paths}
        topics = self.topics(self.envelope({'shipping': self.paths[:2]}))
        replace = __import__('os').replace
        second = sync.publication_name(self.config.repository, 'knowledge/beta.md')

        def fault(source, destination, *args, **kwargs):
            if destination == second:
                raise OSError('synthetic publisher fault')
            return replace(source, destination, *args, **kwargs)

        with patch('os.replace', side_effect=fault):
            result = self.run_topic(source=ObservedSource(self.paths, '2' * 40), topics=topics)
        self.assertEqual({path: self.note(path) for path in self.paths}, before)
        self.assertEqual(result['failed_paths'], self.paths[:2])
        self.assertFalse(result['topic_selection']['full_topic_coverage'])
        self.assertEqual(result['uncertain_paths'], [])
        for raw in self.files().values():
            self.assertNotIn(b'RAW_SOURCE_CANARY_NEVER_STORED', raw)

    def test_cli_topic_is_sync_only_and_no_match_is_not_success(self):
        config, mapping = self.config_file()
        for action, topic in (('status', 'shipping'), ('maintain', 'shipping'),
                ('inventory', 'shipping'), ('sync', 'empty')):
            code, stdout, _ = self.run_main([action, topic, '--config', str(config),
                '--topic-map', str(mapping)])
            self.assertNotEqual(code, 0)
            if action == 'sync':
                self.assertTrue(stdout, 'no-match must return explicit topic metadata')
                self.assertEqual(json.loads(stdout)['topic_selection']['outcome'], 'no_match')
        self.assertEqual(self.source.reads, [])
        self.assertFalse(self.config.vault.exists())

    def test_cli_mapping_symlink_or_broad_permissions_refuses_without_source_reads(self):
        config, mapping = self.config_file()
        mapping.chmod(0o644)
        code, _, _ = self.run_main(['sync', 'shipping', '--config', str(config), '--topic-map', str(mapping)])
        self.assertNotEqual(code, 0)
        mapping.chmod(0o600)
        link = mapping.with_name('linked.json')
        link.symlink_to(mapping)
        code, _, _ = self.run_main(['sync', 'shipping', '--config', str(config), '--topic-map', str(link)])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.source.snapshots, [])
        self.assertEqual(self.source.reads, [])


if __name__ == '__main__':
    unittest.main()
