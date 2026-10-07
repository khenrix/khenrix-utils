import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from curation import parse_publication, render_publication, validate_curated_batch, validate_source_batch


STAMP = '2026-10-05T12:34:56Z'


def source_fixture(kind='slack'):
    from test_sources import reference
    return {'schema_version': 'workvault-curated-batch-v2', 'cards': [{
        'source': reference(kind), 'title': 'Synthetic source fact',
        'summary': 'A curated observation.', 'details': 'Earlier event 2026-10-01 retained.',
        'date': '2026-10-05', 'draft': True, 'uncertainty': ['Unverified proposal.'],
        'source_links': [reference(kind)['citation']], 'evidence_kind': 'source_observation',
        'supersedes': [dict(reference(kind), revision='revision-1', event_at='2026-10-01T10:00:00Z')],
        'dependencies': [dict(reference('jira'), container='PROJECT', item_id='PROJECT-1')],
    }]}


def fixture():
    return {
        'schema_version': 'workvault-curated-batch-v1',
        'repository': 'example/team-wiki',
        'commit': '1111111111111111111111111111111111111111',
        'inventory_complete': True,
        'discovered_paths': [
            'knowledge/alpha.md', 'knowledge/drafts/beta.md',
            'knowledge/unindexed.md',
        ],
        'indexed_paths': ['knowledge/alpha.md', 'knowledge/drafts/beta.md'],
        'requested_paths': ['knowledge/alpha.md', 'knowledge/drafts/beta.md'],
        'cards': [
            {
                'path': 'knowledge/alpha.md',
                'title': 'Synthetic Alpha',
                'summary': 'A curated synthetic fact.',
                'details': 'This is fixture content, not a copied workplace page.',
                'date': '2026-10-05',
                'draft': False,
                'uncertainty': ['The fixture does not prove a live service fact.'],
                'source_links': ['https://example.invalid/source/alpha'],
                'cross_references': ['knowledge/drafts/beta.md'],
            },
            {
                'path': 'knowledge/drafts/beta.md',
                'title': 'Synthetic Beta',
                'summary': 'A draft synthetic decision.',
                'details': 'Keep draft status explicit.',
                'date': '2026-10-04',
                'draft': True,
                'uncertainty': [],
                'source_links': [],
                'cross_references': ['knowledge/alpha.md'],
            },
        ],
    }


def with_path(path):
    batch = fixture()
    for field in ('discovered_paths', 'indexed_paths', 'requested_paths'):
        batch[field][0] = path
    batch['cards'][0]['path'] = path
    return batch


def with_card_count(count):
    batch = fixture()
    cards = []
    for number in range(count):
        card = copy.deepcopy(batch['cards'][0])
        card['path'] = f'knowledge/card-{number}.md'
        card['cross_references'] = []
        cards.append(card)
    paths = [card['path'] for card in cards]
    batch.update(
        cards=cards, discovered_paths=paths.copy(),
        indexed_paths=paths.copy(), requested_paths=paths.copy(),
    )
    return batch


