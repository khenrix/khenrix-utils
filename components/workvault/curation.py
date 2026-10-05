"""Pure validation and canonical Markdown encoding for curated WorkVault batches."""

from datetime import date, datetime
import html
from ipaddress import IPv6Address
import json
import re
from urllib.parse import quote, urlsplit


BATCH_SCHEMA = 'workvault-curated-batch-v1'
PUBLICATION_SCHEMA = 'workvault-publication-v1'
MAX_CARDS = 5
MAX_INVENTORY_PATHS = 4096
MAX_METADATA_ITEMS = 32
MAX_PATH_BYTES = 512
MAX_TITLE_BYTES = 512
MAX_SUMMARY_BYTES = 4096
MAX_DETAILS_BYTES = 16384
MAX_UNCERTAINTY_BYTES = 1024
MAX_URL_BYTES = 2048
MAX_BATCH_BYTES = 1024 * 1024
MAX_PUBLICATION_BYTES = 4 * 1024 * 1024

_BATCH_FIELDS = set('schema_version repository commit inventory_complete discovered_paths indexed_paths requested_paths cards'.split())
_CARD_FIELDS = set('path title summary details date draft uncertainty source_links cross_references'.split())
_PUBLICATION_FIELDS = {'schema_version', 'batch', 'published_at'}
_OPEN = '<!-- workvault-publication-v1\n'
_CLOSE = '\n-->\n\n'
_MARKDOWN_ESCAPES = {ord(char): f'&#{ord(char)};' for char in '\\`*_{}[]()#+.!|=-~$%^'}


def _object(value, fields):
    if type(value) is not dict or set(value) != fields:
        raise ValueError('invalid fields')
    return value


def _literal(value, expected):
    if type(value) is not str or value != expected:
        raise ValueError('invalid schema or repository')


def _text(value, maximum, allow_empty=False):
    if type(value) is not str:
        raise ValueError('invalid text')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError:
        raise ValueError('invalid UTF-8 text') from None
    if size > maximum or (not allow_empty and not value.strip()):
        raise ValueError('invalid text length')
    if any((ord(char) < 32 and char not in '\r\n\t') or ord(char) == 127 for char in value):
        raise ValueError('invalid text control character')
    return value


def _path(value):
    value = _text(value, MAX_PATH_BYTES)
    parts = value.split('/')
    if ('\\' in value or len(parts) < 2 or parts[0] != 'knowledge'
            or not value.endswith('.md') or parts[-1] == '.md'
            or any(part in ('', '.', '..') for part in parts)
            or any(ord(char) < 32 for char in value)):
        raise ValueError('invalid knowledge path')
    return value


def validate_repository(value):
    """Accept an unambiguous GitHub owner/repository slug for pinned citations."""
    value = _text(value, 140)
    parts = value.split('/')
    if (len(parts) != 2
            or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', parts[0])
            or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', parts[1])
            or parts[1] in ('.', '..')):
        raise ValueError('invalid repository')
    return value


def _date(value):
    value = _text(value, 10)
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError('invalid date')
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError('invalid date') from None
    return value


def _timestamp(value):
    value = _text(value, 20)
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', value):
        raise ValueError('invalid UTC timestamp')
    try:
        datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        raise ValueError('invalid UTC timestamp') from None
    return value


def _https_url(value):
    value = _text(value, MAX_URL_BYTES)
    if (any(char.isspace() for char in value)
            or any(char in value for char in ('\\', '<', '>', '"', '{', '}', '|', '^', '`'))
            or re.search(r'%(?![0-9A-Fa-f]{2})', value)):
        raise ValueError('invalid HTTPS URL')
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
        if (parts.scheme != 'https' or not hostname
                or parts.username is not None or parts.password is not None
                or '%' in hostname or parts.netloc.endswith(':')
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError('invalid HTTPS URL')
        if ':' in hostname:
            IPv6Address(hostname)
        else:
            hostname = hostname.encode('idna').decode('ascii')
            if hostname.endswith('.'):
                hostname = hostname[:-1]
            if (not hostname or len(hostname) > 253
                    or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                           for label in hostname.split('.'))):
                raise ValueError('invalid HTTPS URL')
    except (ValueError, UnicodeError):
        raise ValueError('invalid HTTPS URL') from None
    return value


def _strings(value, validator, maximum, minimum=0):
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ValueError('invalid list')
    clean = [validator(item) for item in value]
    if len(set(clean)) != len(clean):
        raise ValueError('duplicate list item')
    return clean


