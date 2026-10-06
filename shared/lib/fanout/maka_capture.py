"""Harbor-only capture of an already admitted native reservation.

This seam does not admit an installed consumer or issue quality allocations.
The existing quality caller must establish those before invoking it. Neither
process exit nor model stdout can replace the original native seal.
"""
from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import shlex
import socket
import stat
import subprocess
import tempfile
import time
from urllib.parse import quote

from components.maka.scripts import harbor_judge_capture as native
from . import process as runner
from .container_source import runtime_source_digest


TIMEOUT_SECONDS = 600
CLEANUP_SECONDS = 45
OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
POLL_SECONDS = 0.2
ENVIRONMENT = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'MAKA_JUDGE_PAID_RUN': '1'}
_UNREAPED = {}


def _save(stream, record):
    raw = native._encoded(record)
    native._require(len(raw) <= native.LIMIT)
    stream.seek(0)
    stream.write(raw)
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def group_present(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _stream_state(stream):
    info = os.fstat(stream.fileno())
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _stream_digest(stream, deadline):
    before = os.fstat(stream.fileno())
    remaining = before.st_size
    digest = hashlib.sha256()
    stream.seek(0)
    while remaining:
        native._require(time.monotonic() < deadline)
        chunk = stream.read(min(65536, remaining))
        native._require(bool(chunk))
        digest.update(chunk)
        remaining -= len(chunk)
    snapshot = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    native._require(snapshot == _stream_state(stream))
    return digest.hexdigest(), snapshot


def other_group_members(pgid, deadline):
    remaining = deadline - time.monotonic()
    native._require(remaining > 0)
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,pgid='],
        env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'},
        capture_output=True, timeout=min(2, remaining), check=False)
    native._require(result.returncode == 0 and len(result.stdout) <= 1048576
                    and len(result.stderr) <= 16384)
    rows = [tuple(map(int, line.split())) for line in result.stdout.splitlines() if line.strip()]
    native._require(all(len(row) == 2 and all(value >= 0 for value in row) for row in rows))
    return [pid for pid, group in rows if group == pgid and pid != pgid]


