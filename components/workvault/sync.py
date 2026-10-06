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


def _save_state(directory, state):
    try:
        old = _read(directory, STATE)
    except FileNotFoundError:
        old = None
    data = (json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode()
    staged = '.sync-stage-' + uuid.uuid4().hex
    identity = _write_private(directory, staged, data)
    try:
        try:
            current = _read(directory, STATE)
        except FileNotFoundError:
            current = None
        if current != old:
            raise SyncError('sync state changed')
        os.replace(staged, STATE, src_dir_fd=directory, dst_dir_fd=directory)
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
            return _maintain_status(config)
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('sync', 'status', 'inventory', 'maintain'))
    parser.add_argument('topic', nargs='?')
    parser.add_argument('--config', required=True)
    parser.add_argument('--ref')
    parser.add_argument('--topic-map')
    args = parser.parse_args(argv)
    try:
        if ((args.topic is not None or args.topic_map is not None) and args.command != 'sync'
                or (args.topic is None) != (args.topic_map is None)):
            raise SyncError('topic and map are required together for sync only')
        config = load_config(args.config)
        topics = None
        if args.topic_map is not None:
            path = _root_path(args.topic_map)
            with _directory(path.parent) as directory:
                topics = parse_topics(_read(directory, path.name))
        if args.command in ('status', 'maintain'):
            if args.ref is not None:
                raise SyncError('offline report does not fetch a ref')
            result = sync_status(config) if args.command == 'status' else maintain_status(config)
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
        if args.topic is not None:
            return 0 if result['topic_selection']['full_topic_coverage'] else 1
        return 1 if result.get('full_source_coverage') is False else 0
    except Exception:
        print('WorkVault operation refused; inspect configuration or last published status.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