class CurationTests(unittest.TestCase):
    def test_historical_v1_bytes_are_preserved_on_unchanged_read(self):
        data = render_publication(fixture(), STAMP)
        self.assertEqual(len(data), 2738)
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         '3b12f2c76ccc60e66427c3c21e4079e2650a7d840ede821f20df96f2c735920c')
        checkpoint = parse_publication(data)
        self.assertEqual(render_publication(checkpoint['batch'], checkpoint['published_at']), data)

    def assert_invalid(self, batch):
        with self.assertRaises(ValueError):
            validate_curated_batch(batch)
        with self.assertRaises(ValueError):
            render_publication(batch, STAMP)

    def test_roundtrip_citations_and_explicit_metadata(self):
        batch = fixture()
        data = render_publication(batch, STAMP)
        self.assertEqual(parse_publication(data), {
            'schema_version': 'workvault-publication-v1',
            'batch': fixture(),
            'published_at': '2026-10-05T12:34:56Z',
        })
        self.assertEqual(parse_publication(data)['batch']['cards'][0], {
            'path': 'knowledge/alpha.md',
            'title': 'Synthetic Alpha',
            'summary': 'A curated synthetic fact.',
            'details': 'This is fixture content, not a copied workplace page.',
            'date': '2026-10-05',
            'draft': False,
            'uncertainty': ['The fixture does not prove a live service fact.'],
            'source_links': ['https://example.invalid/source/alpha'],
            'cross_references': ['knowledge/drafts/beta.md'],
        })
        text = data.decode('utf-8')
        for expected in (
            'Source: <a href="https://github.com/example/team-wiki/blob/1111111111111111111111111111111111111111/knowledge/alpha.md">Exact&#45;commit source</a>\n',
            'Source: <a href="https://github.com/example/team-wiki/blob/1111111111111111111111111111111111111111/knowledge/drafts/beta.md">Exact&#45;commit source</a>\n',
            '## Synthetic Alpha', '## Synthetic Beta',
            'Date: 2026-10-05', 'Date: 2026-10-04',
            'Draft: false', 'Draft: true',
            '**Uncertainty:**', '**Sources:**', '**Cross-references:**',
            'https://example.invalid/source/alpha',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)
        self.assertEqual(render_publication(batch, STAMP), data)

    def test_coverage_and_index_gaps_are_literal_and_sorted(self):
        batch = fixture()
        batch['discovered_paths'].reverse()
        batch['requested_paths'].reverse()
        text = render_publication(batch, STAMP).decode('utf-8')
        self.assertIn('Full source coverage: false\n', text)
        self.assertIn(
            '**Covered paths:**\n\n- knowledge/alpha&#46;md\n'
            '- knowledge/drafts/beta&#46;md\n\n', text,
        )
        self.assertIn('**Uncovered paths:**\n\n- knowledge/unindexed&#46;md\n\n', text)
        self.assertIn('**Index gaps:**\n\n- knowledge/unindexed&#46;md\n\n', text)
        batch['discovered_paths'] = ['knowledge/drafts/beta.md', 'knowledge/alpha.md']
        text = render_publication(batch, STAMP).decode('utf-8')
        self.assertIn('Full source coverage: true\n', text)
        self.assertIn('**Uncovered paths:**\n\n- None\n\n', text)
        self.assertIn('**Index gaps:**\n\n- None\n\n', text)

    def test_requested_unindexed_pages_and_external_cross_references_are_metadata(self):
        batch = fixture()
        batch['indexed_paths'] = []
        batch['cards'][0]['cross_references'] = ['knowledge/not-discovered.md']
        checkpoint = parse_publication(render_publication(batch, STAMP))
        self.assertEqual(checkpoint['batch']['indexed_paths'], [])
        self.assertEqual(checkpoint['batch']['cards'][0]['cross_references'],
                         ['knowledge/not-discovered.md'])

    def test_dictionary_order_does_not_change_bytes_or_list_order(self):
        batch = fixture()
        reordered = dict(reversed(list(batch.items())))
        reordered['cards'] = [dict(reversed(list(card.items()))) for card in batch['cards']]
        self.assertEqual(render_publication(batch, STAMP), render_publication(reordered, STAMP))
        checkpoint = parse_publication(render_publication(batch, STAMP))
        self.assertEqual(checkpoint['batch']['discovered_paths'], [
            'knowledge/alpha.md', 'knowledge/drafts/beta.md', 'knowledge/unindexed.md',
        ])

    def test_unknown_and_missing_fields_refuse_without_echoing_payload(self):
        for level in ('batch', 'card'):
            batch = fixture()
            target = batch if level == 'batch' else batch['cards'][0]
            target['raw_body'] = 'RAW_ONLY_SYNTHETIC_CANARY'
            with self.subTest(level=level):
                self.assert_invalid(batch)
                with self.assertRaises(ValueError) as caught:
                    validate_curated_batch(batch)
                self.assertNotIn('RAW_ONLY_SYNTHETIC_CANARY', str(caught.exception))
        for field in fixture():
            batch = fixture()
            del batch[field]
            with self.subTest(missing_batch_field=field):
                self.assert_invalid(batch)
        for field in fixture()['cards'][0]:
            batch = fixture()
            del batch['cards'][0][field]
            with self.subTest(missing_card_field=field):
                self.assert_invalid(batch)

    def test_schema_repository_and_commit_are_exact(self):
        changes = (
            ('schema_version', 'workvault-curated-batch-v2'),
            ('repository', '../wiki'),
            ('commit', 'A' * 40), ('commit', '1' * 39),
            ('commit', 'g' * 40), ('commit', 1),
        )
        for field, value in changes:
            batch = fixture()
            batch[field] = value
            with self.subTest(field=field, value=value):
                self.assert_invalid(batch)

    def test_genuine_booleans_and_complete_inventory(self):
        for value in (False, 0, 1, 'true', None):
            batch = fixture()
            batch['inventory_complete'] = value
            with self.subTest(inventory=value):
                self.assert_invalid(batch)
        for value in (0, 1, 'false', None):
            batch = fixture()
            batch['cards'][0]['draft'] = value
            with self.subTest(draft=value):
                self.assert_invalid(batch)

    def test_membership_duplicates_and_card_request_equality(self):
        for field in ('indexed_paths', 'requested_paths'):
            batch = fixture()
            batch[field][0] = 'knowledge/missing.md'
            with self.subTest(membership=field):
                self.assert_invalid(batch)
        for field in ('discovered_paths', 'indexed_paths', 'requested_paths'):
            batch = fixture()
            batch[field].append(batch[field][0])
            with self.subTest(duplicate=field):
                self.assert_invalid(batch)
        for field in ('uncertainty', 'source_links', 'cross_references'):
            batch = fixture()
            batch['cards'][0][field] *= 2
            with self.subTest(duplicate_metadata=field):
                self.assert_invalid(batch)
        batch = fixture()
        batch['cards'][1] = copy.deepcopy(batch['cards'][0])
        self.assert_invalid(batch)
        batch = fixture()
        batch['cards'][0]['path'] = 'knowledge/unindexed.md'
        self.assert_invalid(batch)
        batch = fixture()
        batch['cards'].pop()
        self.assert_invalid(batch)
        batch = fixture()
        batch['discovered_paths'] = []
        self.assert_invalid(batch)
        for count in (1, 5):
            self.assertEqual(len(validate_curated_batch(with_card_count(count))['cards']), count)
        for count in (0, 6):
            self.assert_invalid(with_card_count(count))

    def test_wrong_container_and_string_types(self):
        for value in (None, [], 'batch'):
            self.assert_invalid(value)
        for field in ('discovered_paths', 'indexed_paths', 'requested_paths', 'cards'):
            batch = fixture()
            batch[field] = tuple(batch[field])
            with self.subTest(container=field):
                self.assert_invalid(batch)
        for field in ('title', 'summary', 'details', 'date', 'path',
                      'uncertainty', 'source_links', 'cross_references'):
            batch = fixture()
            batch['cards'][0][field] = None
            with self.subTest(card_field=field):
                self.assert_invalid(batch)
        for field in ('uncertainty', 'source_links', 'cross_references'):
            batch = fixture()
            batch['cards'][0][field] = [42]
            self.assert_invalid(batch)

    def test_path_normalization_and_metadata_paths(self):
        invalid = (
            '/knowledge/alpha.md', '../knowledge/alpha.md',
            'knowledge/../alpha.md', 'knowledge/./alpha.md',
            'knowledge//alpha.md', 'knowledge\\alpha.md',
            'knowledge/alpha.md/', 'other/alpha.md',
            'knowledge/.md', 'knowledge/alpha.txt',
            'knowledge/a' + chr(0) + '.md', 'knowledge/a\n.md',
            'knowledge/a\t.md', 'knowledge/a\r.md',
        )
        for path in invalid:
            with self.subTest(path=path):
                self.assert_invalid(with_path(path))
                batch = fixture()
                batch['cards'][0]['cross_references'] = [path]
                self.assert_invalid(batch)

    def test_unicode_paths_are_preserved_and_citations_are_url_encoded(self):
        batch = with_path('knowledge/drafts/Å beta?.md')
        data = render_publication(batch, STAMP)
        self.assertEqual(parse_publication(data)['batch']['cards'][0]['path'],
                         'knowledge/drafts/Å beta?.md')
        self.assertIn(
            b'https://github.com/example/team-wiki/blob/1111111111111111111111111111111111111111/knowledge/drafts/%C3%85%20beta%3F.md',
            data,
        )

    def test_dates_and_explicit_utc_timestamps(self):
        for value in ('2026-02-29', '2026-13-01', '2026-1-05',
                      '20261005', '0000-01-01', '2026-10-05T00:00:00Z'):
            batch = fixture()
            batch['cards'][0]['date'] = value
            with self.subTest(date=value):
                self.assert_invalid(batch)
        for value in (None, '2026-10-05T12:34:56', '2026-10-05T12:34:56+00:00',
                      '2026-10-05T12:34:56.000Z', '2026-10-05T12:34:56z',
                      '2026-02-29T12:34:56Z', '2026-10-05T24:00:00Z',
                      '2026-10-05T12:60:00Z', '2026-10-05T12:34:60Z',
                      '0000-01-01T00:00:00Z', '2026-10-05T12:34:56Z\n'):
            with self.subTest(timestamp=value), self.assertRaises(ValueError):
                render_publication(fixture(), value)
        batch = fixture()
        batch['cards'][0]['date'] = '2024-02-29'
        checkpoint = parse_publication(render_publication(batch, '2024-02-29T00:00:00Z'))
        self.assertEqual(checkpoint['published_at'], '2024-02-29T00:00:00Z')
        self.assertEqual(checkpoint['batch']['cards'][0]['date'], '2024-02-29')

    def test_https_urls_refuse_credentials_and_malformed_authorities(self):
        invalid = (
            'http://example.invalid/a', '/relative', 'https:///missing',
            'https://user:pass@example.invalid/a', 'https://user@example.invalid/a',
            'https://@example.invalid/a', 'https://user%40example.invalid/a',
            'https://example.invalid:99999/a', 'https://example.invalid:0/a',
            'https://example.invalid:/a', 'https://[broken/a',
            'https://exa_mple.invalid/a', 'https://example.invalid/a b',
            'https://example.invalid/\na', 'https://example.invalid\\evil/a',
            'https://example.invalid/%zz', 'https://example.invalid/<a>',
        )
        for url in invalid:
            batch = fixture()
            batch['cards'][0]['source_links'] = [url]
            with self.subTest(url=url):
                self.assert_invalid(batch)
        batch = fixture()
        batch['cards'][0]['source_links'] = [
            'https://example.invalid/a?x=1&y=(2)#part',
            'https://[::1]:443/fixture',
        ]
        data = render_publication(batch, STAMP)
        self.assertEqual(parse_publication(data)['batch']['cards'][0]['source_links'], [
            'https://example.invalid/a?x=1&y=(2)#part',
            'https://[::1]:443/fixture',
        ])
        self.assertIn(b'href="https://example.invalid/a?x=1&amp;y=(2)#part"', data)

    def test_string_byte_bounds_and_invalid_text(self):
        for field, limit in (('title', 512), ('summary', 4096), ('details', 16384)):
            batch = fixture()
            batch['cards'][0][field] = 'x' * limit
            self.assertEqual(len(validate_curated_batch(batch)['cards'][0][field]), limit)
            batch['cards'][0][field] += 'x'
            with self.subTest(field=field):
                self.assert_invalid(batch)
        batch = fixture()
        batch['cards'][0]['title'] = 'é' * 256
        validate_curated_batch(batch)
        batch['cards'][0]['title'] += 'é'
        self.assert_invalid(batch)
        for value in ('', '   ', chr(0), chr(127), chr(0xD800)):
            batch = fixture()
            batch['cards'][0]['title'] = value
            self.assert_invalid(batch)
        batch = fixture()
        batch['cards'][0]['details'] = ''
        self.assertEqual(validate_curated_batch(batch)['cards'][0]['details'], '')
        batch = fixture()
        batch['cards'][0]['uncertainty'] = ['x' * 1024]
        validate_curated_batch(batch)
        batch['cards'][0]['uncertainty'][0] += 'x'
        self.assert_invalid(batch)
        prefix = 'https://example.invalid/'
        batch = fixture()
        batch['cards'][0]['source_links'] = [prefix + 'x' * (2048 - len(prefix))]
        validate_curated_batch(batch)
        batch['cards'][0]['source_links'][0] += 'x'
        self.assert_invalid(batch)
        validate_curated_batch(with_path('knowledge/' + 'x' * 499 + '.md'))
        self.assert_invalid(with_path('knowledge/' + 'x' * 500 + '.md'))

    def test_list_and_aggregate_batch_bounds(self):
        factories = {
            'uncertainty': lambda i: f'Synthetic caveat {i}',
            'source_links': lambda i: f'https://example.invalid/{i}',
            'cross_references': lambda i: f'knowledge/ref-{i}.md',
        }
        for field, make in factories.items():
            batch = fixture()
            batch['cards'][0][field] = [make(i) for i in range(32)]
            self.assertEqual(len(validate_curated_batch(batch)['cards'][0][field]), 32)
            batch['cards'][0][field].append(make(32))
            self.assert_invalid(batch)
        batch = fixture()
        batch['discovered_paths'] = ['knowledge/alpha.md', 'knowledge/drafts/beta.md'] + [
            f'knowledge/inventory-{i}.md' for i in range(4094)
        ]
        self.assertEqual(len(validate_curated_batch(batch)['discovered_paths']), 4096)
        batch['discovered_paths'].append('knowledge/extra.md')
        self.assert_invalid(batch)
        batch = fixture()
        paths = ['knowledge/alpha.md', 'knowledge/drafts/beta.md'] + [
            'knowledge/' + 'x' * 140 + str(i) + '.md' for i in range(4094)
        ]
        batch['discovered_paths'] = paths
        batch['indexed_paths'] = paths.copy()
        self.assert_invalid(batch)

    def test_publication_size_is_bounded_in_both_directions(self):
        with self.assertRaises(ValueError):
            parse_publication(b'x' * (4 * 1024 * 1024 + 1))
        batch = fixture()
        batch['discovered_paths'] = ['knowledge/alpha.md', 'knowledge/drafts/beta.md'] + [
            'knowledge/' + '[' * 120 + str(i) + '.md' for i in range(4094)
        ]
        batch['indexed_paths'] = []
        validate_curated_batch(batch)
        with self.assertRaises(ValueError):
            render_publication(batch, STAMP)

    def test_defensive_copy_and_input_preservation(self):
        batch = fixture()
        before = copy.deepcopy(batch)
        clean = validate_curated_batch(batch)
        render_publication(batch, STAMP)
        self.assertEqual(batch, before)
        clean['discovered_paths'].append('knowledge/new.md')
        clean['cards'][0]['title'] = 'Changed copy'
        clean['cards'][0]['uncertainty'].append('New caveat')
        self.assertEqual(batch, before)
        batch['cards'][0]['source_links'].append('https://example.invalid/another')
        self.assertEqual(clean['cards'][0]['source_links'], ['https://example.invalid/source/alpha'])

    def test_unicode_punctuation_and_marker_like_text_roundtrip(self):
        batch = fixture()
        batch['cards'][0]['title'] = 'Ångström 🍂 & punctuation * []'
        batch['cards'][0]['details'] = (
            'First line.\r\n[[reference]]\t\n'
            '<!-- workvault-publication-v1\n-->\n'
            '<script>alert(\'synthetic\')</script> -- !'
        )
        batch['cards'][0]['uncertainty'] = ['Keep “uncertain”, don\'t infer certainty.']
        data = render_publication(batch, STAMP)
        self.assertEqual(parse_publication(data)['batch'], batch)
        self.assertIn('Ångström 🍂'.encode('utf-8'), data)
        self.assertEqual(data.count(b'<!-- workvault-publication-v1\n'), 1)
        self.assertEqual(data.count(b'\n-->\n\n'), 1)
        self.assertNotIn(b'<script>', data)

    def test_block_openers_stay_literal_and_checkpoint_is_unchanged(self):
        for spaces, escaped_prefix in (
            (0, ''),
            (1, '&#32;'),
            (2, '&#32;&#32;'),
            (3, '&#32;&#32;&#32;'),
            (4, '&#32;&#32;&#32;&#32;'),
        ):
            with self.subTest(spaces=spaces):
                batch = fixture()
                batch['cards'][0]['summary'] = ' ' * spaces + '~~~'
                batch['cards'][0]['details'] = '    literal punctuation.'
                data = render_publication(batch, STAMP)
                self.assertEqual(parse_publication(data)['batch'], batch)
                self.assertEqual(render_publication(batch, STAMP), data)
                text = data.decode('utf-8')
                self.assertIn(
                    '### Summary\n\n' + escaped_prefix +
                    '&#126;&#126;&#126;\n\n', text,
                )
                self.assertIn(
                    '### Details\n\n&#32;&#32;&#32;&#32;'
                    'literal punctuation&#46;\n\n', text,
                )

    def test_block_syntax_in_curated_text_stays_literal(self):
        batch = fixture()
        batch['cards'][0]['summary'] = '~~~'
        batch['cards'][0]['details'] = '    %%hidden%% $x$ ^block'
        data = render_publication(batch, STAMP)
        self.assertEqual(parse_publication(data)['batch'], batch)
        text = data.decode('utf-8')
        self.assertIn('### Summary\n\n&#126;&#126;&#126;\n\n', text)
        self.assertIn(
            '### Details\n\n&#32;&#32;&#32;&#32;&#37;&#37;hidden&#37;&#37; '
            '&#36;x&#36; &#94;block\n\n', text,
        )

    def test_truncated_malformed_and_mismatched_publications_refuse(self):
        good = render_publication(fixture(), STAMP)
        invalid = (
            b'', b'\xff', bytearray(good), good[:-1], good[:-80], good + b'extra',
            good[:good.index(b'\n-->\n\n')],
            good.replace(b'## Synthetic Alpha', b'## Changed title', 1),
            good.replace(b'Draft: false', b'Draft: true', 1),
            good.replace(b'Full source coverage: false', b'Full source coverage: true', 1),
            good.replace(b'blob/1111111111111111111111111111111111111111/', b'blob/2222222222222222222222222222222222222222/', 1),
            good.replace(b'"schema_version":', b'"schema_version" ', 1),
            good.replace(b'workvault-publication-v1', b'workvault-publication-v2'),
            good.replace(b'{"batch":', b'{"unexpected":true,"batch":', 1),
            good.replace(b'"inventory_complete":true', b'"inventory_complete":false', 1),
            good.replace(b'"inventory_complete":true', b'"inventory_complete":NaN', 1),
            good.replace(b'"cards":', b'"cards":null,"cards":', 1),
            good.replace(b'2026-10-05T12:34:56Z', b'2026-10-05T24:34:56Z', 1),
            good.replace(b'"schema_version":"workvault-publication-v1"',
                         b'"schema_version":"wrong"', 1),
        )
        for number, data in enumerate(invalid):
            with self.subTest(case=number), self.assertRaises(ValueError):
                parse_publication(data)
        nested = ('<!-- workvault-publication-v1\n' + '[' * 2000 + '0' + ']' * 2000 + '\n-->\n\n').encode()
        with self.assertRaises(ValueError):
            parse_publication(nested)
        self.assertEqual(parse_publication(good)['published_at'], '2026-10-05T12:34:56Z')