def _card(value):
    value = _object(value, _CARD_FIELDS)
    if type(value['draft']) is not bool:
        raise ValueError('invalid draft boolean')
    return {
        'path': _path(value['path']),
        'title': _text(value['title'], MAX_TITLE_BYTES),
        'summary': _text(value['summary'], MAX_SUMMARY_BYTES),
        'details': _text(value['details'], MAX_DETAILS_BYTES, allow_empty=True),
        'date': _date(value['date']),
        'draft': value['draft'],
        'uncertainty': _strings(value['uncertainty'], lambda item: _text(item, MAX_UNCERTAINTY_BYTES), MAX_METADATA_ITEMS),
        'source_links': _strings(value['source_links'], _https_url, MAX_METADATA_ITEMS),
        'cross_references': _strings(value['cross_references'], _path, MAX_METADATA_ITEMS),
    }


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def validate_curated_batch(batch: dict) -> dict:
    """Return an independent copy; reject incomplete, unknown or invalid data."""
    batch = _object(batch, _BATCH_FIELDS)
    _literal(batch['schema_version'], BATCH_SCHEMA)
    repository = validate_repository(batch['repository'])
    commit = _text(batch['commit'], 40)
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('invalid pinned commit')
    if batch['inventory_complete'] is not True:
        raise ValueError('complete inventory required')
    discovered = _strings(batch['discovered_paths'], _path, MAX_INVENTORY_PATHS)
    indexed = _strings(batch['indexed_paths'], _path, MAX_INVENTORY_PATHS)
    requested = _strings(batch['requested_paths'], _path, MAX_CARDS, minimum=1)
    discovery = set(discovered)
    if not set(indexed) <= discovery or not set(requested) <= discovery:
        raise ValueError('path outside discovery')
    if type(batch['cards']) is not list or not 1 <= len(batch['cards']) <= MAX_CARDS:
        raise ValueError('invalid card count')
    cards = [_card(card) for card in batch['cards']]
    card_paths = [card['path'] for card in cards]
    if len(set(card_paths)) != len(card_paths) or set(card_paths) != set(requested):
        raise ValueError('cards must match requested paths exactly')
    clean = {
        'schema_version': BATCH_SCHEMA,
        'repository': repository,
        'commit': commit,
        'inventory_complete': True,
        'discovered_paths': discovered,
        'indexed_paths': indexed,
        'requested_paths': requested,
        'cards': cards,
    }
    if len(_json(clean).encode('utf-8')) > MAX_BATCH_BYTES:
        raise ValueError('curated batch too large')
    return clean


def _checkpoint(batch, published_at):
    return {
        'schema_version': PUBLICATION_SCHEMA,
        'batch': validate_curated_batch(batch),
        'published_at': _timestamp(published_at),
    }


def _escape(value):
    text = (html.escape(value, quote=False).translate(_MARKDOWN_ESCAPES)
            .replace('\r', '&#13;').replace('\t', '&#9;').replace('\n', '<br>'))
    body = text.lstrip(' ')
    return '&#32;' * (len(text) - len(body)) + body


def _link(url, label=None):
    label = url if label is None else label
    return f'<a href="{html.escape(url, quote=True)}">{_escape(label)}</a>'


def _items(label, values):
    lines = [f'**{label}:**', '']
    lines.extend(f'- {value}' for value in values)
    if not values:
        lines.append('- None')
    lines.append('')
    return lines


def _render(checkpoint):
    batch = checkpoint['batch']
    discovered = set(batch['discovered_paths'])
    indexed = set(batch['indexed_paths'])
    requested = set(batch['requested_paths'])
    payload = (_json(checkpoint).replace('&', '\\u0026')
               .replace('<', '\\u003c').replace('>', '\\u003e')
               .replace('--', '\\u002d\\u002d'))
    lines = [
        '# Curated knowledge', '',
        f"Repository: {batch['repository']}",
        f"Commit: {batch['commit']}",
        f"Published at: {checkpoint['published_at']}",
        f"Full source coverage: {'true' if requested == discovered else 'false'}",
        '', '## Coverage', '',
    ]
    for label, paths in (
        ('Covered paths', requested),
        ('Uncovered paths', discovered - requested),
        ('Index gaps', discovered - indexed),
    ):
        lines.extend(_items(label, [_escape(path) for path in sorted(paths)]))
    base = f"https://github.com/{batch['repository']}/blob/{batch['commit']}/"
    for card in sorted(batch['cards'], key=lambda item: item['path']):
        citation = _link(base + quote(card['path'], safe='/'), 'Exact-commit source')
        lines.extend([
            f"## {_escape(card['title'])}", '',
            f"Path: {_escape(card['path'])}",
            f"Date: {card['date']}",
            f"Draft: {'true' if card['draft'] else 'false'}",
            f'Source: {citation}', '',
            '### Summary', '', _escape(card['summary']), '',
            '### Details', '', _escape(card['details']), '',
        ])
        lines.extend(_items('Uncertainty', [_escape(item) for item in card['uncertainty']]))
        lines.extend(_items('Sources', [_link(url) for url in card['source_links']]))
        lines.extend(_items('Cross-references', [
            _link(base + quote(path, safe='/'), path) for path in card['cross_references']
        ]))
    data = (_OPEN + payload + _CLOSE + '\n'.join(lines) + '\n').encode('utf-8')
    if len(data) > MAX_PUBLICATION_BYTES:
        raise ValueError('publication too large')
    return data


def render_publication(batch: dict, published_at: str) -> bytes:
    """Encode a validated batch and explicit UTC time as canonical UTF-8 Markdown."""
    return _render(_checkpoint(batch, published_at))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('invalid JSON constant')


def parse_curated_batch(data: bytes) -> dict:
    """Decode bounded, strict JSON without retaining unknown source fields."""
    if type(data) is not bytes or len(data) > MAX_BATCH_BYTES:
        raise ValueError('invalid curated batch bytes')
    try:
        raw = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object,
                         parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid curated batch JSON') from None
    return validate_curated_batch(raw)


def parse_publication(data: bytes) -> dict:
    """Validate the checkpoint and require an exact matching whole-file rendering."""
    if type(data) is not bytes or len(data) > MAX_PUBLICATION_BYTES:
        raise ValueError('invalid publication bytes')
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        raise ValueError('invalid publication UTF-8') from None
    if not text.startswith(_OPEN):
        raise ValueError('missing publication checkpoint')
    end = text.find(_CLOSE, len(_OPEN))
    if end < 0:
        raise ValueError('truncated publication checkpoint')
    try:
        raw = json.loads(text[len(_OPEN):end], object_pairs_hook=_unique_object,
                         parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ValueError('invalid publication JSON') from None
    raw = _object(raw, _PUBLICATION_FIELDS)
    _literal(raw['schema_version'], PUBLICATION_SCHEMA)
    clean = _checkpoint(raw['batch'], raw['published_at'])
    if _render(clean) != data:
        raise ValueError('mismatched publication rendering')
    return clean
