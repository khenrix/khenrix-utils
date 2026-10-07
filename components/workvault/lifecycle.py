"""Content-free lifecycle evidence under the existing private publication lock.

Only an explicit caller-confirmed collector event enters this API. Source read
failures and model output never call it. Native index/memory owners must register
their derived files and use the guarded read boundary before serving content.
"""
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager

import batch
import curation
import sources
import workvault as vault


STATE = '.workvault-lifecycle.json'
ARTIFACTS = '.workvault-artifacts.json'
MAX_STATE_BYTES = curation.MAX_PUBLICATION_BYTES
MAX_PENDING_FILES = 65536
MAX_ARTIFACTS = 8192
_PUBLICATION = re.compile(r'Knowledge-[0-9a-f]{24}\.md\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
INDEX = '.workvault-hybrid-build'
INDEX_DB = '.obsidian-hybrid-search.db'
INDEX_FILES = (INDEX_DB, INDEX_DB + '-wal', INDEX_DB + '-shm')
_RESERVATION = re.compile(r'\.workvault-hybrid-reserve-[0-9a-f]{32}\Z')
MAX_INDEX_BYTES = 512 * 1024 * 1024
MAX_PROJECTION_BYTES = 256 * 1024 * 1024


def _key(reference):
    return hashlib.sha256(sources.source_key(reference).encode()).hexdigest()


def _generation(reference):
    return hashlib.sha256(sources.revision_key(reference).encode()).hexdigest()


def _legacy_key(repository):
    curation.validate_repository(repository)
    return hashlib.sha256(curation._json(['github-v1-repository', repository]).encode('utf-8')).hexdigest()


def legacy_blocked(directory, repository):
    state = _load(directory)
    return state is not None and _legacy_key(repository) in state['blocked']


def _blocked(state, reference):
    return (_key(reference) in state['blocked']
            or reference['schema_version'] == sources.SOURCE_SCHEMA and _generation(reference) in state['generations'])


def _json(raw):
    return json.loads(raw, object_pairs_hook=curation._unique_object, parse_constant=curation._reject_constant)


def _load(directory):
    try:
        value = _json(vault._read(directory, STATE))
    except FileNotFoundError:
        return None
    return _validate_state(value, directory)


def _bounded(value):
    # Match the existing atomic writer's actual UTF-8 serialization envelope.
    size = len((json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode())
    batch._require(size <= MAX_STATE_BYTES)


def _validate_state(value, directory):
    curation._object(value, {'schema_version', 'vault_identity', 'snapshots_identity', 'blocked', 'generations', 'pending'})
    curation._literal(value['schema_version'], 'workvault-lifecycle-state-v2')
    for name in ('vault_identity', 'snapshots_identity'):
        batch._pair(value[name])
    batch._require(value['vault_identity'] == list(vault._identity(os.fstat(directory))))
    for field in ('blocked', 'generations'):
        batch._require(type(value[field]) is dict and len(value[field]) <= 8192)
        for key, action in value[field].items():
            batch._require(type(key) is str and _DIGEST.fullmatch(key) is not None and action in ('retired', 'withdrawn'))
    pending = value['pending']
    if pending is not None:
        curation._object(pending, {'files', 'artifacts'})
        batch._require(type(pending['files']) is list and len(pending['files']) <= MAX_PENDING_FILES
                       and type(pending['artifacts']) is list and len(pending['artifacts']) <= MAX_ARTIFACTS)
        paths = set()
        for row in pending['files']:
            curation._object(row, {'scope', 'directory', 'directory_identity', 'name', 'info'})
            batch._info(row['info'])
            batch._pair(row['directory_identity'])
            if row['scope'] == 'history':
                batch._require(type(row['directory']) is str and _PUBLICATION.fullmatch(row['directory'] + '.md') is not None
                               and type(row['name']) is str and vault._SNAPSHOT.fullmatch(row['name']) is not None)
            else:
                batch._require(row['scope'] == 'vault' and row['directory'] is None)
                _artifact_name(row['name'], publication=True)
            path = (row['scope'], row['directory'], row['name'])
            batch._require(path not in paths)
            paths.add(path)
        for name in pending['artifacts']:
            if name != INDEX:
                _artifact_name(name)
        batch._require(len(set(pending['artifacts'])) == len(pending['artifacts']))
    _bounded(value)
    return value


def _save(directory, value, name):
    from sync import _save_state
    if name == STATE:
        _validate_state(value, directory)
    else:
        batch._require(name == ARTIFACTS)
        _validate_artifacts(value)
    _save_state(directory, value, name)


def guard(directory, clean=None):
    """All canonical reads, publication and batch recovery pass this fence."""
    state = _load(directory)
    if state is not None:
        batch._require(state['pending'] is None)
    family = _artifacts(directory).get(INDEX)
    batch._require(family is None or family['phase'] == 'ready')
    if family is not None and clean is None:
        _check_index(directory, family)
    if state is not None and clean is not None and clean['schema_version'] == curation.SOURCE_BATCH_SCHEMA:
        for card in clean['cards']:
            batch._require(not any(_blocked(state, ref) for ref in [card['source'], *card['dependencies']]))
    elif state is not None and clean is not None:
        batch._require(_legacy_key(clean['repository']) not in state['blocked'])


def blocked(directory, reference):
    state = _load(directory)
    return state is not None and _blocked(state, reference)


def _artifact_name(name, *, publication=False):
    batch._require(type(name) is str and 1 <= len(name.encode()) <= 255 and name.strip() == name
                   and not name.startswith('.') and not any(char in name for char in '/\\')
                   and not any(ord(char) < 32 or ord(char) == 127 for char in name)
                   and (publication or not name.startswith('Knowledge-')))
    return name


def _artifacts(directory):
    try:
        value = _json(vault._read(directory, ARTIFACTS))
    except FileNotFoundError:
        return {}
    return _validate_artifacts(value)


def _validate_artifacts(value):
    batch._require(type(value) is dict and len(value) <= MAX_ARTIFACTS)
    for name, row in value.items():
        if name == INDEX:
            _validate_index(row)
            continue
        _artifact_name(name)
        curation._object(row, {'kind', 'dependencies', 'info'})
        batch._require(row['kind'] in ('index', 'memory', 'summary') and type(row['dependencies']) is list
                       and 1 <= len(row['dependencies']) <= curation.MAX_METADATA_ITEMS
                       and all(type(ref) is dict and set(ref) == {'item', 'generation'}
                               and all(type(key) is str and _DIGEST.fullmatch(key) is not None for key in ref.values())
                               for ref in row['dependencies']))
        batch._info(row['info'])
    _bounded(value)
    return value


def _index_member(value):
    curation._object(value, {'scope', 'name'})
    batch._require(value['scope'] in ('vault', 'build') and type(value['name']) is str)
    batch._require(value['name'] in INDEX_FILES or value['scope'] == 'build'
                   and _PUBLICATION.fullmatch(value['name']) is not None)
    return value['scope'], value['name']


def _owned_identity(value, *, directory=False):
    curation._object(value, {'identity', 'uid', 'mode'})
    batch._pair(value['identity'])
    batch._require(type(value['uid']) is int and value['uid'] == os.getuid()
                   and type(value['mode']) is int and value['mode'] == (0o700 if directory else 0o600))


def _ownership(info):
    return {'identity': list(vault._identity(info)), 'uid': info.st_uid, 'mode': stat.S_IMODE(info.st_mode)}


def _validate_index(row):
    curation._object(row, {'kind', 'generation', 'phase', 'root', 'build', 'reservation', 'projections', 'members'})
    batch._require(row['kind'] == 'owned-hybrid-index-v1' and type(row['generation']) is str
                   and _DIGEST.fullmatch(row['generation']) is not None
                   and row['phase'] in ('building', 'inflight', 'ready'))
    _owned_identity(row['root'], directory=True)
    if row['build'] is not None:
        _owned_identity(row['build'], directory=True)
    batch._require(row['reservation'] is None or type(row['reservation']) is str
                   and _RESERVATION.fullmatch(row['reservation']) is not None and row['build'] is not None)
    batch._require(type(row['projections']) is list and len(row['projections']) <= 8192
                   and all(type(name) is str and _PUBLICATION.fullmatch(name) is not None for name in row['projections'])
                   and row['projections'] == sorted(set(row['projections'])))
    batch._require(type(row['members']) is list and len(row['members']) <= 8198)
    paths, projection_bytes = set(), 0
    for member in row['members']:
        curation._object(member, {'scope', 'name', 'info'})
        path = _index_member({'scope': member['scope'], 'name': member['name']})
        batch._require(path not in paths)
        paths.add(path)
        info = member['info']
        curation._object(info, {'identity', 'uid', 'mode', 'bytes', 'sha256'})
        _owned_identity({key: info[key] for key in ('identity', 'uid', 'mode')})
        batch._require(type(info['bytes']) is int and 0 <= info['bytes'] <= MAX_INDEX_BYTES
                       and type(info['sha256']) is str and _DIGEST.fullmatch(info['sha256']) is not None)
        if member['name'] not in INDEX_FILES:
            batch._require(member['name'] in row['projections'])
            projection_bytes += info['bytes']
    batch._require(projection_bytes <= MAX_PROJECTION_BYTES)
    if row['phase'] == 'ready':
        batch._require(paths == {('vault', INDEX_DB)} and row['build'] is None
                       and row['reservation'] is None and row['projections'] == [])


@contextmanager
def _build_directory(directory, ownership, reservation=None, *, expose_name=False):
    name = INDEX
    if reservation is not None and reservation in os.listdir(directory):
        batch._require(INDEX not in os.listdir(directory))
        name = reservation
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        vault._private(os.fstat(descriptor), directory=True)
        batch._require(_ownership(os.fstat(descriptor)) == ownership)
        yield (descriptor, name) if expose_name else descriptor
    finally:
        os.close(descriptor)


def _index_record(directory, name, maximum=MAX_INDEX_BYTES):
    with vault._file(directory, name) as descriptor:
        before = os.fstat(descriptor)
        batch._require(before.st_size <= maximum)
        digest, size = hashlib.sha256(), 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            size += len(chunk)
            batch._require(size <= maximum)
            digest.update(chunk)
        after = os.fstat(descriptor)
        batch._require(all(getattr(before, key) == getattr(after, key) for key in
                           ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')))
        batch._require(_ownership(os.stat(name, dir_fd=directory, follow_symlinks=False)) == _ownership(after))
        return dict(_ownership(after), bytes=size, sha256=digest.hexdigest())


def _check_index(directory, row, *, mutable=False):
    _validate_index(row)
    batch._require(_ownership(os.fstat(directory)) == row['root'])
    records = {(item['scope'], item['name']): item['info'] for item in row['members']}
    measured = []
    for name in INDEX_FILES:
        try:
            info = _index_record(directory, name)
        except FileNotFoundError:
            if not mutable and ('vault', name) in records:
                batch._require(False)
            continue
        expected = records.get(('vault', name))
        if expected is None and mutable and row['phase'] in ('building', 'inflight'):
            # A crash after rename still leaves the measured staging inode
            # owned; this never adopts an unrelated root pathname.
            expected = records.get(('build', name))
        batch._require(expected is not None and info['identity'] == expected['identity']
                       and (mutable or info == expected))
        measured.append({'scope': 'vault', 'name': name, 'info': info})
    if row['build'] is None:
        batch._require(INDEX not in os.listdir(directory))
    else:
        try:
            with _build_directory(directory, row['build'], row['reservation']) as build:
                for name in sorted(os.listdir(build)):
                    batch._require(name in INDEX_FILES or name in row['projections'])
                    info = _index_record(build, name, MAX_INDEX_BYTES if name in INDEX_FILES else MAX_PROJECTION_BYTES)
                    expected = records.get(('build', name))
                    # Only the verified private staging directory may produce new members.
                    batch._require(expected is None and mutable or expected is not None
                                   and info['identity'] == expected['identity'] and (mutable or info == expected))
                    measured.append({'scope': 'build', 'name': name, 'info': info})
        except FileNotFoundError:
            batch._require(mutable)
    _validate_index(dict(row, members=measured))
    return measured


def _reserve_build(directory, artifacts, row):
    name = '.workvault-hybrid-reserve-' + uuid.uuid4().hex
    ownership = dict(row['root'], identity=[2 ** 64 - 1, 2 ** 64 - 1])
    prospective = dict(row, build=ownership, reservation=name)
    if row['phase'] == 'inflight':
        info = dict(ownership, mode=0o600, bytes=0, sha256=hashlib.sha256(b'').hexdigest())
        prospective['members'] = [*row['members'], *[
            {'scope': 'vault', 'name': sidecar, 'info': info} for sidecar in INDEX_FILES[1:]]]
    _validate_artifacts({**artifacts, INDEX: prospective})
    batch._require(INDEX not in os.listdir(directory))
    os.mkdir(name, mode=0o700, dir_fd=directory)
    os.fsync(directory)
    row['build'] = _ownership(os.stat(name, dir_fd=directory, follow_symlinks=False))
    row['reservation'] = name
    artifacts[INDEX] = row
    _save(directory, artifacts, ARTIFACTS)
    # The fixed path becomes visible only after this exact inode is durable.
    batch._require(INDEX not in os.listdir(directory))
    batch._require(_ownership(os.stat(name, dir_fd=directory, follow_symlinks=False)) == row['build'])
    os.rename(name, INDEX, src_dir_fd=directory, dst_dir_fd=directory)
    os.fsync(directory)
    row['reservation'] = None


def register_owned_index(directory: int, generation: str, phase: str, members: list[dict]) -> None:
    """Reserve staging before child access; later seal only measured owned bytes.

    Members contain exactly scope/name. Building projections must be enumerated
    before a child writes them. A promoted root DB must retain a measured build
    inode; absence of a root path never grants ownership of a future file.
    """
    try:
        active = vault._ACTIVE_LEASE.get()
        vault._verify_publication_lease(None, (directory, active[1]) if active else None, exclusive=True)
        batch._require(type(generation) is str and _DIGEST.fullmatch(generation) is not None
                       and phase in ('building', 'inflight', 'ready') and type(members) is list
                       and len(members) <= 8198)
        requested = [_index_member(member) for member in members]
        batch._require(len(set(requested)) == len(requested))
        projections = sorted(name for scope, name in requested if scope == 'build' and name not in INDEX_FILES)
        batch._require(len(projections) <= 8192)
        batch._require(all(scope == 'build' for scope, _ in requested) if phase == 'building'
                       else requested == [('vault', INDEX_DB)])
        artifacts = _artifacts(directory)
        prior = artifacts.get(INDEX)
        if prior is None:
            batch._require(phase == 'building' and not any(name in os.listdir(directory) for name in (*INDEX_FILES, INDEX)))
            root = _ownership(os.fstat(directory))
            prior = {'kind': 'owned-hybrid-index-v1', 'generation': generation, 'phase': phase,
                     'root': root, 'build': None, 'reservation': None,
                     'projections': projections,
                     'members': []}
            _reserve_build(directory, artifacts, prior)
        batch._require(prior['generation'] == generation)
        batch._require(phase == 'building' and prior['phase'] == 'building'
                       or phase == 'inflight' and prior['phase'] == 'ready'
                       or phase == 'ready' and prior['phase'] in ('building', 'inflight'))
        row = dict(prior)
        if phase == 'building':
            batch._require(all(scope == 'build' for scope, _ in requested))
            row['projections'] = sorted(set(prior['projections']) | {name for _, name in requested if name not in INDEX_FILES})
            # Persist the projection allowlist before accepting child-produced files.
            artifacts[INDEX] = row
            _save(directory, artifacts, ARTIFACTS)
            row['members'] = _check_index(directory, row, mutable=True)
        elif phase == 'inflight':
            batch._require(requested == [('vault', INDEX_DB)])
            _check_index(directory, prior)
            row['phase'] = 'inflight'
            _reserve_build(directory, artifacts, row)
            with _build_directory(directory, row['build']) as build:
                for name in INDEX_FILES[1:]:
                    vault._write_private(build, name, b'')
                    os.fsync(build)
                    member = {'scope': 'build', 'name': name, 'info': _index_record(build, name)}
                    row['members'] = [*row['members'], member]
                    _save(directory, artifacts, ARTIFACTS)
                    # Root exposure follows the durable measured staging receipt.
                    batch._require(name not in os.listdir(directory))
                    os.rename(name, name, src_dir_fd=build, dst_dir_fd=directory)
                    os.fsync(build)
                    os.fsync(directory)
                    member['scope'] = 'vault'
                    _save(directory, artifacts, ARTIFACTS)
            batch._require(_ownership(os.stat(INDEX, dir_fd=directory, follow_symlinks=False)) == row['build'])
            os.rmdir(INDEX, dir_fd=directory)
            os.fsync(directory)
            row['build'] = None
        else:
            batch._require(requested == [('vault', INDEX_DB)])
            batch._require(not any(name in os.listdir(directory) for name in (*INDEX_FILES[1:], INDEX)))
            info = _index_record(directory, INDEX_DB)
            batch._require(any(item['name'] == INDEX_DB and item['info']['identity'] == info['identity']
                              for item in prior['members']))
            batch._require(_ownership(os.fstat(directory)) == prior['root'])
            row.update(phase='ready', build=None, reservation=None, projections=[],
                       members=[{'scope': 'vault', 'name': INDEX_DB, 'info': info}])
        artifacts[INDEX] = row
        _save(directory, artifacts, ARTIFACTS)
    except Exception:
        raise vault.WorkVaultError('owned index registration refused') from None


def purge_owned_index(directory: int) -> None:
    """Preflight the entire family before unlinking any owned derivative."""
    try:
        active = vault._ACTIVE_LEASE.get()
        vault._verify_publication_lease(None, (directory, active[1]) if active else None, exclusive=True)
        artifacts = _artifacts(directory)
        row = artifacts.get(INDEX)
        if row is None:
            return
        members = _check_index(directory, row, mutable=row['phase'] != 'ready')
        if row['phase'] == 'ready':
            row = dict(row, phase='inflight')
            artifacts[INDEX] = row
            _save(directory, artifacts, ARTIFACTS)
        if row['build'] is not None:
            try:
                with _build_directory(directory, row['build'], row['reservation'], expose_name=True) as (build, name):
                    for member in members:
                        if member['scope'] == 'build':
                            vault._unlink_owned(build, member['name'], tuple(member['info']['identity']))
                    os.fsync(build)
                batch._require(_ownership(os.stat(name, dir_fd=directory, follow_symlinks=False)) == row['build'])
                os.rmdir(name, dir_fd=directory)
            except FileNotFoundError:
                pass
        for member in members:
            if member['scope'] == 'vault':
                vault._unlink_owned(directory, member['name'], tuple(member['info']['identity']))
        os.fsync(directory)
        artifacts.pop(INDEX)
        _save(directory, artifacts, ARTIFACTS)
    except Exception:
        raise vault.WorkVaultError('owned index erasure refused') from None


def register_owned_artifact(config, name, kind, references):
    """Bounded seam: opt in one private vault file with collector dependencies.

    A native store outside this vault is not covered by this registration API.
    The owner must register every replacement before allowing it to be retrieved.
    """
    from sync import _source_config
    try:
        config = _source_config(config)
        _artifact_name(name)
        batch._require(kind in ('index', 'memory', 'summary'))
        refs = sources.validate_references(references)
        batch._require(bool(refs))
        with vault._publication_lock(config.vault) as directory:
            guard(directory, {'schema_version': curation.SOURCE_BATCH_SCHEMA,
                              'cards': [{'source': ref, 'dependencies': []} for ref in refs]})
            artifacts = _artifacts(directory)
            _, info = batch._record(directory, name)
            artifacts[name] = {'kind': kind, 'dependencies': [
                {'item': _key(ref), 'generation': _generation(ref)} for ref in refs], 'info': info}
            _save(directory, artifacts, ARTIFACTS)
    except Exception:
        raise vault.WorkVaultError('artifact registration refused') from None


def _cards(raw, name):
    clean = curation.parse_publication(raw)['batch']
    if clean['schema_version'] != curation.SOURCE_BATCH_SCHEMA:
        return []
    batch._require(len(clean['cards']) == 1 and batch.source_publication_name(clean['cards'][0]['source']) == name)
    return clean['cards']


def _inventory(config, directory, *, legacy_repository=None):
    records = []
    try:
        legacy = curation.parse_publication(vault._read(directory, vault.MANAGED))['batch']
    except FileNotFoundError:
        pass
    else:
        # The accepted coordinator owns one identity-qualified card per file.
        # Legacy aggregate v2 files require a migration, not partial erasure.
        batch._require(legacy['schema_version'] != curation.SOURCE_BATCH_SCHEMA and legacy_repository is None)
    for name in sorted(os.listdir(directory)):
        if _PUBLICATION.fullmatch(name) is None:
            continue
        raw, info = batch._record(directory, name)
        cards = _cards(raw, name)
        if legacy_repository is not None:
            clean = curation.parse_publication(raw)['batch']
            if clean['schema_version'] != curation.SOURCE_BATCH_SCHEMA and clean['repository'] == legacy_repository:
                batch._require(len(clean['cards']) == 1 and clean['requested_paths'] == [clean['cards'][0]['path']]
                               and batch.publication_name(legacy_repository, clean['cards'][0]['path']) == name)
                cards = clean['cards']
            else:
                cards = []
        if cards:
            records.append(({'scope': 'vault', 'directory': None,
                             'directory_identity': list(vault._identity(os.fstat(directory))),
                             'name': name, 'info': info}, cards))
    with vault._directory(config.snapshots, create=True) as root:
        root_identity = list(vault._identity(os.fstat(root)))
        for folder in sorted(os.listdir(root)):
            if _PUBLICATION.fullmatch(folder + '.md') is None:
                continue
            with vault._directory(config.snapshots / folder) as history:
                for _, name in vault._snapshot_files(history):
                    raw, info = batch._record(history, name)
                    cards = _cards(raw, folder + '.md')
                    if legacy_repository is not None:
                        clean = curation.parse_publication(raw)['batch']
                        if clean['schema_version'] != curation.SOURCE_BATCH_SCHEMA and clean['repository'] == legacy_repository:
                            batch._require(len(clean['cards']) == 1 and clean['requested_paths'] == [clean['cards'][0]['path']]
                                           and batch.publication_name(legacy_repository, clean['cards'][0]['path']) == folder + '.md')
                            cards = clean['cards']
                        else:
                            cards = []
                    if cards:
                        records.append(({'scope': 'history', 'directory': folder,
                                         'directory_identity': list(vault._identity(os.fstat(history))),
                                         'name': name, 'info': info}, cards))
    return records, root_identity


def _check_files(config, directory, rows):
    """Preflight the complete deletion set before unlinking any owned file."""
    for row in rows:
        if row['scope'] == 'vault':
            parent = directory
            batch._require(list(vault._identity(os.fstat(parent))) == row['directory_identity'])
            try:
                _, info = batch._record(parent, row['name'])
            except FileNotFoundError:
                continue
        else:
            with vault._directory(config.snapshots / row['directory']) as parent:
                batch._require(list(vault._identity(os.fstat(parent))) == row['directory_identity'])
                try:
                    _, info = batch._record(parent, row['name'])
                except FileNotFoundError:
                    continue
        batch._require(info == row['info'])


def _erase(config, directory, rows):
    _check_files(config, directory, rows)
    for row in rows:
        if row['scope'] == 'vault':
            vault._unlink_owned(directory, row['name'], tuple(row['info']['identity']))
            os.fsync(directory)
        else:
            with vault._directory(config.snapshots / row['directory']) as parent:
                vault._unlink_owned(parent, row['name'], tuple(row['info']['identity']))
                os.fsync(parent)


def _settle(config, directory, state):
    _validate_state(state, directory)
    with vault._directory(config.snapshots) as root:
        batch._require(list(vault._identity(os.fstat(root))) == state['snapshots_identity'])
    if state['pending'] is None:
        return
    artifacts = _artifacts(directory)
    if INDEX in state['pending']['artifacts'] and INDEX in artifacts:
        _check_index(directory, artifacts[INDEX], mutable=artifacts[INDEX]['phase'] != 'ready')
    for name in state['pending']['artifacts']:
        artifacts.pop(name, None)
    _validate_artifacts(artifacts)
    _erase(config, directory, state['pending']['files'])
    if INDEX in state['pending']['artifacts']:
        purge_owned_index(directory)
    if state['pending']['artifacts']:
        _save(directory, artifacts, ARTIFACTS)
    state['pending'] = None
    _save(directory, state, STATE)


def recover_source_lifecycle(config):
    from sync import _source_config, _config, SyncConfig
    try:
        config = _config(config) if type(config) is SyncConfig else _source_config(config)
        try:
            with vault._publication_lock(config.vault, recovery=True, lifecycle=True) as directory:
                state = _load(directory)
                if state is not None:
                    _settle(config, directory, state)
                family = _artifacts(directory).get(INDEX)
                interrupted = family is not None and family['phase'] != 'ready'
                if interrupted:
                    purge_owned_index(directory)
        except FileNotFoundError:
            return False
        return state is not None or interrupted
    except Exception:
        raise vault.WorkVaultError('lifecycle recovery refused') from None


def apply_source_lifecycle(config, selection, event):
    """Explicit collector-confirmed removal/withdrawal, never inferred absence."""
    from sync import _source_config, _source_binding, _bound_reference
    try:
        config = _source_config(config)
        curation._object(event, {'schema_version', 'action', 'reference', 'confirmed', 'evidence'})
        curation._literal(event['schema_version'], 'workvault-source-lifecycle-v1')
        action = event['action']
        batch._require(action in ('retired', 'withdrawn') and event['confirmed'] is True
                       and event['evidence'] == {'retired': 'source_removed', 'withdrawn': 'access_withdrawn'}[action])
        reference = _bound_reference(event['reference'], _source_binding(selection))
        recover_source_lifecycle(config)
        # Settle the existing owned batch before planning; its backups never sit
        # outside the lifecycle fence once withdrawal evidence is durable.
        batch.recover_publication_batch(config.vault, config.snapshots)
        with vault._publication_lock(config.vault, create=True) as directory:
            records, snapshots_identity = _inventory(config, directory)
            state = _load(directory) or {'schema_version': 'workvault-lifecycle-state-v2',
                'vault_identity': list(vault._identity(os.fstat(directory))),
                'snapshots_identity': snapshots_identity, 'blocked': {}, 'generations': {}, 'pending': None}
            batch._require(state['snapshots_identity'] == snapshots_identity)
            root = _key(reference)
            affected = set()
            while True:
                before = len(affected)
                for _, cards in records:
                    for card in cards:
                        if (_key(card['source']) == root
                                or any(_key(ref) == root or _generation(ref) in affected for ref in card['dependencies'])):
                            affected.add(_generation(card['source']))
                if len(affected) == before:
                    break
            files = [row for row, cards in records if (action == 'withdrawn' or row['scope'] == 'vault')
                     and any(_generation(card['source']) in affected for card in cards)]
            artifacts = _artifacts(directory)
            names = [name for name, row in artifacts.items() if name != INDEX and any(
                ref['item'] == root or ref['generation'] in affected for ref in row['dependencies'])]
            files.extend({'scope': 'vault', 'directory': None,
                          'directory_identity': list(vault._identity(os.fstat(directory))),
                          'name': name, 'info': artifacts[name]['info']} for name in names)
            if INDEX in artifacts:
                _check_index(directory, artifacts[INDEX], mutable=artifacts[INDEX]['phase'] != 'ready')
                names.append(INDEX)
            _check_files(config, directory, files)
            if state['blocked'].get(root) != 'withdrawn':
                state['blocked'][root] = action
            for key in affected:
                if state['generations'].get(key) != 'withdrawn':
                    state['generations'][key] = action
            state['pending'] = {'files': files, 'artifacts': names}
            _save(directory, state, STATE)
            _settle(config, directory, state)
        return {'schema_version': 'workvault-source-lifecycle-status-v1', 'state': action,
                'native_index_memory_erasure': 'requires_registered_owner_integration'}
    except Exception:
        raise vault.WorkVaultError('source lifecycle refused') from None


def apply_legacy_repository_withdrawal(config, event) -> dict:
    """An explicit confirmed withdrawal fences the configured v1 repository."""
    from sync import _config
    try:
        config = _config(config)
        curation._object(event, {'schema_version', 'repository', 'action', 'confirmed', 'evidence'})
        batch._require(event == {'schema_version': 'workvault-legacy-withdrawal-v1',
                       'repository': config.repository, 'action': 'withdrawn', 'confirmed': True,
                       'evidence': 'access_withdrawn'} and event['confirmed'] is True)
        recover_source_lifecycle(config)
        batch.recover_publication_batch(config.vault, config.snapshots)
        with vault._publication_lock(config.vault, create=True) as directory:
            records, snapshots_identity = _inventory(config, directory, legacy_repository=config.repository)
            state = _load(directory) or {'schema_version': 'workvault-lifecycle-state-v2',
                'vault_identity': list(vault._identity(os.fstat(directory))),
                'snapshots_identity': snapshots_identity, 'blocked': {}, 'generations': {}, 'pending': None}
            batch._require(state['snapshots_identity'] == snapshots_identity)
            artifacts = _artifacts(directory)
            names = [INDEX] if INDEX in artifacts else []
            if names:
                _check_index(directory, artifacts[INDEX], mutable=artifacts[INDEX]['phase'] != 'ready')
            files = [row for row, _ in records]
            _check_files(config, directory, files)
            state['blocked'][_legacy_key(config.repository)] = 'withdrawn'
            state['pending'] = {'files': files, 'artifacts': names}
            _save(directory, state, STATE)
            _settle(config, directory, state)
        return {'schema_version': 'workvault-legacy-withdrawal-status-v1', 'state': 'withdrawn'}
    except Exception:
        raise vault.WorkVaultError('legacy repository withdrawal refused') from None
