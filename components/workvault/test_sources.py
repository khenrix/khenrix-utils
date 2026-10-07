"""Synthetic multi-source metadata contracts; no service access."""

import copy
import unittest

try:
    import sources
except ImportError:
    sources = None


def reference(kind='slack'):
    return {
        'schema_version': 'workvault-source-v2', 'kind': kind,
        'account': 'workspace-fixture', 'container': 'C123', 'item_id': '123.456',
        'revision': 'revision-2', 'author': 'author-fixture',
        'event_at': '2026-10-05T10:00:00Z', 'observed_at': '2026-10-07T12:00:00Z',
        'modified_at': '2026-10-05T10:00:00Z',
        'citation': 'https://example.invalid/source/123.456?q=&copy;',
        'content_hash': 'sha256:' + 'a' * 64,
    }


def locator(kind='slack'):
    value = reference(kind)
    del value['content_hash']
    value['schema_version'] = 'workvault-source-locator-v2'
    return value


class SourcesTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(sources, 'versioned source contract is missing')

    def test_source_identity_is_not_a_fake_github_path_and_revision_changes_independently(self):
        for kind in ('github', 'slack', 'drive', 'gmail', 'jira', 'backstage'):
            value = reference(kind)
            clean = sources.validate_reference(value)
            self.assertEqual(clean, value)
            self.assertNotIn('commit', clean)
            self.assertNotIn('path', clean)
        original = reference()
        changed = dict(original, revision='revision-3', content_hash='sha256:' + 'b' * 64)
        self.assertEqual(sources.source_key(original), '["slack","workspace-fixture","C123","123.456"]')
        self.assertEqual(sources.source_key(original), sources.source_key(changed))
        self.assertNotEqual(sources.revision_key(original), sources.revision_key(changed))
        self.assertNotEqual(sources.source_key(original), sources.source_key(dict(original, account='other-workspace')))

    def test_reference_fields_types_bounds_and_unknowns_refuse(self):
        for key in reference():
            value = reference()
            del value[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                sources.validate_reference(value)
        for changes in ({'raw_body': 'RAW_CANARY'}, {'kind': 'unknown'}, {'item_id': 123.456},
                        {'revision': ''}, {'author': False}, {'account': 'x' * 513},
                        {'container': 'nul\x00'}, {'content_hash': 'sha256:' + 'A' * 64},
                        {'citation': 'https://user:secret@example.invalid/'},
                        {'observed_at': '2026-10-04T12:00:00Z'}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                sources.validate_reference(dict(reference(), **changes))
        absent = dict(reference(), revision=None, author=None)
        self.assertEqual(sources.validate_reference(absent), absent)
        clean = sources.validate_reference(reference())
        clean['item_id'] = 'changed'
        self.assertEqual(reference()['item_id'], '123.456')

    def test_bounded_pages_keep_cursors_and_honest_gaps_without_raw_fields(self):
        value = {'items': [locator()], 'next_cursor': 'page-2', 'complete': False,
                 'gaps': [{'code': 'retention_gap', 'count': None}]}
        try:
            clean = sources.validate_page(value)
        except ValueError as error:
            self.fail('metadata-only page was refused: ' + str(error))
        self.assertEqual(clean, value)
        terminal = dict(value, next_cursor=None, complete=True)
        self.assertEqual(sources.validate_page(terminal)['gaps'], value['gaps'])
        for changes in ({'next_cursor': None}, {'complete': 1}, {'raw_body': 'RAW_CANARY'},
                        {'items': [locator()] * 2}, {'next_cursor': 'x' * 2049},
                        {'gaps': [{'code': 'retention_gap', 'count': -1}]},
                        {'gaps': [{'code': 'retention_gap', 'count': True}]}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                sources.validate_page(dict(value, **changes))
        rows = [dict(locator(), item_id=str(i)) for i in range(101)]
        with self.assertRaises(ValueError):
            sources.validate_page(dict(value, items=rows))
        complete = dict(terminal, gaps=[])
        clean = sources.validate_page(complete)
        clean['items'][0]['item_id'] = 'changed'
        self.assertEqual(complete['items'][0]['item_id'], '123.456')

    def test_history_pages_keep_multiple_real_revisions_but_refuse_conflicting_revision_hashes(self):
        newer = dict(locator(), revision='revision-3')
        page = {'items': [locator(), newer], 'next_cursor': None, 'complete': True, 'gaps': []}
        try:
            clean = sources.validate_page(page)
        except ValueError as error:
            self.fail('real source history was refused: ' + str(error))
        self.assertEqual(clean['items'], [locator(), newer])
        conflicting = dict(reference(), content_hash='sha256:' + 'b' * 64)
        with self.assertRaises(ValueError):
            sources.validate_references([reference(), conflicting])

    def test_list_locators_need_no_unfetched_hash_and_unknown_event_time_stays_unavailable(self):
        self.assertTrue(hasattr(sources, 'validate_locator'), 'metadata locator validation is missing')
        value = dict(locator('backstage'), revision=None, author=None, event_at=None, modified_at=None)
        self.assertEqual(sources.validate_locator(value), value)
        self.assertEqual(sources.source_key(value), '["backstage","workspace-fixture","C123","123.456"]')
        for bad in (dict(value, content_hash='sha256:' + 'a' * 64), dict(value, raw_body='RAW_CANARY')):
            with self.assertRaises(ValueError):
                sources.validate_locator(bad)
        with self.assertRaises(ValueError):
            sources.validate_page({'items': [reference()], 'next_cursor': None, 'complete': True, 'gaps': []})

    def test_native_creation_modification_and_observation_times_stay_separate(self):
        value = dict(reference('drive'), modified_at='2026-10-06T11:00:00Z')
        try:
            clean = sources.validate_reference(value)
        except ValueError as error:
            self.fail('authoritative modification timestamp was refused: ' + str(error))
        self.assertEqual(clean['event_at'], '2026-10-05T10:00:00Z')
        self.assertEqual(clean['modified_at'], '2026-10-06T11:00:00Z')
        self.assertEqual(clean['observed_at'], '2026-10-07T12:00:00Z')
        with self.assertRaises(ValueError):
            sources.validate_reference(dict(value, modified_at='2026-10-08T11:00:00Z'))

    def test_change_checkpoint_is_separate_from_page_continuation(self):
        self.assertTrue(hasattr(sources, 'validate_changes'), 'change checkpoint validation is missing')
        page = {'items': [], 'next_cursor': None, 'complete': True, 'gaps': []}
        result = sources.validate_changes({'page': page, 'checkpoint': 'terminal-native-token'})
        self.assertEqual(result, {'page': page, 'checkpoint': 'terminal-native-token'})
        self.assertIsNone(sources.validate_changes({'page': page, 'checkpoint': None})['checkpoint'])
        pending = dict(page, next_cursor='page-2', complete=False)
        self.assertEqual(sources.validate_changes({'page': pending, 'checkpoint': None})['page'], pending)
        for bad in ({'page': pending, 'checkpoint': 'premature-token'},
                    {'page': page, 'checkpoint': 'x' * 2049},
                    {'page': page, 'checkpoint': 'token\nraw'},
                    {'page': page, 'checkpoint': True},
                    {'page': page, 'checkpoint': None, 'raw_body': 'RAW_CANARY'}):
            with self.subTest(keys=list(bad)), self.assertRaises(ValueError):
                sources.validate_changes(bad)


if __name__ == '__main__':
    unittest.main()
