"""Pure directory reports from synthetic GitHub routing metadata."""
import copy
from datetime import date, timedelta
import hashlib
import json
import unittest
from unittest.mock import patch

from directory import directory_report
from generated_directory import generated_directory_report


PATH = 'Docs/Guides/index.md'


def pin(path, **changes):
    return {'repository': 'example/directory', 'commit': 'a' * 40, 'path': path, **changes}


def route(**changes):
    return {'repository': 'example/platform', 'entrypoint': PATH,
        'use_when': 'Synthetic platform questions about shared services and their ownership.',
        'caretaker': 'Synthetic operator', 'slack_channel': '#synthetic',
        'status': 'active', 'review_date': '2026-10-06', **changes}


def normalized(records=None, selected=None, **changes):
    arguments = dict(index=pin('index.json'), health=pin('health.md'),
        records=[route()] if records is None else records,
        selected=[('example/platform', PATH)] if selected is None else selected,
        existing_repository='example/current-wiki', inventory_complete=True,
        target_commits={'example/platform': 'b' * 40}, stale_before='2026-10-06')
    arguments.update(changes)
    return directory_report(**arguments)


def entry(**changes):
    return {'team': 'Synthetic area', 'repo': 'example/platform',
        'use_when': route()['use_when'], 'caretaker': 'operator@example.invalid',
        'access': [{'kind': 'repo', 'path': 'Docs/Guides', 'entrypoint': 'index.md'}],
        'status': 'stable', 'last_commit': '2026-07-08', 'review_by': '2026-10-06', **changes}


def envelope(entries=None, **changes):
    entries = [entry()] if entries is None else entries
    return {'generated_at': '2026-10-06', 'count': len(entries), 'knowledge_bases': entries, **changes}


def health(count=1, scanned=None, day='2026-10-06', findings=False):
    scanned = count if scanned is None else scanned
    return (f'# Index health\n\nGenerated {day}. Scanned {scanned} tagged repo(s), {count} registered.\n\n'
        + ('## Past review, or quiet (1)\n' if findings else
           'No findings. Everything tagged is registered, valid, fresh and owned.\n')).encode()


def generated(data=None, **changes):
    data = envelope() if data is None else data
    arguments = dict(index_bytes=json.dumps(data).encode(), health_bytes=health(data['count']),
        index=pin('index.json'), health=pin('health.md'),
        selected=[('example/platform', PATH)], existing_repository='example/current-wiki',
        target_commits={'example/platform': 'b' * 40})
    arguments.update(changes)
    return generated_directory_report(**arguments)


