"""Real synthetic native files; simulated OS/selected metadata, never native calls."""
from contextlib import contextmanager
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import signal
import sys

import pytest

from components.maka import test_native_capture_join as native_fixture

PACKAGE = Path(__file__).resolve().parents[1] / 'shared/lib/fanout'
SPEC = importlib.util.spec_from_file_location('_fanout_maka_capture_contracts', PACKAGE / '__init__.py',
                                           submodule_search_locations=[str(PACKAGE)])
assert SPEC and SPEC.loader
package = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = package
SPEC.loader.exec_module(package)
module = importlib.import_module(f'{SPEC.name}.maka_capture')


@pytest.fixture
def setup(monkeypatch):
    fixture = native_fixture.NativeCaptureJoinTests('test_exact_synthetic_native_join_returns_only_seven_fields')
    fixture.setUpClass()
    fixture.setUp()
    capture_path = Path(fixture.reservation['capture_path'])
    (capture_path / 'launch.json').unlink()
    state = {'now': 0.0, 'exit_at': 0.4, 'descendants_until': 0.4,
             'returncode': 0, 'sealed': True, 'native_gone': False,
             'uncooperative': False, 'changed_mount': False, 'events': []}

    def remove_stage():
        if fixture.stage.exists():
            for path in fixture.stage.iterdir():
                path.unlink()
            fixture.stage.rmdir()

    class Process:
        pid = 42
        returncode = None

        def poll(self):
            pytest.fail('poll would release the unreaped leader identity')

        def wait(self, timeout):
            assert state['now'] >= state['exit_at']
            assert state['now'] >= state['descendants_until']
            state['events'].append(('reap', state['now']))
            self.returncode = state['returncode']
            return self.returncode

    process = Process()

    def popen(argv, **kwargs):
        state['events'].append(('launch', tuple(argv)))
        assert tuple(argv) == (str(fixture.launcher), 'judge-harbor', '-',
                               '--capture-destination', str(capture_path))
        assert kwargs['stdin'].read() == fixture.request
        assert kwargs['start_new_session'] is True
        assert kwargs['env'] == module.ENVIRONMENT
        fixture.capture.native_context(fixture.context_arguments)
        if state['sealed']:
            fixture.complete_fixture()
            state['native_gone'] = True
        else:
            fixture.publish()
        kwargs['stdout'].write(state.get('stdout', fixture.stdout))
        kwargs['stderr'].write(b'complete synthetic launcher stderr\n')
        if state.get('replace_stdout'):
            kwargs['stdout'].flush()
            (capture_path / 'stdout.bin').rename(capture_path / 'retired-stdout.bin')
            (capture_path / 'stdout.bin').write_bytes(fixture.stdout)
            (capture_path / 'stdout.bin').chmod(0o600)
        if state.get('change_after_spawn'):
            (fixture.component / 'controller/harbor_judge.mjs').write_bytes(b'changed after native publication')
        if state.get('replace_metadata'):
            (capture_path / 'process.json').rename(capture_path / 'retired-process.json')
            (capture_path / 'process.json').write_bytes(b'{}')
            (capture_path / 'process.json').chmod(0o600)
        if state.get('signal'):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        return process

    @contextmanager
    def observer():
        yield lambda pid: state['now'] >= state['exit_at']

    def members(pgid, deadline):
        assert pgid == 42 and deadline > state['now']
        assert process.returncode is None
        return [43] if state['now'] < state['descendants_until'] else []

    def term(pgid, number):
        assert pgid == 42 and number == signal.SIGTERM
        assert process.returncode is None
        state['events'].append(('group-term', state['now']))
        if not state['uncooperative']:
            state['exit_at'] = state['descendants_until'] = state['now'] + 0.4
            state['returncode'] = 143

    def docker(self, arguments):
        assert arguments[:2] in (('container', 'inspect'), ('container', 'kill'))
        if arguments[:2] == ('container', 'inspect'):
            assert arguments[2:4] == ('--format', fixture.capture.INSPECT_PROJECTION)
            item = next(item for item in fixture.containers.values() if item['id'] == arguments[4])
            item = json.loads(json.dumps(item))
            if state['changed_mount'] and item['id'] == fixture.containers['judge']['id']:
                item['mounts'][0]['Propagation'] = 'rshared'
            state['events'].append(('native-inspect', item['id']))
            if state.get('change_during_inspect'):
                (fixture.component / 'controller/harbor_judge.mjs').write_bytes(b'changed during selected inspect')
            return json.dumps(item).encode()
        assert arguments == ('container', 'kill', '--signal', 'TERM', fixture.containers['judge']['id'])
        state['events'].append(('native-term', arguments[-1]))
        state['native_gone'] = True
        remove_stage()
        return b''

    monkeypatch.setattr(module.runner, '_exit_observer', observer)
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    monkeypatch.setattr(module, 'other_group_members', members)
    monkeypatch.setattr(module.os, 'getpgid', lambda pid: 42)
    monkeypatch.setattr(module.os, 'getpgrp', lambda: 99)
    monkeypatch.setattr(module.os, 'killpg', term)
    monkeypatch.setattr(module, 'group_present', lambda pid: process.returncode is None)
    monkeypatch.setattr(module.time, 'monotonic', lambda: state['now'])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: state.update(now=state['now'] + seconds))
    monkeypatch.setattr(module, 'runtime_source_digest', lambda: fixture.closure['public_source_sha256'])
    monkeypatch.setattr(module.NativeHarborCapture, '_docker', docker)
    monkeypatch.setattr(module.NativeHarborCapture, '_daemon_status',
                        lambda self, endpoint: 404 if state['native_gone'] else 200)
    yield module, fixture, state, process
    module._UNREAPED.pop(fixture.reservation['invocation_id'], None)
    fixture.doCleanups()


