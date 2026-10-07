"""Collector boundary tests use fictional sources and never call a live service."""

import base64
import copy
import hashlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch

try:
    import collector
except ImportError:
    collector = None


COMMIT = 'a' * 40
TREE = 'b' * 40
CANARY = 'RAW_PRIVATE_CANARY_' + 'x' * 100
SCHEMA_TEXT = 'Use curated cards.'
INDEX_TEXT = '[[guide]]\n[broken](knowledge/missing.md)'
PAGE_TEXT = '# Guide\ndate: 2026-10-01\n' + CANARY + '\n[Reference](https://docs.example.org/guide)'


def blob_sha(text):
    data = text.encode()
    return hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()


SCHEMA = blob_sha(SCHEMA_TEXT)
INDEX = blob_sha(INDEX_TEXT)
PAGE = blob_sha(PAGE_TEXT)
TEMPLATE = blob_sha('# Template')


def blob(text):
    data = text.encode()
    return {'encoding': 'base64', 'size': len(data),
            'content': base64.b64encode(data).decode()}


class Stream(io.BytesIO):
    consumed = 0

    def read(self, size=-1):
        value = super().read(size)
        self.consumed += len(value)
        return value


class Process:
    def __init__(self, result):
        self.args = result.args
        self.stdout = Stream(result.stdout)
        self.stderr = Stream(result.stderr)
        self.returncode = result.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def communicate(self, input=None, timeout=None):
        return self.stdout.read(), self.stderr.read()

    def poll(self):
        return self.returncode

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def card(path='knowledge/guide.md'):
    return dict(path=path, title='Guide', summary='A curated explanation.',
                details='Reviewed procedure.', date='2026-10-01', draft=False,
                uncertainty=[], source_links=['https://docs.example.org/guide'],
                cross_references=[])


