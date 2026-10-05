"""Pure projection of already-curated metadata, not an upstream index adapter."""
import hashlib
import json
import re
from urllib.parse import quote

from curation import (_date, _object, _text, validate_repository,
                      MAX_BATCH_BYTES, MAX_INVENTORY_PATHS, MAX_PATH_BYTES)


_ROUTE_FIELDS = set('repository entrypoint use_when caretaker slack_channel status review_date'.split())


def _repository(value):
    return validate_repository(value).lower()


def _commit(value):
    if type(value) is not str or not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('invalid pinned commit')
    return value


def _entrypoint(value):
    value = _text(value, MAX_PATH_BYTES)
    if (any(part in ('', '.', '..') for part in value.split('/'))
            or any(char in value for char in '\\:%?#')
            or any(char.isspace() for char in value)):
        raise ValueError('invalid declared repository path')
    return value


def _pin(value, path):
    value = _object(value, {'repository', 'commit', 'path'})
    if value['path'] != path:
        raise ValueError('invalid directory source path')
    return {'repository': _repository(value['repository']),
            'commit': _commit(value['commit']), 'path': path}


def _route(value):
    value = _object(value, _ROUTE_FIELDS)
    return {'repository': _repository(value['repository']),
        'entrypoint': _entrypoint(value['entrypoint']),
        'use_when': _text(value['use_when'], 4096),
        'caretaker': _text(value['caretaker'], 512),
        'slack_channel': _text(value['slack_channel'], 512),
        'status': _text(value['status'], 64),
        'review_date': None if value['review_date'] is None else _date(value['review_date'])}


def directory_report(*, index, health, records, selected, existing_repository,
                     inventory_complete, target_commits, stale_before=None):
    """Return bounded curated routing data; perform no reads, writes or authorization.

    Pin metadata is an input assertion, not proof of an authenticated read. Callers
    must establish authenticated provenance before using this metadata.
    Selection is explicit: relevance and source/consumer admission are not inferred.
    """
    index, health = _pin(index, 'index.json'), _pin(health, 'health.md')
    if (index['repository'], index['commit']) != (health['repository'], health['commit']):
        raise ValueError('directory revisions differ')
    existing_repository = _repository(existing_repository)
    if type(inventory_complete) is not bool:
        raise ValueError('invalid inventory completeness')
    if type(records) is not list or len(records) > MAX_INVENTORY_PATHS:
        raise ValueError('invalid directory record count')
    unique = {}
    duplicates = 0
    for value in records:
        clean = _route(value)
        identity = (clean['repository'], clean['entrypoint'])
        if identity in unique:
            if unique[identity] != clean:
                raise ValueError('conflicting duplicate directory identity')
            duplicates += 1
        else:
            unique[identity] = clean
    if type(selected) is not list or len(selected) > MAX_INVENTORY_PATHS:
        raise ValueError('invalid selected route count')
    chosen = set()
    for identity in selected:
        if type(identity) not in (tuple, list) or len(identity) != 2:
            raise ValueError('invalid selected identity')
        key = (_repository(identity[0]), _entrypoint(identity[1]))
        if key not in unique or key in chosen:
            raise ValueError('selected identity missing or repeated')
        chosen.add(key)
    if type(target_commits) is not dict or len(target_commits) > MAX_INVENTORY_PATHS:
        raise ValueError('invalid target pins')
    registered_repositories = {record['repository'] for record in unique.values()}
    pins = {}
    for repository, commit in target_commits.items():
        repository, commit = _repository(repository), _commit(commit)
        if repository in pins and pins[repository] != commit:
            raise ValueError('conflicting target pins')
        pins[repository] = commit
    if not set(pins) <= registered_repositories:
        raise ValueError('target pin outside directory')
    if stale_before is not None:
        stale_before = _date(stale_before)
    cards = []
    existing_routes = []
    for identity, clean in sorted(unique.items()):
        if clean['repository'] == existing_repository:
            existing_routes.append(dict(clean))
            continue
        if identity not in chosen:
            continue
        flags = []
        if clean['review_date'] is None:
            flags.append('review_date_unknown')
        elif stale_before is not None and clean['review_date'] < stale_before:
            flags.append('stale_review')
        if clean['status'] == 'deprecated':
            flags.append('deprecated')
        elif clean['status'] != 'active':
            flags.append('status_unrecognized')
        target = pins.get(clean['repository'])
        if target is None:
            flags.append('target_commit_unresolved')
        cards.append({**clean, 'identity': list(identity),
            'card_id': 'Directory-' + hashlib.sha256(('\n'.join(identity)).encode()).hexdigest()[:24],
            'target_commit': target,
            'entrypoint_citation': None if target is None else
                f"https://github.com/{clean['repository']}/blob/{target}/{quote(clean['entrypoint'], safe='/')}",
            'directory_provenance': {'index': dict(index), 'health': dict(health)},
            'flags': flags})
    uncovered = sorted(identity for identity in unique
        if identity[0] != existing_repository and identity not in chosen)
    result = {'adapter_status': 'unverified',
        'directory_provenance': {'index': index, 'health': health},
        'inventory_complete': inventory_complete,
        'registered_identity_count': len(unique), 'duplicate_record_count': duplicates,
        'cards': cards, 'uncovered_identities': [list(identity) for identity in uncovered],
        'full_directory_coverage': inventory_complete and not uncovered,
        'full_source_coverage': False, 'source_checkpoint': None,
        'existing_registration': 'registered_existing_source' if existing_routes else
            ('missing_registration' if inventory_complete else 'unknown_incomplete_inventory'),
        'existing_routes': existing_routes, 'stale_before': stale_before,
        'stale_check': 'configured' if stale_before is not None else 'unconfigured'}
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_BATCH_BYTES:
        raise ValueError('directory report too large')
    return result