def test_exact_native_seal_and_complete_streams_are_required_for_success(setup):
    module, fixture, state, _ = setup
    record, response = module.capture_reserved(fixture.reservation)
    root = Path(fixture.reservation['capture_path'])
    assert response == fixture.result and len(response) == 7
    assert record['status'] == 'completed-clean' and record['capture_complete']
    assert record['cleanup']['proven'] and record['term_count'] == 0
    assert (root / 'stdout.bin').read_bytes() == fixture.stdout
    assert (root / 'stderr.bin').read_bytes() == b'complete synthetic launcher stderr\n'
    assert record['stdout']['sha256'] == hashlib.sha256(fixture.stdout).hexdigest()
    assert json.loads((root / 'process.json').read_bytes()) == record
    with pytest.raises((ValueError, OSError)):
        module.capture_reserved(fixture.reservation)
    assert len([item for item in state['events'] if item[0] == 'launch']) == 1


def test_plausible_stdout_without_original_native_seal_never_completes(setup):
    module, fixture, state, _ = setup
    state.update(sealed=False, native_gone=True)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['status'] == 'uncertain-no-retry'
    assert record['capture_complete']


@pytest.mark.parametrize('reason', ['timeout', 'output-limit', 'process-exited-with-descendants', 'signal'])
def test_one_owned_term_and_exact_native_term_after_full_revalidation(setup, reason):
    module, fixture, state, _ = setup
    state.update(sealed=False)
    if reason == 'timeout':
        state.update(exit_at=9999, descendants_until=9999)
    elif reason == 'output-limit':
        state.update(stdout=b'x' * (8 * 1024 * 1024 + 1))
    elif reason == 'process-exited-with-descendants':
        state.update(descendants_until=5)
    else:
        state.update(signal=True)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['cancel_reason'] == reason
    assert record['term_count'] == 1 and record['capture_complete']
    events = [item[0] for item in state['events']]
    assert events.count('group-term') == events.count('native-term') == 1
    assert events.index('group-term') < events.index('native-term') < events.index('reap')
    assert events.count('native-inspect') == 3
    assert record['stdout']['sha256'] is not None


def test_changed_full_native_mount_identity_never_authorizes_judge_term(setup):
    module, fixture, state, _ = setup
    state.update(sealed=False, exit_at=9999, descendants_until=9999, changed_mount=True)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['term_count'] == 1
    assert not any(item[0] == 'native-term' for item in state['events'])
    assert record['cleanup']['proven'] is False


def test_unknown_group_keeps_45_seconds_and_unreaped_handle_without_final_hashes(setup):
    module, fixture, state, process = setup
    state.update(sealed=False, exit_at=9999, descendants_until=9999, uncooperative=True)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and 645 <= state['now'] < 645.21
    assert record['capture_complete'] is False and record['returncode'] is None
    assert record['stdout']['sha256'] is record['stderr']['sha256'] is None
    assert module._UNREAPED[fixture.reservation['invocation_id']] is process
    assert not any(item[0] == 'reap' for item in state['events'])