class CollectorTests(unittest.TestCase):
    def test_native_raw_body_and_encoded_reproduction_never_enter_publication_or_errors(self):
        from test_sources import reference
        import curation
        body = ('# Synthetic\n' + CANARY).encode()
        ref = dict(reference(), content_hash='sha256:' + hashlib.sha256(body).hexdigest())
        record = {'reference': ref, 'body': body, 'draft': False, 'supersedes': [], 'dependencies': []}
        card = dict(source_key='["slack","workspace-fixture","C123","123.456"]', title='Synthetic',
                    summary='A curated synthetic observation.', details='', date='2026-10-05', draft=False,
                    uncertainty=[], source_links=[], evidence_kind='source_observation')
        batch = collector.assemble_source_batch([ref], [record], [card])
        data = curation.render_publication(batch, '2026-10-07T12:00:00Z')
        self.assertNotIn(CANARY.encode(), data)
        self.assertNotIn('body', batch['cards'][0])
        for raw in (body.decode(), base64.b64encode(body).decode(), CANARY):
            with self.subTest(encoded=raw != body.decode()), self.assertRaises(ValueError) as error:
                collector.assemble_source_batch([ref], [record], [dict(card, details=raw)])
            self.assertNotIn(CANARY, str(error.exception))

    def test_metadata_only_locator_binds_fetch_without_inventing_an_unavailable_event_time(self):
        from test_sources import locator, reference
        body = b'# Entity\nSynthetic catalog entity.'
        listing = dict(locator('backstage'), author=None, revision=None, event_at=None, modified_at=None)
        ref = dict(reference('backstage'), author=None, revision=None, event_at=None, modified_at=None,
                   content_hash='sha256:' + hashlib.sha256(body).hexdigest())
        record = {'reference': ref, 'body': body, 'draft': False, 'supersedes': [], 'dependencies': []}
        card = dict(source_key='["backstage","workspace-fixture","C123","123.456"]', title='Entity',
                    summary='A catalog entity exists.', details='', date=None, draft=False,
                    uncertainty=['Source event date unavailable; history unsupported.'], source_links=[ref['citation']],
                    evidence_kind='source_observation')
        try:
            batch = collector.assemble_source_batch([listing], [record], [card])
        except ValueError as error:
            self.fail('metadata-only native listing was refused: ' + str(error))
        self.assertIsNone(batch['cards'][0]['source']['event_at'])
        self.assertIsNone(batch['cards'][0]['date'])
        self.assertEqual(batch['cards'][0]['uncertainty'], ['Source event date unavailable; history unsupported.'])
        with self.assertRaises(ValueError):
            collector.assemble_source_batch([listing], [record], [dict(card, date='1970-01-01')])
        with self.assertRaises(ValueError):
            collector.assemble_source_batch([listing], [record], [dict(card, uncertainty=[])])
        for changed in (dict(ref, citation='https://example.invalid/other'), dict(ref, account='other'),
                        dict(ref, observed_at='2026-10-06T12:00:00Z')):
            with self.assertRaises(ValueError):
                collector.assemble_source_batch([listing], [dict(record, reference=changed)], [card])

    def test_source_collection_attaches_only_collector_metadata_after_curation(self):
        from test_sources import reference
        body = b'# Synthetic\nCreated: 2026-10-01\nUpdated: 2026-10-05\nDraft: true\nA proposal.'
        ref = dict(reference(), content_hash='sha256:' + hashlib.sha256(body).hexdigest())
        fetched = {'reference': ref, 'body': body, 'draft': True, 'supersedes': [], 'dependencies': []}
        model_card = dict(title='Proposal', summary='A proposed change.', details='Created 2026-10-01; updated 2026-10-05.',
                          date='2026-10-05', draft=True, uncertainty=['Not decided.'], source_links=[ref['citation']],
                          evidence_kind='agent_claim', source_key='["slack","workspace-fixture","C123","123.456"]')
        self.assertTrue(hasattr(collector, 'assemble_source_batch'), 'collector-owned v2 assembly is missing')
        batch = collector.assemble_source_batch([ref], [fetched], [model_card])
        self.assertEqual(batch['cards'][0]['source'], ref)
        self.assertEqual(batch['cards'][0]['uncertainty'], ['Not decided.'])
        self.assertNotIn('body', batch['cards'][0])
        for key in ('revision', 'author', 'citation', 'account', 'content_hash', 'modified_at'):
            wrong = copy.deepcopy(fetched)
            wrong['reference'][key] = None if key in ('revision', 'author') else 'changed'
            with self.subTest(key=key), self.assertRaises(ValueError):
                collector.assemble_source_batch([ref], [wrong], [model_card])
        for changes in ({'source': dict(ref, author='invented')}, {'source_key': '["slack","other","C123","123.456"]'},
                        {'source_links': ['https://example.invalid/source/999']}, {'draft': False},
                        {'details': 'History erased.'}, {'raw_body': 'RAW_CANARY'}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                collector.assemble_source_batch([ref], [fetched], [dict(model_card, **changes)])
        fetched['body'] += b'Changed bytes.'
        with self.assertRaises(ValueError):
            collector.assemble_source_batch([ref], [fetched], [model_card])

    def setUp(self):
        self.assertIsNotNone(collector, 'bounded source collector is missing')
        self.calls = []
        self.responses = {
            f'repos/example/team-wiki/commits/main': {'sha': COMMIT, 'commit': {'tree': {'sha': TREE}}},
            f'repos/example/team-wiki/git/trees/{TREE}?recursive=1': {
                'sha': TREE, 'truncated': False, 'tree': [
                    {'path': path, 'mode': '100644', 'type': 'blob', 'sha': sha, 'size': 400}
                    for path, sha in [('SCHEMA.md', SCHEMA), ('index.md', INDEX),
                                     ('knowledge/templates/card.md', TEMPLATE), ('knowledge/guide.md', PAGE)]
                ]},
            f'repos/example/team-wiki/git/blobs/{SCHEMA}': blob(SCHEMA_TEXT),
            f'repos/example/team-wiki/git/blobs/{INDEX}': blob(INDEX_TEXT),
            f'repos/example/team-wiki/git/blobs/{PAGE}': blob(PAGE_TEXT),
            f'repos/example/team-wiki/git/blobs/{TEMPLATE}': blob('# Template'),
        }
        for endpoint, value in self.responses.items():
            if '/git/blobs/' in endpoint:
                value['sha'] = endpoint.rsplit('/', 1)[-1]
        tree = self.responses[f'repos/example/team-wiki/git/trees/{TREE}?recursive=1']['tree']
        for entry in tree:
            entry['size'] = self.responses[f"repos/example/team-wiki/git/blobs/{entry['sha']}"]['size']

    def run_command(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == 'gcloud':
            return subprocess.CompletedProcess(args, 0, b'fake-access-token\n', b'')
        if args[-1] not in self.responses:
            return subprocess.CompletedProcess(args, 1, b'', b'HTTP 404')
        return subprocess.CompletedProcess(args, 0, json.dumps(self.responses[args[-1]]).encode(), b'')

    def snapshot(self):
        with patch('collector.subprocess.Popen', side_effect=self.spawn):
            return collector.GitHubSource().snapshot('example/team-wiki', 'main')

    def test_missing_date_cannot_publish_invented_confident_date(self):
        snap = self.snapshot()
        item = dict(card(), source_links=[])
        body = '# Guide\nNo source date appears here.'
        with self.assertRaises(ValueError):
            collector._validate_provenance(snap, {item['path']: body}, {'cards': [item]})
        item['date'] = '1970-01-01'
        with self.assertRaises(ValueError):
            collector._validate_provenance(snap, {item['path']: body}, {'cards': [item]})
        item['uncertainty'] = ['Source date is missing.']
        collector._validate_provenance(snap, {item['path']: body}, {'cards': [item]})

    def test_bold_date_headers_and_updates_preserve_latest_date_and_history(self):
        snap = self.snapshot()
        item = dict(card(), source_links=[])
        body = '# Guide\n**Date:** 2026-10-01\n**Updated:** 2026-10-05\nCorrection recorded.'
        with self.assertRaises(ValueError):
            collector._validate_provenance(snap, {item['path']: body}, {'cards': [item]})
        item['date'] = '2026-10-05'
        with self.assertRaises(ValueError):
            collector._validate_provenance(snap, {item['path']: body}, {'cards': [item]})
        item['uncertainty'] = ['Original dated 2026-10-01; updated correction retained.']
        collector._validate_provenance(snap, {item['path']: body}, {'cards': [item]})

    def test_request_gives_the_model_exact_source_date_requirements(self):
        snap = self.snapshot()
        item = dict(card(), source_links=[], date='2026-10-05',
                    details='Originally dated 2026-10-01; updated 2026-10-05.')
        body = '# Guide\n**Created:** 2026-10-01\nUpdated: 2026-10-05\nDate: invalid\nA correction.'
        response = {'model': 'example-model', 'stop_reason': 'end_turn',
                    'content': [{'type': 'text', 'text': json.dumps({'cards': [item]})}]}
        with patch('collector.subprocess.Popen', side_effect=self.spawn), patch(
            'collector.urllib.request.OpenerDirector.open',
            return_value=io.BytesIO(json.dumps(response).encode()),
        ) as opened:
            collector.VertexCurator('example-project', 'eu', 'example-model').curate(
                snap, {item['path']: body})
        request = json.loads(opened.call_args.args[0].data)
        metadata = json.loads(request['messages'][0]['content'].split('\nUNTRUSTED_EVIDENCE_JSON\n')[0])
        requirements = metadata['allowed_provenance'][item['path']]
        self.assertEqual(requirements['required_date'], '2026-10-05')
        self.assertEqual(requirements['earlier_source_dates'], ['2026-10-01'])
        self.assertIn('required_date', request['system'])
        self.assertIn('earlier_source_dates', request['system'])

    def test_earlier_source_dates_must_be_in_details_or_uncertainty(self):
        snap = self.snapshot()
        body = '# Guide\nDate: 2026-10-01\nUpdated: 2026-10-05\nA correction.'
        for field in ('title', 'summary'):
            with self.subTest(field=field):
                item = dict(card(), source_links=[], date='2026-10-05')
                item[field] = 'Originally dated 2026-10-01.'
                with self.assertRaises(ValueError):
                    collector._validate_provenance(snap, {item['path']: body}, {'cards': [item]})

    def test_top_level_template_is_always_a_draft(self):
        snap = self.snapshot()
        item = dict(card('knowledge/_template.md'), source_links=[], date='1970-01-01', uncertainty=['Source date is missing.'])
        with self.assertRaises(ValueError):
            collector._validate_provenance(snap, {item['path']: '# Template'}, {'cards': [item]})
        item['draft'] = True
        collector._validate_provenance(snap, {item['path']: '# Template'}, {'cards': [item]})

    def spawn(self, args, **kwargs):
        return Process(self.run_command(args, **kwargs))

    def test_recursive_inventory_includes_unindexed_templates_and_reads_no_page_bodies(self):
        snap = self.snapshot()
        self.assertEqual(snap.discovered_paths, ('knowledge/guide.md', 'knowledge/templates/card.md'))
        self.assertEqual(snap.indexed_paths, ('knowledge/guide.md',))
        self.assertEqual(snap.broken_index_references, ('knowledge/missing.md',))
        self.assertNotIn(CANARY, repr(snap))
        self.assertEqual(len(self.calls), 4)

    def test_default_branch_discovery_uses_repository_endpoint_without_trailing_slash(self):
        self.responses['repos/example/team-wiki'] = {'default_branch': 'main'}
        with patch('collector.subprocess.Popen', side_effect=self.spawn):
            try:
                snap = collector.GitHubSource().snapshot('example/team-wiki')
            except ValueError:
                self.fail('default branch discovery requested an invalid endpoint')
        self.assertEqual(snap.commit, COMMIT)
        self.assertEqual(self.calls[0][0][-1], 'repos/example/team-wiki')

    def test_empty_complete_inventory_is_returned_for_last_page_deletion(self):
        endpoint = f'repos/example/team-wiki/git/trees/{TREE}?recursive=1'
        self.responses[endpoint]['tree'] = [entry for entry in self.responses[endpoint]['tree'] if not entry['path'].startswith('knowledge/')]
        snap = self.snapshot()
        self.assertEqual(snap.commit, COMMIT)
        self.assertEqual(snap.discovered_paths, ())
        self.assertEqual(snap.indexed_paths, ())
        self.assertEqual(snap.broken_index_references, ('guide', 'knowledge/missing.md'))

    def test_truncated_or_symlink_tree_is_rejected_without_source_text(self):
        endpoint = f'repos/example/team-wiki/git/trees/{TREE}?recursive=1'
        for invalid in ('truncated', 'symlink'):
            with self.subTest(invalid=invalid):
                response = self.responses[endpoint]
                response['truncated'] = invalid == 'truncated'
                response['tree'][2]['mode'] = '120000' if invalid == 'symlink' else '100644'
                with self.assertRaises(ValueError) as error:
                    self.snapshot()
                self.assertNotIn(CANARY, str(error.exception))

    def test_requested_pages_use_pinned_blob_and_limit_five(self):
        snap = self.snapshot()
        with patch('collector.subprocess.Popen', side_effect=self.spawn):
            pages = collector.GitHubSource().read_pages(snap, ['knowledge/guide.md'])
            self.assertIn(CANARY, pages['knowledge/guide.md'])
            with self.assertRaises(ValueError):
                collector.GitHubSource().read_pages(snap, ['knowledge/guide.md'] * 6)
        self.assertEqual(self.calls[-1][0][-1], f'repos/example/team-wiki/git/blobs/{PAGE}')

    def test_real_batch_curation_keeps_raw_canary_out_of_result_and_publication(self):
        snap = self.snapshot()
        response = {'model': 'claude-sonnet-4-6', 'stop_reason': 'end_turn',
                    'content': [{'type': 'text', 'text': json.dumps({'cards': [card()]})}]}
        with patch('collector.subprocess.Popen', side_effect=self.spawn), patch('collector.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(response).encode())):
            result = collector.collect_batch(snap, ['knowledge/guide.md'], collector.GitHubSource(),
                                             collector.VertexCurator('example-project', 'europe-west1', 'claude-sonnet-4-6'))
        import curation
        self.assertNotIn(CANARY, json.dumps(result))
        self.assertNotIn(CANARY.encode(), curation.render_publication(result, '2026-10-05T10:00:00Z'))

    def test_model_unknown_fields_and_invented_provenance_fail_closed(self):
        snap = self.snapshot()
        variants = [
            {'cards': [dict(card(), raw_body=CANARY)]},
            {'cards': [dict(card(), source_links=['https://invented.example.org/'])]},
            {'cards': [dict(card(), cross_references=['knowledge/missing.md'])]},
            {'cards': [dict(card(), details=self.responses[f'repos/example/team-wiki/git/blobs/{PAGE}']['content'])]},
        ]
        for payload in variants:
            with self.subTest(payload=list(payload)):
                response = {'model': 'claude-sonnet-4-6', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(payload)}]}
                with patch('collector.subprocess.Popen', side_effect=self.spawn), patch('collector.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(response).encode())):
                    with self.assertRaises(ValueError) as error:
                        collector.collect_batch(snap, ['knowledge/guide.md'], collector.GitHubSource(), collector.VertexCurator('example-project', 'europe-west1', 'claude-sonnet-4-6'))
                self.assertNotIn(CANARY, str(error.exception))

    def test_eu_model_thinking_is_discarded_and_adaptive_medium_request_is_sent(self):
        snap = self.snapshot()
        response = {'model': 'example-model', 'stop_reason': 'end_turn', 'content': [
            {'type': 'thinking', 'thinking': CANARY, 'signature': 'signature'},
            {'type': 'redacted_thinking', 'data': CANARY},
            {'type': 'text', 'text': json.dumps({'cards': [card()]})}]}
        requests = []

        def open_request(request, timeout):
            requests.append((request, timeout))
            return io.BytesIO(json.dumps(response).encode())

        with patch('collector.subprocess.Popen', side_effect=self.spawn), patch('collector.urllib.request.OpenerDirector.open', side_effect=open_request):
            result = collector.collect_batch(snap, ['knowledge/guide.md'], collector.GitHubSource(), collector.VertexCurator('example-project', 'eu', 'example-model'))
        self.assertNotIn(CANARY, json.dumps(result))
        request, timeout = requests[0]
        self.assertEqual(request.full_url, 'https://aiplatform.eu.rep.googleapis.com/v1/projects/example-project/locations/eu/publishers/anthropic/models/example-model:rawPredict')
        payload = json.loads(request.data)
        self.assertEqual(payload['thinking'], {'type': 'adaptive'})
        self.assertEqual(payload['output_config']['effort'], 'medium')
        self.assertEqual(payload['max_tokens'], 16384)
        schema = payload['output_config']['format']['schema']['properties']['cards']['items']
        self.assertEqual(schema['properties']['uncertainty'], {'type': 'array', 'items': {'type': 'string'}})
        self.assertIn('uncertainty, source_links, and cross_references', payload['system'])
        self.assertIn('at most 32 unique items', payload['system'])
        for bound in (512, 4096, 16384, 1024):
            self.assertIn(str(bound), payload['system'])
        self.assertFalse(schema['additionalProperties'])
        self.assertNotIn('tools', payload)
        self.assertEqual(timeout, 300)

    def test_blob_mismatched_identity_size_and_invalid_base64_fail_without_raw_error(self):
        snap = self.snapshot()
        endpoint = f'repos/example/team-wiki/git/blobs/{PAGE}'
        corrupted = base64.b64encode(PAGE_TEXT.replace('Guide', 'Other').encode()).decode()
        for changed in ({'sha': '1' * 40}, {'size': 1}, {'content': CANARY}, {'content': corrupted}):
            original = self.responses[endpoint]
            self.responses[endpoint] = dict(original, **changed)
            with patch('collector.subprocess.Popen', side_effect=self.spawn):
                with self.assertRaises(ValueError) as error:
                    collector.GitHubSource().read_pages(snap, ['knowledge/guide.md'])
            self.assertNotIn(CANARY, str(error.exception))
            self.responses[endpoint] = original

    def test_oversized_command_stream_is_killed_before_unbounded_capture(self):
        process = Process(subprocess.CompletedProcess([], 0, b'x' * 1000000, b''))
        with patch('collector.subprocess.Popen', return_value=process):
            with self.assertRaises(ValueError):
                collector._command(['gh', 'api'], maximum=64)
        self.assertEqual(process.returncode, -9)
        self.assertLessEqual(process.stdout.consumed, 65)

    def test_malformed_wrong_model_tool_use_and_incomplete_completion_never_return_raw(self):
        snap = self.snapshot()
        good = {'model': 'example-model', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps({'cards': [card()]})}]}
        variants = [
            dict(good, model='unexpected-model'),
            dict(good, stop_reason='max_tokens'),
            dict(good, content=[{'type': 'tool_use', 'id': 'tool', 'name': 'send', 'input': {'body': CANARY}}]),
            dict(good, content=[{'type': 'text', 'text': CANARY}]),
            dict(good, content=[{'type': 'text', 'text': '{"cards":[],"cards":[]}'}]),
            dict(good, content=[{'type': 'text', 'text': '{"cards":NaN}'}]),
            dict(good, content=[{'type': 'text', 'text': json.dumps({'cards': [dict(card(), details=CANARY)]})}]),
        ]
        for response in variants:
            with self.subTest(response=response.get('stop_reason')):
                with patch('collector.subprocess.Popen', side_effect=self.spawn), patch('collector.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(response).encode())):
                    with self.assertRaises(ValueError) as error:
                        collector.collect_batch(snap, ['knowledge/guide.md'], collector.GitHubSource(), collector.VertexCurator('example-project', 'eu', 'example-model'))
                self.assertNotIn(CANARY, str(error.exception))

    def test_relative_source_link_is_pinned_without_fetching_linked_document(self):
        endpoint = f'repos/example/team-wiki/git/blobs/{PAGE}'
        body = '# Guide\ndate: 2026-10-01\n[Card](templates/card.md)\n[[card]]'
        new_sha = blob_sha(body)
        self.responses[f'repos/example/team-wiki/git/blobs/{new_sha}'] = dict(blob(body), sha=new_sha)
        for entry in self.responses[f'repos/example/team-wiki/git/trees/{TREE}?recursive=1']['tree']:
            if entry['sha'] == PAGE:
                entry['size'] = len(body.encode())
                entry['sha'] = new_sha
        snap = self.snapshot()
        curated = dict(card(), source_links=[f'https://github.com/example/team-wiki/blob/{COMMIT}/knowledge/templates/card.md'], cross_references=['knowledge/templates/card.md'])
        response = {'model': 'example-model', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps({'cards': [curated]})}]}
        with patch('collector.subprocess.Popen', side_effect=self.spawn), patch('collector.urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(response).encode())):
            result = collector.collect_batch(snap, ['knowledge/guide.md'], collector.GitHubSource(), collector.VertexCurator('example-project', 'eu', 'example-model'))
        self.assertEqual(result['cards'][0]['source_links'], [f'https://github.com/example/team-wiki/blob/{COMMIT}/knowledge/templates/card.md'])
        self.assertFalse(any(args[-1].endswith(TEMPLATE) for args, _ in self.calls))

    def test_source_backlink_is_pinned_but_the_source_body_is_not_fetched(self):
        snap = self.snapshot()
        snap.entries['sources/evidence.md'] = {'type': 'blob', 'mode': '100644', 'sha': '1' * 40, 'size': 10}
        body = '# Guide\ndate: 2026-10-01\n[Original](../sources/evidence.md)'
        item = dict(card(), source_links=[f'https://github.com/example/team-wiki/blob/{COMMIT}/sources/evidence.md'])
        collector._validate_provenance(snap, {'knowledge/guide.md': body}, {'cards': [item]})
        self.assertFalse(any('git/blobs/' + '1' * 40 in args[-1] for args, _ in self.calls))

    def test_complete_index_can_exceed_page_limit_without_allowing_oversized_pages(self):
        body = '[[guide]]\n' + 'navigation entry\n' * 18000
        sha = blob_sha(body)
        self.responses[f'repos/example/team-wiki/git/blobs/{sha}'] = dict(blob(body), sha=sha)
        for entry in self.responses[f'repos/example/team-wiki/git/trees/{TREE}?recursive=1']['tree']:
            if entry['path'] == 'index.md':
                entry.update(size=len(body.encode()), sha=sha)
        snap = self.snapshot()
        self.assertEqual(len(snap.index.encode()), len(body.encode()))
        self.assertEqual(snap.indexed_paths, ('knowledge/guide.md',))

    def test_failure_evidence_distinguishes_uncertain_timeout_from_settled_invalid_completion(self):
        snap = self.snapshot()
        curator = collector.VertexCurator('example-project', 'eu', 'example-model')
        with patch('collector.subprocess.Popen', side_effect=self.spawn), patch('collector.urllib.request.OpenerDirector.open', side_effect=TimeoutError()):
            with self.assertRaises(ValueError):
                curator.curate(snap, {'knowledge/guide.md': PAGE_TEXT})
        self.assertEqual(curator.last_evidence['outcome'], 'uncertain')
        response = json.dumps({'model': 'wrong-model', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': CANARY}]}).encode()
        with patch('collector.subprocess.Popen', side_effect=self.spawn), patch('collector.urllib.request.OpenerDirector.open', return_value=io.BytesIO(response)):
            with self.assertRaises(ValueError):
                curator.curate(snap, {'knowledge/guide.md': PAGE_TEXT})
        self.assertEqual(curator.last_evidence['outcome'], 'settled')
        self.assertEqual(curator.last_evidence['failure_code'], 'invalid_completion')
        self.assertEqual(curator.last_evidence['response_sha256'], hashlib.sha256(response).hexdigest())
        self.assertNotIn(CANARY, json.dumps(curator.last_evidence))

    def test_source_command_failure_discards_stderr_and_clears_debug_environment(self):
        observed = []

        def fail(args, **kwargs):
            observed.append(kwargs['env'])
            return Process(subprocess.CompletedProcess(args, 1, CANARY.encode(), CANARY.encode()))

        with patch.dict('collector.os.environ', {'GH_DEBUG': 'api', 'GH_HOST': 'other.example.org'}), patch('collector.subprocess.Popen', side_effect=fail):
            with self.assertRaises(ValueError) as error:
                collector.GitHubSource().snapshot('example/team-wiki', 'main')
        self.assertNotIn(CANARY, str(error.exception))
        self.assertNotIn('GH_DEBUG', observed[0])
        self.assertNotIn('GH_HOST', observed[0])


if __name__ == '__main__':
    unittest.main()
