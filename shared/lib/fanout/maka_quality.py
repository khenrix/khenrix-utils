"""Six fixed original Matrix ballots with native capture and no replenishment."""
from __future__ import annotations

import datetime
import math
import os
from pathlib import Path
import re
import time

from components.maka.scripts import harbor_judge_capture as native
from . import maka_admission, maka_capture
from .evaluation import JudgeTransportError


PROVIDERS = ('claude', 'codex', 'agy')
ORDERS = ('candidate-first', 'baseline-first')
CLEAN_FIELDS = {'schema', 'status', 'invocation_id', 'pid', 'pgid', 'returncode',
    'timeout_seconds', 'cleanup_seconds', 'output_limit_bytes', 'input_sha256',
    'term_count', 'capture_complete', 'cleanup', 'stdout', 'stderr', 'native_queries', 'seconds'}
COMPLETE_FILES = {'request.bin', 'reservation.json', 'launch.json', 'live.json', 'sealed.json',
                  'process.json', 'stdout.bin', 'stderr.bin'}


def _origin(root, boundary):
    from scripts import fanout_container_certify as source
    receipt = source._read_private_receipt(root)
    native._require(receipt.get('mode') == 'live' and receipt.get('private_run_root') == str(root)
                    and tuple(source.PROVIDERS) == PROVIDERS)
    native._require(all(receipt['providers'][provider][route].get('passed') is True
                        for provider in PROVIDERS for route in ('initial', 'resume')))
    answers = source._authenticated_quality_answers(root, receipt)
    raw = native._read(root / 'quality-attempt.json')
    attempt = native._json(raw, {'approval_reference', 'observation_date',
        'source_receipt_sha256', 'boundary_sha256'})
    native._require(attempt['source_receipt_sha256'] == native._sha(source.canonical_json(receipt))
                    and attempt['boundary_sha256'] == boundary
                    and isinstance(attempt['approval_reference'], str)
                    and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}', attempt['approval_reference']))
    observed = datetime.date.fromisoformat(attempt['observation_date'])
    native._require(observed.isoformat() == attempt['observation_date'] and observed <= datetime.date.today())
    requests = []
    for provider in PROVIDERS:
        for order, suffix in zip(ORDERS, ('AB', 'BA'), strict=True):
            request = source._quality_judge_request(provider, answers[provider], order)
            first, second = ((request.candidate_answer, request.baseline_answer) if order == ORDERS[0]
                             else (request.baseline_answer, request.candidate_answer))
            payload = {'panel_id': request.panel_id, 'criterion': request.criterion,
                'answer_a': first, 'answer_b': second, 'expected_boundary_sha256': boundary}
            encoded = native._encoded(payload)
            native._require(len(encoded) <= 131072)
            native._prompt_digest(encoded, 'fanout-judge-' + '0' * 32)
            requests.append((provider, order, provider + '-' + suffix, request.case_id, encoded))
    return raw, native._sha(source.canonical_json(receipt)), requests