class DirectoryTests(unittest.TestCase):
    def test_pinned_normalized_report_has_stable_identity_and_exact_citation(self):
        result = normalized()
        card = result['cards'][0]
        self.assertEqual(card['identity'], ['example/platform', PATH])
        self.assertEqual(card['card_id'], 'Directory-' +
            hashlib.sha256(('example/platform\n' + PATH).encode()).hexdigest()[:24])
        self.assertEqual(card['entrypoint_citation'],
            'https://github.com/example/platform/blob/' + 'b' * 40 + '/' + PATH)
        self.assertEqual(card['directory_provenance'], {'index': pin('index.json'), 'health': pin('health.md')})
        self.assertFalse(result['full_source_coverage'])
        self.assertIsNone(result['source_checkpoint'])

    def test_selection_and_inventory_do_not_overstate_coverage(self):
        records = [route(), route(repository='example/other')]
        result = normalized(records)
        self.assertEqual(result['uncovered_identities'], [['example/other', PATH]])
        self.assertFalse(result['full_directory_coverage'])
        result = normalized(inventory_complete=False)
        self.assertFalse(result['full_directory_coverage'])
        self.assertEqual(result['existing_registration'], 'unknown_incomplete_inventory')
        self.assertEqual(normalized()['existing_registration'], 'missing_registration')

    def test_existing_source_merges_case_aliases_without_new_card(self):
        value = route(repository='Example/Current-Wiki')
        result = normalized([value, value], selected=[('EXAMPLE/CURRENT-WIKI', PATH)],
            existing_repository='EXAMPLE/current-wiki', target_commits={})
        self.assertEqual(result['cards'], [])
        self.assertEqual(result['existing_registration'], 'registered_existing_source')
        self.assertEqual(result['existing_routes'][0]['repository'], 'example/current-wiki')
        self.assertEqual(result['duplicate_record_count'], 1)

    def test_case_aliases_share_identity_but_declared_path_case_remains_distinct(self):
        records = [route(repository='Example/Platform'), route(), route(entrypoint='docs/Guides/index.md')]
        result = normalized(records, selected=[('EXAMPLE/PLATFORM', PATH),
            ('Example/Platform', 'docs/Guides/index.md')],
            target_commits={'EXAMPLE/platform': 'b' * 40, 'example/PLATFORM': 'b' * 40},
            index=pin('index.json', repository='EXAMPLE/Directory'))
        self.assertEqual(result['registered_identity_count'], 2)
        self.assertEqual(result['duplicate_record_count'], 1)
        self.assertEqual(len({card['card_id'] for card in result['cards']}), 2)
        self.assertEqual(result['directory_provenance']['index']['repository'], 'example/directory')

    def test_conflicting_normalized_metadata_selection_and_pins_refuse(self):
        changes = (
            {'records': [route(), route(repository='EXAMPLE/PLATFORM', caretaker='Different operator')]},
            {'selected': [('example/platform', PATH), ('EXAMPLE/PLATFORM', PATH)]},
            {'selected': [('example/missing', PATH)]},
            {'target_commits': {'example/platform': 'b' * 40, 'EXAMPLE/PLATFORM': 'c' * 40}},
            {'target_commits': {'example/missing': 'b' * 40}},
            {'target_commits': {'example/platform': 'main'}},
            {'health': pin('health.md', commit='c' * 40)},
            {'health': pin('health.md', repository='example/other')},
            {'inventory_complete': 1},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalized(**change)

    def test_normalized_unknown_fields_count_and_report_size_are_bounded(self):
        for records in ([route(raw_body='RAW_ONLY_FIXTURE')], [route()] * 4097):
            with self.assertRaises(ValueError) as caught:
                normalized(records)
            self.assertNotIn('RAW_ONLY_FIXTURE', str(caught.exception))
        records = [route(repository=f'example/area-{i}', use_when='x' * 4096) for i in range(300)]
        with self.assertRaises(ValueError):
            normalized(records, selected=[(r['repository'], PATH) for r in records], target_commits={})

    def test_normalized_stale_and_unknown_values_are_explicit(self):
        result = normalized([route(status='deprecated', review_date='2026-10-05')])
        self.assertEqual(result['cards'][0]['flags'], ['stale_review', 'deprecated'])
        result = normalized([route(status='unknown', review_date=None)], stale_before=None, target_commits={})
        self.assertEqual(result['cards'][0]['flags'],
            ['review_date_unknown', 'status_unrecognized', 'target_commit_unresolved'])

    def test_generated_dates_are_not_target_revisions_or_generic_review_dates(self):
        result = generated()
        card = result['cards'][0]
        self.assertEqual((card['last_commit'], card['review_by']), ('2026-07-08', '2026-10-06'))
        self.assertEqual(card['target_commit'], 'b' * 40)
        self.assertEqual(card['status'], 'stable')
        self.assertIsNone(card['slack_channel'])
        self.assertNotIn('review_date', card)
        self.assertEqual(card['flags'], [])

    def test_generated_mcp_first_is_inert_and_first_repo_uses_defaults(self):
        value = entry(access=[{'kind': 'mcp', 'url': 'https://synthetic.invalid/mcp', 'auth': 'none'},
            {'kind': 'repo', 'entrypoint': '.meta/instructions.md'},
            {'kind': 'repo', 'entrypoint': 'other.md'}])
        result = generated(envelope([value]), selected=[('example/platform', '.meta/instructions.md')])
        card = result['cards'][0]
        self.assertEqual(card['entrypoint'], '.meta/instructions.md')
        self.assertEqual((card['mcp_access_count'], card['repo_access_count']), (1, 2))
        self.assertNotIn('synthetic.invalid', json.dumps(result))

    def test_generated_missing_values_have_explicit_defaults_and_no_citation(self):
        value = entry(access=[{'kind': 'repo'}])
        for name in ('status', 'last_commit', 'review_by'):
            del value[name]
        card = generated(envelope([value]), selected=[('example/platform', 'README.md')],
            target_commits={})['cards'][0]
        self.assertEqual(card['status'], 'stable')
        self.assertIsNone(card['review_by'])
        self.assertIsNone(card['last_commit'])
        self.assertIsNone(card['entrypoint_citation'])
        self.assertEqual(card['flags'], ['target_commit_unresolved', 'review_by_unknown', 'last_commit_unknown'])

    def test_generated_staleness_is_generation_day_and_strictly_over_90_days(self):
        cases = ((90, '2026-10-06', 'stable', []),
            (91, '2026-10-05', 'deprecated', ['deprecated', 'review_overdue', 'last_commit_stale']),
            (0, '2026-10-07', 'draft', ['draft']))
        for days, review, status, flags in cases:
            with self.subTest(days=days):
                last = (date(2026, 10, 6) - timedelta(days=days)).isoformat()
                card = generated(envelope([entry(last_commit=last, review_by=review, status=status)]))['cards'][0]
                self.assertEqual(card['flags'], flags)

    def test_generated_tagged_health_does_not_grant_coverage_or_remove_existing_source(self):
        for findings in (False, True):
            with self.subTest(findings=findings):
                result = generated(health_bytes=health(findings=findings))
                self.assertFalse(result['inventory_complete'])
                self.assertFalse(result['full_directory_coverage'])
                self.assertFalse(result['full_source_coverage'])
                self.assertIsNone(result['source_checkpoint'])
                self.assertFalse(result['existing_source_listed'])
                self.assertEqual(result['existing_source_action'], 'preserve')

    def test_generated_existing_source_merges_and_preserves_flags(self):
        result = generated(envelope([entry(repo='EXAMPLE/CURRENT-WIKI', status='deprecated',
            review_by='2026-10-05', last_commit='2026-07-07')]),
            selected=[('example/current-wiki', PATH)], target_commits={})
        self.assertTrue(result['existing_source_listed'])
        self.assertEqual(result['cards'], [])
        self.assertEqual(result['existing_routes'][0]['flags'],
            ['deprecated', 'review_overdue', 'last_commit_stale'])
        self.assertEqual(result['existing_source_action'], 'preserve')

    def test_generated_relative_path_normalization_preserves_root_and_case(self):
        card = generated(envelope([entry(access=[{'kind': 'repo', 'path': './Docs/Guides/',
            'entrypoint': './Index.md'}])]), selected=[('example/platform', 'Docs/Guides/Index.md')])['cards'][0]
        self.assertEqual(card['entrypoint'], 'Docs/Guides/Index.md')

    def test_generated_access_traversal_unknown_fields_and_mcp_only_refuse(self):
        accesses = (
            [{'kind': 'repo', 'path': '../secret'}], [{'kind': 'repo', 'path': '/secret'}],
            [{'kind': 'repo', 'path': 'safe', 'entrypoint': '../outside.md'}],
            [{'kind': 'repo', 'entrypoint': 'https://synthetic.invalid/file'}],
            [{'kind': 'repo', 'entrypoint': '%2e%2e/file'}], [{'kind': 'repo', 'entrypoint': 'a\\b'}],
            [{'kind': 'repo', 'entrypoint': '.'}], [{'kind': 'mcp', 'url': 'https://synthetic.invalid/mcp'}],
            [{'kind': 'repo', 'raw_body': 'RAW_ONLY_FIXTURE'}],
            [{'kind': 'mcp', 'url': 'https://synthetic.invalid/mcp', 'token': 'RAW_ONLY_FIXTURE'}, {'kind': 'repo'}],
        )
        for access in accesses:
            with self.subTest(access=access), self.assertRaises(ValueError) as caught:
                generated(envelope([entry(access=access)]))
            self.assertNotIn('RAW_ONLY_FIXTURE', str(caught.exception))

    def test_generated_envelope_types_dates_counts_and_unknown_fields_refuse(self):
        for change in ({'count': True}, {'count': 2}, {'raw_body': 'RAW_ONLY_FIXTURE'},
                       {'generated_at': '2026-02-30'}, {'knowledge_bases': {}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                generated(envelope(**change))
        for change in ({'last_commit': 'b' * 40}, {'review_by': '2026-02-30'}, {'status': 'active'},
                       {'raw_body': 'RAW_ONLY_FIXTURE'}, {'use_when': 'x' * 601},
                       {'access': [{'kind': 'repo'}] * 33}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                generated(envelope([entry(**change)]))

    def test_generated_enriched_alias_conflicts_refuse(self):
        data = envelope([entry(), entry(repo='EXAMPLE/PLATFORM')])
        self.assertEqual(generated(data)['duplicate_record_count'], 1)
        for change in ({'team': 'Different area'}, {'review_by': '2026-10-07'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                generated(envelope([entry(), entry(repo='EXAMPLE/PLATFORM', **change)]))

    def test_generated_health_and_revision_consistency_refuse(self):
        changes = ({'health_bytes': health(day='2026-10-05')}, {'health_bytes': health(count=2)},
            {'health_bytes': health(scanned=0)}, {'health_bytes': b'No findings.'},
            {'health': pin('health.md', commit='c' * 40)}, {'health_bytes': health() + health()},
            {'target_commits': {'example/platform': '2026-07-08'}})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                generated(**change)

    def test_generated_registration_json_duplicates_encoding_and_bytes_refuse(self):
        for value in (json.dumps(entry()).encode(), b'{"count":1,"count":1}', b'\xff',
                      b'x' * (1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                generated(index_bytes=value)

    def test_both_reports_are_pure_and_preserve_input_metadata(self):
        data = envelope()
        records = [route()]
        original_data, original_records = copy.deepcopy(data), copy.deepcopy(records)
        with patch('builtins.open', side_effect=AssertionError('IO prohibited')), \
             patch('subprocess.run', side_effect=AssertionError('child prohibited')), \
             patch('socket.socket', side_effect=AssertionError('network prohibited')):
            first, second = normalized(records), generated(data)
        self.assertEqual(data, original_data)
        self.assertEqual(records, original_records)
        first['cards'][0]['caretaker'] = 'changed result'
        second['cards'][0]['team'] = 'changed result'
        self.assertEqual(records, original_records)
        self.assertEqual(data, original_data)


if __name__ == '__main__':
    unittest.main()
