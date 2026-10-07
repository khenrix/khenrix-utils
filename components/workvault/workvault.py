"""Owner-operated publication of already-curated WorkVault batches on POSIX."""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import stat
import uuid

from curation import (MAX_PUBLICATION_BYTES, SOURCE_BATCH_SCHEMA, parse_publication,
                      render_publication, validate_curated_batch, validate_source_batch)
from sources import source_key


MANAGED = 'Knowledge.md'
LOCK = '.workvault.lock'
_SNAPSHOT = re.compile(r'snapshot-([0-9]{20})\.md\Z')
_FILE_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_ACTIVE_BATCH = ContextVar('workvault_batch', default=None)


class WorkVaultError(RuntimeError):
    """Content-free refusal; inspect status after an interrupted publication."""


def validate_publication_name(value):
    """Require one visible Markdown basename before opening filesystem paths."""
    if (type(value) is not str or not value.endswith('.md')
            or value.startswith('.') or value.strip() != value
            or any(char in value for char in '/\\')
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise WorkVaultError('unsafe publication name')
    try:
        if not 4 <= len(value.encode('utf-8')) <= 255:
            raise WorkVaultError('unsafe publication name')
    except UnicodeError:
        raise WorkVaultError('unsafe publication name') from None
    return value


def _root_path(value):
    path = Path(value).absolute()
    if '..' in path.parts:
        raise WorkVaultError('unsafe root')
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            raise WorkVaultError('unsafe root')
    except FileNotFoundError:
        pass
    return path.parent.resolve(strict=False) / path.name


def _private(info, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    if (not kind(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != mode
            or (not directory and info.st_nlink != 1)):
        raise WorkVaultError('unsafe private path')


@contextmanager
def _directory(path, create=False):
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _private(os.fstat(descriptor), directory=True)
        yield descriptor
    finally:
        os.close(descriptor)


def _check_existing_root(path):
    try:
        with _directory(path):
            pass
    except FileNotFoundError:
        pass


@contextmanager
def _file(directory, name, flags=os.O_RDONLY, create=False):
    descriptor = os.open(name, flags | _FILE_FLAGS | (os.O_CREAT if create else 0),
                         mode=0o600, dir_fd=directory)
    try:
        _private(os.fstat(descriptor))
        yield descriptor
    finally:
        os.close(descriptor)


def _read(directory, name):
    with _file(directory, name) as descriptor:
        data = bytearray()
        while len(data) <= MAX_PUBLICATION_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_PUBLICATION_BYTES + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
        raise WorkVaultError('publication too large')


def _current(directory, publication):
    try:
        data = _read(directory, publication)
    except FileNotFoundError:
        return None, None
    return data, parse_publication(data)


def _state(checkpoint):
    if checkpoint is None:
        return {'schema_version': 'workvault-status-v1', 'state': 'empty'}
    batch = checkpoint['batch']
    if batch['schema_version'] == SOURCE_BATCH_SCHEMA:
        # Curated cards do not prove completeness of an upstream inventory.
        return {'schema_version': 'workvault-status-v2', 'state': 'published',
                'sources': [card['source'] for card in batch['cards']],
                'published_at': checkpoint['published_at'], 'card_count': len(batch['cards']),
                'full_source_coverage': False}
    discovered = set(batch['discovered_paths'])
    covered = set(batch['requested_paths'])
    return {
        'schema_version': 'workvault-status-v1', 'state': 'published',
        'repository': batch['repository'], 'published_commit': batch['commit'],
        'covered_paths': sorted(covered),
        'uncovered_paths': sorted(discovered - covered),
        'index_gaps': sorted(discovered - set(batch['indexed_paths'])),
        'full_source_coverage': covered == discovered,
        'published_at': checkpoint['published_at'], 'card_count': len(batch['cards']),
    }


def status(vault: Path, publication: str = MANAGED) -> dict:
    """Read one coherent managed file, without creating roots or runtime files."""
    try:
        publication = validate_publication_name(publication)
        path = _root_path(vault)
        try:
            with _publication_lock(path, shared=True) as directory:
                _, checkpoint = _current(directory, publication)
        except FileNotFoundError:
            checkpoint = None
        return _state(checkpoint)
    except Exception:
        raise WorkVaultError('status refused') from None


def _identity(info):
    return info.st_dev, info.st_ino


@contextmanager
def _publication_lock(path, *, create=False, shared=False, recovery=False, required=False):
    active = _ACTIVE_BATCH.get()
    if active is not None:
        if required:
            raise WorkVaultError('batch recovery required')
        _private(path.lstat(), directory=True)
        _private(os.fstat(active.directory), directory=True)
        if path != active.vault or _identity(path.lstat()) != _identity(os.fstat(active.directory)):
            raise WorkVaultError('batch root changed')
        yield active.directory
        return
    with _directory(path, create=create) as directory:
        with ExitStack() as stack:
            try:
                lock = stack.enter_context(_file(directory, LOCK, os.O_RDONLY if shared else os.O_RDWR,
                                                 create=not shared))
            except FileNotFoundError:
                if not shared or required:
                    raise
                if any(name.startswith('.workvault-batch') for name in os.listdir(directory)):
                    raise WorkVaultError('batch recovery required')
                yield directory
                return
            fcntl.flock(lock, (fcntl.LOCK_SH | fcntl.LOCK_NB) if shared else fcntl.LOCK_EX)
            try:
                info = os.stat(LOCK, dir_fd=directory, follow_symlinks=False)
                _private(info)
                if _identity(info) != _identity(os.fstat(lock)):
                    raise WorkVaultError('runtime file changed')
                if not recovery and any(name.startswith('.workvault-batch') for name in os.listdir(directory)):
                    raise WorkVaultError('batch recovery required')
                yield directory
                if required:
                    root_info = path.lstat()
                    lock_info = os.stat(LOCK, dir_fd=directory, follow_symlinks=False)
                    _private(root_info, directory=True)
                    _private(lock_info)
                    if (_identity(root_info) != _identity(os.fstat(directory))
                            or _identity(lock_info) != _identity(os.fstat(lock))
                            or any(name.startswith('.workvault-batch') for name in os.listdir(directory))):
                        raise WorkVaultError('runtime file changed')
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _unlink_owned(directory, name, identity):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    _private(info)
    if _identity(info) != identity:
        raise WorkVaultError('runtime file changed')
    os.unlink(name, dir_fd=directory)


def _write_private(directory, name, data):
    identity = None
    try:
        with _file(directory, name, os.O_WRONLY | os.O_EXCL, create=True) as descriptor:
            identity = _identity(os.fstat(descriptor))
            remaining = memoryview(data)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise WorkVaultError('write failed')
                remaining = remaining[written:]
            os.fsync(descriptor)
        return identity
    except BaseException:
        if identity is not None:
            _unlink_owned(directory, name, identity)
        raise


def _snapshot_files(directory):
    names = os.listdir(directory)
    if len(names) > 8:
        raise WorkVaultError('unexpected snapshots')
    found = []
    for name in names:
        match = _SNAPSHOT.fullmatch(name)
        if match is None:
            raise WorkVaultError('unexpected snapshots')
        data = _read(directory, name)
        parse_publication(data)
        found.append((int(match.group(1)), name))
    return sorted(found)


def _snapshot(directory, old, found):
    if found and _read(directory, found[-1][1]) == old:
        return found
    if len(found) == 8:
        if _ACTIVE_BATCH.get() is not None:
            raise WorkVaultError('snapshot settlement required')
        # A prior replacement completed before its history pruning did.
        _prune_snapshots(directory, found)
        found = found[-7:]
    sequence = found[-1][0] + 1 if found else 1
    if sequence >= 10 ** 20:
        raise WorkVaultError('snapshot sequence exhausted')
    name = f'snapshot-{sequence:020d}.md'
    identity = _write_private(directory, name, old)
    try:
        active = _ACTIVE_BATCH.get()
        if active is not None:
            active.snapshot(name, identity, old)
        os.fsync(directory)
    except BaseException:
        _unlink_owned(directory, name, identity)
        raise
    return found + [(sequence, name)]


def _prune_snapshots(directory, found):
    for _, expired in found[:-7]:
        info = os.stat(expired, dir_fd=directory, follow_symlinks=False)
        _unlink_owned(directory, expired, _identity(info))
    os.fsync(directory)


def _same_selection(checkpoint, batch):
    if checkpoint is not None:
        prior = checkpoint['batch']
        if prior['schema_version'] != batch['schema_version']:
            raise WorkVaultError('source selection changed')
        if batch['schema_version'] == SOURCE_BATCH_SCHEMA:
            before = {source_key(card['source']): card['source'] for card in prior['cards']}
            after = {source_key(card['source']): card['source'] for card in batch['cards']}
            if set(before) != set(after):
                raise WorkVaultError('source selection changed')
            _check_source_revisions([*prior['cards'], *batch['cards']], {})
            return
        if (prior['repository'] != batch['repository']
                or set(prior['requested_paths']) != set(batch['requested_paths'])):
            raise WorkVaultError('source selection changed')


def _check_source_revisions(cards, revisions):
    for card in cards:
        for ref in [card['source'], *card['supersedes'], *card['dependencies']]:
            if ref['revision'] is not None:
                key = (source_key(ref), ref['revision'])
                if key in revisions and revisions[key] != ref['content_hash']:
                    raise WorkVaultError('source revision changed')
                revisions[key] = ref['content_hash']


def _validated_batch(batch):
    return (validate_source_batch(batch) if type(batch) is dict and batch.get('schema_version') == SOURCE_BATCH_SCHEMA
            else validate_curated_batch(batch))


def publish_curated_batch(vault: Path, snapshots: Path, batch: dict,
                          publication: str = MANAGED) -> dict:
    """Snapshot the prior generation, then atomically replace one managed file."""
    try:
        publication = validate_publication_name(publication)
        clean = _validated_batch(batch)
        active = _ACTIVE_BATCH.get()
        if active is not None:
            published_at = active.published_at
        else:
            now = datetime.now(timezone.utc)
            published_at = (now.isoformat(timespec='microseconds').replace('+00:00', 'Z')
                            if clean['schema_version'] == SOURCE_BATCH_SCHEMA else now.strftime('%Y-%m-%dT%H:%M:%SZ'))
        new = render_publication(clean, published_at)
        vault_path, snapshot_path = _root_path(vault), _root_path(snapshots)
        if (vault_path == snapshot_path or vault_path.is_relative_to(snapshot_path)
                or snapshot_path.is_relative_to(vault_path)):
            raise WorkVaultError('overlapping roots')
        _check_existing_root(vault_path)
        _check_existing_root(snapshot_path)
        try:
            with _directory(vault_path) as directory:
                _, checkpoint = _current(directory, publication)
                _same_selection(checkpoint, clean)
        except FileNotFoundError:
            pass
        with _publication_lock(vault_path, create=True) as directory:
            if active is not None:
                active.select(publication, snapshot_path, clean)
            old, checkpoint = _current(directory, publication)
            _same_selection(checkpoint, clean)
            with _directory(snapshot_path, create=True) as snapshot_directory:
                found = _snapshot_files(snapshot_directory)
                if clean['schema_version'] == SOURCE_BATCH_SCHEMA:
                    for _, name in found:
                        _same_selection(parse_publication(_read(snapshot_directory, name)), clean)
                if old is None and found:
                    raise WorkVaultError('snapshots without publication')
                if old is not None:
                    found = _snapshot(snapshot_directory, old, found)
                staged = '.workvault-stage-' + uuid.uuid4().hex
                identity = _write_private(directory, staged, new)
                try:
                    if active is not None:
                        active.stage(publication, identity, new)
                    if _current(directory, publication)[0] != old:
                        raise WorkVaultError('managed file changed')
                    if active is not None:
                        active.check()
                    os.replace(staged, publication, src_dir_fd=directory, dst_dir_fd=directory)
                    if active is None:
                        _prune_snapshots(snapshot_directory, found)
                    os.fsync(directory)
                finally:
                    _unlink_owned(directory, staged, identity)
            return _state(parse_publication(new))
    except Exception:
        raise WorkVaultError('publication refused; inspect status') from None