def test_foreign_group_is_not_signalled_or_reaped(setup, monkeypatch):
    module, fixture, state, _ = setup
    monkeypatch.setattr(module.os, 'getpgid', lambda pid: 99)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['term_count'] == 0
    assert not any(item[0] in {'group-term', 'native-term', 'reap'} for item in state['events'])


def test_changed_current_installed_bytes_refuse_before_launcher_spawn(setup):
    module, fixture, state, _ = setup
    (fixture.component / 'controller/harbor_judge.mjs').write_bytes(b'changed installed bytes')
    with pytest.raises(ValueError):
        module.capture_reserved(fixture.reservation)
    assert state['events'] == []


def test_replaced_stream_cannot_turn_different_actual_stdout_into_clean_native_result(setup):
    module, fixture, state, _ = setup
    state.update(stdout=b'actual launcher prefix\n' + fixture.stdout, replace_stdout=True)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['capture_complete'] is False
    assert record['stdout']['sha256'] is record['stderr']['sha256'] is None


def test_unknown_daemon_status_never_authorizes_native_term(setup, monkeypatch):
    module, fixture, state, _ = setup
    state.update(sealed=False, exit_at=9999, descendants_until=9999)
    monkeypatch.setattr(module.NativeHarborCapture, '_daemon_status', lambda self, endpoint: 503)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['term_count'] == 1
    assert not any(item[0] == 'native-term' for item in state['events'])


def test_current_closure_change_after_native_publication_refuses_completion(setup):
    module, fixture, state, _ = setup
    state.update(change_after_spawn=True)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['cleanup']['proven'] is False


def test_unavailable_group_metadata_does_not_reap_or_stamp_final_hashes(setup, monkeypatch):
    module, fixture, state, process = setup
    monkeypatch.setattr(module, 'other_group_members',
                        lambda *args: (_ for _ in ()).throw(ValueError('private metadata canary')))
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and 45 <= state['now'] < 45.21
    assert record['term_count'] == 1 and record['capture_complete'] is False
    assert record['stdout']['sha256'] is None and process.returncode is None
    assert 'canary' not in json.dumps(record)


def test_postspawn_persistence_failure_uses_one_term_and_preserves_private_error(setup, monkeypatch):
    module, fixture, state, _ = setup
    original = module._save
    failed = []

    def save(stream, record):
        if record['status'] == 'running' and not failed:
            failed.append(True)
            raise OSError('private persistence canary')
        return original(stream, record)

    monkeypatch.setattr(module, '_save', save)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['term_count'] == 1 and record['capture_complete']
    assert 'canary' not in json.dumps(record)
    assert len([item for item in state['events'] if item[0] == 'group-term']) == 1


def test_replaced_status_path_cannot_leave_a_success_without_durable_named_status(setup):
    module, fixture, state, _ = setup
    state.update(replace_metadata=True)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['capture_complete'] is False
    assert record['stdout']['sha256'] is record['stderr']['sha256'] is None


def test_growth_during_stream_hash_never_stamps_a_complete_old_digest(setup, monkeypatch):
    module, fixture, _, _ = setup
    original = module.hashlib.sha256

    class ChangingDigest:
        def __init__(self, *args):
            self.actual = original(*args)

        def update(self, raw):
            self.actual.update(raw)
            if raw == b'complete synthetic launcher stderr\n':
                with (Path(fixture.reservation['capture_path']) / 'stderr.bin').open('ab') as stream:
                    stream.write(b'writer after digest\n')

        def hexdigest(self):
            return self.actual.hexdigest()

    monkeypatch.setattr(module.hashlib, 'sha256', ChangingDigest)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['capture_complete'] is False
    assert record['stdout']['sha256'] is record['stderr']['sha256'] is None


@pytest.mark.parametrize('label', ['stdout', 'stderr'])
def test_stream_growth_after_actual_native_seal_reopen_withholds_ballot(setup, monkeypatch, label):
    module, fixture, _, _ = setup
    original = module.native.reopen_native_seal
    root = Path(fixture.reservation['capture_path'])

    def reopen(*args, **kwargs):
        response = original(*args, **kwargs)
        with (root / (label + '.bin')).open('ab') as stream:
            stream.write(b'late synthetic descendant writer\n')
        return response

    monkeypatch.setattr(module.native, 'reopen_native_seal', reopen)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['status'] == 'uncertain-no-retry'
    assert record['capture_complete'] is False
    assert record['stdout']['sha256'] is record['stderr']['sha256'] is None
    assert record[label]['bytes'] == (root / (label + '.bin')).stat().st_size
    assert json.loads((root / 'process.json').read_bytes()) == record


