"""Real authenticated synthetic Matrix/storage; no provider/native executions."""
import hashlib
import importlib
import json
from pathlib import Path

import pytest

from test_fanout_container_certify import (
    certifier, company_manifest, complete_receipt, _completed_quality_root,
)
from test_fanout_maka_admission import installed


@pytest.fixture
def quality_root(certifier, complete_receipt, tmp_path, monkeypatch):
    root, _, _ = _completed_quality_root(certifier, complete_receipt, tmp_path, monkeypatch)
    attempt = {'approval_reference': 'synthetic-approved', 'observation_date': '2026-09-30',
        'source_receipt_sha256': hashlib.sha256(certifier.canonical_json(complete_receipt)).hexdigest(),
        'boundary_sha256': 'b' * 64}
    certifier._write_private(root / 'quality-attempt.json', certifier.canonical_json(attempt))
    return root


def test_fixed_constructor_requires_authenticated_original_matrix_before_allocation(quality_root, installed, monkeypatch):
    transport = importlib.import_module('shared.lib.fanout.maka_transport')
    admission, layout, _, _, calls = installed
    actual = importlib.import_module('shared.lib.fanout.maka_admission')
    monkeypatch.setattr(actual, '_metadata', admission._metadata)
    receipt = quality_root / 'receipt.json'
    value = json.loads(receipt.read_bytes())
    value['providers']['codex']['resume']['passed'] = False
    receipt.write_bytes(json.dumps(value).encode())
    with pytest.raises(ValueError):
        transport.HarborMakaTransport(launcher=layout.wrapper,
            no_tools_characterization_sha256='b' * 64, paid_authorized=True,
            quality_run_root=quality_root)
    assert not (quality_root / 'maka-captures').exists()
    assert calls == []


def test_fixed_constructor_reserves_exact_six_current_counterbalanced_inputs(quality_root, installed, monkeypatch):
    transport = importlib.import_module('shared.lib.fanout.maka_transport')
    admission, layout, _, _, calls = installed
    actual = importlib.import_module('shared.lib.fanout.maka_admission')
    monkeypatch.setattr(actual, '_metadata', admission._metadata)
    route = transport.HarborMakaTransport(launcher=layout.wrapper,
        no_tools_characterization_sha256='b' * 64, paid_authorized=True,
        quality_run_root=quality_root)
    root = quality_root / 'maka-captures'
    plan = json.loads((root / 'plan.json').read_bytes())
    assert len(plan['ballots']) == 6 and len({item['invocation_id'] for item in plan['ballots']}) == 6
    assert [item['allocation'] for item in plan['ballots']] == [
        provider + '-' + order for provider in ('claude', 'codex', 'agy') for order in ('AB', 'BA')]
    for item in plan['ballots']:
        request = json.loads((root / item['allocation'] / 'request.bin').read_bytes())
        candidate = 'completed synthetic ' + item['provider'] + ' answer'
        assert (request['answer_a'], request['answer_b']) == (
            (candidate, 'baseline synthetic answer') if item['order'] == 'candidate-first'
            else ('baseline synthetic answer', candidate))
        assert not (root / item['allocation'] / 'process.json').exists()
    assert len(calls) == 5
    with pytest.raises(ValueError):
        transport.HarborMakaTransport(launcher=layout.wrapper,
            no_tools_characterization_sha256='b' * 64, paid_authorized=True,
            quality_run_root=quality_root)
    with pytest.raises(ValueError):
        route.finish_captures()