class NativeHarborCapture:
    """Fixed selected native metadata; no caller-supplied ownership callbacks."""

    def __init__(self, reservation):
        self.reservation, _ = native._reservation(reservation['capture_path'])
        native._require(self.reservation == reservation)
        self.closure = self.reservation['closure']
        self.live = None
        self.live_raw = None
        self.deadline = None
        self.calls = []
        self.socket_identity = None
        self._current()

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        native._require(remaining > 0)
        return min(10, remaining)

    def _current(self):
        closure = self.closure
        native._require(runtime_source_digest() == closure['public_source_sha256'])
        measured = native.observe_closure(closure['component_root'], closure['runtime_root'],
            closure['install_receipt_path'], closure['launcher_path'],
            public_source_sha256=closure['public_source_sha256'],
            boundary_sha256=closure['boundary_sha256'],
            controller_image_id=closure['controller_image_id'], egress_image_id=closure['egress_image_id'])
        native._require(measured == closure)
        if self.deadline is not None:
            self._remaining()
        return measured

    def observe(self):
        owned = native._directory(self.reservation['capture_path'], 0o700)
        try:
            try:
                raw = native._read_at(owned, 'live.json', mode=0o400)
            except FileNotFoundError:
                native._require(self.live is None)
                return
        finally:
            os.close(owned)
        live = native._json(raw, native.LIVE_FIELDS)
        native.validate_live(self.reservation, live, live['containers'])
        native._require(self.live_raw is None or self.live_raw == raw)
        self.live, self.live_raw = live, raw

    def _docker(self, arguments):
        # The component's own mise file owns the pinned Docker runtime.
        mise = self._mise_bin()
        host = 'unix://' + str(self._socket_path())
        environment = {'HOME': str(Path.home()), 'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
                       'DOCKER_HOST': host, 'MISE_AUTO_INSTALL': '0'}
        result = subprocess.run((str(mise), '-C', self.closure['component_root'], 'exec', '--',
                                 '/usr/bin/env', '-u', 'DOCKER_CONTEXT', '-u', 'DOCKER_CONFIG',
                                 '-u', 'DOCKER_TLS_VERIFY', '-u', 'DOCKER_CERT_PATH',
                                 'DOCKER_HOST=' + host, 'docker',
                                 *arguments), env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=self._remaining(), check=False)
        self.calls.append({'kind': arguments[1], 'selector': arguments[-1], 'returncode': result.returncode,
                           'stdout_bytes': len(result.stdout), 'stdout_sha256': native._sha(result.stdout)})
        native._require(result.returncode == 0 and len(result.stdout) <= native.LIMIT)
        self._remaining()
        return result.stdout

    def _mise_bin(self):
        launcher = Path(self.closure['launcher_path'])
        raw = native._read(launcher, mode=stat.S_IMODE(launcher.lstat().st_mode))
        native._require(native._sha(raw) == self.closure['launcher_sha256'])
        assignments = [line.removeprefix('mise_bin=') for line in raw.decode().splitlines()
                       if line.startswith('mise_bin=')]
        native._require(len(assignments) == 1)
        words = shlex.split(assignments[0])
        native._require(len(words) == 1 and words[0] in {
            str(Path.home() / '.local/bin/mise'), '/opt/homebrew/bin/mise', '/usr/local/bin/mise'})
        return words[0]

    def _socket_path(self):
        path = (Path.home() / '.colima/maka-amd64/docker.sock').resolve(strict=True)
        info = path.lstat()
        identity = native._identity(info)
        native._require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
                        and (self.socket_identity is None or self.socket_identity == identity))
        self.socket_identity = identity
        return path

    def _daemon_status(self, endpoint):
        path = self._socket_path()
        connection = http.client.HTTPConnection('localhost', timeout=self._remaining())
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(self._remaining())
        try:
            connection.sock.connect(str(path))
            connection.request('GET', endpoint)
            status = connection.getresponse().status
            # Only the status is selected; container environment/body is never read.
            self.calls.append({'kind': 'absence', 'endpoint': endpoint, 'status': status})
            self._remaining()
            return status
        finally:
            connection.close()

    def term_judge(self, record):
        self.observe()
        native._require(self.live is not None)
        judge = self.live['containers']['judge']['id']
        status = self._daemon_status('/containers/' + judge + '/json')
        if status == 404:
            record['judge_term'] = {'judge_id': judge, 'already_absent': True}
            return
        native._require(status == 200)
        self._current()
        current = {role: native.decode_native_inspect(self._docker(
            ('container', 'inspect', '--format', native.INSPECT_PROJECTION, item['id'])), item['id'])
            for role, item in self.live['containers'].items()}
        native.validate_live(self.reservation, self.live, current)
        self._current()
        self._docker(('container', 'kill', '--signal', 'TERM', judge))
        record['judge_term'] = {'judge_id': judge, 'identity_revalidated': True, 'sent': True}

    def cleanup(self, pgid):
        self.observe()
        native._require(self.live is not None and not group_present(pgid))
        self._current()
        for item in self.live['containers'].values():
            native._require(self._daemon_status('/containers/' + item['id'] + '/json') == 404)
        native._require(self._daemon_status('/volumes/' + quote(self.live['secret_volume'], safe='')) == 404)
        stage = Path(self.live['staging']['path'])
        native._require(not stage.exists() and not stage.is_symlink())
        return {'proven': True, 'process_group_absent': True, 'native_absent': True, 'staging_absent': True}


