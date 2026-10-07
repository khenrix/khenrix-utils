"""Recovery of one owned, curated-only batch under the existing vault lock."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

import curation
import sources
import workvault as vault


JOURNAL = '.workvault-batch.json'
BACKUPS = '.workvault-batch'
SCHEMA = 'workvault-owned-batch-v1'
SOURCE_SCHEMA = 'workvault-owned-batch-v2'
_STAGE = re.compile(r'\.workvault-stage-[0-9a-f]{32}\Z')


class BatchPublicationRejected(vault.WorkVaultError):
    """The original requested batch was durably restored and recovery is closed."""


def publication_name(repository, path):
    curation.validate_repository(repository)
    curation._path(path)
    return 'Knowledge-' + hashlib.sha256((repository + '\n' + path).encode()).hexdigest()[:24] + '.md'


def source_publication_name(reference):
    identity = sources.source_key(sources.validate_reference(reference))
    return 'Knowledge-' + hashlib.sha256(identity.encode()).hexdigest()[:24] + '.md'


def _single(clean, index):
    card = clean['cards'][index]
    result = dict(clean, cards=[card])
    if clean['schema_version'] != curation.SOURCE_BATCH_SCHEMA:
        result['requested_paths'] = [card['path']]
    return result


def _binding(value, row, clean, *, new=False, revisions=None):
    if value['schema'] == SOURCE_SCHEMA:
        _require(clean['schema_version'] == curation.SOURCE_BATCH_SCHEMA and len(clean['cards']) == 1)
        reference = clean['cards'][0]['source']
        _require(sources.source_key(reference) == sources.source_key(row['source']))
        if new:
            _require(reference == row['source'])
        if revisions is not None:
            vault._check_source_revisions(clean['cards'], revisions)
    else:
        _require(clean['schema_version'] == curation.BATCH_SCHEMA
                 and clean['repository'] == value['repository']
                 and clean['requested_paths'] == [row['path']])
        if new:
            _require(clean['commit'] == value['commit'])


def publication_pending(path):
    try:
        with vault._directory(vault._root_path(path)) as directory:
            return any(name.startswith('.workvault-batch') for name in os.listdir(directory))
    except FileNotFoundError:
        return False


def read_curated_cards(path, repository, paths):
    """Internal source-qualified read, not session authority or a client endpoint."""
    from lifecycle import guard
    try:
        curation.validate_repository(repository)
        _require(type(paths) is list and 1 <= len(paths) <= curation.MAX_INVENTORY_PATHS)
        paths = [curation._path(item) for item in paths]
        _require(len(set(paths)) == len(paths))
        result, records = [], {}
        with vault._publication_lock(vault._root_path(path), shared=True, required=True) as directory:
            for source_path in paths:
                name = publication_name(repository, source_path)
                raw, info = _record(directory, name)
                checkpoint = curation.parse_publication(raw)
                clean = checkpoint['batch']
                guard(directory, clean)
                _require(clean['repository'] == repository
                         and clean['requested_paths'] == [source_path]
                         and len(clean['cards']) == 1 and clean['cards'][0]['path'] == source_path)
                records[name] = info
                result.append({'id': name[:-3], 'repository': repository, 'commit': clean['commit'],
                    'published_at': checkpoint['published_at'], 'card': clean['cards'][0],
                    'source_url': f"https://github.com/{repository}/blob/{clean['commit']}/"
                                  + curation.quote(source_path, safe='/')})
            for name, expected in records.items():
                _require(_record(directory, name)[1] == expected)
        return result
    except Exception:
        raise vault.WorkVaultError('curated read refused') from None


def read_source_cards(path, references):
    """Canonical v2 storage boundary; callers must rebuild cached readers on events."""
    from lifecycle import blocked
    try:
        refs = sources.validate_references(references)
        result, records = [], {}
        with vault._publication_lock(vault._root_path(path), shared=True, required=True) as directory:
            for ref in refs:
                if blocked(directory, ref):
                    continue
                name = source_publication_name(ref)
                try:
                    raw, info = _record(directory, name)
                except FileNotFoundError:
                    continue
                checkpoint = curation.parse_publication(raw)
                clean = checkpoint['batch']
                _require(clean['schema_version'] == curation.SOURCE_BATCH_SCHEMA and len(clean['cards']) == 1
                         and sources.source_key(clean['cards'][0]['source']) == sources.source_key(ref))
                _require(not any(blocked(directory, dep) for dep in clean['cards'][0]['dependencies']))
                records[name] = info
                result.append(checkpoint)
            for name, expected in records.items():
                _require(_record(directory, name)[1] == expected)
        return result
    except Exception:
        raise vault.WorkVaultError('source curated read refused') from None


def _require(value):
    if not value:
        raise vault.WorkVaultError('batch recovery is ambiguous')


def _encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def _record(directory, name):
    before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    vault._private(before)
    raw = vault._read(directory, name)
    after = os.stat(name, dir_fd=directory, follow_symlinks=False)
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    _require(all(getattr(before, field) == getattr(after, field) for field in fields))
    return raw, {'identity': list(vault._identity(after)), 'bytes': len(raw),
                 'sha256': hashlib.sha256(raw).hexdigest()}


def _info(value):
    _require(type(value) is dict and set(value) == {'identity', 'bytes', 'sha256'})
    _pair(value['identity'])
    _require(type(value['bytes']) is int and 0 <= value['bytes'] <= curation.MAX_PUBLICATION_BYTES
             and type(value['sha256']) is str and len(value['sha256']) == 64
             and all(char in '0123456789abcdef' for char in value['sha256']))
    return value


def _pair(value):
    _require(type(value) is list and len(value) == 2 and all(type(item) is int and item >= 0 for item in value))
    return value


class _Batch:
    def __init__(self, path, snapshots, directory, value, raw, journal_identity):
        self.vault, self.snapshots, self.directory = path, snapshots, directory
        self.value, self.raw, self.current = value, raw, None
        self.journal_identity = journal_identity
        self.published_at = value['published_at']

    def name(self, row):
        if self.value['schema'] == SOURCE_SCHEMA:
            return source_publication_name(row['source'])
        return publication_name(self.value['repository'], row['path'])

    def save(self):
        current, info = _record(self.directory, JOURNAL)
        _require(current == self.raw and info['identity'] == self.journal_identity)
        raw = _encoded(self.value)
        staged = '.workvault-batch-stage-' + uuid.uuid4().hex
        identity = vault._write_private(self.directory, staged, raw)
        try:
            current, info = _record(self.directory, JOURNAL)
            _require(current == self.raw and info['identity'] == self.journal_identity)
            os.replace(staged, JOURNAL, src_dir_fd=self.directory, dst_dir_fd=self.directory)
            os.fsync(self.directory)
            self.raw = raw
            self.journal_identity = list(identity)
        finally:
            vault._unlink_owned(self.directory, staged, identity)

    def select(self, publication, snapshots, clean):
        selected = [row for row in self.value['rows'] if self.name(row) == publication]
        _require(len(selected) == 1 and snapshots == self.snapshots / publication[:-3])
        row = selected[0]
        with vault._directory(self.vault / BACKUPS) as directory:
            new, info = _record(directory, 'new-' + str(self.value['rows'].index(row)))
        _require(info == row['new']['backup'] and curation.parse_publication(new)['batch'] == clean)
        self.current = row

    def stage(self, publication, identity, raw):
        row = self.current
        _require(row is not None and self.name(row) == publication
                 and hashlib.sha256(raw).hexdigest() == row['new']['backup']['sha256'])
        row['new']['identity'] = list(identity)
        self.save()

    def snapshot(self, name, identity, raw):
        row = self.current
        _require(row is not None and row['old'] is not None
                 and hashlib.sha256(raw).hexdigest() == row['old']['backup']['sha256']
                 and row['added_snapshot'] is None)
        expected = max([int(key[9:29]) for key in row['history']], default=0) + 1
        _require(name == f'snapshot-{expected:020d}.md')
        row['added_snapshot'] = {'name': name, 'info': {'identity': list(identity),
            'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}}
        self.save()

    def stages(self):
        expected = {}
        for row in self.value['rows']:
            if row['new']['identity'] is not None:
                expected[tuple(row['new']['identity'])] = dict(row['new']['backup'],
                    identity=row['new']['identity'])
            if row['restored_identity'] is not None:
                expected[tuple(row['restored_identity'])] = dict(row['old']['backup'],
                    identity=row['restored_identity'])
        names = [name for name in os.listdir(self.directory) if name.startswith('.workvault-stage-')]
        _require(len(names) <= 2 * len(self.value['rows']))
        found = {}
        for name in names:
            _require(_STAGE.fullmatch(name) is not None)
            _, info = _record(self.directory, name)
            _require(expected.get(tuple(info['identity'])) == info)
            found[name] = info
        return found

    def check(self):
        from lifecycle import guard
        revisions = {}
        raw, info = _record(self.directory, JOURNAL)
        _require(raw == self.raw and info['identity'] == self.journal_identity)
        info = self.vault.lstat()
        vault._private(info, directory=True)
        vault._private(os.fstat(self.directory), directory=True)
        _require(list(vault._identity(info)) == self.value['vault_identity'])
        with vault._directory(self.snapshots) as root:
            _require(list(vault._identity(os.fstat(root))) == self.value['snapshots_identity'])
        expected_backups = {}
        for index, row in enumerate(self.value['rows']):
            expected_backups['new-' + str(index)] = (row['new']['backup'], row, True)
            if row['old'] is not None:
                expected_backups['old-' + str(index)] = (row['old']['backup'], row, False)
        try:
            with vault._directory(self.vault / BACKUPS) as directory:
                names = set(os.listdir(directory))
                _require(list(vault._identity(os.fstat(directory))) == self.value['backup_identity']
                         and names <= set(expected_backups)
                         and (self.value['phase'] != 'publishing' or names == set(expected_backups)))
                for name in names:
                    expected, row, new = expected_backups[name]
                    raw, info = _record(directory, name)
                    _require(info == expected)
                    publication = curation.parse_publication(raw)
                    guard(self.directory, publication['batch'])
                    _binding(self.value, row, publication['batch'], new=new, revisions=revisions)
                    if new:
                        _require(publication['published_at'] == self.published_at)
        except FileNotFoundError:
            _require(self.value['phase'] != 'publishing')
        for row in self.value['rows']:
            name = self.name(row)
            try:
                raw, info = _record(self.directory, name)
            except FileNotFoundError:
                _require(row['old'] is None and self.value['phase'] != 'committed')
            else:
                old = row['old']
                old_match = (old is not None and info['identity'] in (old['identity'], row['restored_identity'])
                             and info['bytes'] == old['backup']['bytes']
                             and info['sha256'] == old['backup']['sha256'])
                new_match = (info['identity'] == row['new']['identity']
                             and info['bytes'] == row['new']['backup']['bytes']
                             and info['sha256'] == row['new']['backup']['sha256'])
                _require(new_match if self.value['phase'] == 'committed' else
                         old_match if self.value['phase'] == 'rolled-back' else old_match or new_match)
                publication = curation.parse_publication(raw)
                batch = publication['batch']
                guard(self.directory, batch)
                _binding(self.value, row, batch, new=self.value['phase'] == 'committed', revisions=revisions)
                if self.value['phase'] == 'committed':
                    _require(publication['published_at'] == self.published_at)
            with vault._directory(self.snapshots / name[:-3]) as directory:
                _require(list(vault._identity(os.fstat(directory))) == row['snapshot_identity'])
                expected = dict(row['history'])
                added = row['added_snapshot']
                if added is not None:
                    expected[added['name']] = added['info']
                names = set(os.listdir(directory))
                _require(names <= set(expected))
                required = sorted(expected)[-7:] if self.value['phase'] == 'committed' else row['history']
                _require(set(required) <= names)
                for filename in names:
                    snapshot, info = _record(directory, filename)
                    _require(info == expected[filename])
                    batch = curation.parse_publication(snapshot)['batch']
                    guard(self.directory, batch)
                    _binding(self.value, row, batch, revisions=revisions)
        self.stages()

    def rollback(self):
        self.check()
        with vault._directory(self.vault / BACKUPS) as backups:
            for index, row in enumerate(self.value['rows']):
                name = self.name(row)
                try:
                    current, current_info = _record(self.directory, name)
                except FileNotFoundError:
                    _require(row['old'] is None)
                    current = current_info = None
                else:
                    allowed = [row['new']]
                    if row['old'] is not None:
                        allowed.append(row['old'])
                        allowed.append(dict(row['old'], identity=row['restored_identity']))
                    _require(any(current_info == dict(item['backup'], identity=item['identity'])
                                 for item in allowed))
                if row['old'] is None:
                    if current is not None:
                        vault._unlink_owned(self.directory, name, tuple(current_info['identity']))
                        os.fsync(self.directory)
                    continue
                old, info = _record(backups, 'old-' + str(index))
                _require(info == row['old']['backup'])
                if current == old:
                    continue
                staged = '.workvault-stage-' + uuid.uuid4().hex
                identity = vault._write_private(self.directory, staged, old)
                try:
                    row['restored_identity'] = list(identity)
                    self.save()
                    _require(_record(self.directory, name) == (current, current_info))
                    os.replace(staged, name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
                    os.fsync(self.directory)
                finally:
                    vault._unlink_owned(self.directory, staged, identity)
        self.value['phase'] = 'rolled-back'
        self.save()
        self.check()

    def cleanup(self):
        self.check()
        for name, expected in self.stages().items():
            _, info = _record(self.directory, name)
            _require(info == expected)
            vault._unlink_owned(self.directory, name, tuple(info['identity']))
        os.fsync(self.directory)
        if self.value['phase'] == 'committed':
            for row in self.value['rows']:
                with vault._directory(self.snapshots / self.name(row)[:-3]) as directory:
                    expected = dict(row['history'])
                    if row['added_snapshot'] is not None:
                        expected[row['added_snapshot']['name']] = row['added_snapshot']['info']
                    for _, name in vault._snapshot_files(directory)[:-7]:
                        _, info = _record(directory, name)
                        _require(info == expected[name])
                        vault._unlink_owned(directory, name, tuple(info['identity']))
                    os.fsync(directory)
        expected = {}
        for index, row in enumerate(self.value['rows']):
            expected['new-' + str(index)] = row['new']['backup']
            if row['old'] is not None:
                expected['old-' + str(index)] = row['old']['backup']
        try:
            with vault._directory(self.vault / BACKUPS) as directory:
                _require(list(vault._identity(os.fstat(directory))) == self.value['backup_identity']
                         and set(os.listdir(directory)) <= set(expected))
                for name in os.listdir(directory):
                    _, info = _record(directory, name)
                    _require(info == expected[name])
                    vault._unlink_owned(directory, name, tuple(info['identity']))
                os.fsync(directory)
                _require(list(vault._identity((self.vault / BACKUPS).lstat())) == self.value['backup_identity'])
                os.rmdir(BACKUPS, dir_fd=self.directory)
                os.fsync(self.directory)
        except FileNotFoundError:
            pass
        _require(not any(name.startswith('.workvault-batch-stage-') for name in os.listdir(self.directory)))
        raw, info = _record(self.directory, JOURNAL)
        _require(raw == self.raw and info['identity'] == self.journal_identity)
        vault._unlink_owned(self.directory, JOURNAL, tuple(info['identity']))
        os.fsync(self.directory)


def _load(path, snapshots, directory):
    raw, journal_info = _record(directory, JOURNAL)
    value = json.loads(raw, object_pairs_hook=curation._unique_object, parse_constant=curation._reject_constant)
    _require(type(value) is dict)
    native = value.get('schema') == SOURCE_SCHEMA
    fields = {'schema', 'phase', 'published_at', 'vault_identity', 'snapshots_identity', 'backup_identity', 'rows'}
    _require(set(value) == (fields if native else fields | {'repository', 'commit'})
        and value['schema'] in (SCHEMA, SOURCE_SCHEMA) and value['phase'] in ('publishing', 'committed', 'rolled-back')
        and raw == _encoded(value))
    (curation._source_timestamp if native else curation._timestamp)(value['published_at'])
    if not native:
        curation.validate_repository(value['repository'])
        _require(type(value['commit']) is str and len(value['commit']) == 40
                 and all(char in '0123456789abcdef' for char in value['commit']))
    for key in ('vault_identity', 'snapshots_identity', 'backup_identity'):
        _pair(value[key])
    _require(type(value['rows']) is list and 1 <= len(value['rows']) <= curation.MAX_CARDS)
    paths = []
    for row in value['rows']:
        _require(type(row) is dict and set(row) == {'source' if native else 'path', 'old', 'new',
            'restored_identity', 'snapshot_identity', 'history', 'added_snapshot'})
        paths.append(sources.source_key(sources.validate_reference(row['source'])) if native else curation._path(row['path']))
        _pair(row['snapshot_identity'])
        if row['restored_identity'] is not None:
            _pair(row['restored_identity'])
        _require(type(row['new']) is dict and set(row['new']) == {'identity', 'backup'})
        _info(row['new']['backup'])
        if row['new']['identity'] is not None:
            _pair(row['new']['identity'])
        if row['old'] is not None:
            _require(type(row['old']) is dict and set(row['old']) == {'identity', 'backup'})
            _pair(row['old']['identity'])
            _info(row['old']['backup'])
        _require(type(row['history']) is dict and len(row['history']) <= 8)
        for name, info in row['history'].items():
            _require(vault._SNAPSHOT.fullmatch(name) is not None)
            _info(info)
        added = row['added_snapshot']
        if added is not None:
            _require(type(added) is dict and set(added) == {'name', 'info'}
                     and type(added['name']) is str and vault._SNAPSHOT.fullmatch(added['name']) is not None
                     and added['name'] == f"snapshot-{max([int(key[9:29]) for key in row['history']], default=0) + 1:020d}.md"
                     and row['old'] is not None and len(row['history']) < 8)
            _info(added['info'])
            _require(added['info']['sha256'] == row['old']['backup']['sha256'])
    _require(len(set(paths)) == len(paths))
    result = _Batch(path, snapshots, directory, value, raw, journal_info['identity'])
    result.check()
    return result


def recover_publication_batch(path, snapshots):
    """Explicit sync recovery; report commands never invoke this function."""
    path, snapshots = vault._root_path(path), vault._root_path(snapshots)
    _require(path != snapshots and not path.is_relative_to(snapshots) and not snapshots.is_relative_to(path))
    try:
        with vault._directory(path):
            pass
    except FileNotFoundError:
        return False
    try:
        with vault._publication_lock(path, recovery=True) as directory:
            try:
                batch = _load(path, snapshots, directory)
            except FileNotFoundError:
                _require(not any(name.startswith('.workvault-batch') for name in os.listdir(directory)))
                return False
            if batch.value['phase'] == 'publishing':
                batch.rollback()
            batch.cleanup()
            return True
    except Exception:
        raise vault.WorkVaultError('batch recovery refused') from None


def _begin(path, snapshots, directory, clean):
    from lifecycle import guard
    guard(directory, clean)
    _require(not any(name.startswith('.workvault-batch') for name in os.listdir(directory)))
    os.mkdir(BACKUPS, mode=0o700, dir_fd=directory)
    rows, created = [], {}
    native = clean['schema_version'] == curation.SOURCE_BATCH_SCHEMA
    revisions = {}
    if native:
        vault._check_source_revisions(clean['cards'], revisions)
    now = datetime.now(timezone.utc)
    timestamp = (now.isoformat(timespec='microseconds').replace('+00:00', 'Z')
                 if native else now.strftime('%Y-%m-%dT%H:%M:%SZ'))
    backup_identity = list(vault._identity((path / BACKUPS).lstat()))
    try:
        with vault._directory(path / BACKUPS) as backups, vault._directory(snapshots, create=True) as snapshot_root:
            for index, card in enumerate(clean['cards']):
                name = (source_publication_name(card['source']) if native else
                        publication_name(clean['repository'], card['path']))
                single = _single(clean, index)
                old, checkpoint = vault._current(directory, name)
                vault._same_selection(checkpoint, single)
                if native and checkpoint is not None:
                    vault._check_source_revisions(checkpoint['batch']['cards'], revisions)
                original = None
                if old is not None:
                    current, info = _record(directory, name)
                    _require(current == old)
                    identity = vault._write_private(backups, 'old-' + str(index), old)
                    created['old-' + str(index)] = {'identity': list(identity), 'bytes': len(old),
                        'sha256': hashlib.sha256(old).hexdigest()}
                    _, backup_info = _record(backups, 'old-' + str(index))
                    _require(backup_info == created['old-' + str(index)])
                    original = {'identity': info['identity'], 'backup': backup_info}
                raw = curation.render_publication(single, timestamp)
                identity = vault._write_private(backups, 'new-' + str(index), raw)
                created['new-' + str(index)] = {'identity': list(identity), 'bytes': len(raw),
                    'sha256': hashlib.sha256(raw).hexdigest()}
                _, new = _record(backups, 'new-' + str(index))
                _require(new == created['new-' + str(index)])
                with vault._directory(snapshots / name[:-3], create=True) as history:
                    found = vault._snapshot_files(history)
                    _require(old is not None or not found)
                    if native:
                        for _, filename in found:
                            prior = curation.parse_publication(vault._read(history, filename))
                            vault._same_selection(prior, single)
                            vault._check_source_revisions(prior['batch']['cards'], revisions)
                    records = {filename: _record(history, filename)[1] for _, filename in found}
                    identity = list(vault._identity(os.fstat(history)))
                rows.append({('source' if native else 'path'): card['source'] if native else card['path'],
                    'old': original, 'new': {'identity': None, 'backup': new},
                    'restored_identity': None, 'snapshot_identity': identity,
                    'history': records, 'added_snapshot': None})
            os.fsync(backups)
            os.fsync(snapshot_root)
            value = {'schema': SOURCE_SCHEMA if native else SCHEMA, 'phase': 'publishing', 'published_at': timestamp,
                'vault_identity': list(vault._identity(os.fstat(directory))),
                'snapshots_identity': list(vault._identity(os.fstat(snapshot_root))),
                'backup_identity': list(vault._identity(os.fstat(backups))), 'rows': rows}
            if not native:
                value.update(repository=clean['repository'], commit=clean['commit'])
        raw = _encoded(value)
        journal_identity = vault._write_private(directory, JOURNAL, raw)
        os.fsync(directory)
        return _Batch(path, snapshots, directory, value, raw, list(journal_identity))
    except BaseException as error:
        try:
            os.stat(JOURNAL, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            with vault._directory(path / BACKUPS) as backups:
                _require(list(vault._identity(os.fstat(backups))) == backup_identity
                         and set(os.listdir(backups)) == set(created))
                for name, expected in created.items():
                    _, info = _record(backups, name)
                    _require(info == expected)
                    vault._unlink_owned(backups, name, tuple(info['identity']))
                os.fsync(backups)
                _require(list(vault._identity((path / BACKUPS).lstat())) == backup_identity)
                os.rmdir(BACKUPS, dir_fd=directory)
                os.fsync(directory)
            if isinstance(error, Exception):
                raise BatchPublicationRejected('batch preparation rejected') from None
        raise


@contextmanager
def publication_batch(path, snapshots, clean):
    clean = vault._validated_batch(clean)
    path, snapshots = vault._root_path(path), vault._root_path(snapshots)
    _require(path != snapshots and not path.is_relative_to(snapshots) and not snapshots.is_relative_to(path))
    with vault._publication_lock(path, create=True, recovery=True) as directory:
        batch = _begin(path, snapshots, directory, clean)
        rows = batch.value['rows']
        token = vault._ACTIVE_BATCH.set(batch)
        try:
            for index, row in enumerate(rows):
                single = _single(clean, index)
                batch.select(batch.name(row), snapshots / batch.name(row)[:-3], single)
                if row['old'] is not None:
                    with vault._directory(snapshots / batch.name(row)[:-3]) as history:
                        vault._snapshot(history, vault._read(directory, batch.name(row)), vault._snapshot_files(history))
            yield
            batch.value['phase'] = 'committed'
            batch.check()
            batch.save()
            batch.cleanup()
        except BaseException as error:
            if batch.value['phase'] == 'publishing':
                batch.rollback()
                batch.cleanup()
                if isinstance(error, Exception):
                    raise BatchPublicationRejected('batch publication rolled back') from None
            raise
        finally:
            vault._ACTIVE_BATCH.reset(token)