@pytest.fixture
def live_route(quality_root, installed, monkeypatch, request):
    from contextlib import contextmanager
    import os
    transport = importlib.import_module('shared.lib.fanout.maka_transport')
    capture = importlib.import_module('shared.lib.fanout.maka_capture')
    admission, layout, runtime, receipt, calls = installed
    actual = importlib.import_module('shared.lib.fanout.maka_admission')
    monkeypatch.setattr(actual, '_metadata', admission._metadata)
    if getattr(request, 'param', None) == 'cli':
        (quality_root / 'quality-attempt.json').unlink()
        route = None
    else:
        route = transport.HarborMakaTransport(launcher=layout.wrapper,
            no_tools_characterization_sha256='b' * 64, paid_authorized=True, quality_run_root=quality_root)
    from components.maka import test_native_capture_join as native_fixture
    fixture = native_fixture.NativeCaptureJoinTests('test_exact_synthetic_native_join_returns_only_seven_fields')
    fixture.setUpClass()
    fixture.setUp()
    request.addfinalizer(fixture.doCleanups)
    state = {'launches': [], 'sealed': True}
    actual_popen = capture.subprocess.Popen

    class Process:
        pid = 42
        returncode = None

        def wait(self, timeout):
            assert timeout == 0
            self.returncode = 0
            return 0

        def poll(self):
            pytest.fail('the caller must not replace the nonreaping observer')

    def spawn(argv, **kwargs):
        if argv[0] == 'git':
            assert argv[1] == '-C'
            tail = tuple(argv[3:])
            ancestry = ('-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=' + os.devnull,
                        'rev-parse', '--absolute-git-dir')
            prefix = ('-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
                      '-c', 'core.hooksPath=' + os.devnull)
            assert tail == ancestry or (tail[:6] == prefix and (
                tail[6] in {'rev-parse', 'status', 'ls-files', 'ls-tree'} or
                tail[6] == 'cat-file' and tail[7] in {'-t', '-s', 'blob'}))
            return actual_popen(argv, **kwargs)
        root = Path(argv[-1])
        reservation, _ = capture.native._reservation(root)
        fixture.reservation, fixture.closure = reservation, reservation['closure']
        fixture.component, fixture.runtime, fixture.receipt, fixture.launcher = layout.component, runtime, receipt, layout.wrapper
        fixture.request = kwargs['stdin'].read()
        assert hashlib.sha256(fixture.request).hexdigest() == reservation['input_sha256']
        assert kwargs['env'] == capture.ENVIRONMENT and kwargs['start_new_session'] is True
        state['launches'].append(reservation['allocation'])
        token = reservation['invocation_id']
        fixture.evidence = layout.component / 'evidence/runs' / ('fixture-harbor-judge-' + token)
        fixture.evidence.mkdir(parents=True, mode=0o700)
        fixture.stage = layout.component / 'controller/.build' / ('harbor-judge-' + token)
        fixture.stage.mkdir(parents=True, mode=0o700)
        fixture.fifo = fixture.stage / 'input.fifo'
        os.mkfifo(fixture.fifo, 0o600)
        fixture.staged_receipt = fixture.stage / 'install-receipt.json'
        native_fixture.write(fixture.staged_receipt, receipt.read_bytes())
        decimal = str(int(token, 16))
        fixture.project, fixture.secret = 'maka-judge-' + decimal, 'maka-judge-secret-' + decimal
        fixture.containers = fixture.container_fixture()
        fixture.context_arguments = [str(root), str(layout.component), str(runtime), str(receipt), str(layout.wrapper),
            fixture.boundary, fixture.closure['controller_image_id'], fixture.closure['egress_image_id'],
            hashlib.sha256(fixture.request).hexdigest()]
        fixture.capture.native_context(fixture.context_arguments)
        if state['sealed']:
            fixture.complete_fixture()
            prompt = fixture.capture._prompt_digest(fixture.request, 'fanout-judge-' + token)

            def evidence(raw):
                value = json.loads(raw)
                value['relay_receipt']['prompt_sha256'] = prompt
                value['relay_receipt']['request_marker_sha256'] = hashlib.sha256(
                    ('maka:fanout-judge-' + token).encode()).hexdigest()
                return native_fixture.encoded(value)

            def runtime_record(raw):
                value = json.loads(raw)
                value['prompt_sha256'] = prompt
                value['session_id'] = 'fanout-judge-' + token
                return native_fixture.encoded(value)

            fixture.mutate_sealed('harbor-judge-evidence.json', evidence, reseal=True)
            fixture.mutate_sealed('capture-runtime.json', runtime_record, reseal=True)
        else:
            fixture.publish()
        kwargs['stdout'].write(fixture.stdout)
        kwargs['stderr'].write(b'complete synthetic caller stderr\n')
        return Process()

    @contextmanager
    def observer():
        yield lambda pid: True

    def checked_spawn(*args, **kwargs):
        try:
            return spawn(*args, **kwargs)
        except Exception as error:
            state['fixture_error'] = error
            raise

    monkeypatch.setattr(capture.subprocess, 'Popen', checked_spawn)
    monkeypatch.setattr(capture.runner, '_exit_observer', observer)
    monkeypatch.setattr(capture, 'other_group_members', lambda pgid, deadline: [])
    monkeypatch.setattr(capture.os, 'getpgid', lambda pid: 42)
    monkeypatch.setattr(capture.os, 'getpgrp', lambda: 99)
    monkeypatch.setattr(capture, 'group_present', lambda pgid: False)
    monkeypatch.setattr(capture.NativeHarborCapture, '_daemon_status', lambda self, endpoint: 404)
    return route, state