def _snapshot(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class QualityCaptures:
    """Allocation and reopening use only fixed existing original/native readers."""

    def __init__(self, root, launcher, boundary):
        self.root, self.launcher, self.boundary = Path(root), Path(launcher), boundary
        self.blocked, self.next, self.completed = True, 0, []
        self.attempt, self.receipt_sha256, self.requests = _origin(self.root, boundary)
        self.closure = maka_admission.admit_installed(self.launcher, boundary)
        native._require(_origin(self.root, boundary) == (self.attempt, self.receipt_sha256, self.requests))
        parent = native._directory(self.root, 0o700)
        try:
            self.private_identity = native._identity(os.fstat(parent))
            os.mkdir('maka-captures', 0o700, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)
        self.capture_root = self.root / 'maka-captures'
        self.reservations = [native.reserve(self.capture_root, item[2], item[4], self.closure)
                             for item in self.requests]
        owned = native._directory(self.capture_root, 0o700)
        try:
            self.capture_identity = native._identity(os.fstat(owned))
            self.plan = self._plan()
            self.plan_raw = native._encoded(self.plan)
            native._write_at(owned, 'plan.json', self.plan_raw)
        finally:
            os.close(owned)
        self.blocked = False

    def _plan(self):
        return {'schema': 'fanout-maka-quality-captures-v1', 'private_run_root': str(self.root),
            'private_identity': self.private_identity, 'capture_identity': self.capture_identity,
            'quality_attempt_sha256': native._sha(self.attempt), 'source_receipt_sha256': self.receipt_sha256,
            'closure_sha256': native._sha(native._encoded(self.closure)), 'ballots': [
                {'provider': provider, 'order': order, 'allocation': allocation, 'case_id': case_id,
                 'input_sha256': native._sha(raw), 'invocation_id': reservation['invocation_id'],
                 'reservation_sha256': native._sha(native._encoded(reservation))}
                for (provider, order, allocation, case_id, raw), reservation
                in zip(self.requests, self.reservations, strict=True)]}

    def _current(self):
        native._require(_origin(self.root, self.boundary) == (self.attempt, self.receipt_sha256, self.requests))
        native._require(maka_admission.admit_installed(self.launcher, self.boundary) == self.closure)
        parent = native._directory(self.root, 0o700)
        owned = native._directory(self.capture_root, 0o700)
        try:
            native._require(native._identity(os.fstat(parent)) == self.private_identity
                            and native._identity(os.fstat(owned)) == self.capture_identity
                            and native._read_at(owned, 'plan.json', mode=0o400) == self.plan_raw
                            and set(os.listdir(owned)) == {'plan.json', *(item[2] for item in self.requests)})
            for index, reservation in enumerate(self.reservations):
                current, _ = native._reservation(reservation['capture_path'])
                native._require(current == reservation)
                names = set(os.listdir(reservation['capture_path']))
                native._require(names == (COMPLETE_FILES if index < len(self.completed)
                                          else {'request.bin', 'reservation.json'}))
        finally:
            os.close(parent)
            os.close(owned)

    def _reopen(self, index, expected=None):
        reservation = self.reservations[index]
        root = Path(reservation['capture_path'])
        owned = native._directory(root, 0o700)
        try:
            names = tuple(sorted(COMPLETE_FILES))
            before = {name: os.stat(name, dir_fd=owned, follow_symlinks=False) for name in names}
            raw = native._read_at(owned, 'process.json', mode=0o600)
            record = native._json(raw, CLEAN_FIELDS)
            native._require(record['schema'] == 'fanout-harbor-process-capture-v1'
                and record['status'] == 'completed-clean' and record['capture_complete'] is True
                and record['returncode'] == 0 and type(record['returncode']) is int
                and record['term_count'] == 0 and type(record['term_count']) is int
                and record['timeout_seconds'] == 600 and record['cleanup_seconds'] == 45
                and record['output_limit_bytes'] == 8 * 1024 * 1024
                and record['input_sha256'] == reservation['input_sha256']
                and record['invocation_id'] == reservation['invocation_id']
                and type(record['pid']) is int and record['pid'] > 0 and record['pgid'] == record['pid']
                and isinstance(record['seconds'], (float, int)) and math.isfinite(record['seconds'])
                and 0 <= record['seconds'] <= 645
                and record['cleanup'] == {'proven': True, 'process_group_absent': True,
                    'native_absent': True, 'staging_absent': True}
                and isinstance(record['native_queries'], list) and raw == native._encoded(record))
            streams = {}
            for label in ('stdout', 'stderr'):
                streams[label] = native._read_at(owned, label + '.bin', mode=0o600, limit=8 * 1024 * 1024)
                native._require(record[label] == {'bytes': len(streams[label]),
                                                 'sha256': native._sha(streams[label])})
            native._require(len(streams['stdout']) + len(streams['stderr']) <= 8 * 1024 * 1024)
            adapter = maka_capture.NativeHarborCapture(reservation)
            adapter.deadline = time.monotonic() + 45
            adapter.cleanup(record['pid'])
            response = native.reopen_native_seal(reservation, streams['stdout'], current_closure=self.closure)
            adapter._remaining()
            pointer = native._read_at(owned, 'sealed.json', mode=0o400)
            after = {name: os.stat(name, dir_fd=owned, follow_symlinks=False) for name in names}
            native._require(all(_snapshot(before[name]) == _snapshot(after[name]) for name in names))
            current, _ = native._reservation(root)
            native._require(current == reservation)
            retained = {'status_sha256': native._sha(raw),
                'files': {name: list(_snapshot(after[name])) for name in names},
                'sealed_pointer_sha256': native._sha(pointer), 'result_sha256': native._sha(native._encoded(response))}
            native._require(expected is None or retained == expected)
            return retained, response, record
        finally:
            os.close(owned)

    def judge(self, case_id, payload):
        native._require(not self.blocked and self.next < 6)
        self.blocked = True
        self._current()
        for index, retained in enumerate(self.completed):
            self._reopen(index, retained)
        request = self.requests[self.next]
        native._require(case_id == request[3] and native._encoded(payload) == request[4])
        index = self.next
        self.next += 1
        record, response = maka_capture.capture_reserved(self.reservations[index])
        native._require(response is not None)
        retained, actual, persisted = self._reopen(index)
        native._require(record == persisted and response == actual)
        self.completed.append(retained)
        self._current()
        self.blocked = False
        return actual

    def finish(self):
        native._require(not self.blocked and self.next == 6 and len(self.completed) == 6)
        self.blocked = True
        self._current()
        for index, retained in enumerate(self.completed):
            self._reopen(index, retained)
        self._current()
        return native._sha(native._encoded({'plan_sha256': native._sha(self.plan_raw),
                                            'ballots': self.completed}))


def reopen_quality_captures(root, boundary, ballots):
    """Read the actual completed six-file/native joins; never allocate or launch."""
    try:
        native._require(type(ballots) is list and len(ballots) == 6)
        current = object.__new__(QualityCaptures)
        current.root, current.boundary = Path(root), boundary
        current.capture_root = current.root / 'maka-captures'
        current.attempt, current.receipt_sha256, current.requests = _origin(current.root, boundary)
        current.plan_raw = native._read(current.capture_root / 'plan.json', mode=0o400)
        plan = native._json(current.plan_raw, {'schema', 'private_run_root', 'private_identity',
            'capture_identity', 'quality_attempt_sha256', 'source_receipt_sha256', 'closure_sha256', 'ballots'})
        current.plan = plan
        current.launcher = Path(maka_admission.installer.canonical_layout().wrapper)
        current.closure = maka_admission.admit_installed(current.launcher, boundary)
        current.private_identity, current.capture_identity = plan['private_identity'], plan['capture_identity']
        current.reservations = [native._reservation(current.capture_root / item[2])[0] for item in current.requests]
        native._require(len(current.reservations) == 6 and all(item['closure'] == current.closure for item in current.reservations)
                        and plan == current._plan() and current.plan_raw == native._encoded(plan))
        current.completed = []
        for index, ballot in enumerate(ballots):
            native._require(type(ballot) is dict and ballot.get('case_id') == current.requests[index][3]
                            and ballot.get('order') == current.requests[index][1])
            retained, response, _ = current._reopen(index)
            native._require(native._encoded({name: ballot[name] for name in native.RESULT_FIELDS})
                            == native._encoded(response))
            current.completed.append(retained)
        current.next, current.blocked = 6, False
        return current.finish()
    except Exception:
        raise JudgeTransportError('Maka complete capture inventory is unproven') from None