def test_named_status_substitution_after_actual_final_save_withholds_ballot(setup, monkeypatch):
    module, fixture, _, _ = setup
    original = module._save
    root = Path(fixture.reservation['capture_path'])

    def save(stream, record):
        original(stream, record)
        if record['status'] == 'completed-clean':
            (root / 'process.json').rename(root / 'retired-process.json')
            native_fixture.write(root / 'process.json', b'{"status":"substituted"}\n')

    monkeypatch.setattr(module, '_save', save)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['status'] == 'uncertain-no-retry'
    assert record['capture_complete'] is False
    assert record['stdout']['sha256'] is record['stderr']['sha256'] is None
    assert (root / 'process.json').read_bytes() == b'{"status":"substituted"}\n'
    assert json.loads((root / 'retired-process.json').read_bytes()) == record


def test_named_status_content_change_after_actual_final_save_withholds_ballot(setup, monkeypatch):
    module, fixture, _, _ = setup
    original = module._save
    root = Path(fixture.reservation['capture_path'])

    def save(stream, record):
        original(stream, record)
        if record['status'] == 'completed-clean':
            with (root / 'process.json').open('wb') as changed:
                changed.write(b'{"status":"substituted"}\n')

    monkeypatch.setattr(module, '_save', save)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['status'] == 'uncertain-no-retry'
    assert record['capture_complete'] is False
    assert record['stdout']['sha256'] is record['stderr']['sha256'] is None
    assert json.loads((root / 'process.json').read_bytes()) == record


@pytest.mark.parametrize('label', ['stdout', 'stderr'])
def test_stream_growth_after_actual_final_save_withholds_ballot(setup, monkeypatch, label):
    module, fixture, _, _ = setup
    original = module._save
    root = Path(fixture.reservation['capture_path'])

    def save(stream, record):
        original(stream, record)
        if record['status'] == 'completed-clean':
            with (root / (label + '.bin')).open('ab') as writer:
                writer.write(b'late synthetic descendant writer\n')

    monkeypatch.setattr(module, '_save', save)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['status'] == 'uncertain-no-retry'
    assert record['capture_complete'] is False
    assert record['stdout']['sha256'] is record['stderr']['sha256'] is None
    assert record[label]['bytes'] == (root / (label + '.bin')).stat().st_size
    assert json.loads((root / 'process.json').read_bytes()) == record


def test_group_identity_changed_after_anchor_never_authorizes_a_numeric_group_signal(setup, monkeypatch):
    module, fixture, state, _ = setup
    state.update(sealed=False, exit_at=9999, descendants_until=9999, uncooperative=True)
    observed = []

    def group(pid):
        observed.append(pid)
        return 42 if len(observed) == 1 else 99

    monkeypatch.setattr(module.os, 'getpgid', group)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['capture_complete'] is False
    assert record['group_term']['sent'] is False
    assert not any(item[0] in {'group-term', 'native-term', 'reap'} for item in state['events'])


def test_current_closure_changed_during_selected_inspect_never_authorizes_native_term(setup):
    module, fixture, state, _ = setup
    state.update(sealed=False, exit_at=9999, descendants_until=9999, change_during_inspect=True)
    record, response = module.capture_reserved(fixture.reservation)
    assert response is None and record['term_count'] == 1
    assert not any(item[0] == 'native-term' for item in state['events'])


@pytest.mark.parametrize('selected', ['/opt/homebrew/bin/mise', '/tmp/unadmitted-mise'])
def test_metadata_mise_selector_comes_from_the_exact_rendered_launcher_not_a_guessed_location(setup, selected):
    module, fixture, _, _ = setup
    adapter = module.NativeHarborCapture(fixture.reservation)
    raw = f'#!/bin/sh\nmise_bin={selected}\n'.encode()
    fixture.launcher.write_bytes(raw)
    adapter.closure = {**adapter.closure, 'launcher_sha256': hashlib.sha256(raw).hexdigest()}
    if selected == '/opt/homebrew/bin/mise':
        assert adapter._mise_bin() == selected
    else:
        with pytest.raises(ValueError):
            adapter._mise_bin()
