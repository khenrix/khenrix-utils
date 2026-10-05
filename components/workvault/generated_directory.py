"""Pure bounded adapter for the observed generated index, not kb.yaml registration.

Callers supply already-admitted metadata bytes and independent target pins. This
module establishes no authenticated-read, source-access, or consumer authority.
"""
from datetime import date
import json
import re

from curation import (_date, _object, _text, _unique_object, _reject_constant,
    MAX_BATCH_BYTES, MAX_INVENTORY_PATHS, MAX_METADATA_ITEMS, MAX_PATH_BYTES)
from directory import _repository, directory_report


_REQUIRED = {'team', 'repo', 'use_when', 'caretaker', 'access'}
_OPTIONAL = {'slack', 'status', 'last_commit', 'review_by'}
_HEALTH = re.compile(r'^Generated ([0-9]{4}-[0-9]{2}-[0-9]{2})\. Scanned ([0-9]+) tagged repo\(s\), ([0-9]+) registered\.$', re.MULTILINE)


def _bytes(value):
    if type(value) is not bytes or len(value) > MAX_BATCH_BYTES:
        raise ValueError('invalid directory bytes')
    try:
        return value.decode('utf-8')
    except UnicodeError:
        raise ValueError('invalid directory UTF-8') from None


def _fields(value, required, optional):
    if type(value) is not dict or not required <= set(value) <= required | optional:
        raise ValueError('invalid generated fields')
    return value


def _path(value, allow_root=False):
    value = _text(value, MAX_PATH_BYTES)
    if (value.startswith('/') or any(char in value for char in '\\:%?#')
            or any(char.isspace() for char in value) or '..' in value.split('/')):
        raise ValueError('invalid repository path')
    parts = [part for part in value.split('/') if part not in ('', '.')]
    if not parts and not allow_root:
        raise ValueError('invalid entrypoint')
    return '/'.join(parts)


def _entry(value):
    value = _fields(value, _REQUIRED, _OPTIONAL)
    team = _text(value['team'], 512)
    use_when = _text(value['use_when'], 2400)
    if len(team) < 2 or not 60 <= len(use_when) <= 600:
        raise ValueError('invalid generated text length')
    status = value.get('status', 'stable')
    if type(status) is not str or status not in ('draft', 'stable', 'deprecated'):
        raise ValueError('invalid generated status')
    access = value['access']
    if type(access) is not list or not 1 <= len(access) <= MAX_METADATA_ITEMS:
        raise ValueError('invalid access count')
    paths = []
    mcp_count = 0
    for mechanism in access:
        if type(mechanism) is not dict:
            raise ValueError('invalid access mechanism')
        if mechanism.get('kind') == 'repo':
            mechanism = _fields(mechanism, {'kind'}, {'path', 'entrypoint'})
            base = _path(mechanism.get('path', '.'), allow_root=True)
            entrypoint = _path(mechanism.get('entrypoint', 'README.md'))
            paths.append(_path('/'.join(part for part in (base, entrypoint) if part)))
        elif mechanism.get('kind') == 'mcp':
            mechanism = _fields(mechanism, {'kind', 'url'}, {'auth'})
            url = _text(mechanism['url'], 2048)
            if (not re.fullmatch(r'[A-Za-z][A-Za-z0-9+.-]*:[^\s]+', url)
                    or mechanism.get('auth', 'none') not in ('none', 'bearer')):
                raise ValueError('invalid inert MCP metadata')
            mcp_count += 1
        else:
            raise ValueError('invalid access kind')
    if not paths:
        raise ValueError('repo access required')
    slack = None if 'slack' not in value else _text(value['slack'], 512)
    if slack is not None and not slack.startswith('#'):
        raise ValueError('invalid Slack metadata')
    # Enrichment dates are distinct from registration data and Git revisions.
    return {'repository': _repository(value['repo']), 'entrypoint': paths[0],
        'team': team, 'use_when': use_when, 'caretaker': _text(value['caretaker'], 512),
        'slack_channel': slack, 'status': status,
        'last_commit': None if 'last_commit' not in value else _date(value['last_commit']),
        'review_by': None if 'review_by' not in value else _date(value['review_by']),
        'repo_access_count': len(paths), 'mcp_access_count': mcp_count}


