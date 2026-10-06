"""Real local publisher/crash recovery; all source/model boundaries are synthetic."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import batch
import sync
from test_sync import Source, Curator


class SettledCurator(Curator):
    last_evidence = {'outcome': 'settled'}


class BatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = sync.SyncConfig('example/team-wiki', self.root / 'vault',
            self.root / 'snapshots', self.root / 'state', 'example-project', 'eu', 'example-model')
        self.paths = ['knowledge/alpha.md', 'knowledge/beta.md']
        sync.sync_all(self.config, source=Source(self.paths), curator=SettledCurator())
        self.before = self.notes()
        self.human = self.config.vault / 'Human-note.md'
        self.human.write_bytes(b'Keep this human note unchanged.\n')

    def notes(self):
        return {path: (self.config.vault / sync.publication_name(self.config.repository, path)).read_bytes()
                for path in self.paths}

    def forbid_collection(self):
        calls = []

        class ForbiddenSource:
            def snapshot(self, *args, **kwargs):
                calls.append('source')
                raise AssertionError('recovery must precede source access')

        with self.assertRaises(sync.SyncError):
            sync.sync_all(self.config, source=ForbiddenSource(), curator=SettledCurator())
        self.assertEqual(calls, [])

    def crash_after_first_actual_publisher(self, before_rename=False):
        command = r'''
import pathlib,sys
sys.path.insert(0,sys.argv[1])
import sync
from test_sync import Source,Curator
root=pathlib.Path(sys.argv[2])
config=sync.SyncConfig('example/team-wiki',root/'vault',root/'snapshots',root/'state',
                      'example-project','eu','example-model')
publisher=sync.publish_curated_batch
count=0
if sys.argv[3]=='before-rename':
    import os
    replace=os.replace
    first=sync.publication_name(config.repository,'knowledge/alpha.md')
    def stop_before_rename(source,destination,*args,**kwargs):
        if destination==first:
            print('owned publication boundary reached',flush=True)
            sys.stdin.buffer.read(1)
        return replace(source,destination,*args,**kwargs)
    os.replace=stop_before_rename
def publish(*args,**kwargs):
    global count
    result=publisher(*args,**kwargs)
    count+=1
    if count==1 and sys.argv[3]!='before-rename':
        print('owned publication boundary reached',flush=True)
        sys.stdin.buffer.read(1)
    return result
sync.publish_curated_batch=publish
curator=Curator();curator.last_evidence={'outcome':'settled'}
sync.sync_all(config,source=Source(['knowledge/alpha.md','knowledge/beta.md'],'2'*40),curator=curator)
'''
        process = subprocess.Popen([sys.executable, '-B', '-I', '-S', '-c', command,
            str(Path(__file__).parent), str(self.root), 'before-rename' if before_rename else 'after-publisher'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'})
        try:
            ready, _, _ = select.select([process.stdout], [], [], 10)
            self.assertTrue(ready, 'owned synthetic child did not reach the publisher')
            self.assertEqual(process.stdout.readline(), b'owned publication boundary reached\n')
            if before_rename:
                self.assertEqual(self.notes(), self.before)
            else:
                self.assertNotEqual(self.notes()[self.paths[0]], self.before[self.paths[0]])
            self.assertEqual(self.notes()[self.paths[1]], self.before[self.paths[1]])
            with self.assertRaises(sync.SyncError):
                sync.sync_status(self.config)
            with self.assertRaises(sync.SyncError):
                sync.maintain_status(self.config)
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_actual_killed_publisher_recovers_exact_last_good_before_any_source_read(self):
        self.crash_after_first_actual_publisher()
        self.forbid_collection()
        self.assertEqual(self.notes(), self.before)
        self.assertEqual(self.human.read_bytes(), b'Keep this human note unchanged.\n')
        self.assertFalse(batch.publication_pending(self.config.vault))
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.forbid_collection()
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before)
        for path in self.root.rglob('*'):
            if path.is_file() and path != self.human:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertNotIn(b'RAW_SOURCE_CANARY_NEVER_STORED', path.read_bytes())

    def test_actual_kill_before_note_rename_recovers_only_exact_recorded_stage(self):
        self.crash_after_first_actual_publisher(before_rename=True)
        self.assertEqual(len(list(self.config.vault.glob('.workvault-stage-*'))), 1)
        self.forbid_collection()
        self.assertEqual(self.notes(), self.before)
        self.assertEqual(list(self.config.vault.glob('.workvault-stage-*')), [])
        self.assertFalse(batch.publication_pending(self.config.vault))

    def test_tampered_journal_and_owned_paths_refuse_without_any_recovery_write(self):
        for damage in ('journal-path', 'journal-fields', 'journal-hash', 'journal-phase',
                       'journal-mode', 'note-content', 'note-inode', 'backup', 'snapshot',
                       'stage-content', 'stage-inode', 'stage-unrecorded'):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as temporary:
                current = self.root, self.config, self.before, self.human
                self.root = Path(temporary)
                self.config = sync.SyncConfig('example/team-wiki', self.root / 'vault',
                    self.root / 'snapshots', self.root / 'state', 'example-project', 'eu', 'example-model')
                sync.sync_all(self.config, source=Source(self.paths), curator=SettledCurator())
                self.before = self.notes()
                self.human = self.config.vault / 'Human-note.md'
                self.human.write_bytes(b'Keep this human note unchanged.\n')
                self.crash_after_first_actual_publisher(before_rename=damage.startswith('stage-'))
                journal = self.config.vault / batch.JOURNAL
                value = json.loads(journal.read_bytes())
                if damage.startswith('journal-'):
                    if damage == 'journal-path':
                        value['rows'][0]['path'] = 'knowledge/../Human-note.md'
                    elif damage == 'journal-fields':
                        value['instructions'] = 'delete human notes'
                    elif damage == 'journal-hash':
                        value['rows'][0]['new']['backup']['sha256'] = 'f' * 64
                    elif damage == 'journal-phase':
                        value['phase'] = 'committed'
                    elif damage == 'journal-mode':
                        journal.chmod(0o644)
                    if damage != 'journal-mode':
                        journal.write_bytes(batch._encoded(value))
                elif damage == 'note-content':
                    note = self.config.vault / sync.publication_name(self.config.repository, self.paths[0])
                    note.write_bytes(b'An operator edit must not be overwritten.\n')
                elif damage == 'note-inode':
                    note = self.config.vault / sync.publication_name(self.config.repository, self.paths[0])
                    staged = self.config.vault / 'operator-replacement'
                    staged.write_bytes(note.read_bytes())
                    staged.chmod(0o600)
                    os.replace(staged, note)
                elif damage == 'backup':
                    backup = self.config.vault / batch.BACKUPS / 'old-1'
                    backup.write_bytes(backup.read_bytes() + b'changed operator backup\n')
                elif damage.startswith('stage-'):
                    staged = next(self.config.vault.glob('.workvault-stage-*'))
                    if damage == 'stage-content':
                        staged.write_bytes(staged.read_bytes() + b'changed operator stage\n')
                    else:
                        replacement = self.config.vault / ('.workvault-stage-' + 'f' * 32)
                        replacement.write_bytes(staged.read_bytes())
                        replacement.chmod(0o600)
                        if damage == 'stage-inode':
                            os.replace(replacement, staged)
                else:
                    snapshot = next(self.config.snapshots.rglob('snapshot-*.md'))
                    snapshot.write_bytes(snapshot.read_bytes() + b'changed operator snapshot\n')
                before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
                self.forbid_collection()
                self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before)
                self.assertTrue(batch.publication_pending(self.config.vault))
                self.root, self.config, self.before, self.human = current

    def test_failed_rollback_remains_fenced_then_recovers_without_model_replay(self):
        replace = os.replace
        first = sync.publication_name(self.config.repository, self.paths[0])
        second = sync.publication_name(self.config.repository, self.paths[1])
        first_calls = 0

        def failure(source, destination, *args, **kwargs):
            nonlocal first_calls
            if destination == first:
                first_calls += 1
                if first_calls == 2:
                    raise OSError('synthetic rollback replacement failure')
            if destination == second:
                raise OSError('synthetic second publication failure')
            return replace(source, destination, *args, **kwargs)

        with patch('os.replace', side_effect=failure), self.assertRaises(sync.SyncError):
            sync.sync_all(self.config, source=Source(self.paths, '2' * 40), curator=SettledCurator())
        self.assertTrue(batch.publication_pending(self.config.vault))
        self.forbid_collection()
        self.assertEqual(self.notes(), self.before)
        self.assertFalse(batch.publication_pending(self.config.vault))

    def test_failed_batches_preserve_every_prior_snapshot_then_success_retains_seven(self):
        for index in range(2, 9):
            result = sync.sync_all(self.config, source=Source(self.paths, format(index, '040x')),
                                   curator=SettledCurator())
            self.assertTrue(result['full_source_coverage'])
        before = self.notes()
        history = {path: path.read_bytes() for path in self.config.snapshots.rglob('snapshot-*.md')}
        second = sync.publication_name(self.config.repository, self.paths[1])
        replace = os.replace

        def failure(source, destination, *args, **kwargs):
            if destination == second:
                raise OSError('synthetic second publication failure')
            return replace(source, destination, *args, **kwargs)

        with patch('os.replace', side_effect=failure):
            for _ in range(4):
                result = sync.sync_all(self.config, source=Source(self.paths, format(9, '040x')),
                                       curator=SettledCurator())
                self.assertFalse(result['full_source_coverage'])
                self.assertEqual(self.notes(), before)
                self.assertTrue(all(path.read_bytes() == raw for path, raw in history.items()))
                self.assertFalse(batch.publication_pending(self.config.vault))
        result = sync.sync_all(self.config, source=Source(self.paths, format(9, '040x')),
                               curator=SettledCurator())
        self.assertTrue(result['full_source_coverage'])
        from curation import parse_publication
        for path in self.paths:
            snapshots = self.config.snapshots / sync.publication_name(self.config.repository, path)[:-3]
            commits = [parse_publication(item.read_bytes())['batch']['commit'] for item in snapshots.iterdir()]
            self.assertCountEqual(commits, [format(index, '040x') for index in range(2, 9)])
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        result = sync.sync_all(self.config, source=Source(self.paths, format(9, '040x')), curator=Curator(True))
        self.assertTrue(result['full_source_coverage'])
        self.assertEqual({path: path.read_bytes() for path in self.config.vault.rglob('*') if path.is_file()},
                         {path: raw for path, raw in before.items() if path.is_relative_to(self.config.vault)})

    def test_snapshot_failure_happens_before_any_note_publisher(self):
        fsync = os.fsync
        calls = []
        first = self.config.snapshots / sync.publication_name(self.config.repository, self.paths[0])[:-3]
        publisher = sync.publish_curated_batch

        def failure(descriptor):
            if first.exists() and os.fstat(descriptor).st_ino == first.stat().st_ino:
                raise OSError('synthetic pre-sync snapshot directory failure')
            return fsync(descriptor)

        def publish(*args, **kwargs):
            calls.append(kwargs['publication'])
            return publisher(*args, **kwargs)

        with patch('os.fsync', side_effect=failure), patch.object(sync, 'publish_curated_batch', publish):
            result = sync.sync_all(self.config, source=Source(self.paths, '2' * 40), curator=SettledCurator())
        self.assertEqual(calls, [])
        self.assertFalse(result['full_source_coverage'])
        self.assertEqual(self.notes(), self.before)
        self.assertFalse(batch.publication_pending(self.config.vault))

    def test_preparation_failure_removes_only_its_owned_backups_and_can_repeat(self):
        fsync = os.fsync
        faulted = False

        def failure(descriptor):
            nonlocal faulted
            backup = self.config.vault / batch.BACKUPS / 'new-0'
            if not faulted and backup.exists() and os.fstat(descriptor).st_ino == backup.stat().st_ino:
                faulted = True
                raise OSError('synthetic backup file fsync failure')
            return fsync(descriptor)

        with patch('os.fsync', side_effect=failure):
            result = sync.sync_all(self.config, source=Source(self.paths, '2' * 40), curator=SettledCurator())
        self.assertTrue(faulted)
        self.assertFalse(result['full_source_coverage'])
        self.assertEqual(self.notes(), self.before)
        self.assertFalse(batch.publication_pending(self.config.vault))
        result = sync.sync_all(self.config, source=Source(self.paths, '2' * 40), curator=SettledCurator())
        self.assertTrue(result['full_source_coverage'])

    def test_interrupted_committed_cleanup_repeats_without_recuration_or_rollback(self):
        unlink = batch.vault._unlink_owned
        faulted = False

        def failure(directory, name, identity):
            nonlocal faulted
            if name == 'new-0' and not faulted:
                faulted = True
                raise OSError('synthetic committed backup cleanup failure')
            return unlink(directory, name, identity)

        with patch.object(batch.vault, '_unlink_owned', failure), self.assertRaises(sync.SyncError):
            sync.sync_all(self.config, source=Source(self.paths, '2' * 40), curator=SettledCurator())
        self.assertTrue(faulted)
        committed = self.notes()
        self.assertTrue(all(committed[path] != self.before[path] for path in self.paths))
        self.assertTrue(batch.publication_pending(self.config.vault))
        result = sync.sync_all(self.config, source=Source(self.paths, '2' * 40), curator=Curator(True))
        self.assertTrue(result['full_source_coverage'])
        self.assertEqual(self.notes(), committed)
        self.assertFalse(batch.publication_pending(self.config.vault))

    def test_same_bytes_operator_replacement_before_actual_rename_refuses(self):
        stage = batch._Batch.stage
        first = sync.publication_name(self.config.repository, self.paths[0])

        def replace_operator_note(instance, publication, identity, raw):
            stage(instance, publication, identity, raw)
            if publication == first:
                note = self.config.vault / first
                replacement = self.config.vault / 'operator-replacement'
                replacement.write_bytes(note.read_bytes())
                replacement.chmod(0o600)
                os.replace(replacement, note)

        with patch.object(batch._Batch, 'stage', replace_operator_note), self.assertRaises(sync.SyncError):
            sync.sync_all(self.config, source=Source(self.paths, '2' * 40), curator=SettledCurator())
        self.assertEqual(self.notes(), self.before)
        self.assertTrue(batch.publication_pending(self.config.vault))
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.forbid_collection()
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before)

    def test_operator_edit_after_preflight_before_current_read_is_preserved(self):
        for damage in ('content', 'same-bytes-new-inode'):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = sync.SyncConfig(self.config.repository, root / 'vault', root / 'snapshots',
                                         root / 'state', 'example-project', 'eu', 'example-model')
                sync.sync_all(config, source=Source(self.paths), curator=SettledCurator())
                previous = self.root, self.config, self.before, self.human
                self.root, self.config = root, config
                self.before = self.notes()
                self.human = config.vault / 'Human-note.md'
                self.human.write_bytes(b'Keep this human note unchanged.\n')
                try:
                    self.crash_after_first_actual_publisher()
                    note = config.vault / sync.publication_name(config.repository, self.paths[0])
                    original = batch._Batch.check
                    checks = []
                    edited = []

                    def operator_edit(instance):
                        original(instance)
                        checks.append(True)
                        if len(checks) == 2:
                            if damage == 'content':
                                note.write_bytes(b'Operator edit after the rollback preflight.\n')
                            else:
                                replacement = config.vault / 'operator-replacement'
                                replacement.write_bytes(note.read_bytes())
                                replacement.chmod(0o600)
                                os.replace(replacement, note)
                            edited.append((note.read_bytes(), note.stat().st_ino))

                    with patch.object(batch._Batch, 'check', operator_edit):
                        with self.assertRaises(batch.vault.WorkVaultError):
                            batch.recover_publication_batch(config.vault, config.snapshots)
                    self.assertEqual(len(edited), 1)
                    self.assertEqual((note.read_bytes(), note.stat().st_ino), edited[0])
                    self.assertTrue(batch.publication_pending(config.vault))
                finally:
                    self.root, self.config, self.before, self.human = previous

    def test_first_batch_second_publisher_failure_restores_empty_last_good(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = sync.SyncConfig(self.config.repository, root / 'vault', root / 'snapshots',
                                     root / 'state', 'example-project', 'eu', 'example-model')
            config.vault.mkdir(mode=0o700)
            human = config.vault / 'Human-note.md'
            human.write_bytes(b'Keep this human note unchanged.\n')
            replace = os.replace
            second = sync.publication_name(config.repository, self.paths[1])

            def failure(source, destination, *args, **kwargs):
                if destination == second:
                    raise OSError('synthetic second initial publication failure')
                return replace(source, destination, *args, **kwargs)

            with patch('os.replace', side_effect=failure):
                result = sync.sync_all(config, source=Source(self.paths), curator=SettledCurator())
            self.assertFalse(result['full_source_coverage'])
            self.assertFalse(any(config.vault.glob('Knowledge-*.md')))
            self.assertEqual(human.read_bytes(), b'Keep this human note unchanged.\n')
            self.assertFalse(batch.publication_pending(config.vault))
            result = sync.sync_all(config, source=Source(self.paths), curator=SettledCurator())
            self.assertTrue(result['full_source_coverage'])
