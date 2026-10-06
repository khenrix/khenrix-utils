"""Internal curated reads against real, owned synthetic publications only."""
import fcntl
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
import unittest

import batch
import curation
import workvault
from test_publisher_contract import fixture


REPOSITORY = 'example/team-wiki'
PATHS = ['knowledge/alpha.md', 'knowledge/drafts/beta.md']
IDS = ['Knowledge-767f60e1827e05c91a8c9125', 'Knowledge-e4947e93c241e63bc796d8e8']


def publish(vault, snapshots, value):
    with batch.publication_batch(vault, snapshots, value):
        for card in value['cards']:
            name = batch.publication_name(value['repository'], card['path'])
            single = dict(value, requested_paths=[card['path']], cards=[card])
            workvault.publish_curated_batch(vault, snapshots / name[:-3], single, name)


class CuratedReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.vault = self.root / 'vault'
        self.snapshots = self.root / 'snapshots'
        publish(self.vault, self.snapshots, fixture())

    def read(self, paths=None, repository=REPOSITORY):
        return batch.read_curated_cards(self.vault, repository, PATHS if paths is None else paths)

    def tree(self):
        result = {}
        for path in self.root.rglob('*'):
            info = path.lstat()
            result[str(path.relative_to(self.root))] = (
                info.st_mode, info.st_uid, info.st_nlink, info.st_ino,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                path.read_bytes() if stat.S_ISREG(info.st_mode) else None)
        return result

    def refused_unchanged(self, operation=None):
        before = self.tree()
        with self.assertRaisesRegex(workvault.WorkVaultError, '^curated read refused$'):
            (operation or self.read)()
        self.assertEqual(self.tree(), before)

    def note(self, index=0):
        return self.vault / (IDS[index] + '.md')

    def test_exact_stable_ids_provenance_uncertainty_and_curated_fields(self):
        human = self.vault / 'Human-note.md'
        human.write_bytes(b'Human material is never enumerated or returned.\n')
        raw = self.vault / 'Raw-source.md'
        raw.write_bytes(b'RAW_ONLY_CANARY_NOT_CURATED\n')
        before = self.tree()
        observed = self.read()
        self.assertEqual(self.tree(), before)
        for index, row in enumerate(observed):
            checkpoint = curation.parse_publication(self.note(index).read_bytes())
            self.assertEqual(row, {
                'id': IDS[index], 'repository': REPOSITORY, 'commit': '1' * 40,
                'published_at': checkpoint['published_at'],
                'source_url': 'https://github.com/example/team-wiki/blob/' + '1' * 40 + '/' + PATHS[index],
                'card': fixture()['cards'][index]})
        self.assertNotIn('RAW_ONLY_CANARY_NOT_CURATED', repr(observed))
        observed[0]['card']['uncertainty'].append('Caller mutation is not publication.')
        self.assertEqual(self.read()[0]['card'], fixture()['cards'][0])

    def test_topic_refresh_keeps_stable_ids_and_independent_exact_revisions(self):
        value = fixture('2' * 40)
        value['cards'] = value['cards'][:1]
        value['requested_paths'] = PATHS[:1]
        value['cards'][0]['summary'] = 'A newer curated correction.'
        publish(self.vault, self.snapshots, value)
        observed = self.read(list(reversed(PATHS)))
        self.assertEqual([row['id'] for row in observed], list(reversed(IDS)))
        self.assertEqual([row['commit'] for row in observed], ['1' * 40, '2' * 40])
        self.assertEqual(observed[1]['card']['summary'], 'A newer curated correction.')
        self.assertEqual(observed[0]['card'], fixture()['cards'][1])

    def test_missing_selected_publication_refuses_whole_result_without_creating_it(self):
        self.note(1).unlink()
        self.refused_unchanged()

    def test_empty_or_unsafe_source_selection_refuses_without_changes(self):
        for paths in ([], PATHS[0], [PATHS[0], PATHS[0]], ['../Human-note.md'],
                      ['Human-note.md'], ['knowledge/../Human-note.md'],
                      ['knowledge/alpha.md/'], [None], ['knowledge/x.md'] * 4097):
            with self.subTest(paths=paths[:2] if isinstance(paths, list) else paths):
                self.refused_unchanged(lambda: self.read(paths))
        self.refused_unchanged(lambda: self.read(repository='https://example.invalid/repo'))

    def test_missing_root_refuses_without_creating_ancestors(self):
        self.refused_unchanged(lambda: batch.read_curated_cards(
            self.root / 'missing' / 'vault', REPOSITORY, PATHS))

    def test_pending_recovery_entries_are_not_recovered_or_ignored(self):
        for name in (batch.JOURNAL, batch.BACKUPS, '.workvault-batch-stage-unknown'):
            with self.subTest(name=name):
                path = self.vault / name
                path.write_bytes(b'Malformed recovery evidence must remain untouched.\n')
                path.chmod(0o600)
                self.refused_unchanged()
                path.unlink()

    def test_active_same_context_publisher_cannot_return_a_partial_generation(self):
        value = fixture('2' * 40)
        with batch.publication_batch(self.vault, self.snapshots, value):
            single = dict(value, requested_paths=PATHS[:1], cards=value['cards'][:1])
            workvault.publish_curated_batch(self.vault, self.snapshots / IDS[0], single, IDS[0] + '.md')
            self.refused_unchanged()
            single = dict(value, requested_paths=PATHS[1:], cards=value['cards'][1:])
            workvault.publish_curated_batch(self.vault, self.snapshots / IDS[1], single, IDS[1] + '.md')
        self.assertEqual([row['commit'] for row in self.read()], ['2' * 40] * 2)

    def test_actual_child_partial_publication_refuses_then_completed_generation_reads(self):
        command = """
import pathlib, sys
sys.path.insert(0, sys.argv[1])
import batch, workvault
from test_publisher_contract import fixture
root = pathlib.Path(sys.argv[2])
value = fixture('2' * 40)
with batch.publication_batch(root / 'vault', root / 'snapshots', value):
    for index, card in enumerate(value['cards']):
        name = batch.publication_name(value['repository'], card['path'])
        single = dict(value, requested_paths=[card['path']], cards=[card])
        workvault.publish_curated_batch(root / 'vault', root / 'snapshots' / name[:-3], single, name)
        if index == 0:
            print('synthetic partial publication', flush=True)
            sys.stdin.buffer.read(1)
"""
        process = subprocess.Popen([sys.executable, '-B', '-I', '-S', '-c', command,
            str(Path(__file__).parent), str(self.root)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'})
        try:
            ready, _, _ = select.select([process.stdout], [], [], 10)
            self.assertTrue(ready, 'synthetic publisher failed to reach the owned boundary')
            self.assertEqual(process.stdout.readline(), b'synthetic partial publication\n')
            self.refused_unchanged()
            process.stdin.write(b'1')
            process.stdin.flush()
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertEqual(process.stderr.read(), b'')
            self.assertEqual([row['commit'] for row in self.read()], ['2' * 40] * 2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_exclusive_lock_refuses_and_shared_lock_allows_real_reads(self):
        with (self.vault / workvault.LOCK).open('rb') as locked:
            fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.refused_unchanged()
            fcntl.flock(locked, fcntl.LOCK_SH)
            self.assertEqual([row['id'] for row in self.read()], IDS)

    def test_missing_lock_refuses_instead_of_using_status_unlocked_fallback(self):
        (self.vault / workvault.LOCK).unlink()
        self.refused_unchanged()

    def test_wrong_source_identity_or_multiple_card_payload_cannot_resolve_stable_id(self):
        for damage in ('repository', 'path', 'multiple'):
            with self.subTest(damage=damage):
                value = fixture()
                if damage == 'repository':
                    value['repository'] = 'example/other-wiki'
                    value['requested_paths'] = PATHS[:1]
                    value['cards'] = value['cards'][:1]
                elif damage == 'path':
                    value['requested_paths'] = PATHS[1:]
                    value['cards'] = value['cards'][1:]
                self.note().write_bytes(curation.render_publication(value, '2026-10-06T00:00:00Z'))
                self.refused_unchanged()

    def test_raw_human_and_noncanonical_publications_refuse_without_exposure(self):
        original = self.note().read_bytes()
        for raw in (b'RAW_ONLY_CANARY_NOT_CURATED\n', b'# A human note\n',
                    original + b'RAW_ONLY_CANARY_NOT_CURATED\n',
                    original.replace(b'Synthetic Alpha', b'Changed human rendering', 1),
                    original.replace(b'"schema_version":', b'"schema_version":"unexpected","schema_version":', 1)):
            with self.subTest(size=len(raw)):
                self.note().write_bytes(raw)
                self.refused_unchanged()

    def test_publication_symlink_hardlink_and_wrong_modes_refuse_without_changes(self):
        original = self.note().read_bytes()
        for damage in ('symlink', 'hardlink', 'mode'):
            with self.subTest(damage=damage):
                self.note().unlink()
                target = self.root / ('owned-target-' + damage)
                target.write_bytes(original)
                target.chmod(0o600)
                if damage == 'symlink':
                    self.note().symlink_to(target)
                elif damage == 'hardlink':
                    os.link(target, self.note())
                else:
                    self.note().write_bytes(original)
                    self.note().chmod(0o644)
                self.refused_unchanged()

    def test_root_symlink_and_wrong_mode_refuse_without_changes(self):
        link = self.root / 'linked-vault'
        link.symlink_to(self.vault, target_is_directory=True)
        self.refused_unchanged(lambda: batch.read_curated_cards(link, REPOSITORY, PATHS))
        self.vault.chmod(0o755)
        self.refused_unchanged()

    def test_lock_symlink_hardlink_and_wrong_mode_refuse_without_changes(self):
        lock = self.vault / workvault.LOCK
        for damage in ('symlink', 'hardlink', 'mode'):
            with self.subTest(damage=damage):
                lock.unlink()
                target = self.root / ('owned-lock-' + damage)
                target.write_bytes(b'')
                target.chmod(0o600)
                if damage == 'symlink':
                    lock.symlink_to(target)
                elif damage == 'hardlink':
                    os.link(target, lock)
                else:
                    lock.write_bytes(b'')
                    lock.chmod(0o644)
                self.refused_unchanged()


if __name__ == '__main__':
    unittest.main()
