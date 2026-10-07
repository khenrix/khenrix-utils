"""Bounded metadata contract for source adapters; raw bodies are not page state."""

import re
from typing import Protocol

import curation


SOURCE_SCHEMA = 'workvault-source-v2'
LOCATOR_SCHEMA = 'workvault-source-locator-v2'
MAX_PAGE_ITEMS = 100
_REFERENCE_FIELDS = set('schema_version kind account container item_id revision author event_at modified_at observed_at citation content_hash'.split())
_KINDS = {'github', 'slack', 'drive', 'gmail', 'jira', 'backstage'}


class SourceAdapter(Protocol):
    def list_page(self, *, cursor: str | None, cutoff: str) -> dict: ...
    def fetch(self, reference: dict) -> dict: ...
    def changes(self, *, cursor: str | None) -> dict: ...


def _metadata(value, schema):
    value = curation._object(value, _REFERENCE_FIELDS if schema == SOURCE_SCHEMA else _REFERENCE_FIELDS - {'content_hash'})
    curation._literal(value['schema_version'], schema)
    if type(value['kind']) is not str or value['kind'] not in _KINDS:
        raise ValueError('invalid source kind')
    clean = {'schema_version': schema, 'kind': value['kind']}
    for key in ('account', 'container', 'item_id', 'revision', 'author'):
        clean[key] = None if key in ('revision', 'author') and value[key] is None else curation._text(value[key], 512)
        if clean[key] is not None and any(ord(char) < 32 for char in clean[key]):
            raise ValueError('invalid source identity')
    for key in ('event_at', 'modified_at'):
        clean[key] = None if value[key] is None else curation._timestamp(value[key])
    clean['observed_at'] = curation._timestamp(value['observed_at'])
    if any(clean[key] is not None and clean[key] > clean['observed_at'] for key in ('event_at', 'modified_at')):
        raise ValueError('invalid source chronology')
    clean['citation'] = curation._https_url(value['citation'])
    return clean


def validate_locator(value):
    """Validate listing metadata without demanding an unfetched content hash."""
    return _metadata(value, LOCATOR_SCHEMA)


def validate_reference(value):
    clean = _metadata(value, SOURCE_SCHEMA)
    digest = value['content_hash']
    if type(digest) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('invalid source content hash')
    clean['content_hash'] = digest
    return clean


def source_key(reference):
    clean = (validate_locator(reference) if type(reference) is dict and reference.get('schema_version') == LOCATOR_SCHEMA
             else validate_reference(reference))
    return curation._json([clean[key] for key in ('kind', 'account', 'container', 'item_id')])


def revision_key(reference):
    clean = validate_reference(reference)
    return curation._json([clean[key] for key in ('kind', 'account', 'container', 'item_id', 'revision', 'content_hash')])


def validate_references(value, maximum=curation.MAX_METADATA_ITEMS):
    if type(value) is not list or len(value) > maximum:
        raise ValueError('invalid source reference list')
    clean = [validate_reference(item) for item in value]
    keys = [revision_key(item) for item in clean]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate source reference')
    revisions = {}
    for ref in clean:
        if ref['revision'] is not None:
            key = (source_key(ref), ref['revision'])
            if key in revisions and revisions[key] != ref['content_hash']:
                raise ValueError('conflicting source revision')
            revisions[key] = ref['content_hash']
    return clean


def validate_page(value):
    value = curation._object(value, {'items', 'next_cursor', 'complete', 'gaps'})
    if type(value['items']) is not list or len(value['items']) > MAX_PAGE_ITEMS:
        raise ValueError('invalid page items')
    items = [validate_locator(item) for item in value['items']]
    keys = [(source_key(item), item['revision'], item['event_at'], item['modified_at']) for item in items]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate page item')
    if type(value['complete']) is not bool:
        raise ValueError('invalid page completeness')
    cursor = value['next_cursor']
    if cursor is not None:
        cursor = curation._text(cursor, 2048)
    if value['complete'] != (cursor is None):
        raise ValueError('invalid page cursor')
    if type(value['gaps']) is not list or len(value['gaps']) > curation.MAX_METADATA_ITEMS:
        raise ValueError('invalid coverage gaps')
    gaps = []
    for gap in value['gaps']:
        curation._object(gap, {'code', 'count'})
        code = curation._text(gap['code'], 64)
        if not re.fullmatch(r'[a-z][a-z0-9_]*', code):
            raise ValueError('invalid coverage gap code')
        count = gap['count']
        if count is not None and (type(count) is not int or not 0 <= count <= 2**63 - 1):
            raise ValueError('invalid uncovered count')
        gaps.append({'code': code, 'count': count})
    clean = {'items': items, 'next_cursor': cursor, 'complete': value['complete'], 'gaps': gaps}
    if len(curation._json(clean).encode('utf-8')) > curation.MAX_BATCH_BYTES:
        raise ValueError('source page too large')
    return clean


def validate_changes(value):
    value = curation._object(value, {'page', 'checkpoint'})
    page = validate_page(value['page'])
    checkpoint = value['checkpoint']
    if checkpoint is not None:
        checkpoint = curation._text(checkpoint, 2048)
        if not page['complete'] or any(ord(char) < 32 for char in checkpoint):
            raise ValueError('invalid change checkpoint')
    return {'page': page, 'checkpoint': checkpoint}