def _present(certifier, provider='claude', order='candidate-first'):
    return certifier._quality_judge_request(provider, 'completed synthetic ' + provider + ' answer', order)


def test_six_real_observer_and_original_seal_joins_gate_durable_result(live_route, certifier, quality_root):
    from shared.lib.fanout.evaluation import MakaJudgeAdapter
    from shared.lib.fanout.maka_quality import reopen_quality_captures
    route, state = live_route
    adapter = MakaJudgeAdapter(route)
    ballots = []
    for provider in ('claude', 'codex', 'agy'):
        for order in ('candidate-first', 'baseline-first'):
            try:
                ballot = adapter.judge(_present(certifier, provider, order))
                assert ballot['winner'] == 'A'
                ballots.append(ballot)
            except Exception:
                if 'fixture_error' in state:
                    raise state['fixture_error']
                raise
    assert state['launches'] == [provider + '-' + order
        for provider in ('claude', 'codex', 'agy') for order in ('AB', 'BA')]
    digest = route.finish_captures()
    assert len(digest) == 64 and reopen_quality_captures(quality_root, 'b' * 64, ballots) == digest
    with pytest.raises(ValueError):
        adapter.judge(_present(certifier))
    assert len(state['launches']) == 6


@pytest.mark.parametrize('damage', ['stderr', 'status', 'pointer', 'matrix-answer', 'late-stderr', 'late-pointer'])
def test_changed_completed_original_blocks_next_native_allocation(live_route, certifier, quality_root, damage, monkeypatch):
    from shared.lib.fanout.evaluation import MakaJudgeAdapter
    route, state = live_route
    adapter = MakaJudgeAdapter(route)
    adapter.judge(_present(certifier))
    first = quality_root / 'maka-captures/claude-AB'
    if damage.startswith('late-'):
        from shared.lib.fanout.maka_quality import native
        original = native.reopen_native_seal

        def reopen(*args, **kwargs):
            response = original(*args, **kwargs)
            path = first / ('stderr.bin' if damage == 'late-stderr' else 'sealed.json')
            mode = path.stat().st_mode & 0o777
            path.chmod(0o600)
            path.write_bytes(path.read_bytes() + b'late synthetic native reopen change\n')
            path.chmod(mode)
            return response

        monkeypatch.setattr(native, 'reopen_native_seal', reopen)
    else:
        path = {'stderr': first / 'stderr.bin', 'status': first / 'process.json',
                'pointer': first / 'sealed.json', 'matrix-answer': quality_root / 'answer-claude.bin'}[damage]
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b'changed synthetic retained original\n')
    with pytest.raises(ValueError):
        adapter.judge(_present(certifier, order='baseline-first'))
    with pytest.raises(ValueError):
        adapter.judge(_present(certifier, order='baseline-first'))
    assert state['launches'] == ['claude-AB']
    assert not (quality_root / 'maka-captures/claude-BA/process.json').exists()


def test_plausible_stdout_without_actual_original_seal_consumes_no_following_ballot(live_route, certifier, quality_root):
    from shared.lib.fanout.evaluation import MakaJudgeAdapter
    route, state = live_route
    state['sealed'] = False
    with pytest.raises(ValueError):
        MakaJudgeAdapter(route).judge(_present(certifier))
    with pytest.raises(ValueError):
        MakaJudgeAdapter(route).judge(_present(certifier))
    assert state['launches'] == ['claude-AB']
    assert not (quality_root / 'maka-captures/claude-BA/process.json').exists()