def generated_directory_report(*, index_bytes, health_bytes, index, health,
                               selected, existing_repository, target_commits):
    """Project supplied generated metadata without reads, writes, or dispatch.

    Health corroborates generated date/count only; a tagged scan, even with no
    findings, cannot establish a complete inventory or revoke an existing source.
    """
    try:
        data = json.loads(_bytes(index_bytes), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ValueError('invalid generated directory JSON') from None
    data = _object(data, {'generated_at', 'count', 'knowledge_bases'})
    generated = _date(data['generated_at'])
    entries = data['knowledge_bases']
    if (type(entries) is not list or len(entries) > MAX_INVENTORY_PATHS
            or type(data['count']) is not int or data['count'] != len(entries)):
        raise ValueError('generated count differs')
    health_text = _text(_bytes(health_bytes), MAX_BATCH_BYTES)
    summaries = _HEALTH.findall(health_text)
    if len(summaries) != 1 or not health_text.startswith('# Index health\n'):
        raise ValueError('invalid generated health summary')
    health_day, scanned, registered = summaries[0]
    scanned, registered = int(scanned), int(registered)
    if (_date(health_day) != generated or registered != data['count']
            or not registered <= scanned <= MAX_INVENTORY_PATHS):
        raise ValueError('generated health differs')
    normalized = [_entry(entry) for entry in entries]
    metadata = {}
    for entry in normalized:
        identity = (entry['repository'], entry['entrypoint'])
        if identity in metadata and metadata[identity] != entry:
            raise ValueError('conflicting generated identity')
        metadata[identity] = entry
    records = [{'repository': entry['repository'], 'entrypoint': entry['entrypoint'],
        'use_when': entry['use_when'], 'caretaker': entry['caretaker'],
        'slack_channel': entry['slack_channel'] or 'unknown',
        'status': 'deprecated' if entry['status'] == 'deprecated' else 'active',
        'review_date': None} for entry in normalized]
    result = directory_report(index=index, health=health, records=records,
        selected=selected, existing_repository=existing_repository, inventory_complete=False,
        target_commits=target_commits)
    today = date.fromisoformat(generated)
    for item in result['cards'] + result['existing_routes']:
        entry = metadata[(item['repository'], item['entrypoint'])]
        flags = [flag for flag in item.get('flags', []) if flag != 'review_date_unknown']
        if entry['status'] == 'deprecated' and 'deprecated' not in flags:
            flags.append('deprecated')
        if entry['status'] == 'draft':
            flags.append('draft')
        if entry['review_by'] is None:
            flags.append('review_by_unknown')
        elif entry['review_by'] < generated:
            flags.append('review_overdue')
        if entry['last_commit'] is None:
            flags.append('last_commit_unknown')
        elif (today - date.fromisoformat(entry['last_commit'])).days > 90:
            flags.append('last_commit_stale')
        item.pop('review_date')
        item.update(entry, flags=flags)
    result.update(adapter_status='pure_generated_envelope_candidate',
        generated_at=generated, generated_record_count=data['count'],
        health_summary={'generated_at': health_day, 'tagged_repositories_scanned': scanned,
            'registered': registered, 'findings': 'none_reported' if
                'No findings. Everything tagged is registered, valid, fresh and owned.' in health_text.splitlines()
                else 'reported_or_unknown'},
        existing_source_listed=bool(result['existing_routes']), existing_source_action='preserve',
        staleness_reference=generated, staleness_rule='review_by_before_generation_or_last_commit_age_gt_90_days')
    result.pop('stale_before')
    result.pop('stale_check')
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_BATCH_BYTES:
        raise ValueError('generated report too large')
    return result
