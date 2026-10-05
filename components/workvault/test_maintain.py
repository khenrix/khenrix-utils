"""Offline maintenance reports from synthetic, owned publication fixtures."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import curation
import sync


class MaintainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = sync.SyncConfig('example/team-wiki', self.root / 'vault',
            self.root / 'snapshots', self.root / 'state', 'example-project', 'eu', 'example-model')
        self.paths = ['knowledge/a.md', 'knowledge/b.md', 'knowledge/removed.md']
        self.config.state.mkdir(mode=0o700)
        self.config.vault.mkdir(mode=0o700)
        self.state = {'schema_version': sync.SCHEMA, 'repository': self.config.repository,
            'target_commit': '2' * 40, 'discovered_paths': self.paths[:2],
            'indexed_paths': self.paths[:2], 'tracked_paths': self.paths,
            'failed_paths': [], 'uncertain_paths': [],
            'provider_evidence': {'outcome': 'not_started'}, 'broken_index_references': []}
        self.save_state()
        for path in self.paths:
            self.note(path)

    def save_state(self):
        target = self.config.state / sync.STATE
        target.write_text(json.dumps(self.state))
        target.chmod(0o600)

    def note(self, path, *, commit='1' * 40, summary='CURATED_BODY_NOT_IN_REPORT'):
        batch = {'schema_version': curation.BATCH_SCHEMA, 'repository': self.config.repository,
            'commit': commit, 'inventory_complete': True, 'discovered_paths': self.paths,
            'indexed_paths': self.paths, 'requested_paths': [path],
            'cards': [{'path': path, 'title': 'Same title', 'summary': summary, 'details': '',
                'date': '2026-10-05', 'draft': False, 'uncertainty': [],
                'source_links': [], 'cross_references': []}]}
        target = self.config.vault / sync.publication_name(self.config.repository, path)
        target.write_bytes(curation.render_publication(batch, '2026-10-05T00:00:00Z'))
        target.chmod(0o600)
        return target

    def image(self):
        return {str(p.relative_to(self.root)): (p.stat().st_mode, p.read_bytes())
                for p in self.root.rglob('*') if p.is_file()}

    def test_precise_offline_report_is_content_free_and_read_only(self):
        before = self.image()
        original_open = os.open

        def read_only_open(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            return original_open(path, flags, *args, **kwargs)

        with patch('sync.sync_all', side_effect=AssertionError('sync forbidden')), \
             patch('sync.publish_curated_batch', side_effect=AssertionError('publish forbidden')), \
             patch('sync._save_state', side_effect=AssertionError('state writes forbidden')), \
             patch('os.open', side_effect=read_only_open), \
             patch('subprocess.Popen', side_effect=AssertionError('child forbidden')), \
             patch('collector.GitHubSource', side_effect=AssertionError('source forbidden')), \
             patch('collector.VertexCurator', side_effect=AssertionError('provider forbidden')):
            result = sync.maintain_status(self.config)
        self.assertEqual(result['stale_paths'], self.paths[:2])
        self.assertEqual(result['retained_removed_paths'], self.paths[2:])
        self.assertEqual(result['missing_paths'], [])
        self.assertEqual(result['duplicate_payload_groups'], [self.paths[:2]])
        self.assertEqual(result['conflict_check'], 'unavailable_unstructured_cards')
        self.assertNotIn('CURATED_BODY', json.dumps(result))
        self.assertNotIn('Same title', json.dumps(result))
        self.assertEqual(self.image(), before)
        self.assertFalse(self.config.snapshots.exists())

    def test_current_commit_missing_and_distinct_content_are_not_overstated(self):
        self.note(self.paths[0], commit='2' * 40)
        self.note(self.paths[1], summary='A different statement; not necessarily a contradiction.')
        (self.config.vault / sync.publication_name(self.config.repository, self.paths[1])).unlink()
        result = sync.maintain_status(self.config)
        self.assertEqual(result['stale_paths'], [])
        self.assertEqual(result['missing_paths'], [self.paths[1]])
        self.assertEqual(result['duplicate_payload_groups'], [])

    def test_same_title_different_payload_is_not_inferred_to_be_conflict(self):
        self.note(self.paths[1], summary='Another statement.')
        result = sync.maintain_status(self.config)
        self.assertEqual(result['duplicate_payload_groups'], [])
        self.assertEqual(result['conflict_check'], 'unavailable_unstructured_cards')

    def test_untrusted_publication_refuses_without_content_or_mutation(self):
        target = self.note(self.paths[0])
        for mutation in ('malformed', 'symlink', 'hardlink', 'fifo', 'wrong-source'):
            with self.subTest(mutation=mutation):
                target.unlink()
                if mutation == 'malformed':
                    target.write_text('UNTRUSTED_RAW_ERROR'); target.chmod(0o600)
                elif mutation == 'symlink':
                    target.symlink_to(self.config.vault / sync.publication_name(self.config.repository, self.paths[1]))
                elif mutation == 'hardlink':
                    os.link(self.config.vault / sync.publication_name(self.config.repository, self.paths[1]), target)
                elif mutation == 'fifo':
                    os.mkfifo(target, 0o600)
                else:
                    target.write_bytes(self.note(self.paths[1]).read_bytes()); target.chmod(0o600)
                with self.assertRaises(sync.SyncError) as error:
                    sync.maintain_status(self.config)
                self.assertEqual(str(error.exception), 'maintenance refused')
                target.unlink()
                self.note(self.paths[0])

    def test_empty_state_does_not_create_paths(self):
        self.config = sync.SyncConfig('example/team-wiki', self.root / 'missing/vault',
            self.root / 'missing/snapshots', self.root / 'missing/state',
            'example-project', 'eu', 'example-model')
        before = self.image()
        self.assertEqual(sync.maintain_status(self.config),
            {'schema_version': 'workvault-maintain-status-v1', 'state': 'empty',
             'conflict_check': 'unavailable_unstructured_cards'})
        self.assertEqual(self.image(), before)
        self.assertFalse((self.root / 'missing').exists())

    def test_inventory_disappearing_during_report_is_not_empty_success(self):
        original = sync._read
        calls = 0

        def disappear(directory, name):
            nonlocal calls
            if name == sync.STATE:
                calls += 1
                if calls == 2:
                    raise FileNotFoundError('PRIVATE_ERROR_NOT_IN_REPORT')
            return original(directory, name)

        with patch('sync._read', side_effect=disappear):
            with self.assertRaisesRegex(sync.SyncError, '^maintenance refused$'):
                sync.maintain_status(self.config)

    def test_publication_change_during_report_refuses(self):
        original = sync._read
        name = sync.publication_name(self.config.repository, self.paths[0])
        calls = 0

        def change(directory, requested):
            nonlocal calls
            raw = original(directory, requested)
            if requested == name:
                calls += 1
                if calls == 2:
                    return raw + b'CHANGED'
            return raw

        with patch('sync._read', side_effect=change):
            with self.assertRaisesRegex(sync.SyncError, '^maintenance refused$'):
                sync.maintain_status(self.config)

    def test_malformed_or_unsafe_state_refuses(self):
        target = self.config.state / sync.STATE
        for mode, data in ((0o644, json.dumps(self.state)), (0o600, 'UNTRUSTED_STATE_BODY')):
            with self.subTest(mode=mode):
                target.write_text(data); target.chmod(mode)
                with self.assertRaisesRegex(sync.SyncError, '^maintenance refused$'):
                    sync.maintain_status(self.config)

    def test_root_replacement_or_disappearance_refuses_detached_generation(self):
        for root_name in ('state', 'vault'):
            for replacement in (False, True):
                with self.subTest(root=root_name, replacement=replacement):
                    root = getattr(self.config, root_name)
                    detached = self.root / (root_name + '-detached')
                    original = sync._read
                    changed = False

                    def detach(directory, name):
                        nonlocal changed
                        raw = original(directory, name)
                        if not changed and name != sync.STATE:
                            changed = True
                            root.rename(detached)
                            if replacement:
                                root.mkdir(mode=0o700)
                        return raw

                    try:
                        with patch('sync._read', side_effect=detach):
                            with self.assertRaisesRegex(sync.SyncError, '^maintenance refused$'):
                                sync.maintain_status(self.config)
                    finally:
                        if replacement and root.exists():
                            root.rmdir()
                        if detached.exists():
                            detached.rename(root)

    def test_missing_vault_appearing_during_report_refuses(self):
        from dataclasses import replace
        config = replace(self.config, vault=self.root / 'absent-vault')
        original = sync._read
        calls = 0

        def appear(directory, name):
            nonlocal calls
            raw = original(directory, name)
            if name == sync.STATE:
                calls += 1
                if calls == 2:
                    config.vault.mkdir(mode=0o700)
            return raw

        with patch('sync._read', side_effect=appear):
            with self.assertRaisesRegex(sync.SyncError, '^maintenance refused$'):
                sync.maintain_status(config)

    def test_cli_maintain_rejects_ref_and_does_not_collect(self):
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps({'schema_version': 'workvault-sync-config-v1',
            'repository': self.config.repository, 'vault': str(self.config.vault),
            'snapshots': str(self.config.snapshots), 'state': str(self.config.state),
            'vertex': {'project': self.config.project, 'location': 'eu', 'model': self.config.model}}))
        config_path.chmod(0o600)
        before = self.image()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sync.main(['maintain', '--config', str(config_path)]), 0)
        self.assertEqual(json.loads(output.getvalue())['state'], 'reported')
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(sync.main(['maintain', '--config', str(config_path), '--ref', 'main']), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(self.image(), before)


if __name__ == '__main__':
    unittest.main()
