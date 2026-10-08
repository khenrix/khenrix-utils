"""Full-tree inbound sync. Raw source and model payloads stay in this process."""
import argparse
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

from curation import (_path, _reject_constant, _strings, _unique_object,
                      MAX_BATCH_BYTES, MAX_INVENTORY_PATHS, parse_publication,
                      validate_curated_batch, validate_repository)
from workvault import (_directory, _file, _identity, _private, _read, _root_path,
                       _unlink_owned, _write_private, publish_curated_batch, status)
from batch import (BatchPublicationRejected, publication_batch, publication_name,
                   publication_pending, recover_publication_batch)


STATE = 'source-state.json'
SCHEMA = 'workvault-sync-state-v1'


class SyncError(RuntimeError):
    """A content-free failure; inspect status before resuming."""


@dataclass(frozen=True)
class SyncConfig:
    repository: str
    vault: Path
    snapshots: Path
    state: Path
    project: str
    location: str
    model: str


def _topic_name(value):
    if (type(value) is not str or not value or value.strip() != value
            or len(value.encode('utf-8')) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise SyncError('invalid topic name')
    return value


@dataclass(frozen=True)
class SyncTopics:
    repository: str
    topics: tuple

    def __post_init__(self):
        validate_repository(self.repository)
        if type(self.topics) is not tuple or len(self.topics) > MAX_INVENTORY_PATHS:
            raise SyncError('invalid topic mapping')
        names = set()
        for row in self.topics:
            if type(row) is not tuple or len(row) != 2:
                raise SyncError('invalid topic mapping')
            name, paths = row
            _topic_name(name)
            if name in names or type(paths) is not tuple:
                raise SyncError('invalid topic mapping')
            names.add(name)
            _strings(list(paths), _path, MAX_INVENTORY_PATHS)


def parse_topics(data):
    """Copy a bounded owner-declared map; topic selection grants no source access."""
    try:
        if type(data) is not bytes or len(data) > MAX_BATCH_BYTES:
            raise SyncError('invalid topic mapping bytes')
        value = _json(data)
        if (type(value) is not dict or set(value) != {'schema_version', 'repository', 'topics'}
                or value['schema_version'] != 'workvault-sync-topics-v1'
                or type(value['topics']) is not dict
                or any(type(paths) is not list for paths in value['topics'].values())):
            raise SyncError('invalid topic mapping schema')
        return SyncTopics(value['repository'], tuple((name, tuple(paths))
            for name, paths in value['topics'].items()))
    except Exception:
        raise SyncError('topic mapping refused') from None


def _selected_topic(config, topic, topics):
    if topic is None:
        if topics is not None:
            raise SyncError('topic mapping requires a topic')
        return None
    _topic_name(topic)
    if type(topics) is not SyncTopics or topics.repository != config.repository:
        raise SyncError('topic requires configured source mapping')
    selected = dict(topics.topics)
    if topic not in selected:
        raise SyncError('undeclared topic')
    return selected[topic]


def _config(config):
    validate_repository(config.repository)
    if (not re.fullmatch(r'[a-z][a-z0-9-]{4,62}', config.project)
            or config.location != 'eu'
            or not re.fullmatch(r'[a-z][a-z0-9@._-]{0,127}', config.model)):
        raise SyncError('invalid configured route')
    paths = [_root_path(p) for p in (config.vault, config.snapshots, config.state)]
    for i, first in enumerate(paths):
        for second in paths[i + 1:]:
            if first == second or first.is_relative_to(second) or second.is_relative_to(first):
                raise SyncError('overlapping sync roots')
    return SyncConfig(config.repository, *paths, config.project, config.location, config.model)


@contextmanager
def _read_publications(path):
    from workvault import _publication_lock
    with ExitStack() as stack:
        try:
            stack.enter_context(_publication_lock(_root_path(path), shared=True))
        except FileNotFoundError:
            pass
        yield


def _json(data):
    return json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _state(value, repository):
    fields = {'schema_version', 'repository', 'target_commit', 'discovered_paths',
              'indexed_paths', 'tracked_paths', 'failed_paths', 'uncertain_paths', 'provider_evidence', 'broken_index_references'}
    if (type(value) is not dict or set(value) != fields or value['schema_version'] != SCHEMA
            or value['repository'] != repository
            or type(value['target_commit']) is not str
            or not re.fullmatch(r'[0-9a-f]{40}', value['target_commit'])):
        raise SyncError('invalid sync state')
    for key in ('discovered_paths', 'indexed_paths', 'tracked_paths', 'failed_paths', 'uncertain_paths'):
        value[key] = _strings(value[key], _path, MAX_INVENTORY_PATHS)
    discovered = set(value['discovered_paths'])
    if (not set(value['indexed_paths']) <= discovered
            or not set(value['failed_paths']) <= discovered
            or not set(value['uncertain_paths']) <= discovered
            or not discovered <= set(value['tracked_paths'])):
        raise SyncError('invalid sync coverage')
    refs = value['broken_index_references']
    if (type(refs) is not list or len(refs) > MAX_INVENTORY_PATHS
            or any(type(p) is not str or len(p.encode()) > 512
                   or any(ord(c) < 32 for c in p) for p in refs)):
        raise SyncError('invalid index metadata')
    _evidence(value['provider_evidence'])
    return value


def _evidence(value):
    if type(value) is not dict or not set(value) <= {'outcome', 'response_sha256', 'response_bytes', 'served_model', 'stop_reason', 'http_status', 'failure_code'}:
        raise SyncError('invalid provider evidence')
    if value.get('outcome') not in ('not_started', 'uncertain', 'settled', 'refused'):
        raise SyncError('invalid provider outcome')
    if 'response_sha256' in value and (type(value['response_sha256']) is not str or not re.fullmatch(r'[0-9a-f]{64}', value['response_sha256'])):
        raise SyncError('invalid provider digest')
    if 'response_bytes' in value and (type(value['response_bytes']) is not int or not 0 <= value['response_bytes'] <= 8 * 1024 * 1024):
        raise SyncError('invalid provider size')
    if 'served_model' in value and (type(value['served_model']) is not str or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}', value['served_model'])):
        raise SyncError('invalid provider model')
    if 'http_status' in value and (type(value['http_status']) is not int or not 400 <= value['http_status'] < 500):
        raise SyncError('invalid provider status')
    if 'stop_reason' in value and value['stop_reason'] not in ('end_turn', 'max_tokens', 'tool_use', 'stop_sequence', 'refusal'):
        raise SyncError('invalid provider stop')
    if 'failure_code' in value and value['failure_code'] not in ('invalid_completion', 'invalid_content', 'invalid_curation_fields', 'invalid_json', 'invalid_schema', 'invalid_coverage', 'unsupported_provenance', 'raw_page_reproduction', 'raw_line_reproduction', 'draft_state_lost', 'source_date_lost', 'unknown_date_not_disclosed'):
        raise SyncError('invalid provider failure code')
    return dict(value)


def _load_state(directory, repository):
    try:
        return _state(_json(_read(directory, STATE)), repository)
    except FileNotFoundError:
        return None


def _save_state(directory, state, name=STATE):
    try:
        old = _read(directory, name)
    except FileNotFoundError:
        old = None
    data = (json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode()
    staged = '.sync-stage-' + uuid.uuid4().hex
    identity = _write_private(directory, staged, data)
    try:
        try:
            current = _read(directory, name)
        except FileNotFoundError:
            current = None
        if current != old:
            raise SyncError('sync state changed')
        os.replace(staged, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        _unlink_owned(directory, staged, identity)


@contextmanager
def _lock(root):
    with _directory(root, create=True) as directory:
        with _file(directory, '.sync.lock', os.O_RDWR, create=True) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                info = os.stat('.sync.lock', dir_fd=directory, follow_symlinks=False)
                _private(info)
                if _identity(info) != _identity(os.fstat(lock)):
                    raise SyncError('sync lock changed')
                yield directory
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _status(config, state):
    if state is None:
        return {'schema_version': 'workvault-sync-status-v1', 'state': 'empty'}
    covered, stale = [], []
    discovered = state['discovered_paths']
    for path in discovered:
        item = status(config.vault, publication=publication_name(config.repository, path))
        if item['state'] == 'empty':
            continue
        if item['repository'] != config.repository or item['covered_paths'] != [path]:
            raise SyncError('publication differs from configured source')
        if item['published_commit'] == state['target_commit']:
            covered.append(path)
        else:
            stale.append(path)
    uncovered = sorted(set(discovered) - set(covered))
    failed = sorted(set(state['failed_paths']) - set(covered))
    return {'schema_version': 'workvault-sync-status-v1',
        'state': 'complete' if not uncovered else 'partial',
        'repository': config.repository, 'target_commit': state['target_commit'],
        'discovered_count': len(discovered), 'covered_paths': sorted(covered),
        'uncovered_paths': uncovered, 'stale_paths': sorted(stale), 'failed_paths': failed,
        'uncertain_paths': sorted(set(state['uncertain_paths']) - set(covered)),
        'provider_evidence': state['provider_evidence'],
        'not_attempted_paths': sorted(set(uncovered) - set(failed)),
        'index_gaps': sorted(set(discovered) - set(state['indexed_paths'])),
        'broken_index_references': state['broken_index_references'],
        'retained_removed_paths': sorted(set(state['tracked_paths']) - set(discovered)),
        'full_source_coverage': not uncovered}


def sync_status(config):
    """Inspect the last pinned inventory and publications without network or writes."""
    try:
        config = _config(config)
        with _read_publications(config.vault):
            try:
                with _directory(config.state) as directory:
                    state = _load_state(directory, config.repository)
            except FileNotFoundError:
                state = None
            return _status(config, state)
    except Exception:
        raise SyncError('sync status refused') from None


def maintain_status(config):
    try:
        with _read_publications(config.vault):
            result = _maintain_status(config)
            with _curated_reader(config) as (_, report):
                if report['active_card_count'] or report['retained_retired_publication_count'] or 'sources' in report:
                    result['curated'] = report
                    if result['state'] == 'empty':
                        result['state'] = 'reported'
            return result
    except Exception:
        raise SyncError('maintenance refused') from None


def _maintain_status(config):
    """Report local publication metadata against the last observed inventory."""
    try:
        config = _config(config)
        empty = {'schema_version': 'workvault-maintain-status-v1', 'state': 'empty',
                 'conflict_check': 'unavailable_unstructured_cards'}
        try:
            with _directory(config.state) as state_directory:
                try:
                    raw_state = _read(state_directory, STATE)
                except FileNotFoundError:
                    return empty
                state = _state(_json(raw_state), config.repository)
                active = set(state['discovered_paths'])
                stale, missing, duplicates = [], [], {}
                vault_identity = None
                try:
                    with _directory(config.vault) as directory:
                        vault_identity = _identity(os.fstat(directory))
                        observed = {}
                        for path in sorted(state['tracked_paths']):
                            name = publication_name(config.repository, path)
                            try:
                                raw = _read(directory, name)
                            except FileNotFoundError:
                                observed[name] = None
                                missing.append(path)
                                continue
                            batch = parse_publication(raw)['batch']
                            if (batch['repository'] != config.repository
                                    or batch['requested_paths'] != [path]):
                                raise SyncError('publication source differs')
                            observed[name] = hashlib.sha256(raw).digest()
                            if path not in active:
                                continue
                            if batch['commit'] != state['target_commit']:
                                stale.append(path)
                            card = batch['cards'][0]
                            payload = {key: value for key, value in card.items()
                                       if key not in ('path', 'source_links', 'cross_references')}
                            digest = hashlib.sha256(json.dumps(payload, sort_keys=True,
                                ensure_ascii=False, separators=(',', ':')).encode()).digest()
                            duplicates.setdefault(digest, []).append(path)
                        for name, digest in observed.items():
                            try:
                                current = hashlib.sha256(_read(directory, name)).digest()
                            except FileNotFoundError:
                                current = None
                            if current != digest:
                                raise SyncError('publication changed during report')
                except FileNotFoundError:
                    missing = sorted(state['tracked_paths'])
                try:
                    current_state = _read(state_directory, STATE)
                except FileNotFoundError:
                    raise SyncError('inventory changed during report') from None
                if current_state != raw_state:
                    raise SyncError('inventory changed during report')
                for path, expected in ((config.state, _identity(os.fstat(state_directory))),
                                       (config.vault, vault_identity)):
                    try:
                        with _directory(path) as current_directory:
                            current_identity = _identity(os.fstat(current_directory))
                    except FileNotFoundError:
                        current_identity = None
                    if current_identity != expected:
                        raise SyncError('configured root changed during report')
                return {'schema_version': 'workvault-maintain-status-v1', 'state': 'reported',
                    'repository': config.repository, 'observed_commit': state['target_commit'],
                    'stale_paths': stale, 'missing_paths': missing,
                    'retained_removed_paths': sorted(set(state['tracked_paths']) - active),
                    'duplicate_payload_groups': sorted(paths for paths in duplicates.values() if len(paths) > 1),
                    'conflict_check': 'unavailable_unstructured_cards'}
        except FileNotFoundError:
            return empty
    except Exception:
        raise SyncError('maintenance refused') from None


def _topic_status(config, state, topic, selected, commit):
    result = _status(config, state)
    covered = set(result.get('covered_paths', ())) if result.get('target_commit') == commit else set()
    uncovered = sorted(set(selected) - covered)
    result['topic_selection'] = {'topic': topic, 'target_commit': commit,
        'requested_paths': list(selected), 'uncovered_paths': uncovered,
        'full_topic_coverage': bool(selected) and not uncovered,
        'outcome': 'no_match' if not selected else 'partial' if uncovered else 'complete'}
    return result


def sync_all(config, *, source=None, curator=None, ref=None, progress=None, topic=None, topics=None):
    """Curate at most five pages per call; publish one stable note per source page.

    A failed batch stops further paid work. Repeating the command only processes
    pages not already published at this exact commit. Removed pages are reported,
    never erased automatically.
    """
    try:
        config = _config(config)
        selected = _selected_topic(config, topic, topics)
        if source is None or curator is None:
            from collector import GitHubSource, VertexCurator
            source = source if source is not None else GitHubSource()
            curator = curator if curator is not None else VertexCurator(config.project, config.location, config.model)
        with _lock(config.state) as directory:
            from lifecycle import recover_source_lifecycle, legacy_blocked, purge_owned_index
            from workvault import _publication_lock
            recover_source_lifecycle(config)
            recover_publication_batch(config.vault, config.snapshots)
            previous = _load_state(directory, config.repository)
            if previous is not None and (_status(config, previous)['uncertain_paths']
                    or (topic is not None and previous['uncertain_paths'])):
                raise SyncError('uncertain provider work requires explicit inspection')
            snapshot = source.snapshot(config.repository, ref=ref)
            if selected is not None:
                if (snapshot.repository != config.repository or any(not set(paths) <= set(snapshot.discovered_paths)
                        for _, paths in topics.topics)):
                    raise SyncError('topic mapping differs from pinned inventory')
                if not selected:
                    return _topic_status(config, previous, topic, selected, snapshot.commit)
            state = _state({'schema_version': SCHEMA, 'repository': snapshot.repository,
                'target_commit': snapshot.commit,
                'discovered_paths': sorted(snapshot.discovered_paths),
                'indexed_paths': sorted(snapshot.indexed_paths),
                'tracked_paths': sorted(set(snapshot.discovered_paths) |
                    set(previous['tracked_paths'] if previous else [])),
                'failed_paths': sorted(set(previous['failed_paths']) & set(snapshot.discovered_paths))
                    if selected is not None and previous is not None else [],
                'uncertain_paths': [], 'provider_evidence': previous['provider_evidence']
                    if selected is not None and previous is not None else {'outcome': 'not_started'},
                'broken_index_references': list(snapshot.broken_index_references)}, config.repository)
            with _publication_lock(config.vault, create=True) as publication_directory:
                if legacy_blocked(publication_directory, config.repository):
                    raise SyncError('repository withdrawal fenced')
                if previous is None or previous['discovered_paths'] != state['discovered_paths']:
                    purge_owned_index(publication_directory)
                _save_state(directory, state)
            pending = _status(config, state)['uncovered_paths']
            if selected is not None:
                pending = [path for path in pending if path in selected]
            for start in range(0, len(pending), 5):
                paths = pending[start:start + 5]
                dispatched = False
                publication_started = False
                try:
                    pages = source.read_pages(snapshot, paths)
                    state['uncertain_paths'] = paths
                    state['provider_evidence'] = {'outcome': 'uncertain'}
                    _save_state(directory, state)
                    dispatched = True
                    clean = validate_curated_batch(curator.curate(snapshot, pages))
                    if (clean['repository'] != config.repository or clean['commit'] != snapshot.commit
                            or set(clean['requested_paths']) != set(paths)
                            or set(clean['discovered_paths']) != set(state['discovered_paths'])
                            or set(clean['indexed_paths']) != set(state['indexed_paths'])):
                        raise SyncError('collector coverage differs')
                    state['provider_evidence'] = _evidence(getattr(curator, 'last_evidence', {'outcome': 'settled'}))
                    _save_state(directory, state)
                    publication_started = True
                    with publication_batch(config.vault, config.snapshots, clean):
                        for card in clean['cards']:
                            name = publication_name(config.repository, card['path'])
                            single = dict(clean, requested_paths=[card['path']], cards=[card])
                            publish_curated_batch(config.vault, config.snapshots / name[:-3],
                                                  single, publication=name)
                    state['uncertain_paths'] = []
                    if selected is not None:
                        state['failed_paths'] = sorted(set(state['failed_paths']) - set(paths))
                    _save_state(directory, state)
                    if progress is not None:
                        progress({'published_count': len(paths), 'remaining_count': len(pending) - start - len(paths)})
                except Exception as error:
                    if dispatched:
                        state['provider_evidence'] = _evidence(getattr(curator, 'last_evidence', {'outcome': 'uncertain'}))
                        if (state['provider_evidence']['outcome'] != 'uncertain'
                                and (not publication_started or isinstance(error, BatchPublicationRejected))
                                and not publication_pending(config.vault)):
                            state['uncertain_paths'] = []
                    state['failed_paths'] = sorted(set(state['failed_paths']) | set(paths)) if selected is not None else paths
                    _save_state(directory, state)
                    break
            return _status(config, state) if selected is None else _topic_status(config, state, topic, selected, snapshot.commit)
    except Exception:
        raise SyncError('sync refused; existing notes retained; inspect status') from None


SOURCE_STATE = 'multi-source-state.json'


@dataclass(frozen=True)
class SourceSyncConfig:
    vault: Path
    snapshots: Path
    state: Path


@dataclass(frozen=True)
class SourceSelection:
    """Caller-supplied authority; the coordinator never discovers accounts/routes."""
    source_id: str
    kind: str
    account: str
    container: str
    adapter: object
    route_sha256: str | None = None
    source_sha256: str | None = None


def _source_config(config):
    if type(config) is not SourceSyncConfig:
        raise SyncError('invalid source sync configuration')
    paths = [_root_path(path) for path in (config.vault, config.snapshots, config.state)]
    for index, first in enumerate(paths):
        for second in paths[index + 1:]:
            if first == second or first.is_relative_to(second) or second.is_relative_to(first):
                raise SyncError('overlapping source sync roots')
    return SourceSyncConfig(*paths)


def _source_binding(selection):
    import curation
    import sources
    if type(selection) is not SourceSelection or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', selection.source_id):
        raise SyncError('invalid selected source')
    if selection.kind not in sources._KINDS:
        raise SyncError('invalid selected source kind')
    binding = {'kind': selection.kind}
    for key in ('account', 'container'):
        value = curation._text(getattr(selection, key), 512)
        if any(ord(char) < 32 for char in value):
            raise SyncError('invalid selected source binding')
        binding[key] = value
    for key in ('route_sha256', 'source_sha256'):
        value = getattr(selection, key)
        if value is not None and (type(value) is not str or not re.fullmatch(r'[0-9a-f]{64}', value)):
            raise SyncError('invalid supplied source evidence')
        binding[key] = value
    return binding


def _bound_reference(value, binding, *, fetched=False):
    import sources
    clean = sources.validate_reference(value) if fetched else sources.validate_locator(value)
    if any(clean[key] != binding[key] for key in ('kind', 'account', 'container')):
        raise SyncError('selected source identity drift')
    return clean


def _source_state(value, bindings, cutoff):
    import curation
    import sources
    curation._object(value, {'schema_version', 'sources'})
    curation._literal(value['schema_version'], 'workvault-multi-source-state-v2')
    if (type(value['sources']) is not dict or not value['sources']
            or not set(value['sources']).issubset(bindings)):
        raise SyncError('selected source inventory changed')
    for source_id, row in value['sources'].items():
        curation._object(row, {'binding', 'cutoff', 'phase', 'cursor', 'change_cursor', 'pending',
                              'inflight', 'failed', 'page_count', 'gaps'})
        if row['binding'] != bindings[source_id] or row['cutoff'] != cutoff:
            raise SyncError('frozen selected source changed')
        if row['phase'] not in ('backfill', 'changes') or type(row['failed']) is not bool:
            raise SyncError('invalid source progress')
        for key in ('cursor', 'change_cursor'):
            if row[key] is not None:
                curation._text(row[key], 2048)
                if any(ord(char) < 32 for char in row[key]):
                    raise SyncError('invalid source cursor')
        if type(row['page_count']) is not int or row['page_count'] < 0:
            raise SyncError('invalid source page count')
        if type(row['gaps']) is not list:
            raise SyncError('invalid cumulative source gaps')
        for gap in row['gaps']:
            sources.validate_page({'items': [], 'next_cursor': None, 'complete': True, 'gaps': [gap]})
        pending = row['pending']
        if pending is not None:
            curation._object(pending, {'page', 'checkpoint', 'offset'})
            change = sources.validate_changes({'page': pending['page'], 'checkpoint': pending['checkpoint']})
            for ref in change['page']['items']:
                _bound_reference(ref, row['binding'])
            if type(pending['offset']) is not int or not 0 <= pending['offset'] <= len(change['page']['items']):
                raise SyncError('invalid pending source coverage')
        inflight = row['inflight']
        if inflight is not None:
            curation._object(inflight, {'references', 'end'} | ({'relationships'} if 'relationships' in inflight else set()))
            if pending is None or type(inflight['end']) is not int or not pending['offset'] < inflight['end'] <= len(pending['page']['items']):
                raise SyncError('invalid inflight coverage')
            refs = sources.validate_references(inflight['references'], maximum=5)
            if not refs or inflight['end'] - pending['offset'] != len(refs):
                raise SyncError('invalid inflight batch')
            for ref, listed in zip(refs, pending['page']['items'][pending['offset']:inflight['end']]):
                _bound_reference(ref, row['binding'], fetched=True)
                if not _matches_listing(ref, listed, observed=False):
                    raise SyncError('inflight source differs from listing')
            if 'relationships' in inflight:
                relationships = inflight['relationships']
                if (type(relationships) is not list or len(relationships) != len(refs)
                        or not any(item is not None for item in relationships)):
                    raise SyncError('invalid inflight relationships')
                for item in relationships:
                    if item is not None:
                        curation._object(item, {'supersedes', 'dependencies'})
                        _source_relationships(item)
    return value


def _matches_listing(reference, locator, *, observed=True):
    import curation
    import sources
    return (sources.source_key(reference) == sources.source_key(locator)
            and reference['citation'] == locator['citation']
            and (not observed or curation._source_instant(reference['observed_at']) >= curation._source_instant(locator['observed_at']))
            and all(locator[key] is None or reference[key] == locator[key]
                    for key in ('revision', 'author', 'event_at', 'modified_at')))


def _committed_source(config, locator, *, card=False):
    from batch import source_publication_name
    # Publication naming depends on identity, never on an invented listing hash.
    import sources
    name = 'Knowledge-' + hashlib.sha256(sources.source_key(locator).encode()).hexdigest()[:24] + '.md'
    with _read_publications(config.vault):
        try:
            with _directory(config.vault) as directory:
                clean = parse_publication(_read(directory, name))['batch']
        except FileNotFoundError:
            return None
        if clean['schema_version'] != 'workvault-curated-batch-v2' or len(clean['cards']) != 1:
            raise SyncError('source publication differs')
        ref = sources.validate_reference(clean['cards'][0]['source'])
        if source_publication_name(ref) != name:
            raise SyncError('source publication identity differs')
        if not _matches_listing(ref, locator, observed=False):
            return None
        return clean['cards'][0] if card else ref


def _same_source_content(first, second):
    return first is not None and all(first[key] == value for key, value in second.items() if key != 'observed_at')


def _source_relationships(record):
    import sources
    return {key: sources.validate_references(record[key]) for key in ('supersedes', 'dependencies')}


def _same_source_relationships(first, second):
    def evidence(record, key):
        return sorted(json.dumps({name: value for name, value in ref.items() if name != 'observed_at'},
                                 sort_keys=True) for ref in record[key])
    return all(evidence(first, key) == evidence(second, key) for key in ('supersedes', 'dependencies'))


def _require_active_source(config, references):
    from lifecycle import blocked
    from workvault import _publication_lock
    try:
        with _publication_lock(config.vault, shared=True) as directory:
            if any(blocked(directory, ref) for ref in references):
                raise SyncError('source lifecycle prevents dispatch')
    except FileNotFoundError:
        pass


def _completion_prefix(source_id):
    return 'source-completed-' + hashlib.sha256(source_id.encode()).hexdigest() + '-'


def _record_completed(directory, source_id, reference):
    import sources
    reference = sources.validate_reference(reference)
    name = _completion_prefix(source_id) + hashlib.sha256(sources.revision_key(reference).encode()).hexdigest() + '.json'
    try:
        previous = sources.validate_reference(_json(_read(directory, name)))
        if sources.revision_key(previous) != sources.revision_key(reference):
            raise SyncError('completed source changed')
    except FileNotFoundError:
        _save_state(directory, reference, name)


def _completed_sources(directory, source_id, binding):
    """Acknowledged revision/hash evidence outlives the seven content snapshots.

    These private checkpoints prove prior publication, not retained/retrievable
    content. Reuse never restores an old card or implies withdrawal handling.
    """
    import sources
    prefix = _completion_prefix(source_id)
    completed, revisions = {}, {}
    for name in os.listdir(directory):
        if not name.startswith(prefix):
            continue
        ref = _bound_reference(_json(_read(directory, name)), binding, fetched=True)
        key = sources.revision_key(ref)
        if name != prefix + hashlib.sha256(key.encode()).hexdigest() + '.json':
            raise SyncError('invalid completed revision metadata')
        if ref['revision'] is not None:
            revision = (sources.source_key(ref), ref['revision'])
            if revision in revisions and revisions[revision] != ref['content_hash']:
                raise SyncError('conflicting completed source revision')
            revisions[revision] = ref['content_hash']
        completed[key] = ref
    return completed


def _completed_source(completed, locator):
    return next((ref for ref in completed.values()
                 if _matches_listing(ref, locator, observed=False)), None)


def _fetched_source(record, locator, binding):
    import curation
    import sources
    from collector import MAX_DOCUMENT
    curation._object(record, {'reference', 'body', 'draft', 'supersedes', 'dependencies'})
    ref = _bound_reference(record['reference'], binding, fetched=True)
    body = record['body']
    if (type(body) is not bytes or len(body) > MAX_DOCUMENT
            or 'sha256:' + hashlib.sha256(body).hexdigest() != ref['content_hash']
            or type(record['draft']) is not bool or not _matches_listing(ref, locator)):
        raise SyncError('fetched source refused')
    body.decode('utf-8')
    sources.validate_references(record['supersedes'])
    sources.validate_references(record['dependencies'])
    return ref


def _ack_source_batch(directory, state, source_id, references, end):
    row = state['sources'][source_id]
    for reference in references:
        _record_completed(directory, source_id, reference)
    row['pending']['offset'] = end
    row['inflight'] = None
    row['failed'] = False
    _save_state(directory, state, SOURCE_STATE)


def _source_gaps(*pages):
    counts = {}
    for page in pages:
        for gap in page:
            code, count = gap['code'], gap['count']
            if code not in counts:
                counts[code] = count
            elif counts[code] is None or count is None:
                counts[code] = None
            else:
                counts[code] += count
    return [{'code': code, 'count': count} for code, count in counts.items()]


def _repeated_change_page(row, pending):
    return (row['phase'] == 'changes' and pending['page']['complete']
            and pending['checkpoint'] is not None and pending['checkpoint'] == row['change_cursor'])


def _source_report(directory, state, page_budget):
    report = {}
    for source_id, row in state['sources'].items():
        completed = _completed_sources(directory, source_id, row['binding'])
        gaps = _source_gaps(row['gaps'])
        if row['pending'] and not _repeated_change_page(row, row['pending']):
            gaps = _source_gaps(gaps, row['pending']['page']['gaps'])
        report[source_id] = {'state': 'uncertain' if row['inflight'] else 'failed' if row['failed'] else 'ready',
            'phase': row['phase'], 'covered_count': len(completed),
            'page_count': row['page_count'], 'cursor': row['cursor'], 'change_cursor': row['change_cursor'],
            'pending_count': len(row['pending']['page']['items']) - row['pending']['offset'] if row['pending'] else 0,
            'gaps': gaps, 'cutoff': row['cutoff'], 'binding': row['binding'],
            'history_reuse': 'acknowledged_revision_hash_checkpoint',
            'provenance': 'supplied_digests' if row['binding']['route_sha256'] is not None
                and row['binding']['source_sha256'] is not None else 'not_certified'}
    return {'schema_version': 'workvault-multi-source-status-v2', 'sources': report,
            'page_budget_per_source': page_budget, 'full_source_coverage': False}


def _sync_source(config, directory, state, selection, curator, page_budget):
    import sources
    from collector import assemble_source_batch
    from curation import validate_source_batch
    from batch import source_publication_name
    source_id = selection.source_id
    row = state['sources'][source_id]
    completed = _completed_sources(directory, source_id, row['binding'])
    if row['inflight']:
        refs = row['inflight']['references']
        _require_active_source(config, refs)
        committed = []
        for index, ref in enumerate(refs):
            relationships = row['inflight'].get('relationships')
            if relationships is not None and relationships[index] is not None:
                card = _committed_source(config, ref, card=True)
                committed.append(ref if card is not None and card['source'] == ref
                                 and _same_source_relationships(card, relationships[index]) else None)
                continue
            checkpoint = completed.get(sources.revision_key(ref))
            committed.append(checkpoint if checkpoint == ref else _committed_source(config, ref))
        if committed != refs:
            return
        _ack_source_batch(directory, state, source_id, refs, row['inflight']['end'])
        completed.update((sources.revision_key(ref), ref) for ref in refs)
    for _ in range(page_budget):
        if row['pending'] is None:
            if row['phase'] == 'backfill':
                page = sources.validate_page(selection.adapter.list_page(cursor=row['cursor'], cutoff=row['cutoff']))
                checkpoint = None
            else:
                change = sources.validate_changes(selection.adapter.changes(cursor=row['cursor'] or row['change_cursor']))
                page, checkpoint = change['page'], change['checkpoint']
            for ref in page['items']:
                _bound_reference(ref, row['binding'])
            if not page['complete'] and page['next_cursor'] == row['cursor']:
                raise SyncError('nonadvancing source page')
            row['pending'] = {'page': page, 'checkpoint': checkpoint, 'offset': 0}
            row['failed'] = False
            _save_state(directory, state, SOURCE_STATE)
        pending = row['pending']
        items = pending['page']['items']
        _require_active_source(config, items[pending['offset']:])
        while pending['offset'] < len(items):
            start = pending['offset']
            selected, records, refs, relationships, keys = [], [], [], [], set()
            for listed in items[start:start + 5]:
                key = sources.source_key(listed)
                if key in keys:
                    break
                keys.add(key)
                card = _committed_source(config, listed, card=True)
                committed = (_completed_source(completed, listed) if listed['revision'] is not None else None)
                # Historical checkpoints avoid replaying paid work; they do not
                # verify or refresh relationships of a no-longer-current card.
                if card is None and committed is not None:
                    refs.append(committed)
                    relationships.append(None)
                    continue
                record = selection.adapter.fetch(listed)
                ref = _fetched_source(record, listed, row['binding'])
                # Observation freshness validates the fetch, not cached content.
                if (card is not None and _same_source_content(card['source'], ref)
                        and _same_source_relationships(card, record)):
                    refs.append(card['source'])
                    relationships.append(None)
                    continue
                refs.append(ref)
                relationships.append(_source_relationships(record))
                selected.append(listed)
                records.append(record)
            if not records:
                _ack_source_batch(directory, state, source_id, refs, start + len(refs))
                completed.update((sources.revision_key(ref), ref) for ref in refs)
                continue
            expected = {sources.source_key(ref): ref for ref in refs
                        if any(sources.source_key(listed) == sources.source_key(ref) for listed in selected)}
            from lifecycle import guard
            from workvault import _publication_lock, _ACTIVE_DISPATCH
            # Fetch runs outside the lock. The final collector dependency check
            # and actual dispatch share it with lifecycle, before inflight exists.
            with _publication_lock(config.vault, create=True) as dispatch_directory:
                guard(dispatch_directory, {'schema_version': 'workvault-curated-batch-v2', 'cards': [
                    {'source': record['reference'], 'dependencies': record['dependencies']} for record in records]})
                row['inflight'] = {'references': refs, 'end': start + len(refs), 'relationships': relationships}
                _save_state(directory, state, SOURCE_STATE)
                token = _ACTIVE_DISPATCH.set(True)
                try:
                    curated = curator.curate_sources(selected, records)
                finally:
                    _ACTIVE_DISPATCH.reset(token)
            clean = validate_source_batch(assemble_source_batch(selected, records, curated))
            if {sources.source_key(card['source']): card['source'] for card in clean['cards']} != expected:
                raise SyncError('curation changed fetched provenance')
            intended = {sources.source_key(ref): relation for ref, relation in zip(refs, relationships)
                        if relation is not None}
            if any(not _same_source_relationships(card, intended[sources.source_key(card['source'])])
                   for card in clean['cards']):
                raise SyncError('curation changed fetched relationships')
            with publication_batch(config.vault, config.snapshots, clean):
                for card in clean['cards']:
                    name = source_publication_name(card['source'])
                    publish_curated_batch(config.vault, config.snapshots / name[:-3],
                        {'schema_version': clean['schema_version'], 'cards': [card]}, publication=name)
            _ack_source_batch(directory, state, source_id, refs, start + len(refs))
            completed.update((sources.revision_key(ref), ref) for ref in refs)
        page = pending['page']
        row['page_count'] += 1
        if not _repeated_change_page(row, pending):
            row['gaps'] = _source_gaps(row['gaps'], page['gaps'])
        row['cursor'] = page['next_cursor']
        if page['complete']:
            if row['phase'] == 'changes':
                row['change_cursor'] = pending['checkpoint'] or row['change_cursor']
            row['phase'] = 'changes'
        row['pending'] = None
        row['failed'] = False
        _save_state(directory, state, SOURCE_STATE)
        if page['complete']:
            break


def sync_sources(config, *, selections, cutoff, curator, page_budget=1):
    """Internal resumable seam; supplied adapters/curator grant no new authority.

    Each call handles at most page_budget pages per selected source, paying for at
    most five distinct items per batch. Repeat calls exhaust history without a
    historical page cap. Ambiguous dispatch requires external reconciliation.
    """
    import curation
    try:
        config = _source_config(config)
        curation._source_timestamp(cutoff)
        if type(selections) is not tuple or not selections or type(page_budget) is not int or page_budget < 1:
            raise SyncError('invalid source sync bounds')
        bindings = {selection.source_id: _source_binding(selection) for selection in selections}
        if len(bindings) != len(selections):
            raise SyncError('duplicate selected source')
        with _lock(config.state) as directory:
            try:
                state = _source_state(_json(_read(directory, SOURCE_STATE)), bindings, cutoff)
            except FileNotFoundError:
                state = {'schema_version': 'workvault-multi-source-state-v2', 'sources': {}}
            additions = {source_id: {'binding': binding, 'cutoff': cutoff, 'phase': 'backfill', 'cursor': None,
                'change_cursor': None, 'pending': None, 'inflight': None, 'failed': False,
                'page_count': 0, 'gaps': []} for source_id, binding in bindings.items()
                if source_id not in state['sources']}
            if additions:
                # Validate retained revision evidence before admitting any new authority.
                for source_id, row in state['sources'].items():
                    _completed_sources(directory, source_id, row['binding'])
                state['sources'].update(additions)
                _save_state(directory, state, SOURCE_STATE)
            from lifecycle import recover_source_lifecycle
            recover_source_lifecycle(config)
            recover_publication_batch(config.vault, config.snapshots)
            for selection in selections:
                try:
                    _sync_source(config, directory, state, selection, curator, page_budget)
                except Exception:
                    state['sources'][selection.source_id]['failed'] = True
                    _save_state(directory, state, SOURCE_STATE)
            return _source_report(directory, state, page_budget)
    except Exception:
        raise SyncError('source sync refused; inspect safe source status') from None


def load_config(path):
    """Read one owner-only config; credential values are never part of its schema."""
    try:
        path = _root_path(path)
        with _directory(path.parent) as directory:
            value = _json(_read(directory, path.name))
        if type(value) is not dict or set(value) != {'schema_version', 'repository', 'vault', 'snapshots', 'state', 'vertex'}:
            raise SyncError('invalid configuration fields')
        vertex = value['vertex']
        if (value['schema_version'] != 'workvault-sync-config-v1' or type(vertex) is not dict
                or set(vertex) != {'project', 'location', 'model'}):
            raise SyncError('invalid configuration schema')
        paths = []
        for name in ('vault', 'snapshots', 'state'):
            if type(value[name]) is not str or not Path(value[name]).is_absolute():
                raise SyncError('absolute roots required')
            paths.append(Path(value[name]))
        return _config(SyncConfig(value['repository'], *paths, vertex['project'], vertex['location'], vertex['model']))
    except Exception:
        raise SyncError('configuration refused') from None


@contextmanager
def _curated_reader(config, *, publication_lease=None):
    """Read the complete canonical collection under the existing storage fences.

    Human notes, snapshots and registered artifacts are not publications. The
    observed v1 inventory and explicit v2 lifecycle determine active content;
    publication revisions are retained rather than replaced with a target pin.
    """
    import curation
    from batch import _record, source_publication_name
    from lifecycle import blocked, legacy_blocked, guard, STATE as LIFECYCLE_STATE
    from retrieval import CuratedReader, MAX_PUBLICATIONS, MAX_COLLECTION_BYTES
    from workvault import _publication_lock, LOCK

    config = _config(config)
    if publication_lease is not None:
        from workvault import _verify_publication_lease
        _verify_publication_lease(config.vault, publication_lease)
    with ExitStack() as stack:
        try:
            state_directory = stack.enter_context(_directory(config.state))
        except FileNotFoundError:
            state_directory = None
        raw_states = {}
        for name in (STATE, SOURCE_STATE):
            try:
                raw_states[name] = _read(state_directory, name) if state_directory is not None else None
            except FileNotFoundError:
                raw_states[name] = None
        legacy = _state(_json(raw_states[STATE]), config.repository) if raw_states[STATE] is not None else None
        native = _json(raw_states[SOURCE_STATE]) if raw_states[SOURCE_STATE] is not None else None
        bindings = {}
        if native is not None:
            curation._object(native, {'schema_version', 'sources'})
            curation._literal(native['schema_version'], 'workvault-multi-source-state-v2')
            if type(native['sources']) is not dict or len(native['sources']) > MAX_PUBLICATIONS:
                raise SyncError('invalid source inventory')
            for source_id, row in native['sources'].items():
                binding = row['binding']
                curation._object(binding, {'kind', 'account', 'container', 'route_sha256', 'source_sha256'})
                selected = SourceSelection(source_id, binding['kind'], binding['account'],
                    binding['container'], None, binding['route_sha256'], binding['source_sha256'])
                bindings[source_id] = _source_binding(selected)
                curation._source_timestamp(row['cutoff'])
                _source_state({'schema_version': native['schema_version'], 'sources': {source_id: row}},
                    {source_id: bindings[source_id]}, row['cutoff'])
        try:
            config.vault.lstat()
        except FileNotFoundError:
            directory = None
        else:
            directory = (publication_lease[0] if publication_lease is not None else
                         stack.enter_context(_publication_lock(config.vault, shared=True)))
        publications, records = [], {}
        fences = {}
        if directory is not None:
            for name in (LOCK, LIFECYCLE_STATE):
                try:
                    fences[name] = _record(directory, name)[1]
                except FileNotFoundError:
                    fences[name] = None
            guard(directory)
        collection_bytes = 0
        retired_count = 0
        if directory is not None and 'Knowledge.md' in os.listdir(directory):
            raise SyncError('aggregate publication requires reviewed migration')
        names = [] if directory is None else sorted(name for name in os.listdir(directory)
            if re.fullmatch(r'Knowledge-[0-9a-f]{24}\.md', name))
        if len(names) > MAX_PUBLICATIONS:
            raise SyncError('curated collection limit exceeded')
        for name in names:
            raw, info = _record(directory, name)
            checkpoint = parse_publication(raw)
            clean = checkpoint['batch']
            if len(clean['cards']) != 1:
                raise SyncError('publication is not identity qualified')
            card = clean['cards'][0]
            records[name] = info
            if clean['schema_version'] == curation.SOURCE_BATCH_SCHEMA:
                ref = card['source']
                if source_publication_name(ref) != name or not any(all(ref[key] == binding[key]
                        for key in ('kind', 'account', 'container')) for binding in bindings.values()):
                    raise SyncError('publication differs from selected source')
                if any(blocked(directory, item) for item in [ref, *card['dependencies']]):
                    retired_count += 1
                    continue
                guard(directory, clean)
            else:
                path = card['path']
                if (clean['repository'] != config.repository or clean['requested_paths'] != [path]
                        or publication_name(config.repository, path) != name
                        or legacy is None or path not in legacy['tracked_paths']):
                    raise SyncError('publication differs from observed inventory')
                if legacy_blocked(directory, config.repository) or path not in legacy['discovered_paths']:
                    retired_count += 1
                    continue
            collection_bytes += len(curation._json(checkpoint).encode('utf-8'))
            if collection_bytes > MAX_COLLECTION_BYTES:
                raise SyncError('curated collection limit exceeded')
            publications.append(checkpoint)
        reader = CuratedReader(publications)
        report = {'active_card_count': len(publications), 'retained_retired_publication_count': retired_count,
            'uncertain_card_count': sum(bool(item['batch']['cards'][0]['uncertainty']) for item in publications),
            'draft_card_count': sum(item['batch']['cards'][0]['draft'] for item in publications),
            'conflict_check': 'unavailable_unstructured_cards', 'full_source_coverage': False}
        if native is not None:
            report['sources'] = _source_report(state_directory, native, 0)['sources']
        yield reader, report
        if directory is not None:
            if 'Knowledge.md' in os.listdir(directory):
                raise SyncError('aggregate publication requires reviewed migration')
            for name, expected in records.items():
                if _record(directory, name)[1] != expected:
                    raise SyncError('publication changed during read')
            if sorted(name for name in os.listdir(directory)
                    if re.fullmatch(r'Knowledge-[0-9a-f]{24}\.md', name)) != names:
                raise SyncError('publication inventory changed during read')
        for name, expected in raw_states.items():
            try:
                current = _read(state_directory, name) if state_directory is not None else None
            except FileNotFoundError:
                current = None
            if current != expected:
                raise SyncError('source inventory changed during read')
        for root, descriptor in ((config.state, state_directory), (config.vault, directory)):
            try:
                with _directory(root) as current:
                    identity = _identity(os.fstat(current))
            except FileNotFoundError:
                identity = None
            if identity != (_identity(os.fstat(descriptor)) if descriptor is not None else None):
                raise SyncError('configured root changed during read')
        if directory is not None:
            if publication_pending(config.vault):
                raise SyncError('batch recovery required')
            for name, expected in fences.items():
                try:
                    current = _record(directory, name)[1]
                except FileNotFoundError:
                    current = None
                if current != expected:
                    raise SyncError('storage fence changed during read')
            guard(directory)


def _selection(reader, config, value, *, exact=False):
    """A reference is data, never a filename or a URL to fetch."""
    from retrieval import _reference
    from batch import source_publication_name
    if type(value) is not str or len(value.encode()) > MAX_BATCH_BYTES:
        raise SyncError('invalid curated selection')
    if value.startswith('{'):
        return _reference(_json(value))
    if exact:
        raise SyncError('exact source reference required')
    if re.fullmatch(r'Knowledge-[0-9a-f]{24}(?:\.md)?', value):
        name = value if value.endswith('.md') else value + '.md'
        matches = [row['reference'] for row in reader.rows()
            if ('source' not in row['card'] and publication_name(config.repository, row['card']['path']) == name)
            or ('source' in row['card'] and source_publication_name(row['reference']) == name)]
    else:
        path = _path(value)
        matches = [row['reference'] for row in reader.rows()
                   if row['reference'].get('repository') == config.repository and row['reference'].get('path') == path]
    if len(matches) > 1:
        raise SyncError('ambiguous curated selection')
    return matches[0] if matches else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('command', choices=('sync', 'status', 'inventory', 'maintain', 'topic', 'search', 'get', 'follow',
                                         'hybrid-search', 'hybrid-reindex', 'hybrid-serve'))
    parser.add_argument('topic', nargs='?')
    parser.add_argument('--config', required=True)
    parser.add_argument('--ref')
    parser.add_argument('--topic-map')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--origin')
    parser.add_argument('--hybrid-config')
    args = parser.parse_args(argv)
    try:
        if args.command in ('sync', 'topic'):
            if (args.topic is None) != (args.topic_map is None) or (args.command == 'topic' and args.topic is None):
                raise SyncError('topic and map are required together')
        elif args.topic_map is not None or (args.command not in ('search', 'get', 'follow', 'hybrid-search') and args.topic is not None):
            raise SyncError('unexpected selection')
        if ((args.limit is not None and args.command not in ('search', 'hybrid-search'))
                or (args.origin is not None and args.command != 'follow')):
            raise SyncError('unexpected retrieval option')
        if args.command in ('topic', 'search', 'get', 'follow', 'hybrid-search', 'hybrid-reindex', 'hybrid-serve') and args.ref is not None:
            raise SyncError('curated consumption does not fetch a ref')
        hybrid_action = args.command.startswith('hybrid-')
        if hybrid_action != (args.hybrid_config is not None):
            raise SyncError('hybrid binding is required only for hybrid actions')
        config = load_config(args.config)
        topics = None
        if args.topic_map is not None:
            path = _root_path(args.topic_map)
            with _directory(path.parent) as directory:
                topics = parse_topics(_read(directory, path.name))
        if hybrid_action:
            import hybrid
            binding = hybrid.load_binding(Path(args.hybrid_config))
            if args.command == 'hybrid-search':
                result = hybrid.search(config, binding, args.topic,
                                       {'limit': 5 if args.limit is None else args.limit})
            elif args.command == 'hybrid-reindex':
                result = hybrid.reindex(config, binding)
            else:
                return hybrid.serve(config, binding, sys.stdin, sys.stdout)
        elif args.command in ('topic', 'search', 'get', 'follow'):
            with _curated_reader(config) as (reader, _):
                if args.command == 'search':
                    result = reader.search(args.topic, limit=5 if args.limit is None else args.limit)
                elif args.command == 'topic':
                    selected = _selected_topic(config, args.topic, topics)
                    result = [row for row in reader.rows() if row['reference'].get('repository') == config.repository
                              and row['reference'].get('path') in selected]
                elif args.command == 'get':
                    reference = _selection(reader, config, args.topic)
                    result = None if reference is None else reader.get(reference)
                else:
                    result = reader.follow(_selection(reader, config, args.origin, exact=True),
                                           _selection(reader, config, args.topic, exact=True))
        elif args.command in ('status', 'maintain'):
            if args.ref is not None:
                raise SyncError('offline report does not fetch a ref')
            result = sync_status(config) if args.command == 'status' else maintain_status(config)
            if args.command == 'status':
                with _curated_reader(config) as (_, report):
                    if report['active_card_count'] or report['retained_retired_publication_count'] or 'sources' in report:
                        result['curated'] = report
                        if result['state'] == 'empty':
                            result['state'] = 'partial'
                            result['full_source_coverage'] = False
        elif args.command == 'inventory':
            from collector import GitHubSource
            snapshot = GitHubSource().snapshot(config.repository, ref=args.ref)
            result = {'repository': snapshot.repository, 'commit': snapshot.commit,
                'discovered_paths': list(snapshot.discovered_paths), 'indexed_paths': list(snapshot.indexed_paths),
                'broken_index_references': list(snapshot.broken_index_references)}
        else:
            result = sync_all(config, ref=args.ref,
                progress=lambda p: print(json.dumps(p), file=sys.stderr, flush=True),
                topic=args.topic, topics=topics)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if args.topic is not None and args.command == 'sync':
            return 0 if result['topic_selection']['full_topic_coverage'] else 1
        return 1 if type(result) is dict and result.get('full_source_coverage') is False else 0
    except Exception:
        print('WorkVault operation refused; inspect configuration or last published status.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    # Lifecycle recovery imports the canonical configuration classes from sync.
    from sync import main as canonical_main
    sys.exit(canonical_main())
