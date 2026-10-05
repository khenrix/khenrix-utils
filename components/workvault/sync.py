"""Full-tree inbound sync. Raw source and model payloads stay in this process."""
import argparse
from contextlib import contextmanager
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
                      MAX_INVENTORY_PATHS, validate_curated_batch, validate_repository)
from workvault import (_directory, _file, _identity, _private, _read, _root_path,
                       _unlink_owned, _write_private, publish_curated_batch, status)


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


def publication_name(repository, path):
    validate_repository(repository)
    _path(path)
    digest = hashlib.sha256((repository + '\n' + path).encode()).hexdigest()[:24]
    return 'Knowledge-' + digest + '.md'


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
        try:
            with _directory(config.state) as directory:
                state = _load_state(directory, config.repository)
        except FileNotFoundError:
            state = None
        return _status(config, state)
    except Exception:
        raise SyncError('sync status refused') from None


def sync_all(config, *, source=None, curator=None, ref=None, progress=None):
    """Curate at most five pages per call; publish one stable note per source page.

    A failed batch stops further paid work. Repeating the command only processes
    pages not already published at this exact commit. Removed pages are reported,
    never erased automatically.
    """
    try:
        config = _config(config)
        if source is None or curator is None:
            from collector import GitHubSource, VertexCurator
            source = source if source is not None else GitHubSource()
            curator = curator if curator is not None else VertexCurator(config.project, config.location, config.model)
        with _lock(config.state) as directory:
            previous = _load_state(directory, config.repository)
            if previous is not None and _status(config, previous)['uncertain_paths']:
                raise SyncError('uncertain provider work requires explicit inspection')
            snapshot = source.snapshot(config.repository, ref=ref)
            state = _state({'schema_version': SCHEMA, 'repository': snapshot.repository,
                'target_commit': snapshot.commit,
                'discovered_paths': sorted(snapshot.discovered_paths),
                'indexed_paths': sorted(snapshot.indexed_paths),
                'tracked_paths': sorted(set(snapshot.discovered_paths) |
                    set(previous['tracked_paths'] if previous else [])),
                'failed_paths': [], 'uncertain_paths': [], 'provider_evidence': {'outcome': 'not_started'},
                'broken_index_references': list(snapshot.broken_index_references)}, config.repository)
            _save_state(directory, state)
            pending = _status(config, state)['uncovered_paths']
            for start in range(0, len(pending), 5):
                paths = pending[start:start + 5]
                dispatched = False
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
                    state['uncertain_paths'] = []
                    state['provider_evidence'] = _evidence(getattr(curator, 'last_evidence', {'outcome': 'settled'}))
                    _save_state(directory, state)
                    with _directory(config.snapshots, create=True):
                        pass
                    for card in clean['cards']:
                        name = publication_name(config.repository, card['path'])
                        single = dict(clean, requested_paths=[card['path']], cards=[card])
                        publish_curated_batch(config.vault, config.snapshots / name[:-3],
                                              single, publication=name)
                    if progress is not None:
                        progress({'published_count': len(paths), 'remaining_count': len(pending) - start - len(paths)})
                except Exception:
                    if dispatched:
                        state['provider_evidence'] = _evidence(getattr(curator, 'last_evidence', {'outcome': 'uncertain'}))
                        if state['provider_evidence']['outcome'] != 'uncertain':
                            state['uncertain_paths'] = []
                    state['failed_paths'] = paths
                    _save_state(directory, state)
                    break
            return _status(config, state)
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
    parser.add_argument('command', choices=('sync', 'status', 'inventory'))
    parser.add_argument('--config', required=True)
    parser.add_argument('--ref')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == 'status':
            if args.ref is not None:
                raise SyncError('status does not fetch a ref')
            result = sync_status(config)
        elif args.command == 'inventory':
            from collector import GitHubSource
            snapshot = GitHubSource().snapshot(config.repository, ref=args.ref)
            result = {'repository': snapshot.repository, 'commit': snapshot.commit,
                'discovered_paths': list(snapshot.discovered_paths), 'indexed_paths': list(snapshot.indexed_paths),
                'broken_index_references': list(snapshot.broken_index_references)}
        else:
            result = sync_all(config, ref=args.ref,
                progress=lambda p: print(json.dumps(p), file=sys.stderr, flush=True))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1 if result.get('full_source_coverage') is False else 0
    except Exception:
        print('WorkVault operation refused; inspect configuration or last published status.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