class MultiSourceCurationTests(unittest.TestCase):
    def test_unavailable_event_time_is_rendered_as_unavailable_not_observation_time(self):
        value = source_fixture('backstage')
        value['cards'][0]['source']['event_at'] = None
        value['cards'][0]['source']['modified_at'] = None
        value['cards'][0]['supersedes'] = []
        value['cards'][0]['date'] = None
        value['cards'][0]['uncertainty'] = ['Source event date unavailable; history unsupported.']
        data = self.render(value)
        self.assertIn(b'Event at: Unavailable', data)
        self.assertIn(b'Date: Unavailable', data)
        self.assertNotIn(b'1970-01-01', data)
        self.assertIsNone(parse_publication(data)['batch']['cards'][0]['source']['event_at'])
        self.assertIsNone(parse_publication(data)['batch']['cards'][0]['date'])

    def test_publisher_refuses_implicit_v1_to_v2_selection_migration(self):
        import workvault
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workvault.publish_curated_batch(root / 'vault', root / 'snapshots', fixture())
            before = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
            with self.assertRaises(workvault.WorkVaultError):
                workvault.publish_curated_batch(root / 'vault', root / 'snapshots', source_fixture())
            self.assertEqual({path: path.read_bytes() for path in root.rglob('*') if path.is_file()}, before)

    def render(self, value):
        try:
            return render_publication(value, '2026-10-07T12:00:00Z')
        except ValueError as error:
            self.fail('valid native provenance was refused: ' + str(error))

    def test_each_native_source_roundtrips_without_fabricated_github_metadata(self):
        for kind in ('slack', 'drive', 'gmail', 'jira', 'backstage'):
            with self.subTest(kind=kind):
                value = source_fixture(kind)
                data = self.render(value)
                self.assertEqual(parse_publication(data)['batch'], value)
                self.assertIn(b'workvault-publication-v2', data)
                self.assertNotIn(b'https://github.com/', data)
                self.assertIn(b'Draft: true', data)
                self.assertIn(b'2026-10-01', data)
                self.assertIn(b'Unverified proposal', data)

    def test_evidence_categories_remain_distinct_without_duplicate_source_reinforcement(self):
        for category in ('source_observation', 'user_decision', 'agent_claim', 'verified_result', 'owner_synthesis'):
            value = source_fixture()
            value['cards'][0]['evidence_kind'] = category
            data = self.render(value)
            self.assertIn(('Evidence kind: ' + category).encode(), data)
        duplicate = source_fixture()
        duplicate['cards'].append(copy.deepcopy(duplicate['cards'][0]))
        duplicate['cards'][1]['evidence_kind'] = 'agent_claim'
        with self.assertRaises(ValueError):
            validate_source_batch(duplicate)

    def test_v2_limits_unknown_fields_and_invalid_relationships_refuse(self):
        good = source_fixture()
        self.assertEqual(parse_publication(self.render(good))['batch'], good)
        for field, value in (('evidence_kind', 'fact'), ('raw_body', 'RAW_CANARY'),
                             ('dependencies', [good['cards'][0]['source']]),
                             ('supersedes', [dict(good['cards'][0]['source'], item_id='other')])):
            bad = source_fixture()
            bad['cards'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_source_batch(bad)
        for count in (0, 6):
            bad = source_fixture()
            bad['cards'] = [dict(copy.deepcopy(good['cards'][0]), source=dict(good['cards'][0]['source'], item_id=str(i)), supersedes=[]) for i in range(count)]
            with self.assertRaises(ValueError):
                validate_source_batch(bad)
        data = self.render(good)
        with self.assertRaises(ValueError):
            parse_publication(data.replace(b'Evidence kind: source_observation', b'Evidence kind: verified_result'))
        with self.assertRaises(ValueError):
            parse_publication(data.replace(b'https://example.invalid/source/123.456', b'https://example.invalid/source/999.999', 1))

    def test_publication_cannot_predate_observation(self):
        with self.assertRaises(ValueError):
            render_publication(source_fixture(), '2026-10-06T12:00:00Z')

    def test_same_real_revision_cannot_be_presented_as_superseded(self):
        bad = source_fixture()
        bad['cards'][0]['supersedes'] = [dict(bad['cards'][0]['source'], content_hash='sha256:' + 'b' * 64)]
        with self.assertRaises(ValueError):
            validate_source_batch(bad)

    def test_revision_hash_consistency_covers_all_cards_and_relationships(self):
        for relationship in ('source', 'supersedes'):
            batch = source_fixture()
            first = batch['cards'][0]
            other = copy.deepcopy(source_fixture('drive')['cards'][0])
            other['source']['item_id'] = 'another-source'
            other['supersedes'] = []
            original = first['source'] if relationship == 'source' else first['supersedes'][0]
            other['dependencies'] = [copy.deepcopy(original)]
            batch['cards'].append(other)
            self.assertEqual(parse_publication(self.render(batch))['batch'], batch)
            other['dependencies'][0]['content_hash'] = 'sha256:' + 'b' * 64
            with self.subTest(relationship=relationship), self.assertRaises(ValueError):
                validate_source_batch(batch)


if __name__ == '__main__':
    unittest.main()
