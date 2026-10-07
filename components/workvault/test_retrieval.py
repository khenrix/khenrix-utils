"""Synthetic consumption contracts; no vault, source transport or model calls."""
import json
import unittest

import curation
try:
    import retrieval
except ImportError:
    retrieval = None


def publication(number=1, summary='Carrier capacity is not guaranteed.', commit='1' * 40):
    path = f'knowledge/item-{number}.md'
    card = {'path': path, 'title': f'Carrier item {number}', 'summary': summary,
            'details': 'Curated evidence only.', 'date': '2026-10-07', 'draft': False,
            'uncertainty': ['Owner confirmation is pending.'], 'source_links': [],
            'cross_references': []}
    batch = {'schema_version': 'workvault-curated-batch-v1', 'repository': 'example/wiki',
             'commit': commit, 'inventory_complete': True, 'discovered_paths': [path],
             'indexed_paths': [path], 'requested_paths': [path], 'cards': [card]}
    return curation.parse_publication(curation.render_publication(batch, '2026-10-07T12:00:00Z'))


def reference(number=1, commit='1' * 40):
    return {'repository': 'example/wiki', 'commit': commit, 'path': f'knowledge/item-{number}.md'}


def native_publication(kind='jira', revision='r1', summary='Carrier approval is uncertain.'):
    source = {'schema_version': 'workvault-source-v2', 'kind': kind, 'account': 'company',
              'container': 'selected', 'item_id': '123', 'revision': revision, 'author': None,
              'event_at': None, 'modified_at': None, 'observed_at': '2026-10-07T11:00:00Z',
              'citation': f'https://example.invalid/{kind}/123?revision={revision}',
              'content_hash': 'sha256:' + ('a' if revision == 'r1' else 'b') * 64}
    card = {'source': source, 'title': 'Carrier approval', 'summary': summary, 'details': '',
            'date': None, 'draft': True, 'uncertainty': ['Conflicting owner reports.'],
            'source_links': [], 'evidence_kind': 'owner_synthesis', 'supersedes': [], 'dependencies': []}
    return curation.parse_publication(curation.render_publication(
        {'schema_version': 'workvault-curated-batch-v2', 'cards': [card]}, '2026-10-07T12:00:00Z'))