def test_unallocated_counterbalance_order_refuses_before_native_launch(live_route, certifier, quality_root):
    from shared.lib.fanout.evaluation import MakaJudgeAdapter
    route, state = live_route
    with pytest.raises(ValueError):
        MakaJudgeAdapter(route).judge(_present(certifier, order='baseline-first'))
    assert state['launches'] == []
    assert all(not path.exists() for path in (quality_root / 'maka-captures').glob('*/process.json'))


@pytest.mark.parametrize('live_route', ['cli'], indirect=True)
def test_existing_quality_caller_and_release_consumer_require_all_durable_native_captures(
        live_route, certifier, quality_root, installed):
    _, state = live_route
    _, layout, _, _, _ = installed
    assessment = certifier._quality_run(quality_root, allow_paid=True,
        approval_reference='synthetic-approved', observation_date='2026-09-30',
        launcher=layout.wrapper, boundary_sha256='b' * 64)
    assert len(assessment.ballots) == len(state['launches']) == 6
    quality = json.loads((quality_root / 'quality.json').read_bytes())
    assert len(quality['capture_inventory_sha256']) == 64
    release = importlib.import_module('scripts.fanout_release_evaluations')
    proof = release.maka_evidence(quality_root)
    release.validate_maka_evidence(proof, release.release_source_binding())
    status = quality_root / 'maka-captures/agy-BA/process.json'
    status.write_bytes(status.read_bytes() + b'changed synthetic durable status\n')
    with pytest.raises(ValueError):
        release.validate_maka_evidence(proof, release.release_source_binding())
    assert len(state['launches']) == 6


@pytest.mark.parametrize('field,replacement', [
    (None, None), ('winner', 'B'), ('auth_route', 'api-key-relay'),
    ('requested_model', 'gpt-6-sol'), ('observed_model', 'gpt-6-sol'),
    ('effort', 'high'), ('tool_calls', 1), ('tool_boundary_sha256', 'e' * 64),
], ids=['untouched', 'winner', 'auth-route', 'requested-model', 'observed-model',
        'effort', 'tool-calls', 'boundary'])
@pytest.mark.parametrize('live_route', ['cli'], indirect=True)
def test_original_native_response_fields_bind_reauthenticated_stored_ballot(
        live_route, certifier, quality_root, installed, field, replacement):
    from shared.lib.fanout.controller import write_evidence
    from shared.lib.fanout.maka_quality import reopen_quality_captures
    _, state = live_route
    _, layout, _, _, _ = installed
    certifier._quality_run(quality_root, allow_paid=True, approval_reference='synthetic-approved',
        observation_date='2026-09-30', launcher=layout.wrapper, boundary_sha256='b' * 64)
    release = importlib.import_module('scripts.fanout_release_evaluations')
    binding = release.release_source_binding()
    original = release.maka_evidence(quality_root)
    release.validate_maka_evidence(original, binding)
    capture_root = quality_root / 'maka-captures'
    originals = {path.relative_to(capture_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in capture_root.rglob('*') if path.is_file()}
    if field is not None:
        result = json.loads((quality_root / 'quality.json').read_bytes())
        result['ballots'][0][field] = replacement
        result['ballot_digest'] = hashlib.sha256(certifier.canonical_json(result['ballots'])).hexdigest()
        unsigned = {key: value for key, value in result.items()
            if key not in ('completion_evidence_name', 'completion_evidence_digest')}
        result['completion_evidence_name'], result['completion_evidence_digest'] = write_evidence(
            certifier._quality_controller(quality_root), 'container-maka-quality-complete',
            {'quality_sha256': hashlib.sha256(certifier.canonical_json(unsigned)).hexdigest()})
        (quality_root / 'quality.json').write_bytes(certifier.canonical_json(result))
        with pytest.raises(ValueError):
            reopen_quality_captures(quality_root, 'b' * 64, result['ballots'])
        with pytest.raises(ValueError):
            release.validate_maka_evidence(release.maka_evidence(quality_root), binding)
    else:
        release.validate_maka_evidence(release.maka_evidence(quality_root), binding)
    assert {path.relative_to(capture_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in capture_root.rglob('*') if path.is_file()} == originals
    assert reopen_quality_captures(quality_root, 'b' * 64, original['quality']['ballots']) == original['quality']['capture_inventory_sha256']
    assert len(state['launches']) == 6
