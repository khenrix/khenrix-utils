"""Run the real publication CLI against private temporary filesystems."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).with_name('cli.py')


def batch(repository='example/team-wiki'):
    return {
        'schema_version': 'workvault-curated-batch-v1', 'repository': repository,
        'commit': '1' * 40, 'inventory_complete': True,
        'discovered_paths': ['knowledge/alpha.md', 'knowledge/unindexed.md'],
        'indexed_paths': ['knowledge/alpha.md'], 'requested_paths': ['knowledge/alpha.md'],
        'cards': [{'path': 'knowledge/alpha.md', 'title': 'Synthetic Alpha',
                   'summary': 'A curated synthetic fact.', 'details': '',
                   'date': '2026-10-05', 'draft': False, 'uncertainty': [],
                   'source_links': [], 'cross_references': []}],
    }


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault, self.snapshots = self.root / 'vault', self.root / 'snapshots'

    def run_cli(self, command, *, data=None, publication='Knowledge.md', raw=None):
        args = [sys.executable, '-B', str(CLI), command, '--vault', str(self.vault),
                '--publication', publication]
        if command == 'publish':
            args += ['--snapshots', str(self.snapshots), '--input', '-']
        return subprocess.run(args, input=raw if raw is not None else json.dumps(data) if data is not None else '',
                              capture_output=True, text=True, timeout=10)

    def test_status_does_not_create_missing_vault(self):
        result = self.run_cli('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'schema_version': 'workvault-status-v1', 'state': 'empty'})
        self.assertFalse(self.vault.exists())
        self.assertFalse(self.snapshots.exists())

    def test_publish_records_configured_repository_and_exact_citation(self):
        result = self.run_cli('publish', data=batch())
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout)
        self.assertEqual(state['repository'], 'example/team-wiki')
        self.assertEqual(state['uncovered_paths'], ['knowledge/unindexed.md'])
        self.assertEqual(state['index_gaps'], ['knowledge/unindexed.md'])
        text = (self.vault / 'Knowledge.md').read_text()
        self.assertIn('https://github.com/example/team-wiki/blob/' + '1' * 40 + '/knowledge/alpha.md', text)
        self.assertEqual(os.stat(self.vault).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.vault / 'Knowledge.md').st_mode & 0o777, 0o600)

    def test_another_repository_is_supported_but_cannot_replace_existing_source(self):
        self.assertEqual(self.run_cli('publish', data=batch('other/second-wiki')).returncode, 0)
        before = (self.vault / 'Knowledge.md').read_bytes()
        result = self.run_cli('publish', data=batch())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.vault / 'Knowledge.md').read_bytes(), before)

    def test_unknown_raw_payload_field_is_rejected_without_body_in_diagnostics(self):
        bad = batch()
        bad['raw_source'] = 'RAW_CANARY_MUST_NOT_PERSIST'
        result = self.run_cli('publish', data=bad)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(bad['raw_source'], result.stdout + result.stderr)
        self.assertFalse(self.vault.exists())

    def test_selected_batch_filename_preserves_other_batch_and_human_notes(self):
        self.vault.mkdir(mode=0o700)
        note = self.vault / 'My note.md'
        note.write_text('human note')
        self.assertEqual(self.run_cli('publish', data=batch(), publication='Batch-one.md').returncode, 0)
        before = (self.vault / 'Batch-one.md').read_bytes()
        self.snapshots = self.root / 'other-snapshots'
        other = batch('other/second-wiki')
        self.assertEqual(self.run_cli('publish', data=other, publication='Batch-two.md').returncode, 0)
        self.assertEqual((self.vault / 'Batch-one.md').read_bytes(), before)
        self.assertEqual(note.read_text(), 'human note')
        state = self.run_cli('status', publication='Batch-two.md')
        self.assertEqual(json.loads(state.stdout)['repository'], 'other/second-wiki')

    def test_unsafe_publication_names_never_create_or_escape_vault(self):
        for name in ('../outside.md', '/outside.md', '.hidden.md', 'folder/note.md',
                     'note.txt', 'folder\\note.md', 'bad\n.md', '  .md', '.md'):
            with self.subTest(name=name):
                result = self.run_cli('publish', data=batch(), publication=name)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.vault.exists())
                self.assertFalse((self.root / 'outside.md').exists())

    def test_strict_json_refusal_preserves_last_good_and_has_content_free_error(self):
        self.assertEqual(self.run_cli('publish', data=batch()).returncode, 0)
        before = (self.vault / 'Knowledge.md').read_bytes()
        good = json.dumps(batch())
        invalid = (
            good.replace('"repository":', '"repository":"RAW_CANARY", "repository":', 1),
            good.replace('"details": ""', '"details": "", "details": "RAW_CANARY"', 1),
            good.replace('"inventory_complete": true', '"inventory_complete": NaN'),
            good.replace('"inventory_complete": true', '"inventory_complete": Infinity'),
            good + ' RAW_CANARY', '[' * 2000 + '0' + ']' * 2000,
            ' ' * (1024 * 1024 + 1),
        )
        for raw in invalid:
            with self.subTest(size=len(raw)):
                result = self.run_cli('publish', raw=raw)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertEqual(result.stderr, 'workvault: publication refused\n')
                self.assertEqual((self.vault / 'Knowledge.md').read_bytes(), before)
        self.assertEqual(list(self.snapshots.iterdir()), [])

    def test_changed_selection_refuses_and_valid_update_snapshots_last_good(self):
        self.assertEqual(self.run_cli('publish', data=batch()).returncode, 0)
        before = (self.vault / 'Knowledge.md').read_bytes()
        changed = batch()
        changed['requested_paths'] = ['knowledge/unindexed.md']
        changed['cards'][0]['path'] = 'knowledge/unindexed.md'
        self.assertNotEqual(self.run_cli('publish', data=changed).returncode, 0)
        self.assertEqual((self.vault / 'Knowledge.md').read_bytes(), before)
        updated = batch()
        updated['commit'] = '2' * 40
        self.assertEqual(self.run_cli('publish', data=updated).returncode, 0)
        self.assertEqual([path.read_bytes() for path in self.snapshots.iterdir()], [before])
        self.assertEqual(json.loads(self.run_cli('status').stdout)['published_commit'], '2' * 40)

    def test_unsafe_publication_is_refused_by_direct_api_without_side_effects(self):
        from workvault import publish_curated_batch, status, WorkVaultError
        for name in ('../outside.md', '.hidden.md', 'folder/note.md', 'note.txt'):
            with self.subTest(name=name):
                with self.assertRaises(WorkVaultError):
                    publish_curated_batch(self.vault, self.snapshots, batch(), publication=name)
                with self.assertRaises(WorkVaultError):
                    status(self.vault, publication=name)
                self.assertFalse(self.vault.exists())


if __name__ == '__main__':
    unittest.main()