def capture_reserved(reservation):
    """Observe one existing native reservation; incomplete state never returns a ballot.

    No timeout, environment, launcher vector, Ownership or cleanup callback can
    be supplied. The quality caller still owns installed admission and sequencing.
    """
    ownership = NativeHarborCapture(reservation)
    root = Path(reservation['capture_path'])
    root_fd = native._directory(root, 0o700)
    process = None
    anchored = reaped = False
    cleanup_deadline = None
    response = None
    cancellations = []
    handlers = {}
    began = time.monotonic()
    record = {'schema': 'fanout-harbor-process-capture-v1', 'status': 'attempt-reserved',
        'invocation_id': reservation['invocation_id'], 'pid': None, 'returncode': None,
        'timeout_seconds': TIMEOUT_SECONDS, 'cleanup_seconds': CLEANUP_SECONDS,
        'output_limit_bytes': OUTPUT_LIMIT_BYTES, 'input_sha256': reservation['input_sha256'],
        'term_count': 0, 'capture_complete': False, 'cleanup': {'proven': False}}
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        descriptor = os.open('process.json', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=root_fd)
        with os.fdopen(descriptor, 'w+b') as metadata:
            _save(metadata, record)
            for number in (signal.SIGINT, signal.SIGTERM):
                handlers[number] = signal.signal(number, lambda number, frame: cancellations.append(number))
            output_fd = os.open('stdout.bin', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600, dir_fd=root_fd)
            with os.fdopen(output_fd, 'w+b') as output:
                error_fd = os.open('stderr.bin', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                   0o600, dir_fd=root_fd)
                with os.fdopen(error_fd, 'w+b') as errors, tempfile.TemporaryFile(dir=root) as source, runner._exit_observer() as exited:
                    source.write(native._read_at(root_fd, 'request.bin', mode=0o400, limit=131072))
                    source.seek(0)

                    def cancel():
                        nonlocal cleanup_deadline
                        cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
                        ownership.deadline = cleanup_deadline
                        record['term_count'] = 1
                        record['group_term'] = {'pgid': process.pid, 'signal': int(signal.SIGTERM), 'sent': False}
                        try:
                            native._require(anchored and not reaped and os.getpgid(process.pid) == process.pid)
                            os.killpg(process.pid, signal.SIGTERM)
                            record['group_term']['sent'] = True
                            ownership.term_judge(record)
                        except Exception:
                            record.setdefault('failure', 'owned-term-or-native-identity-unproven')
                        _save(metadata, record)

                    try:
                        argv = (reservation['closure']['launcher_path'], 'judge-harbor', '-',
                                '--capture-destination', str(root))
                        process = subprocess.Popen(argv, stdin=source, stdout=output, stderr=errors,
                            cwd=root, env=ENVIRONMENT, start_new_session=True)
                        record.update(pid=process.pid, pgid=process.pid, status='running')
                        anchored = os.getpgid(process.pid) == process.pid and process.pid != os.getpgrp()
                        native._require(anchored)
                        exited(process.pid)
                        _save(metadata, record)
                        deadline = began + TIMEOUT_SECONDS
                        ownership.deadline = deadline
                        while True:
                            now = time.monotonic()
                            if cleanup_deadline is not None and now >= cleanup_deadline:
                                break
                            if now >= deadline:
                                record.setdefault('cancel_reason', 'timeout')
                            if os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size > OUTPUT_LIMIT_BYTES:
                                record.setdefault('cancel_reason', 'output-limit')
                            if cancellations:
                                record.setdefault('cancel_reason', 'signal')
                                record['cancellation_signals'] = cancellations[:]
                            observed_exit = exited(process.pid)
                            members = (other_group_members(process.pid, cleanup_deadline or deadline)
                                       if cleanup_deadline is not None or now < deadline else None)
                            if observed_exit and members == []:
                                record['returncode'] = process.wait(timeout=0)
                                reaped = True
                                break
                            if observed_exit and members:
                                record.setdefault('cancel_reason', 'process-exited-with-descendants')
                            if cleanup_deadline is None and 'cancel_reason' not in record:
                                ownership.observe()
                            if cleanup_deadline is None and 'cancel_reason' in record:
                                cancel()
                            time.sleep(min(POLL_SECONDS, max(0, (cleanup_deadline or deadline) - time.monotonic())))
                    except BaseException:
                        record.setdefault('failure', 'capture-error')
                        record.setdefault('cancel_reason', 'capture-error')
                        if anchored and not reaped and record['term_count'] == 0:
                            try:
                                cancel()
                            except Exception:
                                record.setdefault('failure', 'capture-persistence-error')
                        while anchored and not reaped and time.monotonic() < (cleanup_deadline or began):
                            try:
                                if exited(process.pid) and not other_group_members(process.pid, cleanup_deadline):
                                    record['returncode'] = process.wait(timeout=0)
                                    reaped = True
                                    break
                            except Exception:
                                record['cleanup_observation_error'] = True
                            time.sleep(min(POLL_SECONDS, max(0, cleanup_deadline - time.monotonic())))
                    finally:
                        output.flush()
                        errors.flush()
                        os.fsync(output.fileno())
                        os.fsync(errors.fileno())
                        group_absent = anchored and reaped and not group_present(process.pid)
                        streams = {'stdout': output, 'stderr': errors}

                        def namespace_current():
                            current, _ = native._reservation(root)
                            native._require(current == reservation)
                            native._require(native._identity(os.fstat(root_fd)) == reservation['capture_identity'])
                            for name, stream in [('process.json', metadata),
                                                 *((label + '.bin', stream) for label, stream in streams.items())]:
                                held = os.fstat(stream.fileno())
                                named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                                native._require(stat.S_ISREG(held.st_mode) and held.st_uid == os.getuid()
                                                and stat.S_IMODE(held.st_mode) == 0o600 and held.st_nlink == 1
                                                and native._identity(held) == native._identity(named))

                        complete = False
                        if group_absent:
                            try:
                                namespace_current()
                                complete = True
                            except Exception:
                                record.setdefault('failure', 'capture-file-identity-unproven')
                        record['capture_complete'] = complete
                        record['cleanup'] = {'proven': False, 'process_group_absent': bool(group_absent)}
                        if process is not None and not reaped:
                            # Popen.__del__ otherwise polls/reaps even though descendants may remain.
                            _UNREAPED[reservation['invocation_id']] = process
                            record['process_retained'] = True
                        if group_absent:
                            ownership.deadline = cleanup_deadline or time.monotonic() + CLEANUP_SECONDS
                            try:
                                record['cleanup'] = ownership.cleanup(process.pid)
                            except Exception:
                                record.setdefault('failure', 'native-cleanup-unproven')
                        digests, snapshots = {}, {}

                        def streams_current():
                            native._require(all(_stream_state(stream) == snapshots[label]
                                                for label, stream in streams.items()))

                        def incomplete(failure):
                            nonlocal response
                            response = None
                            record['capture_complete'] = False
                            record['failure'] = failure
                            for label, stream in streams.items():
                                record[label] = {'bytes': os.fstat(stream.fileno()).st_size, 'sha256': None}

                        if complete:
                            try:
                                for label, stream in streams.items():
                                    digests[label], snapshots[label] = _stream_digest(stream, ownership.deadline)
                                namespace_current()
                                streams_current()
                            except Exception:
                                complete = False
                                digests = {}
                                record['capture_complete'] = False
                                record.setdefault('failure', 'capture-stream-stability-unproven')
                        for label, stream in streams.items():
                            record[label] = {'bytes': os.fstat(stream.fileno()).st_size,
                                             'sha256': digests.get(label)}
                        if (complete and record['returncode'] == 0 and record['cleanup'].get('proven')
                                and 'cancel_reason' not in record and 'failure' not in record):
                            try:
                                closure = ownership._current()
                                output.seek(0)
                                raw = output.read(4097)
                                native._require(len(raw) <= 4096)
                                response = native.reopen_native_seal(reservation, raw, current_closure=closure)
                                ownership._remaining()
                                namespace_current()
                                streams_current()
                            except Exception:
                                response = None
                                record['failure'] = 'original-native-seal-unproven'
                        record['native_queries'] = ownership.calls
                        record['seconds'] = time.monotonic() - began
                        record['status'] = 'completed-clean' if response is not None else 'uncertain-no-retry'
                        try:
                            _save(metadata, record)
                            os.fsync(root_fd)
                            namespace_current()
                            if complete:
                                streams_current()
                            native._require(native._read_at(root_fd, 'process.json', mode=0o600)
                                            == native._encoded(record))
                            namespace_current()
                            if complete:
                                streams_current()
                        except Exception:
                            incomplete('capture-publication-unproven')
                            record['status'] = 'uncertain-no-retry'
                            # Keep the held inode; never repair a substituted named path.
                            _save(metadata, record)
                            os.fsync(root_fd)
        return record, response
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)
        os.close(root_fd)
