"""Content-free lifecycle evidence under the existing private publication lock.

Only an explicit caller-confirmed collector event enters this API. Source read
failures and model output never call it. Native index/memory owners must register
their derived files and use the guarded read boundary before serving content.
"""
import hashlib
import json
import os
import re

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


def _key(reference):
    return hashlib.sha256(sources.source_key(reference).encode()).hexdigest()


def _generation(reference):
    return hashlib.sha256(sources.revision_key(reference).encode()).hexdigest()


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
    if state is None:
        return
    batch._require(state['pending'] is None)
    if clean is not None and clean['schema_version'] == curation.SOURCE_BATCH_SCHEMA:
        for card in clean['cards']:
            batch._require(not any(_blocked(state, ref) for ref in [card['source'], *card['dependencies']]))


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


def _inventory(config, directory):
    records = []
    try:
        legacy = curation.parse_publication(vault._read(directory, vault.MANAGED))['batch']
    except FileNotFoundError:
        pass
    else:
        # The accepted coordinator owns one identity-qualified card per file.
        # Legacy aggregate v2 files require a migration, not partial erasure.
        batch._require(legacy['schema_version'] != curation.SOURCE_BATCH_SCHEMA)
    for name in sorted(os.listdir(directory)):
        if _PUBLICATION.fullmatch(name) is None:
            continue
        raw, info = batch._record(directory, name)
        cards = _cards(raw, name)
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
    for name in state['pending']['artifacts']:
        artifacts.pop(name, None)
    _validate_artifacts(artifacts)
    _erase(config, directory, state['pending']['files'])
    if state['pending']['artifacts']:
        _save(directory, artifacts, ARTIFACTS)
    state['pending'] = None
    _save(directory, state, STATE)


def recover_source_lifecycle(config):
    from sync import _source_config
    try:
        config = _source_config(config)
        try:
            with vault._publication_lock(config.vault, recovery=True, lifecycle=True) as directory:
                state = _load(directory)
                if state is not None:
                    _settle(config, directory, state)
        except FileNotFoundError:
            return False
        return state is not None
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
            names = [name for name, row in artifacts.items() if any(
                ref['item'] == root or ref['generation'] in affected for ref in row['dependencies'])]
            files.extend({'scope': 'vault', 'directory': None,
                          'directory_identity': list(vault._identity(os.fstat(directory))),
                          'name': name, 'info': artifacts[name]['info']} for name in names)
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
