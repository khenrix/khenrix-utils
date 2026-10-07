"""Native-source publication and existing owned recovery with synthetic cards."""
import copy
from datetime import datetime, timezone
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
import curation
import workvault as vault


def fixture(revision='r1'):
    cards = []
    for kind in ('jira', 'backstage'):
        ref = {'schema_version': 'workvault-source-v2', 'kind': kind,
               'account': 'company', 'container': 'selected', 'item_id': '123',
               'revision': revision, 'author': None, 'event_at': None, 'modified_at': None,
               'observed_at': '2020-01-01T00:00:00Z',
               'citation': f'https://example.invalid/{kind}/123?revision={revision}',
               'content_hash': 'sha256:' + format(int(revision[1:]), '064x')}
        cards.append({'source': ref, 'title': f'Synthetic {kind}',
                      'summary': 'Approval is not guaranteed.', 'details': 'Curated synthetic evidence.',
                      'date': None, 'draft': True, 'uncertainty': ['Conflicting owner reports.'],
                      'source_links': [], 'evidence_kind': 'owner_synthesis',
                      'supersedes': [], 'dependencies': []})
    return {'schema_version': 'workvault-curated-batch-v2', 'cards': cards}


class SourcePublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.notes, self.history = self.root / 'vault', self.root / 'snapshots'

    def publish(self, value=None):
        return vault.publish_curated_batch(self.notes, self.history, fixture() if value is None else value)

    def files(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def publish_owned(self, value):
        with batch.publication_batch(self.notes, self.history, value):
            for card in value['cards']:
                name = batch.source_publication_name(card['source'])
                vault.publish_curated_batch(self.notes, self.history / name[:-3],
                    {'schema_version': value['schema_version'], 'cards': [card]}, publication=name)

    def test_native_roundtrip_status_does_not_fabricate_github_coverage(self):
        value = fixture()
        result = self.publish(value)
        raw = (self.notes / vault.MANAGED).read_bytes()
        self.assertEqual(curation.parse_publication(raw)['batch'], value)
        self.assertNotIn(b'https://github.com/', raw)
        self.assertEqual(result, vault.status(self.notes))
        self.assertEqual(result['schema_version'], 'workvault-status-v2')
        self.assertEqual(result['sources'], [card['source'] for card in value['cards']])
        self.assertFalse(result['full_source_coverage'])
        self.assertNotIn('repository', result)
        self.assertNotIn('published_commit', result)
        before = self.files()
        vault.status(self.notes)
        self.assertEqual(self.files(), before)

    def test_v2_publication_and_owned_batch_preserve_fractional_publication_time(self):
        precise = datetime(2026, 10, 7, 12, 0, 0, 123456, tzinfo=timezone.utc)
        class Clock:
            @staticmethod
            def now(zone):
                return precise
        value = fixture()
        for card in value['cards']:
            card['source']['observed_at'] = '2026-10-07T12:00:00.123455Z'
        with patch.object(vault, 'datetime', Clock), patch.object(batch, 'datetime', Clock):
            try:
                result = self.publish(value)
                self.assertEqual(result['published_at'], '2026-10-07T12:00:00.123456Z')
                self.publish_owned(value)
            except (vault.WorkVaultError, ValueError):
                self.fail('valid same-second native observation was refused')
        for path in self.notes.glob('Knowledge-*.md'):
            self.assertEqual(curation.parse_publication(path.read_bytes())['published_at'], '2026-10-07T12:00:00.123456Z')

    def test_selected_identity_changes_and_same_revision_hash_conflict_refuse(self):
        self.publish()
        before = self.files()
        invalid = []
        for key, replacement in [('kind', 'drive'), ('account', 'other'),
                                 ('container', 'other'), ('item_id', '456')]:
            value = fixture('r2')
            value['cards'][0]['source'][key] = replacement
            invalid.append(value)
        invalid.append({'schema_version': fixture()['schema_version'], 'cards': fixture()['cards'][:1]})
        value = fixture()
        value['cards'][0]['source']['content_hash'] = 'sha256:' + 'f' * 64
        invalid.append(value)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(vault.WorkVaultError):
                self.publish(value)
            self.assertEqual(self.files(), before)
        self.publish(fixture('r2'))
        self.assertEqual(curation.parse_publication((self.notes / vault.MANAGED).read_bytes())['batch'], fixture('r2'))

    def test_native_snapshots_retain_seven_exact_generations_and_human_notes(self):
        self.publish()
        human = self.notes / 'Human.md'
        human.write_bytes(b'Private authored note stays untouched.\n')
        prior = []
        for number in range(2, 11):
            prior.append((self.notes / vault.MANAGED).read_bytes())
            self.publish(fixture(f'r{number}'))
        self.assertCountEqual([path.read_bytes() for path in self.history.iterdir()], prior[-7:])
        self.assertEqual(human.read_bytes(), b'Private authored note stays untouched.\n')
        for path in [self.notes / vault.MANAGED, *self.history.iterdir()]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_declared_dependency_revision_cannot_change_hash_across_publications(self):
        prior = fixture()
        dependency = dict(prior['cards'][0]['source'], kind='drive', item_id='dependency',
                          citation='https://example.invalid/drive/dependency')
        prior['cards'][0]['dependencies'] = [dependency]
        self.publish(prior)
        next_batch = fixture('r2')
        next_batch['cards'][0]['dependencies'] = [dict(dependency, content_hash='sha256:' + 'f' * 64)]
        before = self.files()
        with self.assertRaises(vault.WorkVaultError):
            self.publish(next_batch)
        self.assertEqual(self.files(), before)

    def test_owned_generation_rejects_conflicting_revision_moved_to_another_card(self):
        prior = fixture()
        dependency = dict(prior['cards'][0]['source'], kind='drive', item_id='dependency',
                          citation='https://example.invalid/drive/dependency')
        prior['cards'][0]['dependencies'] = [dependency]
        self.publish_owned(prior)
        current = fixture('r2')
        current['cards'][1]['dependencies'] = [dict(dependency, content_hash='sha256:' + 'f' * 64)]
        self.assertEqual(curation.validate_source_batch(current), current)
        before = self.files()
        with self.assertRaises(batch.BatchPublicationRejected):
            self.publish_owned(current)
        self.assertEqual(self.files(), before)
        self.assertFalse(batch.publication_pending(self.notes))

    def test_owned_generation_checks_dependency_and_supersession_in_retained_history(self):
        for relation in ('dependencies', 'supersedes'):
            with self.subTest(relation=relation), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary)
                self.notes, self.history = self.root / 'vault', self.root / 'snapshots'
                prior = fixture()
                if relation == 'dependencies':
                    evidence = dict(prior['cards'][0]['source'], kind='drive', item_id='dependency',
                                    citation='https://example.invalid/drive/dependency')
                    prior['cards'][0][relation] = [evidence]
                else:
                    evidence = dict(prior['cards'][0]['source'], revision='r0')
                    prior['cards'][0][relation] = [evidence]
                self.publish_owned(prior)
                self.publish_owned(fixture('r2'))
                current = fixture('r3')
                current['cards'][1]['dependencies'] = [dict(evidence, content_hash='sha256:' + 'f' * 64)]
                before = self.files()
                with self.assertRaises(batch.BatchPublicationRejected):
                    self.publish_owned(current)
                self.assertEqual(self.files(), before)
                self.assertFalse(batch.publication_pending(self.notes))

    def test_invalid_extra_raw_fields_and_six_cards_never_create_paths(self):
        invalid = fixture()
        invalid['cards'][0]['raw_body'] = 'RAW_SOURCE_MUST_NOT_PERSIST'
        oversized = fixture()
        oversized['cards'] = [copy.deepcopy(fixture()['cards'][0]) for _ in range(6)]
        for index, card in enumerate(oversized['cards']):
            card['source']['item_id'] = str(index)
            card['source']['citation'] = f'https://example.invalid/jira/{index}'
        for value in (invalid, oversized):
            with self.assertRaises(vault.WorkVaultError):
                self.publish(value)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_native_symlink_and_hardlink_publication_refuse_without_target_write(self):
        self.notes.mkdir(mode=0o700)
        external = self.root / 'Human.md'
        external.write_bytes(b'Keep human content.')
        managed = self.notes / vault.MANAGED
        managed.symlink_to(external)
        with self.assertRaises(vault.WorkVaultError):
            self.publish()
        self.assertEqual(external.read_bytes(), b'Keep human content.')
        managed.unlink()
        self.publish()
        external.unlink()
        os.link(managed, external)
        before = external.read_bytes()
        with self.assertRaises(vault.WorkVaultError):
            self.publish(fixture('r2'))
        self.assertEqual(external.read_bytes(), before)

    def test_owned_native_names_are_identity_bound_and_revision_independent(self):
        first, second = fixture()['cards']
        name = batch.source_publication_name(first['source'])
        self.assertEqual(name, batch.source_publication_name(fixture('r2')['cards'][0]['source']))
        self.assertNotEqual(name, batch.source_publication_name(second['source']))
        changed = copy.deepcopy(first['source'])
        changed['account'] = 'different'
        self.assertNotEqual(name, batch.source_publication_name(changed))
        self.assertRegex(name, r'^Knowledge-[0-9a-f]{24}\.md$')

    def test_owned_native_batch_rolls_back_second_rename_failure(self):
        self.publish_owned(fixture())
        before = {path.name: path.read_bytes() for path in self.notes.glob('Knowledge-*.md')}
        second = batch.source_publication_name(fixture()['cards'][1]['source'])
        replace = os.replace
        def failure(source, destination, *args, **kwargs):
            if destination == second:
                raise OSError('synthetic second native replacement failure')
            return replace(source, destination, *args, **kwargs)
        with patch('os.replace', side_effect=failure), self.assertRaises(batch.BatchPublicationRejected):
            self.publish_owned(fixture('r2'))
        self.assertEqual({path.name: path.read_bytes() for path in self.notes.glob('Knowledge-*.md')}, before)
        self.assertFalse(batch.publication_pending(self.notes))

    def test_owned_failed_retries_preserve_prior_history_then_keep_seven(self):
        for number in range(1, 9):
            self.publish_owned(fixture(f'r{number}'))
        prior = {path: path.read_bytes() for path in self.history.rglob('snapshot-*.md')}
        current = {path.name: path.read_bytes() for path in self.notes.glob('Knowledge-*.md')}
        second = batch.source_publication_name(fixture()['cards'][1]['source'])
        replace = os.replace
        def failure(source, destination, *args, **kwargs):
            if destination == second:
                raise OSError('synthetic replacement interruption')
            return replace(source, destination, *args, **kwargs)
        with patch('os.replace', side_effect=failure):
            for _ in range(3):
                with self.assertRaises(batch.BatchPublicationRejected):
                    self.publish_owned(fixture('r9'))
                self.assertEqual({path.name: path.read_bytes() for path in self.notes.glob('Knowledge-*.md')}, current)
                self.assertTrue(all(path.read_bytes() == raw for path, raw in prior.items()))
                self.assertFalse(batch.publication_pending(self.notes))
        self.publish_owned(fixture('r9'))
        for directory in self.history.iterdir():
            rows = [curation.parse_publication(path.read_bytes())['batch']['cards'][0]['source'] for path in directory.iterdir()]
            self.assertCountEqual([ref['revision'] for ref in rows], [f'r{number}' for number in range(2, 9)])

    def crash(self, boundary):
        script = r'''
import os,pathlib,sys
sys.path.insert(0,sys.argv[1])
import batch,workvault
from test_source_publication import fixture
root=pathlib.Path(sys.argv[2]);value=fixture('r2')
first=batch.source_publication_name(value['cards'][0]['source'])
replace=os.replace
def stop(source,destination,*args,**kwargs):
    if destination==first and sys.argv[3]=='before':
        print('owned native publication boundary',flush=True);sys.stdin.buffer.read(1)
    result=replace(source,destination,*args,**kwargs)
    if destination==first and sys.argv[3]=='after':
        print('owned native publication boundary',flush=True);sys.stdin.buffer.read(1)
    return result
os.replace=stop
with batch.publication_batch(root/'vault',root/'snapshots',value):
    for card in value['cards']:
        name=batch.source_publication_name(card['source'])
        workvault.publish_curated_batch(root/'vault',root/'snapshots'/name[:-3],
            {'schema_version':value['schema_version'],'cards':[card]},publication=name)
'''
        process = subprocess.Popen([sys.executable, '-B', '-I', '-S', '-c', script,
            str(Path(__file__).parent), str(self.root), boundary], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'})
        try:
            ready, _, _ = select.select([process.stdout], [], [], 10)
            self.assertTrue(ready, 'synthetic native publisher did not reach boundary')
            line = process.stdout.readline()
            if line != b'owned native publication boundary\n':
                self.fail('synthetic native publisher failed: ' + process.stderr.read().decode())
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_actual_kill_before_and_after_native_rename_recovers_exact_last_good(self):
        for boundary in ('before', 'after'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary)
                self.notes, self.history = self.root / 'vault', self.root / 'snapshots'
                self.publish_owned(fixture())
                human = self.notes / 'Human.md'
                human.write_bytes(b'Keep authored note unchanged.')
                before = {path.name: path.read_bytes() for path in self.notes.glob('Knowledge-*.md')}
                self.crash(boundary)
                self.assertTrue(batch.publication_pending(self.notes))
                for name in before:
                    with self.assertRaises(vault.WorkVaultError):
                        vault.status(self.notes, publication=name)
                value = json.loads((self.notes / batch.JOURNAL).read_bytes())
                self.assertEqual(value['schema'], 'workvault-owned-batch-v2')
                self.assertNotIn('repository', value)
                self.assertNotIn('commit', value)
                self.assertEqual([row['source'] for row in value['rows']], [card['source'] for card in fixture('r2')['cards']])
                self.assertTrue(batch.recover_publication_batch(self.notes, self.history))
                self.assertEqual({path.name: path.read_bytes() for path in self.notes.glob('Knowledge-*.md')}, before)
                self.assertFalse(batch.publication_pending(self.notes))
                self.assertEqual(list(self.notes.glob('.workvault-stage-*')), [])
                self.assertEqual(human.read_bytes(), b'Keep authored note unchanged.')
                self.assertFalse(batch.recover_publication_batch(self.notes, self.history))

    def test_interrupted_first_generation_removes_only_owned_native_notes(self):
        self.notes.mkdir(mode=0o700)
        human = self.notes / 'Human.md'
        human.write_bytes(b'Never remove authored notes.')
        self.crash('after')
        self.assertTrue(list(self.notes.glob('Knowledge-*.md')))
        self.assertTrue(batch.recover_publication_batch(self.notes, self.history))
        self.assertEqual(list(self.notes.glob('Knowledge-*.md')), [])
        self.assertEqual(human.read_bytes(), b'Never remove authored notes.')
        self.assertFalse(batch.publication_pending(self.notes))

    def test_native_journal_source_tampering_refuses_before_recovery_writes(self):
        self.publish_owned(fixture())
        self.crash('after')
        journal = self.notes / batch.JOURNAL
        original = json.loads(journal.read_bytes())
        for key, replacement in [('kind', 'drive'), ('account', 'other'), ('item_id', '456'),
                                 ('revision', 'other'), ('content_hash', 'sha256:' + 'f' * 64)]:
            value = copy.deepcopy(original)
            value['rows'][0]['source'][key] = replacement
            journal.write_bytes(batch._encoded(value))
            before = self.files()
            with self.subTest(key=key), self.assertRaises(vault.WorkVaultError):
                batch.recover_publication_batch(self.notes, self.history)
            self.assertEqual(self.files(), before)
        journal.write_bytes(batch._encoded(original))
        self.assertTrue(batch.recover_publication_batch(self.notes, self.history))

    def test_committed_native_cleanup_recovery_keeps_new_generation(self):
        self.publish_owned(fixture())
        unlink = vault._unlink_owned
        def failure(directory, name, identity):
            if name == 'new-0':
                raise OSError('synthetic cleanup interruption')
            return unlink(directory, name, identity)
        with patch.object(vault, '_unlink_owned', failure), self.assertRaises(OSError):
            self.publish_owned(fixture('r2'))
        committed = {path.name: path.read_bytes() for path in self.notes.glob('Knowledge-*.md')}
        self.assertTrue(batch.publication_pending(self.notes))
        self.assertTrue(batch.recover_publication_batch(self.notes, self.history))
        self.assertEqual({path.name: path.read_bytes() for path in self.notes.glob('Knowledge-*.md')}, committed)
        self.assertTrue(all(curation.parse_publication(raw)['batch']['cards'][0]['source']['revision'] == 'r2' for raw in committed.values()))

    def test_v1_exact_publication_bytes_remain_canonical(self):
        from test_publisher_contract import fixture as v1_fixture
        value = v1_fixture()
        result = self.publish(value)
        raw = (self.notes / vault.MANAGED).read_bytes()
        self.assertEqual(raw, curation.render_publication(value, result['published_at']))
        self.assertEqual(result['schema_version'], 'workvault-status-v1')
        self.assertEqual(batch.publication_name('example/team-wiki', 'knowledge/alpha.md'),
                         'Knowledge-767f60e1827e05c91a8c9125.md')


if __name__ == '__main__':
    unittest.main()