class RetrievalContract(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(retrieval, 'pure curated retrieval helper is absent')

    def test_search_is_bounded_relevant_and_retains_negation_uncertainty(self):
        reader = retrieval.CuratedReader([publication(i) for i in range(1, 8)])
        rows = reader.search('CARRIER capacity', limit=5)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]['card']['summary'], 'Carrier capacity is not guaranteed.')
        self.assertEqual(rows[0]['card']['uncertainty'], ['Owner confirmation is pending.'])
        self.assertEqual(reader.search('unrelated zebra'), [])
        for limit in (0, 6, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                reader.search('carrier', limit=limit)
        for query in ('', ' ' * 10, 'x' * 513, ' '.join(str(i) for i in range(17))):
            with self.subTest(query=query), self.assertRaises(ValueError):
                reader.search(query)

    def test_get_retains_actual_mixed_revisions_without_rewriting_native_identity(self):
        native = native_publication()
        records = [publication(), publication(2, commit='2' * 40), native,
                   native_publication(revision='r2', summary='Carrier approval is denied.')]
        reader = retrieval.CuratedReader(records)
        self.assertEqual(reader.get(reference(2, '2' * 40))['reference']['commit'], '2' * 40)
        row = reader.get(native['batch']['cards'][0]['source'])
        self.assertEqual(row['reference'], native['batch']['cards'][0]['source'])
        self.assertIsNone(row['card']['date'])
        self.assertTrue(row['card']['draft'])
        self.assertEqual(len(reader.search('carrier')), 4)
        self.assertIsNone(reader.get(reference(99)))
        row['card']['summary'] = 'Mutated caller copy'
        self.assertNotEqual(reader.get(native['batch']['cards'][0]['source'])['card']['summary'], row['card']['summary'])

    def test_follow_requires_one_exact_cited_curated_target(self):
        origin, target = publication(), publication(2)
        origin['batch']['cards'][0]['cross_references'] = ['knowledge/item-2.md']
        reader = retrieval.CuratedReader([origin, target, publication(3)])
        self.assertEqual(reader.follow(reference(), reference(2))['reference'], reference(2))
        for target_ref in (reference(3), reference(2, '2' * 40), 'https://example.invalid/arbitrary'):
            with self.subTest(target=target_ref), self.assertRaises(ValueError):
                reader.follow(reference(), target_ref)
        native, dependency = native_publication(), native_publication('drive')
        native['batch']['cards'][0]['dependencies'] = [dependency['batch']['cards'][0]['source']]
        reader = retrieval.CuratedReader([native, dependency])
        self.assertEqual(reader.follow(native['batch']['cards'][0]['source'], dependency['batch']['cards'][0]['source'])['card']['source']['kind'], 'drive')

    def test_retired_references_are_excluded_without_interpreting_source_text(self):
        reader = retrieval.CuratedReader([publication(), publication(2, 'Carrier owner said withdraw the draft.')], retired=[reference()])
        self.assertIsNone(reader.get(reference()))
        self.assertEqual([row['reference'] for row in reader.search('carrier')], [reference(2)])
        self.assertEqual(reader.injection('capacity', token_counter=lambda text: 1), '')

    def test_no_scan_cap_silently_hides_late_card_and_explicit_limit_refuses(self):
        publications = [publication(i, 'Ordinary curated item.') for i in range(1, 4098)]
        publications[-1]['batch']['cards'][0]['summary'] = 'Unique late needle.'
        reader = retrieval.CuratedReader(publications)
        self.assertEqual(reader.search('needle')[0]['reference'], reference(4097))
        with self.assertRaisesRegex(ValueError, 'collection limit'):
            retrieval.CuratedReader([publication()] * (retrieval.MAX_PUBLICATIONS + 1))

    def test_no_raw_notes_paths_or_conflicting_source_revisions_are_admitted(self):
        bad = publication()
        bad['batch']['cards'][0]['path'] = '../human-note.md'
        for records in ([bad], [{'path': 'Human.md', 'body': 'Personal note'}], [publication(), publication()]):
            with self.subTest(records=records), self.assertRaises(ValueError):
                retrieval.CuratedReader(records)
        first, second = native_publication(), native_publication()
        second['batch']['cards'][0]['source']['content_hash'] = 'sha256:' + 'c' * 64
        with self.assertRaises(ValueError):
            retrieval.CuratedReader([first, second])
        reader = retrieval.CuratedReader([publication()])
        with self.assertRaises(ValueError):
            reader.get({'repository': 'example/wiki', 'commit': '1' * 40, 'path': '../secret.md'})

    def test_injection_counts_final_serialization_with_metadata_and_preserves_conflicts(self):
        first = native_publication(summary='Carrier capacity is not guaranteed.')
        second = native_publication('drive', summary='Carrier capacity is guaranteed according to another owner.')
        reader = retrieval.CuratedReader([first, second, publication(3)])
        counted = []
        def counter(text):
            counted.append(text)
            return 500 if len(json.loads(text)) <= 2 else 501
        injected = reader.injection('carrier', token_counter=counter)
        rows = json.loads(injected)
        self.assertEqual(len(rows), 2)
        self.assertIn(injected, counted)
        self.assertTrue(all('published_at' in row and 'reference' in row for row in rows))
        self.assertTrue(all('details' not in row['card'] for row in rows))
        self.assertEqual(rows[0]['card']['summary'], first['batch']['cards'][0]['summary'])
        self.assertEqual(rows[1]['card']['summary'], second['batch']['cards'][0]['summary'])
        self.assertEqual(rows[0]['card']['uncertainty'], ['Conflicting owner reports.'])
        self.assertEqual(rows[0]['reference']['content_hash'], 'sha256:' + 'a' * 64)

    def test_aggregate_budget_is_not_individual_sum_and_never_truncates_summary(self):
        reader = retrieval.CuratedReader([publication(), publication(2)])
        counter = lambda text: 501 if len(json.loads(text)) == 2 else 250
        rows = json.loads(reader.injection('carrier', token_counter=counter))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['card']['summary'], 'Carrier capacity is not guaranteed.')
        self.assertEqual(reader.injection('carrier', token_counter=lambda text: 501), '')

    def test_missing_counter_disables_only_injection_and_invalid_counter_fails_closed(self):
        reader = retrieval.CuratedReader([publication()])
        self.assertEqual(reader.injection('carrier'), '')
        self.assertTrue(reader.search('carrier'))
        self.assertIsNotNone(reader.get(reference()))
        self.assertEqual(reader.injection('zebra', token_counter=lambda text: self.fail('irrelevant injection counted')), '')
        for counter in (lambda text: True, lambda text: -1, lambda text: 2.5, 'unsupported'):
            with self.subTest(counter=counter), self.assertRaises(ValueError):
                reader.injection('carrier', token_counter=counter)

    def test_v1_injection_contains_exact_clickable_revision_citation_and_counts_it(self):
        reader = retrieval.CuratedReader([publication(2, commit='2' * 40)])
        counted = []
        def counter(text):
            counted.append(text)
            return 100
        text = reader.injection('carrier', token_counter=counter)
        expected = 'https://github.com/example/wiki/blob/' + '2' * 40 + '/knowledge/item-2.md'
        self.assertEqual(json.loads(text)[0].get('citation'), expected)
        self.assertIn(text, counted)


if __name__ == '__main__':
    unittest.main()
