"""Real filesystem regressions for the provisional publisher."""
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from curation import parse_publication, validate_curated_batch
from test_publisher_contract import fixture
from workvault import publish_curated_batch, status


def publish_worker(vault, snapshots, commit, start, answers):
    start.wait(10)
    try:
        result = publish_curated_batch(Path(vault), Path(snapshots), fixture(commit))
        answers.put((True, result['published_commit']))
    except Exception:
        answers.put((False, None))


class PublicationFailures(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = self.root / 'vault'
        self.snapshots = self.root / 'snapshots'

    def publish(self, commit='1' * 40):
        return publish_curated_batch(self.vault, self.snapshots, fixture(commit))

    def test_invalid_first_batch_does_not_create_roots(self):
        batch = fixture()
        batch['inventory_complete'] = False
        with self.assertRaises(Exception):
            publish_curated_batch(self.vault, self.snapshots, batch)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_render_expansion_refuses_before_creating_roots_or_lock(self):
        batch = fixture()
        batch['discovered_paths'] = batch['requested_paths'] + [
            'knowledge/' + '#' * 230 + f'{index:04d}.md' for index in range(4094)]
        validate_curated_batch(batch)
        with self.assertRaises(Exception):
            publish_curated_batch(self.vault, self.snapshots, batch)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_status_with_absent_ancestors_has_no_side_effect(self):
        self.assertEqual(status(self.root / 'absent' / 'vault'),
                         {'schema_version': 'workvault-status-v1', 'state': 'empty'})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_fifo_managed_file_refuses_without_blocking(self):
        self.vault.mkdir(mode=0o700)
        managed = self.vault / 'Knowledge.md'
        os.mkfifo(managed, mode=0o600)
        with self.assertRaises(Exception):
            status(self.vault)
        with self.assertRaises(Exception):
            self.publish()
        self.assertTrue(managed.exists())
        self.assertFalse(self.snapshots.exists())

    def test_hardlinked_lock_refuses_without_external_write(self):
        self.publish()
        lock = self.vault / '.workvault.lock'
        external = self.root / 'external-lock'
        os.link(lock, external)
        before = (self.vault / 'Knowledge.md').read_bytes()
        with self.assertRaises(Exception):
            self.publish('2' * 40)
        self.assertEqual(external.read_bytes(), b'')
        self.assertEqual((self.vault / 'Knowledge.md').read_bytes(), before)

    def test_snapshot_symlink_refuses_and_preserves_external_note(self):
        self.publish()
        external = self.root / 'human-note.md'
        external.write_text('Keep human note')
        (self.snapshots / 'snapshot-00000000000000000001.md').symlink_to(external)
        before = (self.vault / 'Knowledge.md').read_bytes()
        with self.assertRaises(Exception):
            self.publish('2' * 40)
        self.assertEqual(external.read_text(), 'Keep human note')
        self.assertEqual((self.vault / 'Knowledge.md').read_bytes(), before)

    def test_failed_atomic_replace_cleans_private_staging_and_has_safe_error(self):
        self.publish()
        before = (self.vault / 'Knowledge.md').read_bytes()
        original_replace = os.replace

        def fail_managed(source, destination, *args, **kwargs):
            if Path(destination).name == 'Knowledge.md':
                raise OSError('Synthetic rejected source body: SHOULD-NOT-ECHO')
            return original_replace(source, destination, *args, **kwargs)

        with patch('os.replace', side_effect=fail_managed):
            with self.assertRaises(Exception) as error:
                self.publish('2' * 40)
        self.assertNotIn('SHOULD-NOT-ECHO', str(error.exception))
        self.assertEqual((self.vault / 'Knowledge.md').read_bytes(), before)
        self.assertEqual(set(p.name for p in self.vault.iterdir()),
                         {'Knowledge.md', '.workvault.lock'})
        self.assertEqual(len(list(self.snapshots.iterdir())), 1)
        self.assertEqual(next(self.snapshots.iterdir()).read_bytes(), before)

    def test_concurrent_publications_leave_one_coherent_generation_and_snapshots(self):
        self.publish()
        context = multiprocessing.get_context('fork')
        start = context.Event()
        answers = context.Queue()
        self.addCleanup(answers.close)
        workers = [context.Process(target=publish_worker,
                   args=(str(self.vault), str(self.snapshots), digit * 40, start, answers))
                   for digit in ('2', '3')]
        for worker in workers:
            worker.start()
        start.set()
        try:
            for worker in workers:
                worker.join(10)
                self.assertFalse(worker.is_alive())
                self.assertEqual(worker.exitcode, 0)
            self.assertCountEqual([answers.get(timeout=2), answers.get(timeout=2)],
                                  [(True, '2' * 40), (True, '3' * 40)])
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(2)
        current = parse_publication((self.vault / 'Knowledge.md').read_bytes())['batch']['commit']
        self.assertIn(current, ('2' * 40, '3' * 40))
        previous = [parse_publication(path.read_bytes())['batch']['commit']
                    for path in self.snapshots.iterdir()]
        self.assertEqual(len(previous), 2)
        self.assertIn('1' * 40, previous)
        self.assertEqual(set(previous + [current]), {'1' * 40, '2' * 40, '3' * 40})


if __name__ == '__main__':
    unittest.main(verbosity=2)
