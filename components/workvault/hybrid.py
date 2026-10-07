"""One-shot local hybrid search over the complete owned curated collection."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import sqlite3
import stat
import subprocess
import time

import batch
import curation
import lifecycle
import retrieval
import sync
import workvault as vault


MODEL = 'Xenova/multilingual-e5-small'
VERSION = '0.15.6'
SEARCH_TIMEOUT = 30
REINDEX_TIMEOUT = 1800
MAX_OUTPUT = 1024 * 1024
MAX_LINE = 16 * 1024
_DIGEST = re.compile(r'[0-9a-f]{64}')
_BINDING_FIELDS = {'schema_version', 'workvault_root', 'cli_path', 'cli_artifact_sha256',
                   'cli_version', 'local_model', 'model_cache_manifest_sha256'}
_CACHE_FILES = {'config.json', 'tokenizer.json', 'tokenizer_config.json', 'onnx/model_quantized.onnx'}


@contextmanager
def _refused(category):
    try:
        yield
    except Exception:
        raise vault.WorkVaultError('hybrid ' + category + ' refused') from None


def _json(raw):
    return sync._json(raw)


def _exact_path(value):
    batch._require(type(value) is str and value.startswith('/') and '\x00' not in value)
    path = vault._root_path(value)
    batch._require(str(path) == value)
    return path


def _hash_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        batch._require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                       and before.st_uid == os.getuid() and not before.st_mode & 0o022)
        digest, size = hashlib.sha256(), 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        batch._require(all(getattr(before, key) == getattr(after, key) for key in
                           ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')))
        batch._require(vault._identity(path.lstat()) == vault._identity(after))
        return size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _validate_binding(binding, config=None):
    curation._object(binding, _BINDING_FIELDS)
    batch._require(binding['schema_version'] == 'workvault-hybrid-binding-v1'
                   and binding['cli_version'] == VERSION and binding['local_model'] == MODEL)
    for key in ('cli_artifact_sha256', 'model_cache_manifest_sha256'):
        batch._require(type(binding[key]) is str and _DIGEST.fullmatch(binding[key]))
    root, executable = _exact_path(binding['workvault_root']), _exact_path(binding['cli_path'])
    if config is not None:
        batch._require(root == config.vault)
    with vault._directory(root):
        pass
    batch._require(os.access(executable, os.X_OK)
                   and _hash_file(executable)[1] == binding['cli_artifact_sha256'])


def load_binding(path: Path) -> dict:
    with _refused('binding'):
        path = vault._root_path(path)
        with vault._directory(path.parent) as directory:
            binding = _json(vault._read(directory, path.name))
        _validate_binding(binding)
        return binding


def _cache(binding):
    home = Path.home()
    manifest = home / '.config/workvault/hybrid-model-cache.json'
    with vault._directory(manifest.parent) as directory:
        raw = vault._read(directory, manifest.name)
    batch._require(hashlib.sha256(raw).hexdigest() == binding['model_cache_manifest_sha256'])
    value = _json(raw)
    curation._object(value, {'schema_version', 'local_model', 'cache_root', 'files'})
    cache_root = home / '.cache/huggingface'
    batch._require(value['schema_version'] == 'workvault-hybrid-model-cache-v1'
                   and value['local_model'] == MODEL and value['cache_root'] == str(cache_root)
                   and type(value['files']) is list and len(value['files']) == len(_CACHE_FILES))
    expected = {MODEL + '/' + name for name in _CACHE_FILES}
    paths = set()
    for row in value['files']:
        curation._object(row, {'path', 'bytes', 'sha256'})
        batch._require(type(row['path']) is str and row['path'] in expected and row['path'] not in paths
                       and type(row['bytes']) is int and row['bytes'] > 0
                       and type(row['sha256']) is str and _DIGEST.fullmatch(row['sha256']))
        target = cache_root / row['path']
        batch._require(target.resolve(strict=True) == target)
        batch._require(_hash_file(target) == (row['bytes'], row['sha256']))
        paths.add(row['path'])
    batch._require(paths == expected)
    # Reviewed BertModel/q8 has a single ONNX session without external-data
    # chunks. A different model layout needs another explicit source review.
    with (cache_root / MODEL / 'config.json').open('rb') as stream:
        model_config = _json(stream.read(1024 * 1024 + 1))
    batch._require(type(model_config) is dict and model_config.get('model_type') == 'bert'
                   and model_config.get('hidden_size') == 384
                   and model_config.get('architectures') == ['BertModel']
                   and 'transformers.js_config' not in model_config)


def _rows(reader):
    rows = {}
    for row in reader.rows():
        reference = row['reference']
        name = (batch.source_publication_name(reference) if 'schema_version' in reference
                else batch.publication_name(reference['repository'], reference['path']))
        batch._require(name not in rows)
        rows[name] = row
    return rows


def collection_generation(reader: retrieval.CuratedReader, binding: dict) -> str:
    rows = _rows(reader)
    raw = curation._json({'binding': binding, 'publications': [[name, rows[name]] for name in sorted(rows)]})
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _environment(binding, root):
    # The reviewed executable launcher owns its absolute Node runtime. No shell
    # startup, provider credentials or caller-selected model/endpoints reach it.
    return {'HOME': str(Path.home()), 'PATH': str(Path(binding['cli_path']).parent) + ':/usr/bin:/bin',
            'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
            'OBSIDIAN_VAULT_PATH': str(root), 'LOCAL_EMBEDDING_MODEL': MODEL,
            'OBSIDIAN_RESPECT_GITIGNORE': 'false',
            'OBSIDIAN_IGNORE_PATTERNS': '.obsidian/**,templates/**,*.canvas,.workvault-*/**'}


def _run_child(argv, root, environment, lock, timeout):
    """Bound both pipes while retaining the kernel lease through child reaping."""
    process = subprocess.Popen(argv, cwd=root, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(lock,), start_new_session=True, umask=0o077)
    output, count = bytearray(), 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, True)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                batch._require(remaining > 0)
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    count += len(chunk)
                    batch._require(count <= MAX_OUTPUT)
                    if key.data:
                        output.extend(chunk)
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        batch._require(process.returncode == 0)
        return bytes(output)
    finally:
        # Descendants in the owned group cannot keep pipes, document copies or
        # the inherited lease alive beyond this bounded operation.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        process.stderr.close()


def _options(options, names):
    options = {} if options is None else options
    batch._require(type(options) is dict and set(options) <= {
        'mode', 'limit', 'threshold', 'scope', 'tag', 'frontmatter', 'snippetLength', 'anchors', 'rerank', 'notePath'})
    mode, limit, threshold = options.get('mode', 'hybrid'), options.get('limit', 5), options.get('threshold', 0)
    batch._require(mode in ('hybrid', 'semantic', 'fulltext', 'title')
                   and type(limit) is int and 1 <= limit <= 5
                   and type(threshold) in (int, float) and math.isfinite(threshold) and 0 <= threshold <= 1)
    argv = ['--mode', mode, '--limit', str(limit), '--threshold', str(threshold)]
    for key in ('scope', 'tag', 'frontmatter'):
        if key not in options:
            continue
        values = [options[key]] if type(options[key]) is str else options[key]
        batch._require(type(values) is list and len(values) <= 16)
        for value in values:
            batch._require(type(value) is str and 1 <= len(value) <= 512
                           and not any(ord(char) < 32 for char in value))
            argv.append('--' + key + '=' + value)
    if 'snippetLength' in options:
        length = options['snippetLength']
        batch._require(type(length) is int and 0 <= length <= 400)
        argv.extend(['--snippet-length', str(length)])
    if 'anchors' in options:
        batch._require(type(options['anchors']) is bool)
        if options['anchors']:
            argv.append('--anchors')
    if 'rerank' in options:
        batch._require(options['rerank'] is False)
    if 'notePath' in options:
        batch._require(type(options['notePath']) is str and options['notePath'] in names)
        argv.extend(['--path', options['notePath']])
    return argv, limit


def _inputs(directory, rows):
    result = {}
    for name, row in rows.items():
        raw, _ = batch._record(directory, name)
        checkpoint = curation.parse_publication(raw)
        batch._require(len(checkpoint['batch']['cards']) == 1
                       and retrieval.CuratedReader([checkpoint]).rows() == [row])
        result[name] = raw
    return result


def _stamp(config, directory):
    state = []
    with vault._directory(config.state) as source:
        for name in (sync.STATE, sync.SOURCE_STATE):
            try:
                state.append(vault._read(source, name))
            except FileNotFoundError:
                state.append(None)
    try:
        fence = vault._read(directory, lifecycle.STATE)
    except FileNotFoundError:
        fence = None
    names = sorted(name for name in os.listdir(directory) if re.fullmatch(r'Knowledge-[0-9a-f]{24}\.md', name))
    return state, fence, names


@contextmanager
def _database(path, *, writable=False):
    # Never immutable=1: that would hide settings present only in a WAL.
    conn = sqlite3.connect(path.as_uri() + ('?mode=rw' if writable else '?mode=ro'), uri=True)
    try:
        conn.execute('PRAGMA trusted_schema=OFF')
        if not writable:
            conn.execute('PRAGMA query_only=ON')
        yield conn
    finally:
        conn.close()


def _database_preflight(conn, root, names, *, ready=True):
    if ready:
        batch._require(conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'delete')
    batch._require(conn.execute('PRAGMA quick_check').fetchall() == [('ok',)])
    settings = dict(conn.execute('SELECT key,value FROM settings'))
    batch._require(settings.get('api_base_url') in (None, 'https://api.openai.com/v1')
                   and settings.get('api_model') in (None, 'text-embedding-3-small')
                   and settings.get('embedding_model') == 'local:' + MODEL
                   and settings.get('embedding_dim') == '384'
                   and settings.get('vault_path') == str(root))
    paths = [row[0] for row in conn.execute('SELECT path FROM notes')]
    batch._require(len(paths) == len(names) and set(paths) == set(names))
    batch._require(conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding_status = 'failed'").fetchone()[0] == 0)
    batch._require(conn.execute('SELECT COUNT(*) FROM chunks WHERE note_id NOT IN (SELECT id FROM notes)').fetchone()[0] == 0)
    batch._require(conn.execute('SELECT COUNT(DISTINCT note_id) FROM chunks').fetchone()[0] == len(names))
    return settings


def _normalize(path, root, names, *, promote_to=None):
    sidecars = {}
    for suffix in ('-wal', '-shm'):
        target = Path(str(path) + suffix)
        if target.exists():
            sidecars[suffix] = vault._identity(target.lstat())
    with _database(path, writable=True) as conn:
        _database_preflight(conn, root, names, ready=False)
        if promote_to is not None:
            # Keep metadata writes inside the registered WAL family; a crash
            # must not leave an unregistered rollback-journal derivative.
            batch._require(conn.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal')
            conn.execute("UPDATE settings SET value=? WHERE key='vault_path'", (str(promote_to),))
            conn.commit()
        checkpoint = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        batch._require(checkpoint[0] == 0)
        batch._require(conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() == 'delete')
        _database_preflight(conn, root if promote_to is None else promote_to, names)
    with vault._directory(path.parent) as directory:
        for suffix, identity in sidecars.items():
            if Path(str(path) + suffix).exists():
                vault._unlink_owned(directory, path.name + suffix, identity)
    batch._require(not any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm')))
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ready(directory, generation):
    row = lifecycle._artifacts(directory).get(lifecycle.INDEX)
    batch._require(row is not None and row['phase'] == 'ready' and row['generation'] == generation)
    lifecycle._check_index(directory, row)
    batch._require(not any(name in os.listdir(directory) for name in lifecycle.INDEX_FILES[1:]))
    return row


def _result(result, rows):
    batch._require(type(result) is dict and type(result.get('path')) is str and result['path'] in rows
                   and type(result.get('title')) is str and type(result.get('score')) in (int, float)
                   and math.isfinite(result['score']))
    for key in ('tags', 'aliases'):
        batch._require(type(result.get(key)) is list and all(type(value) is str for value in result[key]))
    if 'snippet' in result:
        batch._require(type(result['snippet']) is str)
    if 'previewAnchors' in result:
        batch._require(type(result['previewAnchors']) is list)
        for anchor in result['previewAnchors']:
            batch._require(type(anchor) is dict and anchor.get('kind') in ('bm25', 'semantic')
                           and (anchor.get('headingPath') is None or type(anchor['headingPath']) is str)
                           and type(anchor.get('matchText')) is str)
            for key in ('charStart', 'charEnd'):
                batch._require(key in anchor and (anchor[key] is None or type(anchor[key]) is int and anchor[key] >= 0))
    result['reference'] = rows[result['path']]['reference']
    result['citation'] = rows[result['path']]['citation']


def reindex(config, binding: dict) -> dict:
    with _refused('reindex'):
        config = sync._config(config)
        _validate_binding(binding, config)
        _cache(binding)
        with vault._publication_lock(config.vault, required=True, expose_lock=True) as lease:
            directory, lock = lease
            with sync._curated_reader(config, publication_lease=lease) as (reader, _):
                _validate_binding(binding, config)
                rows = _rows(reader)
                generation = collection_generation(reader, binding)
                source_stamp = _stamp(config, directory)
                inputs = _inputs(directory, rows)
                lifecycle.purge_owned_index(directory)
                members = [{'scope': 'build', 'name': name} for name in rows]
                try:
                    lifecycle.register_owned_index(directory, generation, 'building', members)
                    build_path = config.vault / lifecycle.INDEX
                    row = lifecycle._artifacts(directory)[lifecycle.INDEX]
                    with lifecycle._build_directory(directory, row['build']) as build:
                        for name, raw in inputs.items():
                            vault._write_private(build, name, raw)
                        os.fsync(build)
                    lifecycle.register_owned_index(directory, generation, 'building', members)
                    _validate_binding(binding, config)
                    _cache(binding)
                    _run_child([binding['cli_path'], 'reindex'], build_path,
                               _environment(binding, build_path), lock, REINDEX_TIMEOUT)
                    _validate_binding(binding, config)
                    _cache(binding)
                    batch._require(collection_generation(reader, binding) == generation)
                    row = lifecycle._artifacts(directory)[lifecycle.INDEX]
                    lifecycle._check_index(directory, row, mutable=True)
                    _normalize(build_path / lifecycle.INDEX_DB, build_path, rows, promote_to=config.vault)
                    batch._require(_stamp(config, directory) == source_stamp and _inputs(directory, rows) == inputs)
                    vault._verify_publication_lease(config.vault, lease, exclusive=True)
                    lifecycle.register_owned_index(directory, generation, 'building', members)
                    row = lifecycle._artifacts(directory)[lifecycle.INDEX]
                    with lifecycle._build_directory(directory, row['build']) as build:
                        batch._require(lifecycle.INDEX_DB not in os.listdir(directory))
                        os.rename(lifecycle.INDEX_DB, lifecycle.INDEX_DB, src_dir_fd=build, dst_dir_fd=directory)
                        for name in inputs:
                            member = next(item for item in row['members'] if item['name'] == name)
                            vault._unlink_owned(build, name, tuple(member['info']['identity']))
                        batch._require(os.listdir(build) == [])
                        os.fsync(build)
                    batch._require(lifecycle._ownership(os.stat(lifecycle.INDEX, dir_fd=directory, follow_symlinks=False)) == row['build'])
                    os.rmdir(lifecycle.INDEX, dir_fd=directory)
                    os.fsync(directory)
                    lifecycle.register_owned_index(directory, generation, 'ready', [{'scope': 'vault', 'name': lifecycle.INDEX_DB}])
                except BaseException:
                    lifecycle.purge_owned_index(directory)
                    raise
            return {'index_state': 'ready', 'generation': generation, 'total': len(rows)}


def search(config, binding: dict, query: str, options: dict | None = None) -> list[dict]:
    with _refused('search'):
        config = sync._config(config)
        _validate_binding(binding, config)
        batch._require(type(query) is str and len(query) <= 512
                       and not any(ord(char) < 32 for char in query))
        with vault._publication_lock(config.vault, required=True, expose_lock=True) as lease:
            directory, lock = lease
            with sync._curated_reader(config, publication_lease=lease) as (reader, _):
                _validate_binding(binding, config)
                rows = _rows(reader)
                argv, limit = _options(options, rows)
                if not query.strip() and (options is None or 'notePath' not in options):
                    results = []
                else:
                    generation = collection_generation(reader, binding)
                    _ready(directory, generation)
                    path = config.vault / lifecycle.INDEX_DB
                    with _database(path) as conn:
                        _database_preflight(conn, config.vault, rows)
                    _cache(binding)
                    lifecycle.register_owned_index(directory, generation, 'inflight', [{'scope': 'vault', 'name': lifecycle.INDEX_DB}])
                    try:
                        raw = _run_child([binding['cli_path'], '--db', str(path), 'search', *argv, '--json', '--', query],
                                         config.vault, _environment(binding, config.vault), lock, SEARCH_TIMEOUT)
                        _validate_binding(binding, config)
                        _cache(binding)
                        batch._require(collection_generation(reader, binding) == generation)
                        row = lifecycle._artifacts(directory)[lifecycle.INDEX]
                        lifecycle._check_index(directory, row, mutable=True)
                        _normalize(path, config.vault, rows)
                        lifecycle.register_owned_index(directory, generation, 'ready', [{'scope': 'vault', 'name': lifecycle.INDEX_DB}])
                        results = _json(raw)
                        batch._require(type(results) is list and len(results) <= limit)
                        for result in results:
                            _result(result, rows)
                    except BaseException:
                        lifecycle.purge_owned_index(directory)
                        raise
            return results


def status(config, binding: dict) -> dict:
    with _refused('status'):
        config = sync._config(config)
        _validate_binding(binding, config)
        result = {'api_base_url': None, 'active_model': 'local:' + MODEL, 'version': VERSION,
                  'index_state': 'absent'}
        with vault._publication_lock(config.vault, shared=True, required=True, expose_lock=True, lifecycle=True) as lease:
            directory, _ = lease
            family = lifecycle._artifacts(directory).get(lifecycle.INDEX)
            source_state = lifecycle._load(directory)
            if (family is not None and family['phase'] != 'ready'
                    or source_state is not None and source_state['pending'] is not None):
                result['index_state'] = 'recovery_required'
                return result
            with sync._curated_reader(config, publication_lease=lease) as (reader, _):
                row = lifecycle._artifacts(directory).get(lifecycle.INDEX)
                if row is None:
                    batch._require(not any(name in os.listdir(directory) for name in lifecycle.INDEX_FILES))
                else:
                    result['index_state'] = 'ready' if row['generation'] == collection_generation(reader, binding) else 'stale'
                    lifecycle._check_index(directory, row)
                    if result['index_state'] == 'stale':
                        return result
                    with _database(config.vault / lifecycle.INDEX_DB) as conn:
                        settings = _database_preflight(conn, config.vault, _rows(reader))
                        for key, sql in (
                            ('total', 'SELECT COUNT(*) FROM notes'),
                            ('indexed', 'SELECT COUNT(DISTINCT note_id) FROM chunks'),
                            ('chunks', 'SELECT COUNT(*) FROM chunks'),
                            ('failed_chunks', "SELECT COUNT(*) FROM chunks WHERE embedding_status = 'failed'"),
                            ('links', 'SELECT COUNT(*) FROM links')):
                            result[key] = conn.execute(sql).fetchone()[0]
                        result.update(notes_without_chunks=result['total'] - result['indexed'],
                            model=settings['embedding_model'], embedding_dim=int(settings['embedding_dim']),
                            last_indexed=settings.get('last_indexed'))
        return result


def _safe_id(value):
    return (type(value) is int and -(2 ** 53) < value < 2 ** 53
            or type(value) is str and len(value) <= 128
            and not any(ord(char) < 32 or 0xd800 <= ord(char) <= 0xdfff for char in value))


def serve(config, binding: dict, input_stream, output_stream) -> int:
    with _refused('configuration'):
        config = sync._config(config)
        _validate_binding(binding, config)
    def emit(value):
        output_stream.write(json.dumps(value, ensure_ascii=False) + '\n')
        output_stream.flush()
    emit({'ready': True})
    while True:
        line = input_stream.readline(MAX_LINE + 1)
        if not line:
            return 0
        identifier = None
        try:
            if len(line.encode('utf-8')) > MAX_LINE or not line.endswith('\n'):
                while line and not line.endswith('\n'):
                    line = input_stream.readline(MAX_LINE + 1)
                raise ValueError('request length')
            request = _json(line)
            batch._require(type(request) is dict and _safe_id(request.get('id')))
            identifier = request['id']
            if request.get('action') == 'status':
                batch._require(set(request) == {'id', 'action'})
                emit({'id': identifier, 'status': status(config, binding)})
            else:
                batch._require(set(request) <= {'id', 'query', 'options'} and 'query' in request)
                emit({'id': identifier, 'results': search(config, binding, request['query'], request.get('options'))})
        except Exception:
            emit({'id': identifier, 'error': 'hybrid request refused'})
