"""Real v2 provenance preserves precision without changing the v1 timestamp contract."""

import copy
import hashlib
import unittest

import collector
import curation
import sources
from test_curation import fixture, source_fixture
from test_sources import locator


class SourcePrecisionTests(unittest.TestCase):
    def test_collector_orders_mixed_precision_listing_and_fetch_observations(self):
        body = b'Synthetic catalog entity.'
        listing = dict(locator('backstage'), author=None, revision=None, event_at=None, modified_at=None,
                       observed_at='2026-10-07T12:00:00Z')
        ref = dict(listing, schema_version=sources.SOURCE_SCHEMA,
                   content_hash='sha256:' + hashlib.sha256(body).hexdigest(),
                   observed_at='2026-10-07T12:00:00.123000Z')
        record = {'reference': ref, 'body': body, 'draft': False, 'supersedes': [], 'dependencies': []}
        card = dict(source_key='["backstage","workspace-fixture","C123","123.456"]', title='Entity',
                    summary='A catalog entity exists.', details='', date=None, draft=False,
                    uncertainty=['Source event date unavailable.'], source_links=[ref['citation']],
                    evidence_kind='source_observation')
        result = collector.assemble_source_batch([listing], [record], [card])
        self.assertEqual(result['cards'][0]['source']['observed_at'], '2026-10-07T12:00:00.123000Z')
        listing['observed_at'], ref['observed_at'] = ref['observed_at'], listing['observed_at']
        with self.assertRaisesRegex(ValueError, 'metadata mismatch'):
            collector.assemble_source_batch([listing], [record], [card])

    def test_fractional_native_times_survive_page_validation(self):
        ref = dict(locator('jira'), event_at='2026-10-07T12:00:00.123000Z',
                   modified_at='2026-10-07T12:00:00.456000Z',
                   observed_at='2026-10-07T12:00:00.789000Z')
        page = {'items': [ref], 'next_cursor': None, 'complete': True, 'gaps': []}
        self.assertEqual(sources.validate_page(page), page)

    def test_mixed_precision_source_chronology_uses_instants(self):
        ref = dict(locator(), event_at='2026-10-07T12:00:00Z',
                   modified_at='2026-10-07T12:00:00.123000Z',
                   observed_at='2026-10-07T12:00:00.456000Z')
        self.assertEqual(sources.validate_locator(ref), ref)
        for field in ('event_at', 'modified_at'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'chronology'):
                sources.validate_locator(dict(ref, observed_at='2026-10-07T12:00:00Z',
                                              **{field: '2026-10-07T12:00:00.000001Z'}))

    def test_precise_publication_roundtrips_and_cannot_predate_observation(self):
        batch = source_fixture()
        batch['cards'][0]['source']['observed_at'] = '2026-10-07T12:00:00.123000Z'
        stamp = '2026-10-07T12:00:00.456000Z'
        raw = curation.render_publication(batch, stamp)
        checkpoint = curation.parse_publication(raw)
        self.assertEqual(checkpoint['batch'], batch)
        self.assertEqual(checkpoint['published_at'], stamp)
        with self.assertRaisesRegex(ValueError, 'predates'):
            curation.render_publication(batch, '2026-10-07T12:00:00Z')

    def test_whole_second_observation_precedes_fractional_publication(self):
        batch = source_fixture()
        batch['cards'][0]['source']['observed_at'] = '2026-10-07T12:00:00Z'
        raw = curation.render_publication(batch, '2026-10-07T12:00:00.000001Z')
        self.assertEqual(curation.parse_publication(raw)['batch'], batch)

    def test_supersession_orders_mixed_precision_event_times(self):
        batch = source_fixture()
        ref = batch['cards'][0]['source']
        ref.update(event_at='2026-10-05T10:00:00.123000Z')
        prior = dict(ref, revision='earlier', content_hash='sha256:' + 'b' * 64,
                     event_at='2026-10-05T10:00:00Z')
        batch['cards'][0]['supersedes'] = [prior]
        self.assertEqual(curation.validate_source_batch(batch), batch)
        bad = copy.deepcopy(batch)
        bad['cards'][0]['supersedes'][0]['event_at'] = '2026-10-05T10:00:00.456000Z'
        with self.assertRaisesRegex(ValueError, 'supersession'):
            curation.validate_source_batch(bad)

    def test_noncanonical_fraction_and_v1_fraction_remain_refused(self):
        for stamp in ('2026-10-07T12:00:00.123Z', '2026-10-07T12:00:00.1234567Z',
                      '2026-10-07T12:00:00.123456+00:00', '2026-02-30T12:00:00.123456Z'):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                sources.validate_locator(dict(locator(), observed_at=stamp))
        with self.assertRaises(ValueError):
            curation.render_publication(fixture(), '2026-10-07T12:00:00.123000Z')


if __name__ == '__main__':
    unittest.main()
