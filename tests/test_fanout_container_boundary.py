"""Controller-sealed Linux seat and exact-session contracts; no provider spend."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.broker import Broker, BrokerPermit, BrokerResponse  # noqa: E402
from shared.lib.fanout.artifacts import ArtifactStore  # noqa: E402
from shared.lib.fanout.container_broker import PermitLedger  # noqa: E402
from shared.lib.fanout.container_runtime import (  # noqa: E402
    ContainerHandle, ContainerLauncher, ContainerSpec, GuestSocketAttestation,
    private_volume_names,
)
from shared.lib.fanout.container_vm import VMProbe  # noqa: E402
from shared.lib.fanout.container_transfer import TransferIdentity, snapshot_manifest  # noqa: E402
from shared.lib.fanout.container_transfer_transport import issue_input_source_grant  # noqa: E402
from shared.lib.fanout.controller import (  # noqa: E402
    create_lifecycle_controller, read_evidence, write_evidence,
)
from shared.lib.fanout.errors import ProviderRequestError  # noqa: E402
from shared.lib.fanout.process import ProcessCommand, ProcessResult, ProcessStatus  # noqa: E402
from shared.lib.fanout.providers import (  # noqa: E402
    ProviderRegistry, ProviderRequest, run_provider,
)
import test_fanout_container_runtime as runtime_tests  # noqa: E402
import test_fanout_container_broker as broker_tests  # noqa: E402
import test_fanout_container_transfer_transport as transfer_transport_tests  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/fanout_providers"
IMAGE = "fanout-seat@sha256:" + "a" * 64
CLI = "b" * 64
CLAUDE_ID = "11111111-1111-4111-8111-111111111111"
SESSION_IDS = {"claude": CLAUDE_ID, "codex": "codex-thread-1",
               "agy": "agy-conversation-1"}


def _quality_container_original(tmp_path, provider, *, resume=False, tier='standard'):
    from shared.lib.fanout.eval_provider import quality_session_command
    from shared.lib.fanout.container_transfer_transport import _identity, _workdir
    profile = ProviderRegistry.default().select(provider, 'read-only', tier)
    request = ProviderRequest(provider, 'Räkna € och 税.\nUse only this context.'.encode(),
        cwd=tmp_path, profile=profile, timeout=31, resume=resume,
        session_id=SESSION_IDS[provider] if resume or provider == 'claude' else None,
        run_id='quality-run', task_id='quality-task', target_id='quality-target',
        seat_id=provider + '-seat', inputs_digest='a' * 64,
        slot_cap=2, slot_timeout=1.25, slot_root=tmp_path / 'slots')
    workdir = _workdir(_identity(request))
    catalog = workdir.removesuffix('/repo') + '/context/quality-catalog.json' if provider == 'codex' else None
    canonical = replace(quality_session_command(request, catalog_path=catalog),
        slot_cap=request.slot_cap, slot_timeout=request.slot_timeout, slot_root=request.slot_root,
        max_depth=1, term_grace=0.125, reap_timeout=0.75, max_output_bytes=1024)
    return request, canonical, workdir, catalog


@pytest.mark.parametrize('provider', ['claude', 'codex', 'agy'])
@pytest.mark.parametrize('resume', [False, True])
@pytest.mark.parametrize('tier', ['standard', 'deep'])
def test_quality_container_exact_canonical_guest_pair_preserves_request_and_controls(
    tmp_path, provider, resume, tier,
):
    from shared.lib.fanout import container_boundary as module
    from shared.lib.fanout.container_transfer_transport import admitted_request_digest
    from shared.lib.fanout.native_boundary import _command_digest
    request, canonical, workdir, catalog = _quality_container_original(tmp_path, provider, resume=resume, tier=tier)
    original = admitted_request_digest(request)
    pair = module.quality_container_command(request, canonical, workdir=workdir, catalog_path=catalog)
    expected = list(canonical.argv)
    if provider == 'codex':
        level = 3 if resume else 2
        overrides = ('-c', 'model_provider="fanout"', '-c',
            'model_providers.fanout={name="Fanout",base_url="http://127.0.0.1:43187/v1",'
            'wire_api="responses",http_headers={x-fanout-seat="disposable-placeholder"},'
            'request_max_retries=0,stream_max_retries=0}',
            '-c', 'approval_policy="never"', '-c', 'approvals_reviewer="user"',
            '-c', 'allow_login_shell=false')
        expected[level:level] = overrides
        if resume:
            expected[expected.index('sandbox_mode="read-only"')] = 'sandbox_mode="danger-full-access"'
        else:
            expected[expected.index('--sandbox') + 1] = 'danger-full-access'
            expected[expected.index('-C') + 1] = workdir
    elif provider == 'agy':
        expected[0] = '/usr/local/bin/agy'
    assert pair.canonical is canonical
    assert pair.guest == replace(canonical, argv=tuple(expected))
    assert pair.guest.stdin == canonical.stdin
    assert pair.workdir == workdir and pair.catalog_path == catalog
    assert pair.request_digest == original == admitted_request_digest(request)
    assert pair.canonical_digest == _command_digest(canonical)
    assert pair.guest_digest == _command_digest(pair.guest)
    assert request.profile.requested_model == {'claude': 'claude-opus-5-5',
        'codex': 'gpt-6.1-sol', 'agy': 'gemini-3.8-flash-high'}[provider]
    assert request.profile.requested_effort == {
        ('claude', 'standard'): 'medium', ('claude', 'deep'): 'ultracode',
        ('codex', 'standard'): 'medium', ('codex', 'deep'): 'ultra',
        ('agy', 'standard'): 'high', ('agy', 'deep'): 'high'}[provider, tier]
    if provider == 'agy':
        assert json.loads(pair.guest.stdin)['message']['content'] == request.prompt_bytes.decode()
        assert canonical.argv == ('agy', *(('--conversation', request.session_id) if resume else ('--new-project',)),
            '--mode', 'plan', '--input-format', 'stream-json', '--output-format', 'stream-json',
            '--model', request.profile.requested_model, '--effort', request.profile.requested_effort,
            '--agent', 'eval-no-tools', '--disable-slash-commands')
    else:
        assert pair.guest.stdin == request.prompt_bytes
        if provider == 'claude':
            assert canonical.argv == ('claude', '--print', '--output-format', 'json',
                '--resume' if resume else '--session-id', request.session_id,
                '--model', request.profile.requested_model, '--effort', request.profile.requested_effort,
                '--safe-mode', '--disable-slash-commands', '--strict-mcp-config', '--no-chrome', '--tools', '')
        else:
            disabled = ('shell_tool', 'unified_exec', 'view_image', 'apps', 'plugins',
                'remote_plugin', 'hooks', 'skill_search', 'multi_agent', 'multi_agent_v2',
                'deferred_executor', 'token_budget', 'sleep_tool', 'current_time_reminder',
                'send_message_to_user_async', 'request_permissions_tool', 'tool_suggest', 'goals')
            prefix = ('codex', 'exec', 'resume') if resume else ('codex', 'exec')
            expected_canonical = (*prefix, '--ignore-user-config', '--ignore-rules', '--json', '--skip-git-repo-check',
                '--enable', 'skip_host_skill_discovery',
                *(part for feature in disabled for part in ('--disable', feature)),
                '-c', 'agents.enabled=false', '-c', 'suppress_unstable_features_warning=true',
                '-c', 'web_search="disabled"', '-c', 'tools.experimental_request_user_input.enabled=false',
                '-c', 'tools.update_plan.enabled=false', '-c', f'model_catalog_json="{catalog}"',
                '-m', request.profile.requested_model, '-c', f'model_reasoning_effort="{request.profile.requested_effort}"')
            expected_canonical += (('-c', 'sandbox_mode="read-only"', request.session_id, '-') if resume else
                                   ('--sandbox', 'read-only', '-C', str(request.cwd), '-'))
            assert canonical.argv == expected_canonical
            assert [pair.guest.argv[index + 1] for index, value in enumerate(pair.guest.argv)
                    if value == '--disable'] == list(disabled)
    # The guest adaptation cannot be admitted again as a canonical native command.
    if provider != 'claude':
        with pytest.raises(ProviderRequestError, match='canonical'):
            module.quality_container_command(request, pair.guest, workdir=workdir, catalog_path=catalog)


@pytest.mark.parametrize('provider', ['claude', 'codex', 'agy'])
@pytest.mark.parametrize('damage', ['tools', 'model', 'effort', 'session', 'stdin', 'cwd',
    'timeout', 'config', 'host-operand', 'environment', 'native', 'slots'])
def test_quality_container_modified_canonical_command_refuses(tmp_path, provider, damage):
    from shared.lib.fanout import container_boundary as module
    request, command, workdir, catalog = _quality_container_original(tmp_path, provider, resume=True)
    argv = list(command.argv)
    if damage == 'tools':
        if provider == 'codex':
            argv[argv.index('agents.enabled=false')] = 'agents.enabled=true'
        else:
            argv[argv.index('--tools' if provider == 'claude' else '--agent') + 1] = 'default'
    elif damage == 'model':
        argv[argv.index('-m' if provider == 'codex' else '--model') + 1] = 'foreign-model'
    elif damage == 'effort':
        if provider == 'codex':
            argv[argv.index('model_reasoning_effort="medium"')] = 'model_reasoning_effort="xhigh"'
        else:
            argv[argv.index('--effort') + 1] = 'max'
    elif damage == 'session':
        argv[argv.index(request.session_id)] = 'foreign-session'
    elif damage in ('config', 'host-operand'):
        argv.extend(('-c', 'unknown.enabled=true') if damage == 'config' else ('--add-dir', str(tmp_path)))
    elif damage == 'stdin':
        command = replace(command, stdin=b'foreign prompt')
    elif damage == 'cwd':
        other = tmp_path / 'other'
        other.mkdir()
        command = replace(command, cwd=other)
    elif damage == 'timeout':
        command = replace(command, timeout=32)
    elif damage == 'environment':
        command = replace(command, environment={'HOME': str(tmp_path)})
    elif damage == 'native':
        command = replace(command, native_original=command)
    else:
        command = replace(command, slot_cap=3)
    command = replace(command, argv=tuple(argv))
    with pytest.raises(ProviderRequestError):
        module.quality_container_command(request, command, workdir=workdir, catalog_path=catalog)


@pytest.mark.parametrize('provider', ['claude', 'codex', 'agy'])
def test_quality_container_writer_or_substituted_profile_refuses(tmp_path, provider):
    from shared.lib.fanout import container_boundary as module
    request, command, workdir, catalog = _quality_container_original(tmp_path, provider)
    writer = replace(request, execution_class='repo-write',
        profile=ProviderRegistry.default().select(provider, 'repo-write', 'standard'))
    with pytest.raises(ProviderRequestError):
        module.quality_container_command(writer, command, workdir=workdir, catalog_path=catalog)
    changed = replace(request, profile=replace(request.profile, requested_model='foreign-model', profile_sha256=None))
    with pytest.raises(ProviderRequestError):
        module.quality_container_command(changed, command, workdir=workdir, catalog_path=catalog)


def test_quality_container_claimed_profile_digest_cannot_admit_a_foreign_model(tmp_path):
    from dataclasses import fields
    from shared.lib.fanout import container_boundary as module
    from shared.lib.fanout.eval_provider import quality_session_command
    request, command, workdir, catalog = _quality_container_original(tmp_path, 'codex')
    claimed_digest = request.profile.digest

    class ClaimedProfile(type(request.profile)):
        @property
        def digest(self):
            return claimed_digest

    descriptor = {field.name: getattr(request.profile, field.name) for field in fields(request.profile)}
    descriptor.update(requested_model='foreign-model', profile_sha256=None)
    request = replace(request, profile=ClaimedProfile(**descriptor))
    expected = quality_session_command(request, catalog_path=catalog)
    command = replace(command, argv=expected.argv)
    with pytest.raises(ProviderRequestError):
        module.quality_container_command(request, command, workdir=workdir, catalog_path=catalog)


def test_quality_container_original_command_must_have_a_finite_output_limit(tmp_path):
    from shared.lib.fanout import container_boundary as module
    request, command, workdir, catalog = _quality_container_original(tmp_path, 'codex')
    with pytest.raises(ProviderRequestError):
        module.quality_container_command(request, replace(command, max_output_bytes=None),
            workdir=workdir, catalog_path=catalog)


@pytest.mark.parametrize('foreign', ['host', 'other-import', 'dot-component', 'catalog'])
def test_quality_container_foreign_workdir_or_catalog_refuses(tmp_path, foreign):
    from shared.lib.fanout import container_boundary as module
    request, command, workdir, catalog = _quality_container_original(tmp_path, 'codex')
    if foreign == 'catalog':
        catalog = '/session/quality-catalog.json'
    elif foreign == 'host':
        workdir = str(tmp_path)
    elif foreign == 'other-import':
        workdir = '/workspace/inputs/' + 'b' * 64 + '/published/repo'
    else:
        workdir += '/../repo'
    with pytest.raises(ProviderRequestError):
        module.quality_container_command(request, command, workdir=workdir, catalog_path=catalog)


def test_quality_container_agy_invalid_utf8_and_host_direct_binary_refuse(tmp_path):
    from shared.lib.fanout import container_boundary as module
    from shared.lib.fanout.errors import ProcessValidationError
    request, command, workdir, _ = _quality_container_original(tmp_path, 'agy')
    with pytest.raises(ProcessValidationError, match='UTF-8'):
        module.quality_container_command(replace(request, prompt=b'bad\xff'), command, workdir=workdir)
    command = replace(command, argv=(str(tmp_path / 'host-agy-bin'), *command.argv[1:]))
    with pytest.raises(ProviderRequestError):
        module.quality_container_command(request, command, workdir=workdir)


@pytest.mark.parametrize('provider', ['claude', 'codex', 'agy'])
def test_quality_container_invalid_utf8_never_creates_a_guest_command(tmp_path, provider):
    from shared.lib.fanout import container_boundary as module
    from shared.lib.fanout.errors import ProcessValidationError
    request, command, workdir, catalog = _quality_container_original(tmp_path, provider)
    request = replace(request, prompt=b'bad\xff')
    if provider != 'agy':
        command = replace(command, stdin=request.prompt_bytes)
    with pytest.raises(ProcessValidationError, match='UTF-8'):
        module.quality_container_command(request, command,
            workdir=workdir, catalog_path=catalog)


@pytest.mark.parametrize('provider', ['codex', 'agy'])
def test_quality_container_initial_cannot_claim_a_resume_session(tmp_path, provider):
    from shared.lib.fanout import container_boundary as module
    request, command, workdir, catalog = _quality_container_original(tmp_path, provider)
    with pytest.raises(ProviderRequestError):
        module.quality_container_command(replace(request, session_id='foreign-session'), command,
            workdir=workdir, catalog_path=catalog)


def _run_quality_agent_script(command, home, monkeypatch):
    script = command.argv[command.argv.index('-c') + 1]
    arguments = list(command.argv[command.argv.index('-c') + 2:])
    assert arguments[:2] == ['/session', '65532']
    arguments[:2] = [str(home), str(os.getuid())]
    monkeypatch.setattr(sys, 'argv', ['quality-agent', *arguments])
    exec(compile(script, 'fixed-quality-agent', 'exec'), {})


def test_quality_container_agent_operation_creates_and_rechecks_exact_private_bytes(tmp_path, monkeypatch, capsys):
    from shared.lib.fanout import container_boundary as module
    from shared.lib.fanout.eval_provider import AGY_AGENT_TEXT
    import hashlib
    handle = ContainerHandle('a' * 64, 'b' * 64, 'workspace-volume', 'session-volume')
    create = module.quality_agy_agent_command(handle)
    verify = module.quality_agy_agent_command(handle, verify_only=True)
    expected = AGY_AGENT_TEXT.encode()
    assert create.argv[:9] == ('docker', '--context', 'colima-fanout-isolated', 'exec', '-i',
        '--user', '65532:65532', handle.container_id, 'python')
    assert create.argv[-4:] == ('/session', '65532', 'create', expected.hex())
    assert verify.argv[-4:] == ('/session', '65532', 'verify', expected.hex())
    assert create.stdin == verify.stdin == b''
    assert create.environment == verify.environment == {}
    assert create.timeout == verify.timeout == 30
    assert create.max_output_bytes == verify.max_output_bytes == 65
    home = tmp_path / 'session'
    home.mkdir(mode=0o700)
    with pytest.raises(FileNotFoundError):
        _run_quality_agent_script(verify, home, monkeypatch)
    assert tuple(home.iterdir()) == ()
    original = os.umask(0o022)
    try:
        _run_quality_agent_script(create, home, monkeypatch)
    finally:
        os.umask(original)
    agent = home / '.gemini/config/agents/eval-no-tools.md'
    assert agent.read_bytes() == expected and stat.S_IMODE(agent.stat().st_mode) == 0o600
    for parent in (home / '.gemini', home / '.gemini/config', home / '.gemini/config/agents'):
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    identity = (agent.stat().st_dev, agent.stat().st_ino)
    _run_quality_agent_script(create, home, monkeypatch)
    _run_quality_agent_script(verify, home, monkeypatch)
    assert identity == (agent.stat().st_dev, agent.stat().st_ino)
    assert capsys.readouterr().out.splitlines() == [hashlib.sha256(expected).hexdigest()] * 3


@pytest.mark.parametrize('damage', ['bytes', 'file-mode', 'parent-mode', 'symlink',
    'parent-symlink', 'hardlink', 'fifo', 'owner'])
def test_quality_container_agent_changed_or_foreign_material_refuses_without_repair(
    tmp_path, monkeypatch, damage,
):
    from shared.lib.fanout import container_boundary as module
    handle = ContainerHandle('a' * 64, 'b' * 64, 'workspace-volume', 'session-volume')
    create = module.quality_agy_agent_command(handle)
    verify = module.quality_agy_agent_command(handle, verify_only=True)
    home = tmp_path / 'session'
    home.mkdir(mode=0o700)
    _run_quality_agent_script(create, home, monkeypatch)
    agent = home / '.gemini/config/agents/eval-no-tools.md'
    if damage == 'bytes':
        agent.write_bytes(b'foreign tools enabled definition')
    elif damage == 'file-mode':
        agent.chmod(0o644)
    elif damage == 'parent-mode':
        agent.parent.chmod(0o755)
    elif damage == 'parent-symlink':
        other = home / '.gemini/original-config'
        (home / '.gemini/config').rename(other)
        (home / '.gemini/config').symlink_to(other, target_is_directory=True)
    elif damage in ('symlink', 'fifo'):
        other = agent.with_name('original-agent')
        agent.rename(other)
        if damage == 'symlink':
            agent.symlink_to(other)
        else:
            os.mkfifo(agent, 0o600)
    elif damage == 'hardlink':
        os.link(agent, agent.with_name('alias-agent'))
    else:
        original = os.fstat
        def foreign_owner(fd):
            info = original(fd)
            return SimpleNamespace(**{key: getattr(info, key) for key in
                ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size')}, st_uid=info.st_uid + 1)
        monkeypatch.setattr(os, 'fstat', foreign_owner)
    before = agent.lstat()
    content = agent.read_bytes() if stat.S_ISREG(before.st_mode) else None
    for operation in (create, verify):
        with pytest.raises((SystemExit, OSError)):
            _run_quality_agent_script(operation, home, monkeypatch)
    after = agent.lstat()
    assert (before.st_dev, before.st_ino, before.st_mode) == (after.st_dev, after.st_ino, after.st_mode)
    if content is not None:
        assert agent.read_bytes() == content


@pytest.mark.parametrize('damage', ['named-mode', 'named-substitution', 'parent-mode',
    'parent-substitution', 'same-size-bytes'])
def test_quality_container_agent_named_object_is_rechecked_after_actual_read(tmp_path, monkeypatch, damage):
    from shared.lib.fanout import container_boundary as module
    handle = ContainerHandle('a' * 64, 'b' * 64, 'workspace-volume', 'session-volume')
    home = tmp_path / 'session'
    home.mkdir(mode=0o700)
    _run_quality_agent_script(module.quality_agy_agent_command(handle), home, monkeypatch)
    agent = home / '.gemini/config/agents/eval-no-tools.md'
    replacement = agent.with_name('replacement')
    replacement.write_bytes(agent.read_bytes())
    replacement.chmod(0o600)
    foreign_parent = tmp_path / 'foreign-agents'
    foreign_parent.mkdir(mode=0o700)
    (foreign_parent / 'eval-no-tools.md').write_bytes(agent.read_bytes())
    (foreign_parent / 'eval-no-tools.md').chmod(0o600)
    actual = os.read

    def changed_after_read(fd, size):
        raw = actual(fd, size)
        if damage == 'named-mode':
            agent.chmod(0o644)
        elif damage == 'named-substitution':
            os.replace(replacement, agent)
        elif damage == 'parent-mode':
            agent.parent.chmod(0o755)
        elif damage == 'parent-substitution':
            agent.parent.rename(agent.parent.with_name('original-agents'))
            foreign_parent.rename(agent.parent)
        else:
            agent.write_bytes(b'?' + raw[1:])
        return raw

    monkeypatch.setattr(os, 'read', changed_after_read)
    with pytest.raises(SystemExit):
        _run_quality_agent_script(module.quality_agy_agent_command(handle, verify_only=True), home, monkeypatch)


def test_quality_docker_constructor_bounds_every_attempt_before_transport(tmp_path, monkeypatch):
    from shared.lib.fanout import container_boundary as module
    controller = create_lifecycle_controller(tmp_path / "quality-owner")
    seen = []
    def inert(command, **kwargs):
        seen.append((command, kwargs))
        return ProcessResult(ProcessStatus.EXIT, 0, b"genuine inert transport output", b"")
    monkeypatch.setattr(module, "_run_bounded_command", inert)
    runner = module.quality_docker_runner(controller, allocation_digest="a" * 64,
        max_commands=2, command_seconds=5, outer_seconds=30)
    result = runner(("docker", "--context", "colima-fanout-isolated", "ps", "-aq"), None)
    assert result.stdout == b"genuine inert transport output"
    assert seen[0][0].timeout == 5
    assert seen[0][1]["stdout_cap"] == 1024 * 1024
    assert seen[0][1]["acquire_slot"] is False
    runner(("docker", "--context", "colima-fanout-isolated", "inspect", "a" * 64), None)
    with pytest.raises(ProviderRequestError, match="attempt"):
        runner(("docker", "--context", "colima-fanout-isolated", "ps", "-aq"), None)
    assert len(seen) == 2
    with pytest.raises(ProviderRequestError, match="already"):
        module.quality_docker_runner(controller, allocation_digest="a" * 64,
            max_commands=2, command_seconds=5, outer_seconds=30)
    with pytest.raises(ProviderRequestError, match="context"):
        module.quality_docker_runner(controller, allocation_digest="b" * 64,
            max_commands=1, command_seconds=5, outer_seconds=30)(("docker", "ps"), None)


def _inert_quality_docker(monkeypatch, module):
    actual = module._run_bounded_command
    calls = []

    def inert(command, **kwargs):
        calls.append(command)
        return actual(replace(command, argv=(sys.executable, "-B", "-I", "-S", "-c",
            "import sys;sys.stdout.buffer.write(b'inert quality control\\n');"
            "sys.stderr.buffer.write(b'inert stderr\\n')")), **kwargs)

    monkeypatch.setattr(module, "_run_bounded_command", inert)
    return calls


def _cold_quality_docker(controller, controls, *, max_commands=2, command_seconds=5,
                         outer_seconds=30, allocation='a' * 64, argv=None):
    code = r'''
import json,pathlib,sys
sys.path.insert(0,sys.argv[1])
from dataclasses import replace
from shared.lib.fanout import container_boundary as module
from shared.lib.fanout.controller import LifecycleCapability,resume_lifecycle_controller
value=json.load(sys.stdin)
controller=resume_lifecycle_controller(pathlib.Path(value['root']),LifecycleCapability(value['capability']))
actual=module._run_bounded_command
calls=[]
def inert(command,**kwargs):
    calls.append(True)
    return actual(replace(command,argv=(sys.executable,'-B','-I','-S','-c',
        "import sys;sys.stdout.buffer.write(b'inert quality control\\n');sys.stderr.buffer.write(b'inert stderr\\n')")),**kwargs)
module._run_bounded_command=inert
try:
    original={'original_controls':tuple(value['controls'])} if value['controls'] is not None else {}
    runner=module.quality_docker_runner(controller,allocation_digest=value['allocation'],
        max_commands=value['max_commands'],command_seconds=value['command_seconds'],
        outer_seconds=value['outer_seconds'],**original)
    result=runner(tuple(value['argv']),None)
    output={'state':'settled','stdout':result.stdout.decode(),'calls':len(calls)}
except Exception as error:
    output={'state':'refused','error':str(error),'kind':type(error).__name__,'calls':len(calls)}
print(json.dumps(output))
'''
    value = {'root': str(controller.root), 'capability': controller.capability.export_token(),
        'controls': list(controls) if controls is not None else None,
        'allocation': allocation, 'max_commands': max_commands,
        'command_seconds': command_seconds, 'outer_seconds': outer_seconds,
        'argv': list(argv or ('docker', '--context', 'colima-fanout-isolated', 'ps', '-aq'))}
    result = subprocess.run([sys.executable, '-B', '-I', '-S', '-c', code, str(ROOT)],
        input=json.dumps(value).encode(), cwd=controller.root.parent, capture_output=True,
        timeout=10, env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
                        'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_quality_docker_actual_fresh_child_preserves_consumed_count_and_captures(tmp_path, monkeypatch):
    from shared.lib.fanout import container_boundary as module
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    calls = _inert_quality_docker(monkeypatch, module)
    runner = module.quality_docker_runner(controller, allocation_digest='a' * 64,
        max_commands=2, command_seconds=5, outer_seconds=30)
    result = runner(('docker', '--context', 'colima-fanout-isolated', 'ps', '-aq'), None)
    assert result.stdout == b'inert quality control\n'
    cold = _cold_quality_docker(controller, runner.controls)
    assert cold == {'state': 'settled', 'stdout': 'inert quality control\n', 'calls': 1}
    exhausted = _cold_quality_docker(controller, runner.controls)
    assert exhausted['state'] == 'refused' and exhausted['calls'] == 0
    assert 'exhausted' in exhausted['error']
    assert len(calls) == 1
    with ArtifactStore.open_existing(controller.root / 'quarantine' / ('quality-docker-' + 'a' * 64)) as retained:
        from shared.lib.fanout.artifacts import ArtifactRef
        import hashlib
        for attempt in (1, 2):
            raw = b'inert quality control\n'
            assert retained.read_bytes(ArtifactRef(f'{attempt}/stdout.bin',
                hashlib.sha256(raw).hexdigest(), len(raw))) == raw


@pytest.mark.parametrize('removed', ['attempt', 'store'])
def test_quality_docker_retained_attempt_refuses_marker_and_capture_rollback(
    tmp_path, monkeypatch, removed,
):
    from shared.lib.fanout import container_boundary as module
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    calls = _inert_quality_docker(monkeypatch, module)
    runner = module.quality_docker_runner(controller, allocation_digest='a' * 64,
        max_commands=1, command_seconds=5, outer_seconds=30)
    marker = controller.root / 'locks' / ('quality-docker-' + 'a' * 64)
    prefix, inode = marker.read_bytes(), marker.stat().st_ino
    runner(('docker', '--context', 'colima-fanout-isolated', 'ps', '-aq'), None)
    receipts = {path.name: path.read_bytes() for path in (controller.root / 'receipts').iterdir()}
    retained = controller.root / 'quarantine' / ('quality-docker-' + 'a' * 64)
    target = retained / '1' if removed == 'attempt' else retained
    target.rename(tmp_path / 'retained-original')
    with marker.open('r+b') as handle:
        handle.write(prefix)
        handle.truncate()
    assert marker.stat().st_ino == inode
    cold = _cold_quality_docker(controller, runner.controls, max_commands=1)
    assert cold['state'] == 'refused' and cold['calls'] == 0
    assert len(calls) == 1
    assert {path.name: path.read_bytes() for path in (controller.root / 'receipts').iterdir()} == receipts


@pytest.mark.parametrize('mutation', ['missing', 'foreign', 'mode', 'hardlink', 'symlink', 'extra'])
def test_quality_docker_retained_claim_tamper_refuses_without_subprocess(
    tmp_path, monkeypatch, mutation,
):
    from shared.lib.fanout import container_boundary as module
    from shared.lib.fanout.artifacts import canonical_json
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    calls = _inert_quality_docker(monkeypatch, module)
    runner = module.quality_docker_runner(controller, allocation_digest='a' * 64,
        max_commands=2, command_seconds=5, outer_seconds=30)
    runner(('docker', '--context', 'colima-fanout-isolated', 'ps', '-aq'), None)
    claim = controller.root / 'locks' / ('quality-docker-' + 'a' * 64 + '-attempt-1')
    if mutation == 'missing':
        claim.rename(tmp_path / 'original-claim')
    elif mutation == 'foreign':
        claim.write_bytes(canonical_json({'attempt': list(runner.controls)}))
    elif mutation == 'mode':
        claim.chmod(0o644)
    elif mutation == 'hardlink':
        os.link(claim, tmp_path / 'claim-link')
    elif mutation == 'symlink':
        claim.rename(tmp_path / 'original-claim')
        claim.symlink_to(tmp_path / 'original-claim')
    else:
        extra = claim.with_name(claim.name[:-1] + '3')
        extra.write_bytes(claim.read_bytes())
        extra.chmod(0o600)
    cold = _cold_quality_docker(controller, runner.controls)
    assert cold['state'] == 'refused' and cold['calls'] == 0
    assert len(calls) == 1


@pytest.mark.parametrize('allowed', [200, 256])
def test_quality_docker_full_declared_capacity_crosses_old_limit_and_cold_reopens(tmp_path, monkeypatch, allowed):
    from shared.lib.fanout import container_boundary as module
    from shared.lib.fanout.artifacts import canonical_json
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    calls = _inert_quality_docker(monkeypatch, module)
    runner = module.quality_docker_runner(controller, allocation_digest='a' * 64,
        max_commands=allowed, command_seconds=1, outer_seconds=3600)
    marker = controller.root / 'locks' / ('quality-docker-' + 'a' * 64)
    prefix = marker.stat().st_size
    pair_bytes = sum(len(canonical_json({kind: list(runner.controls)}))
                     for kind in ('attempt', 'terminal'))
    crossover = min(213, allowed)
    command = ('docker', '--context', 'colima-fanout-isolated', 'ps', '-aq')
    for _ in range(crossover - 1):
        assert runner(command, None).stdout == b'inert quality control\n'
    cold = _cold_quality_docker(controller, runner.controls, max_commands=allowed,
                               command_seconds=1, outer_seconds=3600)
    assert cold == {'state': 'settled', 'stdout': 'inert quality control\n', 'calls': 1}
    for _ in range(crossover, allowed):
        assert runner(command, None).stdout == b'inert quality control\n'
    assert len(calls) == allowed - 1
    assert marker.stat().st_size == prefix + allowed * pair_bytes
    assert marker.stat().st_size == (61696 if allowed == 200 else 78888)
    exhausted = _cold_quality_docker(controller, runner.controls, max_commands=allowed,
                                    command_seconds=1, outer_seconds=3600)
    assert exhausted['state'] == 'refused' and exhausted['calls'] == 0
    assert 'exhausted' in exhausted['error']
    retained = controller.root / 'quarantine' / ('quality-docker-' + 'a' * 64)
    assert {path.name for path in retained.iterdir()} - {'.artifact.lock', '.fanout-artifact-tmp'} == {
        str(index) for index in range(1, allowed + 1)}
    for index in (1, crossover, allowed):
        assert (retained / str(index) / 'stdout.bin').read_bytes() == b'inert quality control\n'
        assert (retained / str(index) / 'stderr.bin').read_bytes() == b'inert stderr\n'


def test_quality_docker_oversized_declared_capacity_refuses_before_any_allocation_state(tmp_path):
    from shared.lib.fanout import container_boundary as module
    from shared.lib.fanout.artifacts import ArtifactLimits
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    too_many = ArtifactLimits().max_file_bytes // 307 + 1
    with pytest.raises(ProviderRequestError, match='capacity'):
        module.quality_docker_runner(controller, allocation_digest='a' * 64,
            max_commands=too_many, command_seconds=1, outer_seconds=3600)
    assert tuple((controller.root / 'locks').iterdir()) == ()
    assert tuple((controller.root / 'receipts').iterdir()) == ()
    assert tuple((controller.root / 'quarantine').iterdir()) == ()


def test_quality_docker_exact_artifact_capacity_ceiling_is_checked_before_creation(tmp_path):
    from shared.lib.fanout import container_boundary as module
    from shared.lib.fanout.artifacts import ArtifactLimits, canonical_json
    ceiling = ArtifactLimits().max_file_bytes
    reference = ['0' * 64 + '.json', '0' * 64]
    pair_bytes = sum(len(canonical_json({kind: reference})) for kind in ('attempt', 'terminal'))

    def declared_bytes(count):
        limits = {'allocation': 'a' * 64, 'max_commands': count,
                  'command_seconds': 1, 'outer_seconds': 3600}
        return (len(canonical_json(limits)) + len(canonical_json({'controls': reference}))
                + count * pair_bytes)

    largest = ceiling // pair_bytes
    while declared_bytes(largest) > ceiling:
        largest -= 1
    assert declared_bytes(largest) <= ceiling < declared_bytes(largest + 1)
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    runner = module.quality_docker_runner(controller, allocation_digest='a' * 64,
        max_commands=largest, command_seconds=1, outer_seconds=3600)
    cold = _cold_quality_docker(controller, runner.controls, max_commands=largest,
                               command_seconds=1, outer_seconds=3600)
    assert cold == {'state': 'settled', 'stdout': 'inert quality control\n', 'calls': 1}
    proof = read_evidence(controller, 'quality-docker-controls', *runner.controls)
    assert proof['limits']['max_commands'] == largest
    refused = create_lifecycle_controller(tmp_path / 'refused-owner')
    with pytest.raises(ProviderRequestError, match='capacity'):
        module.quality_docker_runner(refused, allocation_digest='a' * 64,
            max_commands=largest + 1, command_seconds=1, outer_seconds=3600)
    for directory in ('locks', 'receipts', 'quarantine'):
        assert tuple((refused.root / directory).iterdir()) == ()


@pytest.mark.parametrize('changed', [
    {'max_commands': 3}, {'command_seconds': 5.0}, {'outer_seconds': 31},
    {'allocation': 'b' * 64},
])
def test_quality_docker_cold_changed_original_limits_or_allocation_refuse(tmp_path, changed):
    from shared.lib.fanout import container_boundary as module
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    runner = module.quality_docker_runner(controller, allocation_digest='a' * 64,
        max_commands=2, command_seconds=5, outer_seconds=30)
    result = _cold_quality_docker(controller, runner.controls, **changed)
    assert result['state'] == 'refused' and result['calls'] == 0


def test_quality_docker_cold_foreign_or_resigned_controls_and_duplicate_initial_refuse(tmp_path):
    from shared.lib.fanout import container_boundary as module
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    runner = module.quality_docker_runner(controller, allocation_digest='a' * 64,
        max_commands=2, command_seconds=5, outer_seconds=30)
    foreign = create_lifecycle_controller(tmp_path / 'foreign-owner')
    result = _cold_quality_docker(foreign, runner.controls)
    assert result['state'] == 'refused' and result['calls'] == 0
    proof = read_evidence(controller, 'quality-docker-controls', *runner.controls)
    proof['started_monotonic'] += 1
    proof['deadline_monotonic'] += 1
    proof['started_wall'] += 1
    proof['deadline_wall'] += 1
    changed = write_evidence(controller, 'quality-docker-controls', proof)
    result = _cold_quality_docker(controller, changed)
    assert result['state'] == 'refused' and result['calls'] == 0
    duplicate = _cold_quality_docker(controller, None)
    assert duplicate['state'] == 'refused' and duplicate['calls'] == 0
    assert 'already attempted' in duplicate['error']


@pytest.mark.parametrize('damage', ['marker-inode', 'marker-mode', 'marker-symlink',
    'marker-fifo', 'marker-hardlink', 'marker-rewind', 'marker-oversized', 'terminal-missing', 'stdout',
    'stderr', 'attempt-claim'])
def test_quality_docker_cold_tampering_never_replenishes_or_invokes(tmp_path, monkeypatch, damage):
    from shared.lib.fanout import container_boundary as module
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    _inert_quality_docker(monkeypatch, module)
    runner = module.quality_docker_runner(controller, allocation_digest='a' * 64,
        max_commands=2, command_seconds=5, outer_seconds=30)
    runner(('docker', '--context', 'colima-fanout-isolated', 'ps', '-aq'), None)
    marker = controller.root / 'locks' / ('quality-docker-' + 'a' * 64)
    if damage == 'marker-inode':
        replacement = marker.with_name('replacement')
        replacement.write_bytes(marker.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, marker)
    elif damage == 'marker-mode':
        marker.chmod(0o644)
    elif damage in ('marker-symlink', 'marker-fifo'):
        original = marker.with_name('original-marker')
        marker.rename(original)
        if damage == 'marker-symlink':
            marker.symlink_to(original)
        else:
            os.mkfifo(marker, 0o600)
    elif damage == 'marker-hardlink':
        os.link(marker, marker.with_name('marker-link'))
    elif damage in ('marker-rewind', 'terminal-missing'):
        lines = marker.read_bytes().splitlines(keepends=True)
        marker.write_bytes(b''.join(lines[:2] if damage == 'marker-rewind' else lines[:-1]))
    elif damage == 'marker-oversized':
        from shared.lib.fanout.artifacts import canonical_json
        proof = read_evidence(controller, 'quality-docker-controls', *runner.controls)
        limit = (len(canonical_json(proof['limits']))
                 + len(canonical_json({'controls': list(runner.controls)}))
                 + 2 * sum(len(canonical_json({kind: list(runner.controls)}))
                           for kind in ('attempt', 'terminal')))
        marker.write_bytes(marker.read_bytes().ljust(limit + 1, b'x'))
    else:
        retained = controller.root / 'quarantine' / ('quality-docker-' + 'a' * 64) / '1'
        name = 'attempt.json' if damage == 'attempt-claim' else damage + '.bin'
        item = retained / name
        item.write_bytes(item.read_bytes() + b'changed retained bytes')
    result = _cold_quality_docker(controller, runner.controls)
    assert result['state'] == 'refused' and result['calls'] == 0


def test_quality_docker_durable_pending_attempt_blocks_even_in_fresh_child(tmp_path, monkeypatch):
    from shared.lib.fanout import container_boundary as module
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    runner = module.quality_docker_runner(controller, allocation_digest='a' * 64,
        max_commands=2, command_seconds=5, outer_seconds=30)
    marker = controller.root / 'locks' / ('quality-docker-' + 'a' * 64)

    def interrupted(command, **kwargs):
        item = json.loads(marker.read_bytes().splitlines()[-1])
        proof = read_evidence(controller, 'quality-docker-attempt', *item['attempt'])
        assert proof['attempt'] == 1 and proof['controls'] == list(runner.controls)
        assert (controller.root / 'quarantine' / ('quality-docker-' + 'a' * 64) / '1/attempt.json').is_file()
        raise OSError('owned test interruption after durable consumption')

    monkeypatch.setattr(module, '_run_bounded_command', interrupted)
    with pytest.raises(OSError):
        runner(('docker', '--context', 'colima-fanout-isolated', 'ps', '-aq'), None)
    result = _cold_quality_docker(controller, runner.controls)
    assert result['state'] == 'refused' and result['calls'] == 0
    assert 'pending' in result['error']


def test_quality_docker_actual_timeout_terminal_does_not_authorize_continuation(tmp_path, monkeypatch):
    from shared.lib.fanout import container_boundary as module
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    actual = module._run_bounded_command

    def timeout(command, **kwargs):
        return actual(replace(command, argv=(sys.executable, '-B', '-I', '-S', '-c',
            "import sys,time;print('bounded timeout output',flush=True);time.sleep(2)")), **kwargs)

    monkeypatch.setattr(module, '_run_bounded_command', timeout)
    runner = module.quality_docker_runner(controller, allocation_digest='a' * 64,
        max_commands=2, command_seconds=0.05, outer_seconds=30)
    with pytest.raises(ProviderRequestError, match='did not settle'):
        runner(('docker', '--context', 'colima-fanout-isolated', 'ps', '-aq'), None)
    result = _cold_quality_docker(controller, runner.controls, command_seconds=0.05)
    assert result['state'] == 'refused' and result['calls'] == 0
    assert 'did not settle' in result['error']
    assert (controller.root / 'quarantine' / ('quality-docker-' + 'a' * 64) / '1/stdout.bin').read_bytes() == b'bounded timeout output\n'


@pytest.mark.parametrize('clock', ['monotonic', 'wall'])
def test_quality_docker_either_original_deadline_exhausts_without_new_command(tmp_path, monkeypatch, clock):
    from shared.lib.fanout import container_boundary as module
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    runner = module.quality_docker_runner(controller, allocation_digest='a' * 64,
        max_commands=2, command_seconds=5, outer_seconds=30)
    proof = read_evidence(controller, 'quality-docker-controls', *runner.controls)
    monkeypatch.setattr(module.time, 'monotonic' if clock == 'monotonic' else 'time',
        lambda: proof['deadline_monotonic' if clock == 'monotonic' else 'deadline_wall'])
    with pytest.raises(ProviderRequestError, match='exhausted'):
        module.quality_docker_runner(controller, allocation_digest='a' * 64,
            max_commands=2, command_seconds=5, outer_seconds=30, original_controls=runner.controls)


def test_quality_docker_actual_cold_deadline_is_not_restarted_and_unknown_operation_is_free(tmp_path, monkeypatch):
    from shared.lib.fanout import container_boundary as module
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    calls = _inert_quality_docker(monkeypatch, module)
    runner = module.quality_docker_runner(controller, allocation_digest='a' * 64,
        max_commands=2, command_seconds=0.1, outer_seconds=0.4)
    marker = controller.root / 'locks' / ('quality-docker-' + 'a' * 64)
    before = marker.read_bytes()
    with pytest.raises(ProviderRequestError, match='unknown'):
        runner(('docker', '--context', 'colima-fanout-isolated', 'system', 'prune'), None)
    assert marker.read_bytes() == before and calls == []
    proof = read_evidence(controller, 'quality-docker-controls', *runner.controls)
    time.sleep(max(0, proof['deadline_monotonic'] - time.monotonic()) + 0.03)
    result = _cold_quality_docker(controller, runner.controls, command_seconds=0.1, outer_seconds=0.4)
    assert result['state'] == 'refused' and result['calls'] == 0
    assert 'exhausted' in result['error']


def test_quality_docker_fresh_child_cannot_open_an_active_original_marker(tmp_path):
    from shared.lib.fanout import container_boundary as module
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    runner = module.quality_docker_runner(controller, allocation_digest='a' * 64,
        max_commands=2, command_seconds=5, outer_seconds=30)
    marker = controller.root / 'locks' / ('quality-docker-' + 'a' * 64)
    descriptor = os.open(marker, os.O_RDWR | os.O_NOFOLLOW)
    try:
        module.fcntl.flock(descriptor, module.fcntl.LOCK_EX)
        result = _cold_quality_docker(controller, runner.controls)
        assert result['state'] == 'refused' and result['calls'] == 0
        assert 'pending' in result['error']
    finally:
        os.close(descriptor)


def test_quality_docker_actual_nonzero_exit_remains_settled_and_consumed(tmp_path, monkeypatch):
    from shared.lib.fanout import container_boundary as module
    controller = create_lifecycle_controller(tmp_path / 'quality-owner')
    actual = module._run_bounded_command

    def nonzero(command, **kwargs):
        return actual(replace(command, argv=(sys.executable, '-B', '-I', '-S', '-c',
            "import sys;sys.stdout.buffer.write(b'observed nonzero output');sys.exit(7)")), **kwargs)

    monkeypatch.setattr(module, '_run_bounded_command', nonzero)
    runner = module.quality_docker_runner(controller, allocation_digest='a' * 64,
        max_commands=2, command_seconds=5, outer_seconds=30)
    result = runner(('docker', '--context', 'colima-fanout-isolated', 'ps', '-aq'), None)
    assert result.returncode == 7 and result.stdout == b'observed nonzero output'
    assert _cold_quality_docker(controller, runner.controls)['state'] == 'settled'
    assert _cold_quality_docker(controller, runner.controls)['calls'] == 0


def _private_failure_control(module, tmp_path):
    controller = create_lifecycle_controller(tmp_path / "private-control")
    permit = BrokerPermit("claude", "run-control", "task", "target", "seat", "initial",
                          "test-model", "disposable-placeholder", 1, 1024, 1024, 32)
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    control = object.__new__(module.PinnedSSHSeatControl)
    control.controller = controller
    control.broker = Broker(permit, credential="controller-secret", send=lambda *_: None)
    control.ledger = ledger
    control.permit_id = ledger.register(permit, 1)
    control.epoch = 1
    workspace, session = private_volume_names("run-control", "task", "target", "seat")
    control.spec = ContainerSpec("run-control", "task", "target", "seat", IMAGE,
                                 workspace, session, "/run/fanout/seat.sock", CLI)
    control._argv = lambda _: ("ssh", "pinned", "PRIVATE_ARGV_CANARY")
    return control


@pytest.mark.parametrize("kind,stderr,want_text,omission", [
    ("exit1", b"actual bounded stderr", "actual bounded stderr", None),
    ("exit255", b"", "", None),
    ("timeout", b"actual timed out stderr", "actual timed out stderr", None),
    ("spawn", b"ignored", None, "unavailable"),
    ("stdout_budget", b"bounded", "bounded", None),
    ("exit1", b"x" * 8193, None, "stderr_budget"),
    ("exit1", b"\xff", None, "unsafe_stderr"),
    ("exit1", b"controller-secret", None, "unsafe_stderr"),
    ("exit1", b'{"key":"controller\\u002dsecret"}', None, "unsafe_stderr"),
], ids=["exit1", "exit255-empty", "timeout", "spawn", "stdout-budget", "stderr-budget", "non-utf8", "reflection", "escaped-reflection"])
def test_private_ssh_failure_retains_only_bounded_actual_control_fields(
    boundary_module, tmp_path, monkeypatch, kind, stderr, want_text, omission,
):
    control = _private_failure_control(boundary_module, tmp_path)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if kind == "timeout":
            raise subprocess.TimeoutExpired("EXCEPTION_CMD_CANARY", 30,
                                             output=b"OUTPUT_CANARY", stderr=stderr)
        if kind == "spawn": raise OSError("EXCEPTION_CANARY")
        return ProcessResult(ProcessStatus.EXIT, 255 if kind == "exit255" else (0 if kind == "stdout_budget" else 1),
                             b"x" * 1048577 if kind == "stdout_budget" else b"STDOUT_CANARY", stderr)
    monkeypatch.setattr(boundary_module, "_run_control_command", run)
    try:
        with pytest.raises(ProviderRequestError):
            control._ssh(("trusted",), b"INPUT_CANARY", operation="relay_drain")
    except TypeError: pytest.fail("pinned SSH failure does not retain fixed-operation diagnostics")
    assert len(calls) == 1
    paths = list((control.controller.root / "receipts").glob("*.json"))
    assert len(paths) == 1
    raw = paths[0].read_bytes()
    proof = read_evidence(control.controller, "container-ssh-control-failure", paths[0].name,
                          __import__("hashlib").sha256(raw).hexdigest())
    assert proof["operation"] == "relay_drain" and proof["permit_id"] == control.permit_id
    assert proof["stderr"] == want_text and proof["stderr_omission"] == omission
    assert proof["timeout"] is (kind == "timeout")
    assert proof["returncode"] == (None if kind in {"timeout", "spawn"} else 255 if kind == "exit255" else 0 if kind == "stdout_budget" else 1)
    assert len(proof["source_sha256"]) == len(proof["permit_digest"]) == 64
    assert all(marker not in raw.decode() for marker in ("CANARY", "controller-secret"))


def test_private_ssh_success_unknown_operation_and_capture_failure_are_closed(boundary_module, tmp_path, monkeypatch):
    control = _private_failure_control(boundary_module, tmp_path)
    calls = []
    def run(argv, **kwargs):
        calls.append(True)
        return ProcessResult(ProcessStatus.EXIT, 0, b"unchanged success", b"")
    monkeypatch.setattr(boundary_module, "_run_control_command", run)
    try: assert control._ssh(("trusted",), operation="vm_nonce") == b"unchanged success"
    except TypeError: pytest.fail("pinned SSH fixed-operation diagnostic interface is absent")
    with pytest.raises(ProviderRequestError): control._ssh(("trusted",), operation="attacker-command")
    assert len(calls) == 1 and list((control.controller.root / "receipts").glob("*.json")) == []
    monkeypatch.setattr(boundary_module, "_run_control_command", lambda *args, **kwargs: ProcessResult(ProcessStatus.EXIT, 1, b"", b"safe"))
    writes = []
    def fail_write(*args):
        writes.append(True)
        raise OSError("PRIVATE_WRITER_CANARY")
    monkeypatch.setattr(boundary_module, "write_evidence", fail_write)
    with pytest.raises(ProviderRequestError) as caught: control._ssh(("trusted",), operation="socket_closed_check")
    assert writes == [True] and "PRIVATE_WRITER_CANARY" not in str(caught.value)


@pytest.mark.parametrize("failure", ("writer_base", "malformed_source"))
def test_private_ssh_capture_cannot_bless_malformed_source_or_leak_writer_error(boundary_module, tmp_path, monkeypatch, failure):
    control = _private_failure_control(boundary_module, tmp_path)
    monkeypatch.setattr(boundary_module, "_run_control_command", lambda *args, **kwargs: ProcessResult(ProcessStatus.EXIT, 1, b"", b"safe"))
    if failure == "writer_base":
        def broken(*_args): raise SystemExit("PRIVATE_WRITER_CANARY")
        monkeypatch.setattr(boundary_module, "write_evidence", broken)
    else:
        monkeypatch.setattr(boundary_module, "runtime_source_digest", lambda _: "MALFORMED_SOURCE_CANARY")
    with pytest.raises(ProviderRequestError) as caught: control._ssh(("trusted",), operation="socket_closed_check")
    assert "CANARY" not in str(caught.value)
    assert list((control.controller.root / "receipts").glob("*.json")) == []


@pytest.fixture
def boundary_module():
    # Resolve inside test bodies so a missing API is RED, not a setup error.
    if importlib.util.find_spec("shared.lib.fanout.container_boundary") is None:
        class MissingBoundary:
            def __getattr__(self, _name):
                assert False, "container boundary API is missing"
        return MissingBoundary()
    return importlib.import_module("shared.lib.fanout.container_boundary")


class FakeTransport:
    def __init__(self, vm_probe, handle, socket, cli_digest, result):
        self.vm_probe = vm_probe
        self.handle = handle
        self.socket = socket
        self.cli_digest = cli_digest
        self.result = result
        self.calls = []

    def attest_vm(self):
        return self.vm_probe

    def attest_container(self):
        return self.handle

    def attest_socket(self):
        return self.socket

    def attest_cli(self, _provider):
        return self.cli_digest

    def attest_image(self):
        return IMAGE

    def run(self, command, _boundary):
        self.calls.append(command)
        return self.result


def _terminal(provider: str, *, session_id: str | None = None) -> ProcessResult:
    if provider == "claude":
        data = json.loads((FIXTURES / "claude-success.json").read_text())
        data["session_id"] = session_id
        stdout = json.dumps(data).encode()
    elif provider == "codex":
        rows = [json.loads(line) for line in
                (FIXTURES / "codex-success.ndjson").read_text().splitlines()]
        if session_id is None:
            rows.pop(0)
        else:
            rows[0]["thread_id"] = session_id
        stdout = b"\n".join(json.dumps(row).encode() for row in rows) + b"\n"
    else:
        rows = [json.loads(line) for line in
                (FIXTURES / "agy-success.json").read_text().splitlines()]
        if session_id is None:
            rows[0].pop("conversation_id")
            rows[1]["step_update"].pop("conversation_id")
            rows[2]["result"].pop("conversation_id")
        else:
            rows[0]["conversation_id"] = session_id
            rows[1]["step_update"]["conversation_id"] = session_id
            rows[2]["result"]["conversation_id"] = session_id
        stdout = b"\n".join(json.dumps(row).encode() for row in rows) + b"\n"
    return ProcessResult(ProcessStatus.EXIT, 0, stdout, b"")


def _seat(module, tmp_path: Path, provider: str, *, controller=None,
          store=None, ledger=None, epoch=1, resume=False,
          session_id=None, terminal_id=None, prompt=None,
          route_digest="a" * 64):
    if controller is None:
        controller = create_lifecycle_controller(tmp_path / "controller")
    if ledger is None:
        ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    workspace, volume = private_volume_names("run-one", "task-one", "target-one",
                                              "seat-one")
    if store is None:
        store = module.SessionStore(controller, "run-one", "task-one",
                                    "target-one", "seat-one", volume)
    profile = ProviderRegistry.default().select(provider, "repo-write", "standard")
    request = ProviderRequest(
        executor_id=provider, prompt=prompt or f"{provider} turn {epoch}", cwd=tmp_path,
        execution_class="repo-write", profile=profile, run_id="run-one",
        task_id="task-one", target_id="target-one", seat_id="seat-one",
        inputs_digest="1" * 64, session_id=session_id, resume=resume,
        context_sha256="a" * 64, skill_bundle_sha256="b" * 64,
        skill_delivery_sha256="8" * 64,
    )
    vm_probe = VMProbe("fanout-isolated", "unix:///private/dedicated/docker.sock",
                       "2" * 64, "3" * 64, "4" * 64, "5" * 64)
    handle = ContainerHandle("6" * 64, "7" * 64, workspace, volume)
    socket = GuestSocketAttestation("/run/fanout/seat-one.sock", 65532,
                                    stat.S_IFSOCK | 0o600, 1, 2, 3)
    broker_permit = BrokerPermit(
        provider=provider, run_id=request.run_id, task_id=request.task_id,
        target_id=request.target_id, seat_id=request.seat_id,
        route="resume" if resume else "initial", model=profile.requested_model,
        placeholder_key="disposable-placeholder", max_requests=2,
        max_request_bytes=100_000, max_response_bytes=100_000,
        max_output_tokens=1024, inputs_digest=request.inputs_digest,
        session_id=session_id,
        upstream_route_digest=route_digest,
        upstream_origin=("https://eu-aiplatform.googleapis.com"
                         if route_digest else None),
        upstream_model=profile.requested_model if route_digest else None,
        upstream_path="/v1/pinned-route" if route_digest else None,
    )
    permit_id = ledger.register(broker_permit, epoch)
    transport = FakeTransport(vm_probe, handle, socket, CLI,
                              _terminal(provider, session_id=terminal_id))
    spec = ContainerSpec(request.run_id, request.task_id, request.target_id,
                         request.seat_id, IMAGE, workspace, volume, socket.path, CLI)
    launch = {
        "spec": {field: getattr(spec, field) for field in spec.__dataclass_fields__},
        "handle": {field: getattr(handle, field) for field in handle.__dataclass_fields__},
        "route": "recover" if resume else "start",
        "volumes": __import__("test_fanout_container_runtime")._volumes(spec),
    }
    if resume:
        launch["recovery"] = {"spec": dict(launch["spec"]), "handle": dict(launch["handle"]), "predecessors": []}
        from shared.lib.fanout.container_transfer_transport import _InputClaimJournal
        from shared.lib.fanout.container_transfer import TransferIdentity
        identity = TransferIdentity(request.run_id, request.task_id, request.target_id,
                                    request.seat_id, request.inputs_digest)
        launch["original_input"] = list(_InputClaimJournal(controller, identity, workspace).completed())
    name, digest = write_evidence(controller, "container-seat-launch", launch)
    if not resume and route_digest is not None and (session_id is not None or provider != "claude"):
        from test_fanout_container_transfer_transport import _complete_original_inputs
        _complete_original_inputs(controller, request, spec, handle, broker_permit,
                                  ledger, permit_id, epoch, (name, digest), vm_probe, socket, store)
    admission = module.ContainerAdmission(
        broker_permit=broker_permit, permit_id=permit_id, epoch=epoch,
        ledger=ledger, image_digest=IMAGE, cli_digest=CLI,
        socket_attestation=socket, transport=transport, session_store=store,
        handle_evidence_name=name, handle_evidence_digest=digest,
    )
    boundary = module.issue_container_boundary(controller, request, vm_probe,
                                               handle, admission)
    return request, boundary, transport, store, ledger, controller


@pytest.mark.parametrize("changed", (
    "image_digest", "cli_digest", "vm_effective_mounts_sha256",
    "container_inspect_sha256", "workspace_volume", "session_volume",
    "socket_path", "permit_epoch", "profile_digest",
    "upstream_route_digest",
))
def test_changed_boundary_fails_before_model_send(boundary_module, tmp_path, changed):
    request, boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    old = getattr(boundary, changed)
    tampered = replace(boundary, **{changed: "f" * 64 if isinstance(old, str) else old + 1})
    with pytest.raises(ProviderRequestError):
        boundary_module.run_container_turn(request, tampered)
    assert transport.calls == []


def test_changed_prompt_under_same_boundary_fails_before_model_send(
    boundary_module, tmp_path,
):
    request, boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    with pytest.raises(ProviderRequestError, match="prompt|association"):
        boundary_module.run_container_turn(
            replace(request, prompt="different task"), boundary)
    assert transport.calls == []


@pytest.mark.parametrize("changes", (
    {"timeout": 30.0},
    {"timeout_override_reason": "reviewed", "timeout_override_review_sha256": "a" * 64},
))
def test_changed_timeout_controls_under_same_boundary_fail_before_send(
    boundary_module, tmp_path, changes,
):
    request, boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    with pytest.raises(ProviderRequestError, match="association"):
        boundary_module.run_container_turn(replace(request, **changes), boundary)
    assert transport.calls == []


@pytest.mark.parametrize("provider", ("claude", "codex", "agy"))
def test_successful_initial_binds_exact_id_for_cold_resume(boundary_module, tmp_path, provider):
    session_id = SESSION_IDS[provider]
    initial_id = session_id if provider == "claude" else None
    request, boundary, transport, store, ledger, controller = _seat(
        boundary_module, tmp_path, provider, session_id=initial_id,
        terminal_id=session_id,
    )
    result = boundary_module.run_container_turn(request, boundary)
    assert result.valid and result.session_id == session_id
    assert len(transport.calls) == 1
    assert store.resume(provider).session_id == session_id

    # A new store instance proves the binding survived process memory loss.
    ledger.fence_epoch(boundary.permit_id, 1)
    cold_store = boundary_module.SessionStore(controller, "run-one", "task-one",
                                               "target-one", "seat-one",
                                               boundary.session_volume)
    resumed, cold_boundary, cold_transport, *_ = _seat(
        boundary_module, tmp_path, provider, controller=controller,
        store=cold_store, ledger=ledger, epoch=2, resume=True,
        session_id=session_id, terminal_id=session_id,
    )
    resumed_result = boundary_module.run_container_turn(resumed, cold_boundary)
    assert resumed_result.valid and resumed_result.session_id == session_id
    assert len(cold_transport.calls) == 1


@pytest.mark.parametrize("fault", (
    None, "mac", "noncanonical", "mode", "record-symlink", "lock-symlink",
    "oversized", "controller", "identity", "volume",
))
def test_cold_session_record_authority_precedes_dispatch(boundary_module, tmp_path, fault):
    from shared.lib.fanout import canonical_json
    from shared.lib.fanout.controller import resume_lifecycle_controller
    request, boundary, _, store, ledger, controller = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID, terminal_id=CLAUDE_ID)
    assert boundary_module.run_container_turn(request, boundary).valid
    binding = asdict(store.resume("claude"))
    ledger.fence_epoch(boundary.permit_id, 1)
    cold = resume_lifecycle_controller(controller.root, controller.capability)
    cold_store = boundary_module.SessionStore(cold, *store.identity, store.volume)
    cold_ledger = PermitLedger(cold.root / "locks/spend", cold)
    resumed, sealed, transport, *_ = _seat(boundary_module, tmp_path, "claude",
        controller=cold, store=cold_store, ledger=cold_ledger, epoch=2, resume=True,
        session_id=CLAUDE_ID, terminal_id=CLAUDE_ID)
    assert asdict(cold_store.resume("claude")) == binding
    record = json.loads(cold_store._record.read_bytes())
    target = None
    if fault == "mac":
        record["mac"] = "0" * 64
    elif fault == "controller":
        foreign = create_lifecycle_controller(tmp_path / "foreign-controller")
        record["payload"]["controller_id"] = foreign.controller_id
    elif fault == "identity":
        record["payload"]["identity"][1] = "foreign-task"
    elif fault == "volume":
        record["payload"]["volume"] = "foreign-session-volume"
    elif fault == "oversized":
        record["payload"]["turns"].update({f"{index:064x}": {
            "provider": "claude", "state": "uncertain"} for index in range(1000)})
    if fault in {"controller", "identity", "volume", "oversized"}:
        record["mac"] = cold_store._mac(record["payload"])
    if fault in {"mac", "controller", "identity", "volume", "oversized"}:
        data = canonical_json(record)
        if fault == "oversized":
            assert len(data) > 64 * 1024
        cold_store._record.write_bytes(data)
    elif fault == "noncanonical":
        cold_store._record.write_text(json.dumps(record, indent=2))
    elif fault == "mode":
        cold_store._record.chmod(0o640)
    elif fault in {"record-symlink", "lock-symlink"}:
        path = cold_store._record if fault == "record-symlink" else cold_store._lock
        target = path.with_suffix(".owned-target")
        path.rename(target)
        target_before = target.read_bytes()
        path.symlink_to(target)
    if fault is None:
        assert boundary_module.run_container_turn(resumed, sealed).valid
        assert len(transport.calls) == 1
        assert cold_store.resume("claude").session_id == CLAUDE_ID
    else:
        before = cold_store._record.read_bytes()
        with pytest.raises((ProviderRequestError, OSError)) as rejected:
            boundary_module.run_container_turn(resumed, sealed)
        if isinstance(rejected.value, OSError):
            import errno
            assert fault in {"record-symlink", "lock-symlink"}
            assert rejected.value.errno == errno.ELOOP
        assert transport.calls == []
        assert cold_ledger.attempt_counts(sealed.permit_id, 2) == (0, 0)
        assert cold_store._record.read_bytes() == before
        if target is not None:
            assert target.read_bytes() == target_before
            assert path.is_symlink()


@pytest.mark.parametrize("provider,resume,returned", (
    ("claude", False, "22222222-2222-4222-8222-222222222222"),
    *((provider, True, returned) for provider in ("claude", "codex", "agy")
      for returned in (None, "22222222-2222-4222-8222-222222222222")),
))
def test_invalid_returned_session_keeps_cold_uncertainty(
    boundary_module, tmp_path, provider, resume, returned,
):
    from shared.lib.fanout.controller import resume_lifecycle_controller
    native_id = SESSION_IDS[provider]
    request, sealed, transport, store, ledger, controller = _seat(
        boundary_module, tmp_path, provider,
        session_id=native_id if provider == "claude" else None, terminal_id=native_id)
    if resume:
        assert boundary_module.run_container_turn(request, sealed).valid
        prior = asdict(store.resume(provider))
        ledger.fence_epoch(sealed.permit_id, 1)
        cold = resume_lifecycle_controller(controller.root, controller.capability)
        store = boundary_module.SessionStore(cold, *store.identity, store.volume)
        ledger = PermitLedger(cold.root / "locks/spend", cold)
        request, sealed, transport, *_ = _seat(boundary_module, tmp_path, provider,
            controller=cold, store=store, ledger=ledger, epoch=2, resume=True,
            session_id=native_id, terminal_id=returned)
    else:
        prior = None
        transport.result = _terminal(provider, session_id=returned)
    result = boundary_module.run_container_turn(request, sealed)
    assert not result.valid and len(transport.calls) == 1
    binding = store.read(provider)
    assert binding.session_id == native_id
    if prior is not None:
        assert asdict(binding) == prior
    cold = resume_lifecycle_controller(controller.root, controller.capability)
    reopened = boundary_module.SessionStore(cold, *store.identity, store.volume)
    assert asdict(reopened.read(provider)) == asdict(binding)
    with pytest.raises(ProviderRequestError, match="uncertain"):
        reopened.resume(provider)


@pytest.mark.parametrize("route", ("initial", "resume"))
def test_codex_admitted_container_uses_outer_sandbox_on_exact_turn(
    boundary_module, tmp_path, route,
):
    request, boundary, transport, store, ledger, controller = _seat(
        boundary_module, tmp_path, "codex", terminal_id="codex-thread-1",
    )
    assert boundary_module.run_container_turn(request, boundary).valid
    if route == "resume":
        ledger.fence_epoch(boundary.permit_id, 1)
        cold_store = boundary_module.SessionStore(
            controller, "run-one", "task-one", "target-one", "seat-one",
            boundary.session_volume,
        )
        request, boundary, transport, store, *_ = _seat(
            boundary_module, tmp_path, "codex", controller=controller,
            store=cold_store, ledger=ledger, epoch=2, resume=True,
            session_id=cold_store.resume("codex").session_id,
            terminal_id="codex-thread-1",
        )
        assert boundary_module.run_container_turn(request, boundary).valid
    command, = transport.calls
    if route == "resume":
        assert 'sandbox_mode="danger-full-access"' in command.argv
    else:
        assert command.argv[command.argv.index("--sandbox") + 1] == "danger-full-access"
    prefix = (
        "codex", "exec", "--ignore-user-config", "--strict-config",
        "--skip-git-repo-check",
    )
    overrides = (
        "-c", 'model_provider="fanout"', "-c",
        'model_providers.fanout={name="Fanout",'
        'base_url="http://127.0.0.1:43187/v1",wire_api="responses",'
        'http_headers={x-fanout-seat="disposable-placeholder"},'
        'request_max_retries=0,stream_max_retries=0}',
        "-c", 'approval_policy="never"', "-c", 'approvals_reviewer="user"',
        "-c", "allow_login_shell=false",
    )
    tail = (
        ("resume", *overrides, "-c", 'sandbox_mode="danger-full-access"', "-m", "gpt-6.1-sol",
         "-c", 'model_reasoning_effort="medium"', "--json", "codex-thread-1", "-")
        if route == "resume" else
        (*overrides, "-", "--json", "--sandbox", "danger-full-access", "-m", "gpt-6.1-sol",
         "-c", 'model_reasoning_effort="medium"')
    )
    assert command.argv == prefix + tail
    native = ProviderRegistry.default().require("codex").build_command(request)
    assert replace(command, argv=native.argv) == native
    assert store.resume("codex").session_id == "codex-thread-1"


@pytest.mark.parametrize("changed", ("unprofiled", "read-only", "target", "prompt"))
def test_codex_outer_sandbox_refuses_changed_request_before_dispatch(
    boundary_module, tmp_path, changed,
):
    request, boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "codex", terminal_id="codex-thread-1",
    )
    changes = {
        "unprofiled": {"profile": None},
        "read-only": {
            "execution_class": "read-only",
            "profile": ProviderRegistry.default().select("codex", "read-only", "standard"),
        },
        "target": {"target_id": "other-target"},
        "prompt": {"prompt": "other task"},
    }
    with pytest.raises(ProviderRequestError):
        boundary_module.run_container_turn(replace(request, **changes[changed]), boundary)
    assert transport.calls == []


@pytest.mark.parametrize("changed", ("initial_argv", "resume_argv", "requested_model", "requested_effort"))
def test_codex_outer_sandbox_refuses_sealed_ad_hoc_profile_before_dispatch(
    boundary_module, tmp_path, changed,
):
    request, boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "codex", terminal_id="codex-thread-1",
    )
    changes = {
        "initial_argv": ("codex", "exec", "--sandbox", "danger-full-access"),
        "resume_argv": ("codex", "exec", "resume", "--last", "-"),
        "requested_model": "other-model",
        "requested_effort": "low",
    }
    profile = replace(request.profile, **{changed: changes[changed]}, profile_sha256=None)
    altered = replace(request, profile=profile)
    if changed == "requested_model":
        with pytest.raises(ProviderRequestError, match="association"):
            boundary_module.issue_container_boundary(
                boundary.controller, altered, transport.vm_probe, transport.handle,
                boundary.admission,
            )
    else:
        sealed = boundary_module.issue_container_boundary(
            boundary.controller, altered, transport.vm_probe, transport.handle,
            boundary.admission,
        )
        with pytest.raises(ProviderRequestError, match="profile changed"):
            boundary_module.run_container_turn(altered, sealed)
    assert transport.calls == []


def test_cold_resume_refuses_changed_upstream_route(boundary_module, tmp_path):
    initial, boundary, _, _, ledger, controller = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    assert boundary_module.run_container_turn(initial, boundary).valid
    ledger.fence_epoch(boundary.permit_id, 1)
    cold_store = boundary_module.SessionStore(controller, "run-one", "task-one",
                                               "target-one", "seat-one",
                                               boundary.session_volume)
    with pytest.raises(ProviderRequestError, match="route|session"):
        _seat(boundary_module, tmp_path, "claude", controller=controller,
              store=cold_store, ledger=ledger, epoch=2, resume=True,
              session_id=CLAUDE_ID, terminal_id=CLAUDE_ID,
              route_digest="b" * 64)


def test_private_boundary_requires_route_digest(boundary_module, tmp_path):
    with pytest.raises(ProviderRequestError, match="route"):
        _seat(boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
              terminal_id=CLAUDE_ID, route_digest=None)


def test_live_launch_refuses_null_route_before_host_control(boundary_module, tmp_path):
    controller = create_lifecycle_controller(tmp_path / "controller")
    workspace, volume = private_volume_names("run-one", "task-one", "target-one",
                                              "seat-one")
    spec = ContainerSpec("run-one", "task-one", "target-one", "seat-one",
                         IMAGE, workspace, volume, "/run/fanout/seat-one.sock", CLI)
    permit = BrokerPermit(
        provider="claude", run_id=spec.run_id, task_id=spec.task_id,
        target_id=spec.target_id, seat_id=spec.seat_id, route="initial",
        model="test-model", placeholder_key="disposable-placeholder",
        max_requests=1, max_request_bytes=1024, max_response_bytes=1024,
        max_output_tokens=32,
    )
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    permit_id = ledger.register(permit, 1)
    broker = Broker(permit, credential="controller-secret",
                    send=lambda *_: BrokerResponse(200, {}, b""))
    with pytest.raises(ProviderRequestError, match="upstream route"):
        boundary_module.PinnedDockerTransport.start_live(
            controller, spec, ledger=ledger, broker=broker,
            permit_id=permit_id, epoch=1)


@pytest.mark.parametrize("provider", ("claude", "codex", "agy"))
def test_identical_prompt_can_be_a_second_authenticated_resume(
    boundary_module, tmp_path, provider,
):
    session_id = SESSION_IDS[provider]
    initial, boundary, _, store, ledger, controller = _seat(
        boundary_module, tmp_path, provider,
        session_id=session_id if provider == "claude" else None,
        terminal_id=session_id,
    )
    assert boundary_module.run_container_turn(initial, boundary).valid
    ledger.fence_epoch(boundary.permit_id, 1)
    first, first_boundary, _, *_ = _seat(
        boundary_module, tmp_path, provider, controller=controller,
        store=store, ledger=ledger, epoch=2, resume=True,
        session_id=session_id, terminal_id=session_id,
        prompt="identical resumed prompt",
    )
    assert boundary_module.run_container_turn(first, first_boundary).valid
    ledger.fence_epoch(first_boundary.permit_id, 2)
    second, second_boundary, transport, *_ = _seat(
        boundary_module, tmp_path, provider, controller=controller,
        store=store, ledger=ledger, epoch=3, resume=True,
        session_id=session_id, terminal_id=session_id,
        prompt="identical resumed prompt",
    )
    assert boundary_module.run_container_turn(second, second_boundary).valid
    assert len(transport.calls) == 1
    with pytest.raises(ProviderRequestError, match="replay"):
        boundary_module.run_container_turn(second, second_boundary)


def test_settle_rejects_session_id_outside_broker_grammar(boundary_module, tmp_path):
    _, _, _, store, *_ = _seat(boundary_module, tmp_path, "codex")
    turn = "a" * 64
    store.reserve("codex", turn, initial=True, upstream_route_digest="a" * 64)
    with pytest.raises(ProviderRequestError, match="session identity"):
        store.settle("codex", "bad id with spaces", turn, initial=True,
                     upstream_route_digest="a" * 64)
    assert store.read("codex") is None


def test_claude_id_is_durable_before_initial_process(boundary_module, tmp_path):
    request, boundary, transport, store, *_ = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    original_run = transport.run

    def observe_binding(command, sealed_boundary):
        assert store.read("claude").session_id == CLAUDE_ID
        with pytest.raises(ProviderRequestError, match="uncertain"):
            store.resume("claude")
        return original_run(command, sealed_boundary)

    transport.run = observe_binding
    assert boundary_module.run_container_turn(request, boundary).valid


@pytest.mark.parametrize("provider", ("codex", "agy"))
def test_settled_session_refuses_another_initial_before_send(
    boundary_module, tmp_path, provider,
):
    request, boundary, _, store, ledger, controller = _seat(
        boundary_module, tmp_path, provider, terminal_id=SESSION_IDS[provider],
    )
    assert boundary_module.run_container_turn(request, boundary).valid
    ledger.fence_epoch(boundary.permit_id, 1)
    another, new_boundary, transport, *_ = _seat(
        boundary_module, tmp_path, provider, controller=controller,
        store=store, ledger=ledger, epoch=2,
        terminal_id=SESSION_IDS[provider],
    )
    with pytest.raises(ProviderRequestError, match="initial session already bound"):
        boundary_module.run_container_turn(another, new_boundary)
    assert transport.calls == []


def test_transport_flag_cannot_bypass_live_host_lease(boundary_module, tmp_path):
    request, boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    transport.holds_lease = True
    with boundary_module.HostSessionLease(boundary.controller,
                                          boundary.permit_id,
                                          boundary.permit_epoch):
        with pytest.raises(ProviderRequestError, match="lease"):
            boundary_module.run_container_turn(request, boundary)
    assert transport.calls == []


@pytest.mark.parametrize("mutation", [None, "closed", "mode", "replacement", "owner"])
def test_host_session_lease_requires_current_exact_held_file(boundary_module, tmp_path, monkeypatch, mutation):
    controller = create_lifecycle_controller(tmp_path / "controller")
    with boundary_module.HostSessionLease(controller, "a" * 64, 1) as lease:
        original = lease.require_held()
        with monkeypatch.context() as patch:
            if mutation == "closed":
                patch.setattr(lease, "_fd", None)
            elif mutation == "mode":
                lease.path.chmod(0o644)
            elif mutation == "replacement":
                saved = lease.path.with_suffix(".held")
                lease.path.rename(saved)
                lease.path.touch(mode=0o600)
            elif mutation == "owner":
                native = os.fstat
                def changed_owner(fd):
                    info = native(fd)
                    return SimpleNamespace(**{field: getattr(info, field) for field in (
                        "st_mode", "st_dev", "st_ino", "st_nlink")}, st_uid=os.getuid() + 1)
                patch.setattr(os, "fstat", changed_owner)
            if mutation:
                with pytest.raises(ProviderRequestError, match="lease"):
                    lease.require_held()
            else:
                assert lease.require_held() == original
@pytest.mark.parametrize("provider", ("codex", "agy"))
def test_success_without_observed_id_cannot_resume(boundary_module, tmp_path, provider):
    request, boundary, transport, store, *_ = _seat(
        boundary_module, tmp_path, provider, terminal_id=None,
    )
    result = boundary_module.run_container_turn(request, boundary)
    assert not result.valid
    assert len(transport.calls) == 1
    assert store.read(provider) is None
    with pytest.raises(ProviderRequestError, match="exact session"):
        store.resume(provider)


def test_host_version_match_cannot_override_changed_linux_bytes(boundary_module, tmp_path):
    request, boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    assert request.profile.cli_version == "2.1.284"
    transport.cli_digest = "e" * 64
    with pytest.raises(ProviderRequestError, match="CLI"):
        boundary_module.run_container_turn(request, boundary)
    assert transport.calls == []


def test_second_host_session_cannot_hold_live_permit(boundary_module, tmp_path):
    _, boundary, _, _, _, controller = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    child = """
import sys
from shared.lib.fanout.controller import LifecycleCapability, resume_lifecycle_controller
from shared.lib.fanout.container_boundary import HostSessionLease
from shared.lib.fanout.errors import ProviderRequestError
owner = resume_lifecycle_controller(sys.argv[1], LifecycleCapability(sys.stdin.read()))
try:
    with HostSessionLease(owner, sys.argv[2], int(sys.argv[3])):
        pass
except ProviderRequestError:
    raise SystemExit(17)
"""

    def other_process():
        return subprocess.run(
            [sys.executable, "-c", child, str(controller.root), boundary.permit_id,
             str(boundary.permit_epoch)],
            input=controller.capability.export_token().encode(),
            cwd=ROOT, capture_output=True, check=False,
        )

    with boundary_module.HostSessionLease(controller, boundary.permit_id,
                                          boundary.permit_epoch):
        denied = other_process()
        assert denied.returncode == 17, denied.stderr.decode()
    allowed = other_process()
    assert allowed.returncode == 0, allowed.stderr.decode()


def test_uncertain_turn_is_not_replayed_after_recovery(boundary_module, tmp_path):
    request, boundary, transport, store, _, controller = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    transport.result = ProcessResult(ProcessStatus.TIMEOUT, None, b"", b"")
    assert not boundary_module.run_container_turn(request, boundary).valid
    cold_store = boundary_module.SessionStore(controller, "run-one", "task-one",
                                               "target-one", "seat-one",
                                               boundary.session_volume)
    cold_boundary = replace(boundary, session_store=cold_store)
    with pytest.raises(ProviderRequestError, match="uncertain|replay"):
        boundary_module.run_container_turn(request, cold_boundary)
    assert len(transport.calls) == 1


def test_uncertain_resume_blocks_a_new_prompt_for_same_seat(boundary_module, tmp_path):
    request, boundary, _, store, ledger, controller = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    assert boundary_module.run_container_turn(request, boundary).valid
    ledger.fence_epoch(boundary.permit_id, 1)
    resumed, resume_boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "claude", controller=controller,
        store=store, ledger=ledger, epoch=2, resume=True,
        session_id=CLAUDE_ID, terminal_id=CLAUDE_ID,
    )
    transport.result = ProcessResult(ProcessStatus.TIMEOUT, None, b"", b"")
    assert not boundary_module.run_container_turn(resumed, resume_boundary).valid
    cold_store = boundary_module.SessionStore(controller, "run-one", "task-one",
                                               "target-one", "seat-one",
                                               boundary.session_volume)
    with pytest.raises(ProviderRequestError, match="uncertain"):
        cold_store.resume("claude")


def test_public_provider_entry_stays_closed_with_container_descriptor(boundary_module, tmp_path):
    request, boundary, transport, *_ = _seat(
        boundary_module, tmp_path, "claude", session_id=CLAUDE_ID,
        terminal_id=CLAUDE_ID,
    )
    with pytest.raises(ProviderRequestError):
        run_provider(replace(request, container_boundary=boundary))
    assert transport.calls == []


def _launched_seat(module, tmp_path, *, before_start=None, provider="claude"):
    controller = create_lifecycle_controller(tmp_path / "controller")
    spec = ContainerSpec(
        "run-synthetic", "task-plan", "target-one", "seat-claude", IMAGE,
        runtime_tests.WORKSPACE, runtime_tests.SESSION, runtime_tests.SOCKET, CLI,
    )
    class SeatDocker(runtime_tests.FakeDocker):
        fail_stop = False

        def __call__(self, argv, stdin):
            args = argv[3:]
            if args == ("update", "--restart", "no", runtime_tests.CONTAINER_ID):
                self.calls.append(argv)
                self.inspect["HostConfig"]["RestartPolicy"]["Name"] = "no"
                self.events.append("disable-restart-seat")
                return self._ok(runtime_tests.CONTAINER_ID + "\n", json_output=False)
            if args == ("stop", "--time", "0", runtime_tests.CONTAINER_ID):
                self.calls.append(argv)
                if self.fail_stop:
                    self.events.append("failed-stop-seat")
                    return subprocess.CompletedProcess(argv, 1, b"", b"synthetic stop failure")
                self.inspect["State"]["Status"] = "exited"
                self.inspect["State"]["Running"] = False
                self.events.append("stop-seat")
                return self._ok(runtime_tests.CONTAINER_ID + "\n", json_output=False)
            return super().__call__(argv, stdin)

    fake_docker = SeatDocker(spec)
    if before_start is not None:
        before_start(fake_docker)
    socket_attestor = runtime_tests.FakeAttestor()
    launcher = ContainerLauncher(fake_docker, guest_socket_attestor=socket_attestor)
    vm_probe = VMProbe("fanout-isolated", "unix:///private/dedicated/docker.sock",
                       "2" * 64, "3" * 64, "4" * 64, "5" * 64)
    sent = []

    def process_runner(command):
        sent.append(command)
        return _terminal(provider, session_id=SESSION_IDS[provider])

    transport = module.PinnedDockerTransport.start(
        controller, spec, launcher=launcher, vm_attestor=lambda: vm_probe,
        socket_attestor=socket_attestor, image_attestor=lambda: IMAGE,
        cli_attestor=lambda _provider: CLI, process_runner=process_runner,
    )
    profile = ProviderRegistry.default().select(provider, "repo-write", "standard")
    request = ProviderRequest(
        executor_id=provider, prompt="synthetic", cwd=tmp_path,
        execution_class="repo-write", profile=profile, run_id=spec.run_id,
        task_id=spec.task_id, target_id=spec.target_id, seat_id=spec.seat_id,
        inputs_digest="1" * 64, session_id=CLAUDE_ID if provider == "claude" else None,
        context_sha256="a" * 64, skill_bundle_sha256="b" * 64,
        skill_delivery_sha256="8" * 64,
    )
    broker_permit = BrokerPermit(
        provider=provider, run_id=spec.run_id, task_id=spec.task_id,
        target_id=spec.target_id, seat_id=spec.seat_id, route="initial",
        model=profile.requested_model, placeholder_key="disposable-placeholder",
        max_requests=2, max_request_bytes=100_000,
        max_response_bytes=100_000, max_output_tokens=1024,
        inputs_digest=request.inputs_digest, session_id=request.session_id,
        upstream_route_digest="a" * 64,
        upstream_origin="https://eu-aiplatform.googleapis.com",
        upstream_model=profile.requested_model,
        upstream_path="/v1/pinned-route",
    )
    ledger = PermitLedger(controller.root / "locks" / "spend", controller)
    permit_id = ledger.register(broker_permit, 1)
    store = module.SessionStore(controller, spec.run_id, spec.task_id,
                                spec.target_id, spec.seat_id, spec.session_volume)
    admission = module.ContainerAdmission(
        broker_permit=broker_permit, permit_id=permit_id, epoch=1,
        ledger=ledger, image_digest=IMAGE, cli_digest=CLI,
        socket_attestation=socket_attestor.proof, transport=transport,
        session_store=store,
        handle_evidence_name=transport.handle_evidence_name,
        handle_evidence_digest=transport.handle_evidence_digest,
    )
    source = controller.root / "quarantine" / "source"
    source.mkdir(mode=0o700)
    (source / "repo").mkdir(mode=0o700)
    (source / "context").mkdir(mode=0o700)
    (source / "repo" / "base.txt").write_text("baseline\n")
    (source / "context" / "full-plan.md").write_text("complete plan\n")
    identity = TransferIdentity(spec.run_id, spec.task_id, spec.target_id,
                                spec.seat_id, request.inputs_digest)
    manifest = snapshot_manifest(source, identity)
    with ArtifactStore(controller.root / "quarantine" / "input-material-store") as artifacts:
        manifest_ref = artifacts.write_json("expected/manifest.json", asdict(manifest))
    info = source.stat()
    material_ref = write_evidence(controller, "container-input-material", {
        "schema_version": "InputMaterialV1", "identity": asdict(identity),
        "source_device": info.st_dev, "source_inode": info.st_ino,
        "original_handle": asdict(transport.handle),
        "workspace_volume": spec.workspace_volume,
        "run_inputs_sha256": request.inputs_digest,
        "task_packet_sha256": "5" * 64,
        "target_baseline_sha256": "6" * 64,
        "workspace_sha256": "7" * 64,
        "skill_delivery_sha256": "8" * 64,
        "dependency_context_sha256": "9" * 64,
        "provider_context_sha256": request.context_sha256,
        "skill_bundle_sha256": request.skill_bundle_sha256,
        "manifest_ref": asdict(manifest_ref),
    })
    transport._transfer_popen, _ = transfer_transport_tests._guest_popen(
        tmp_path, [])
    grant = issue_input_source_grant(
        controller, request, transport, admission, source, *material_ref)
    admission = replace(admission, input_receipt=transport.stage_inputs(request, grant))
    boundary = module.issue_container_boundary(controller, request, vm_probe,
                                               transport.handle, admission)
    return request, boundary, transport, fake_docker, socket_attestor, sent


def test_turn_stops_exact_owned_seat_after_success(boundary_module, tmp_path):
    request, boundary, _, docker, _, sent = _launched_seat(boundary_module, tmp_path)
    assert boundary_module.run_container_turn(request, boundary).valid
    assert len(sent) == 1
    assert docker.inspect["State"]["Running"] is False
    assert docker.events[-2:] == ["disable-restart-seat", "stop-seat"]


@pytest.mark.parametrize("fault", (None, "nonzero", "nonhex", "agy-settings"))
def test_actual_execution_proofs_precede_model_handoff(boundary_module, tmp_path, monkeypatch, fault):
    from shared.lib.fanout.controller import resume_lifecycle_controller
    provider = "agy" if fault == "agy-settings" else "claude"
    request, boundary, transport, docker, _, sent = _launched_seat(
        boundary_module, tmp_path, provider=provider)
    proofs = []
    def control_command(argv):
        proofs.append(argv)
        if fault == "agy-settings":
            assert argv[-3] == boundary_module._AGY_SETTINGS_SCRIPT
            return ProcessResult(ProcessStatus.EXIT, 0, b"0" * 64 + b"\n", b"")
        # The first live validation succeeds; the second is after reservation.
        bad = len(proofs) == 2
        return ProcessResult(ProcessStatus.EXIT, 1 if bad and fault == "nonzero" else 0,
            (("g" * 64 if bad and fault == "nonhex" else CLI) + "\n").encode(), b"")
    monkeypatch.setattr(boundary_module, "_run_control_command", control_command)
    if provider == "agy":
        control = object.__new__(boundary_module.PinnedSSHSeatControl)
        control.controller, control.permit_id, control.epoch, control._lease = boundary.controller, boundary.permit_id, 1, None
        control.close = lambda: None
        transport._control = control
    else:
        transport._cli_attestor = lambda executor: transport._attest_linux_cli(transport.spec, executor, transport.handle)
    if fault is None:
        assert boundary_module.run_container_turn(request, boundary).valid
        assert len(sent) == 1 and len(proofs) == 3
    else:
        with pytest.raises(ProviderRequestError, match="live attestation failed|settings are unsafe") as rejected:
            boundary_module.run_container_turn(request, boundary)
        if provider != "agy":
            assert "CLI byte attestation failed" in str(rejected.value.__cause__)
        assert sent == []
        assert len(proofs) == (1 if provider == "agy" else 2)
        cold = resume_lifecycle_controller(boundary.controller.root, boundary.controller.capability)
        sessions = boundary_module.SessionStore(cold, request.run_id, request.task_id,
            request.target_id, request.seat_id, boundary.session_volume)
        with pytest.raises(ProviderRequestError, match="uncertain"):
            sessions.assert_clear(provider)
        with pytest.raises(ProviderRequestError, match="uncertain|exact session binding"):
            sessions.resume(provider)
    assert docker.inspect["State"]["Running"] is False
    assert boundary.admission.ledger.attempt_counts(boundary.permit_id, 1) == (0, 0)


@pytest.mark.parametrize("phase", ("after-send", "during-stream", "after-settlement"))
@pytest.mark.parametrize("cleanup_failure", (False, True))
def test_post_ready_worker_exception_rejects_terminal_and_retains_cold_truth(
    boundary_module, tmp_path, phase, cleanup_failure,
):
    import base64
    from scripts.fanout_container_certify import _fake_preview
    from shared.lib.fanout.container_broker import HostRelay
    from shared.lib.fanout.controller import resume_lifecycle_controller
    request, boundary, transport, docker, socket_attestor, _ = _launched_seat(boundary_module, tmp_path)
    admission = boundary.admission
    control = object.__new__(boundary_module.PinnedSSHSeatControl)
    control.controller, control.spec, control.ledger = boundary.controller, transport.spec, admission.ledger
    control.permit_id, control.epoch = admission.permit_id, admission.epoch
    sends, frames_sent = [], []
    def send(*args):
        sends.append(args)
        return BrokerResponse(200, {"content-type": "text/event-stream"}, [b'data: {"ok":true}\n\n'])
    send.preview = lambda *args: _fake_preview(admission.broker_permit, *args)
    control.broker = Broker(admission.broker_permit, credential="synthetic-private", send=send)
    body = json.dumps({"model": request.profile.requested_model, "messages": [], "max_tokens": 16}).encode()
    incoming = iter(({"type": "ready", "seat_id": request.seat_id,
        "permit_id": admission.permit_id, "epoch": admission.epoch},
        {"type": "request", "permit_id": admission.permit_id, "epoch": admission.epoch,
         "request_id": "actual-worker-request", "method": "POST", "provider_path": "/v1/messages",
         "headers": {"x-api-key": "disposable-placeholder", "content-type": "application/json"},
         "body_b64": base64.b64encode(body).decode()}))
    fault = RuntimeError("actual escaped worker failure")
    class Frames:
        acked = False
        def recv(self):
            try:
                return next(incoming)
            except StopIteration:
                if phase == "after-settlement" and not self.acked:
                    self.acked = True
                    end = next(frame for frame in frames_sent if frame["type"] == "response_end")
                    return {"type": "response_ack", "request_id": end["request_id"],
                            "response_digest": end["response_digest"]}
                raise fault
        def send(self, frame):
            frames_sent.append(frame)
            if ((phase == "after-send" and frame["type"] == "response_start")
                    or (phase == "during-stream" and frame["type"] == "response_chunk")):
                raise fault
    control._process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    owned_process = control._process
    control._socket_at_open = socket_attestor.proof
    control._lease = boundary_module.HostSessionLease(boundary.controller, admission.permit_id, admission.epoch)
    control._lease.__enter__()
    control._relay_error = None
    control._drain_relay = lambda _: None
    def closed_socket(*_args, **_kwargs):
        if cleanup_failure:
            raise OSError("actual additional socket cleanup failure")
        return b""
    control._ssh = closed_socket
    def worker():
        try:
            HostRelay(admission.ledger, control.broker).serve(Frames())
        except BaseException as error:
            control._relay_error = error
    control._thread = threading.Thread(target=worker, daemon=True)
    def terminal(_command):
        control._thread.start()
        control._thread.join(timeout=2)
        assert not control._thread.is_alive() and control._relay_error is fault, frames_sent
        assert len(sends) == 1
        assert any(frame["type"] == {"after-send": "response_start", "during-stream": "response_chunk",
                                    "after-settlement": "response_end"}[phase] for frame in frames_sent)
        return _terminal("claude", session_id=CLAUDE_ID)
    transport._process_runner = terminal
    transport._control = control
    try:
        with pytest.raises(ProviderRequestError, match="relay|worker") as raised:
            boundary_module.run_container_turn(request, boundary)
        assert raised.value.__cause__ is fault
        assert len(sends) == 1 and owned_process.poll() is not None
        assert control._process is None and control._thread is None and control._lease is None
        assert docker.inspect["State"]["Running"] is False
        cold = resume_lifecycle_controller(boundary.controller.root, boundary.controller.capability)
        reopened = PermitLedger(cold.root / "locks/spend", cold)
        state, _ = reopened.disposition("actual-worker-request")
        assert state == ("settled" if phase == "after-settlement" else "uncertain")
        assert reopened.attempt_counts(admission.permit_id, admission.epoch) == (1, 0)
        with reopened._locked() as durable:
            assert durable["permits"][admission.permit_id]["used"] == 1
        sessions = boundary_module.SessionStore(cold, request.run_id, request.task_id,
            request.target_id, request.seat_id, boundary.session_volume)
        with pytest.raises(ProviderRequestError, match="uncertain|quarantine"):
            sessions.resume("claude")
        assert transport.stop_evidence is not None
    finally:
        if owned_process.poll() is None:
            owned_process.kill()
            owned_process.wait(timeout=2)
        for pipe in (owned_process.stdin, owned_process.stdout):
            pipe.close()


@pytest.mark.parametrize("session_id", ("bad id", ":bad", "a" * 257, "a/b"))
def test_reserve_rejects_invalid_initial_id_before_any_write(boundary_module, tmp_path, session_id):
    _, _, _, sessions, *_ = _seat(boundary_module, tmp_path, "codex")
    before = {path.name: path.read_bytes() for path in sessions._record.parent.iterdir() if path.is_file()}
    with pytest.raises(ProviderRequestError, match="session identity"):
        sessions.reserve("claude", "a" * 64, initial=True,
            upstream_route_digest="a" * 64, initial_session_id=session_id)
    assert {path.name: path.read_bytes() for path in sessions._record.parent.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("phase", ("write", "fsync", "replace"))
def test_failed_session_write_removes_only_its_new_temp(boundary_module, tmp_path, monkeypatch, phase):
    _, _, _, sessions, *_ = _seat(boundary_module, tmp_path, "codex")
    sessions.reserve("codex", "a" * 64, initial=True, upstream_route_digest="a" * 64)
    original = sessions._record.read_bytes()
    foreign = sessions._record.parent / ".session-foreign"
    foreign.write_bytes(b"unrelated temporary record")
    payload = sessions._read()
    fault = OSError("actual session atomic write failure")
    def fail(*_args):
        raise fault
    with monkeypatch.context() as patch:
        patch.setattr(boundary_module.os, phase, fail)
        with pytest.raises(OSError) as raised:
            sessions._write(payload)
    assert raised.value is fault and sessions._record.read_bytes() == original
    assert foreign.read_bytes() == b"unrelated temporary record"
    assert sorted(path.name for path in sessions._record.parent.glob(".session-*")) == [foreign.name]


def test_second_stopped_inspect_rejects_non_dictionary_state(boundary_module, tmp_path):
    request, boundary, transport, docker, *_ = _launched_seat(boundary_module, tmp_path)
    original = transport.launcher._run
    def run(*args):
        if args == ("inspect", "--type", "container", transport.handle.container_id) and not docker.inspect["State"]["Running"]:
            item = json.loads(original(*args))[0]
            item["State"] = ["not a dictionary"]
            return json.dumps([item]).encode()
        return original(*args)
    transport.launcher._run = run
    with pytest.raises(ProviderRequestError, match="shutdown|state|stopped"):
        boundary_module.run_container_turn(request, boundary)
    with pytest.raises(ProviderRequestError, match="uncertain|quarantine"):
        boundary.session_store.resume("claude")


def test_initial_launch_seals_complete_volume_records_before_worker(boundary_module, tmp_path):
    _, boundary, transport, docker, _, sent = _launched_seat(boundary_module, tmp_path)
    launch = read_evidence(boundary.controller, "container-seat-launch",
                          transport.handle_evidence_name, transport.handle_evidence_digest)
    assert launch.get("volumes") == docker.original_volumes
    assert launch["volumes"]["workspace"]["CreatedAt"] == "2026-10-04T12:34:56.123456789Z"
    assert sent == []
    transport.close()


def test_successful_close_retains_authenticated_full_stopped_inspect(boundary_module, tmp_path):
    _, boundary, transport, docker, _, _ = _launched_seat(boundary_module, tmp_path)
    transport.close()
    name, digest = transport.stop_evidence
    proof = read_evidence(boundary.controller, "container-seat-stopped", name, digest)
    assert proof["handle"]["container_id"] == transport.handle.container_id
    assert proof["launch_evidence_digest"] == transport.handle_evidence_digest
    from shared.lib.fanout.artifacts import ArtifactRef, ArtifactStore
    with ArtifactStore.open_existing(boundary.controller.root / "quarantine" / "runtime-proofs") as store:
        raw = store.read_bytes(ArtifactRef(**proof["raw_inspect"]))
    assert json.loads(raw)[0]["State"]["Running"] is False
    assert json.loads(raw)[0]["Id"] == docker.inspect["Id"]
    transport.close()
    assert transport.stop_evidence == (name, digest)


def test_running_attestation_retains_exact_raw_validation_observations(boundary_module, tmp_path):
    _, _, transport, docker, _, _ = _launched_seat(boundary_module, tmp_path)
    assert transport.attest_container() == transport.handle
    observations = transport.runtime_observation
    import base64
    raw = next(base64.b64decode(item["stdout"]) for item in observations
               if item["argv"] == ["inspect", "--type", "container", transport.handle.container_id])
    assert json.loads(raw)[0] == docker.inspect


def test_running_attestation_refuses_complete_transcript_over_artifact_budget(boundary_module, tmp_path):
    def retained_inventory(docker):
        for index in range(51):
            container_id = f"{index + 1000:064x}"
            docker.old[container_id] = {"Id": container_id, "Config": {"Labels": {}},
                "HostConfig": {"Mounts": []}, "Mounts": [], "Padding": "x" * 990_000}
    with pytest.raises(ProviderRequestError) as failure:
        _launched_seat(boundary_module, tmp_path, before_start=retained_inventory)
    assert "runtime" in str(failure.value.__cause__)
    assert "budget" in str(failure.value.__cause__)


def test_vm_attestation_retains_complete_observed_bytes(boundary_module, tmp_path, monkeypatch):
    import base64
    from scripts import fanout_container_vm_probe as vm_probe
    monkeypatch.setattr(Path, "home", classmethod(lambda _: tmp_path))
    control = _private_failure_control(boundary_module, tmp_path)
    control._host_key = "ssh-ed25519 " + "A" * 44
    colima = tmp_path / ".colima/fanout-isolated/colima.yaml"
    lima = tmp_path / ".colima/_lima/colima-fanout-isolated/lima.yaml"
    colima.parent.mkdir(parents=True)
    lima.parent.mkdir(parents=True)
    colima.write_text("arch: x86_64\nvmType: qemu\nmounts: null\n")
    lima.write_text("mounts: []\n")
    context = {"Name": "colima-fanout-isolated", "Endpoints": {"docker": {
        "Host": f"unix://{tmp_path}/.colima/fanout-isolated/docker.sock"}}}
    def run(argv, **_kwargs):
        output = (json.dumps(context).encode() if "context" in argv else
                  b"27.0\n" if "info" in argv else b'{"status":"Running"}')
        return ProcessResult(ProcessStatus.EXIT, 0, output, b"")
    monkeypatch.setattr(boundary_module, "_run_control_command", run)
    monkeypatch.setattr(vm_probe, "_profile_identity", lambda *_: {"synthetic": True})
    monkeypatch.setattr(vm_probe, "_read_binding", lambda *_: {"synthetic": True})
    monkeypatch.setattr(vm_probe, "_require_profile_binding", lambda *_: None)
    control._ssh = lambda argv, data=None, **_kwargs: data if data is not None else b"proc /proc proc rw 0 0\n"
    proof = control.attest_vm()
    raw = control.vm_observation
    assert base64.b64decode(raw["colima"]) == colima.read_bytes()
    assert base64.b64decode(raw["lima"]) == lima.read_bytes()
    assert base64.b64decode(raw["nonce"]) == base64.b64decode(raw["reply"])
    assert raw["probe"] == asdict(proof)
    assert raw["profile_identity"] == {"synthetic": True}
    assert raw["profile_binding"] == {"synthetic": True}


def test_model_docker_exec_uses_sealed_published_repo_workdir(
    boundary_module, tmp_path, monkeypatch,
):
    request, boundary, transport, _, _, _ = _launched_seat(boundary_module, tmp_path)
    wrapped = []

    def bounded(command):
        wrapped.append(command.argv)
        return _terminal("claude", session_id=CLAUDE_ID)

    monkeypatch.setattr(boundary_module, "_run_bounded_command", bounded)
    transport._process_runner = lambda command: transport._docker_exec(
        transport.handle, command, transport._model_workdir)
    result = boundary_module.run_container_turn(request, boundary)
    assert result.valid
    assert wrapped[0][:10] == (
        "docker", "--context", "colima-fanout-isolated", "exec", "-i",
        "--user", "65532:65532", "--workdir", boundary.input_workdir,
        transport.handle.container_id,
    )
    assert boundary.input_workdir.endswith("/published/repo")


@pytest.mark.parametrize("stdout_bytes,stderr_bytes,limit,refused", [
    (1025, 0, 1024, True), (0, 1025, 1024, True), (600, 600, 1024, True),
    (600, 424, 1024, False), (600, 600, None, False),
])
def test_model_docker_exec_honors_original_combined_capture_limit(
    boundary_module, tmp_path, monkeypatch, stdout_bytes, stderr_bytes, limit, refused,
):
    _, boundary, transport, _, _, _ = _launched_seat(boundary_module, tmp_path)
    real_popen = subprocess.Popen
    pid_file = tmp_path / "bounded-child.pid"
    script = (
        "import os,sys,time;"
        "open(sys.argv[1],'w').write(str(os.getpid()));"
        "os.write(1,b'a'*int(sys.argv[2]));"
        "os.write(2,b'b'*int(sys.argv[3]));"
        "time.sleep(0.5)"
    )

    def inert_container(argv, **kwargs):
        assert tuple(argv[:10]) == (
            "docker", "--context", "colima-fanout-isolated", "exec", "-i",
            "--user", "65532:65532", "--workdir", boundary.input_workdir,
            transport.handle.container_id,
        )
        return real_popen(
            (sys.executable, "-c", script, str(pid_file),
             str(stdout_bytes), str(stderr_bytes)), **kwargs,
        )

    monkeypatch.setattr(boundary_module.subprocess, "Popen", inert_container)
    command = ProcessCommand(
        ("claude", "--print"), b"", cwd=tmp_path, timeout=3,
        slot_root=tmp_path / "slots", term_grace=0.01, reap_timeout=1,
        max_output_bytes=limit,
    )
    if refused:
        with pytest.raises(ProviderRequestError, match="output limit"):
            transport._docker_exec(transport.handle, command, boundary.input_workdir)
    else:
        result = transport._docker_exec(transport.handle, command, boundary.input_workdir)
        assert result.status == ProcessStatus.EXIT and result.returncode == 0
        assert result.stdout == b"a" * stdout_bytes
        assert result.stderr == b"b" * stderr_bytes
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_fixed_docker_execution_retains_raw_result_and_clears_failed_attempt(
    boundary_module, tmp_path, monkeypatch,
):
    _, boundary, transport, _, _, callback_calls = _launched_seat(boundary_module, tmp_path)
    transport._production_edge = True
    terminal = ProcessResult(ProcessStatus.EXIT, 0, b"raw stdout\r\n", b"raw stderr\n")
    commands = []

    def docker_io(command, *, before_spawn=None):
        if before_spawn is not None:
            before_spawn()
        commands.append(command)
        if len(commands) == 2:
            raise RuntimeError("fixed execution failed")
        return terminal

    monkeypatch.setattr(boundary_module, "_run_bounded_command", docker_io)
    command = ProcessCommand(("claude", "--print"), b"private synthetic input")
    assert transport.run(command, boundary) is terminal
    assert transport.process_result is terminal
    assert transport.process_result.stdout == b"raw stdout\r\n"
    assert transport.process_result.stderr == b"raw stderr\n"
    with pytest.raises(RuntimeError, match="fixed execution failed"):
        transport.run(command, boundary)
    assert transport.process_result is None
    assert callback_calls == []
    assert len(commands) == 2
    assert commands[0].argv[:10] == (
        "docker", "--context", "colima-fanout-isolated", "exec", "-i",
        "--user", "65532:65532", "--workdir", boundary.input_workdir,
        transport.handle.container_id,
    )
    transport.close()


def test_refusal_stops_exact_owned_seat_without_prompt_send(boundary_module, tmp_path):
    request, boundary, _, docker, socket, sent = _launched_seat(boundary_module, tmp_path)
    socket.proof = replace(socket.proof, inode=socket.proof.inode + 1)
    with pytest.raises(ProviderRequestError, match="socket"):
        boundary_module.run_container_turn(request, boundary)
    assert sent == []
    assert docker.inspect["State"]["Running"] is False


def test_process_failure_stops_seat_and_retains_uncertain_turn(
    boundary_module, tmp_path,
):
    request, boundary, transport, docker, _, sent = _launched_seat(
        boundary_module, tmp_path)

    def failed_process(_command):
        raise ProviderRequestError("container process output limit exceeded")

    transport._process_runner = failed_process
    with pytest.raises(ProviderRequestError, match="output limit"):
        boundary_module.run_container_turn(request, boundary)
    assert sent == []
    assert docker.inspect["State"]["Running"] is False
    with pytest.raises(ProviderRequestError, match="uncertain"):
        boundary.session_store.resume("claude")


def test_valid_terminal_is_not_settled_when_exact_stop_fails(
    boundary_module, tmp_path,
):
    request, boundary, _, docker, _, sent = _launched_seat(boundary_module, tmp_path)
    docker.fail_stop = True
    with pytest.raises(ProviderRequestError, match="stop|shutdown"):
        boundary_module.run_container_turn(request, boundary)
    assert len(sent) == 1
    assert docker.inspect["State"]["Running"] is True
    cold = boundary_module.SessionStore(boundary.controller, request.run_id,
                                        request.task_id, request.target_id,
                                        request.seat_id, boundary.session_volume)
    with pytest.raises(ProviderRequestError, match="uncertain"):
        cold.resume("claude")


@pytest.mark.parametrize("mismatch", ("request", "boundary"))
def test_mismatched_turn_cannot_close_another_owned_seat(
    boundary_module, tmp_path, mismatch,
):
    request, boundary, _, docker, _, sent = _launched_seat(boundary_module, tmp_path)
    if mismatch == "request":
        request = replace(request, target_id="different-target")
    else:
        boundary = replace(boundary, target_id="different-target")
    with pytest.raises(ProviderRequestError):
        boundary_module.run_container_turn(request, boundary)
    assert sent == []
    assert docker.inspect["State"]["Running"] is True
    assert "stop-seat" not in docker.events


def test_changed_retry_policy_cannot_close_repo_write_seat(
    boundary_module, tmp_path,
):
    request, boundary, _, docker, _, sent = _launched_seat(boundary_module, tmp_path)
    changed = replace(request, retries=1)
    with pytest.raises(ProviderRequestError):
        boundary_module.run_container_turn(changed, boundary)
    assert sent == []
    assert docker.inspect["State"]["Running"] is True


def test_transient_shutdown_authority_failure_refuses_before_prompt_send(
    boundary_module, tmp_path, monkeypatch,
):
    request, boundary, _, docker, _, sent = _launched_seat(
        boundary_module, tmp_path)
    original = boundary_module.read_evidence
    attempts = 0

    def first_admission_read_fails(controller, category, name, digest):
        nonlocal attempts
        if category == "container-seat-admission":
            attempts += 1
            if attempts == 1:
                raise OSError("transient admission evidence read failure")
        return original(controller, category, name, digest)

    monkeypatch.setattr(boundary_module, "read_evidence",
                        first_admission_read_fails)
    with pytest.raises(ProviderRequestError, match="shutdown authority"):
        boundary_module.run_container_turn(request, boundary)
    assert attempts >= 1
    assert sent == []
    assert docker.inspect["State"]["Running"] is True
    assert "stop-seat" not in docker.events


def test_primary_process_failure_survives_failed_shutdown(
    boundary_module, tmp_path,
):
    request, boundary, transport, docker, _, _ = _launched_seat(
        boundary_module, tmp_path)
    docker.fail_stop = True

    def fail_process(_command):
        raise ProviderRequestError("primary process failure")

    transport._process_runner = fail_process
    with pytest.raises(ProviderRequestError, match="primary process failure") as raised:
        boundary_module.run_container_turn(request, boundary)
    assert any("cleanup" in note.lower() for note in raised.value.__notes__)
    with pytest.raises(ProviderRequestError, match="uncertain"):
        boundary.session_store.resume("claude")
    receipts = boundary.controller.root / "receipts"
    cleanup = [path for path in receipts.glob("*.json") if
               json.loads(path.read_text()).get("category") ==
               "container-seat-cleanup-failure"]
    assert len(cleanup) == 1
    proof = read_evidence(boundary.controller, "container-seat-cleanup-failure",
                          cleanup[0].name, cleanup[0].stem)
    assert proof["container_id"] == boundary.container_id
    assert proof["primary_type"] == "ProviderRequestError"
    assert proof["cleanup_type"] == "ProviderRequestError"


def test_failed_shutdown_after_owned_pre_reservation_refusal_blocks_seat(
    boundary_module, tmp_path,
):
    request, boundary, _, docker, socket, sent = _launched_seat(
        boundary_module, tmp_path)
    docker.fail_stop = True
    socket.proof = replace(socket.proof, inode=socket.proof.inode + 1)
    with pytest.raises(ProviderRequestError, match="guest socket changed") as raised:
        boundary_module.run_container_turn(request, boundary)
    assert sent == []
    assert any("cleanup" in note.lower() for note in raised.value.__notes__)
    with pytest.raises(ProviderRequestError, match="uncertain"):
        boundary.session_store.assert_clear("claude")
    cold = boundary_module.SessionStore(boundary.controller, request.run_id,
                                        request.task_id, request.target_id,
                                        request.seat_id, boundary.session_volume)
    with pytest.raises(ProviderRequestError, match="uncertain"):
        cold.assert_clear("claude")


def test_issue_primary_error_survives_failed_owned_cleanup(
    boundary_module, tmp_path, monkeypatch,
):
    request, boundary, transport, docker, _, _ = _launched_seat(
        boundary_module, tmp_path)
    docker.fail_stop = True
    original = boundary_module.write_evidence

    def fail_admission(controller, category, payload):
        if category == "container-seat-admission":
            raise OSError("primary admission receipt failure")
        return original(controller, category, payload)

    monkeypatch.setattr(boundary_module, "write_evidence", fail_admission)
    with pytest.raises(OSError, match="primary admission receipt failure") as raised:
        boundary_module.issue_container_boundary(
            boundary.controller, request, transport.attest_vm(),
            transport.handle, boundary.admission)
    assert any("cleanup" in note.lower() for note in raised.value.__notes__)
    with pytest.raises(ProviderRequestError, match="uncertain"):
        boundary.session_store.assert_clear("claude")


def test_start_live_primary_error_survives_failed_relay_cleanup(
    boundary_module, tmp_path, monkeypatch,
):
    controller = create_lifecycle_controller(tmp_path / "controller")

    class BrokenControl:
        def __init__(self, *_args):
            pass

        def open(self):
            raise RuntimeError("primary relay-open failure")

        def close(self):
            raise RuntimeError("secondary relay-close failure")

    monkeypatch.setattr(boundary_module, "PinnedSSHSeatControl", BrokenControl)
    permit = BrokerPermit(
        provider="claude", run_id="run-one", task_id="task-one",
        target_id="target-one", seat_id="seat-one", route="initial",
        model="test-model", placeholder_key="disposable-placeholder",
        max_requests=1, max_request_bytes=1024, max_response_bytes=1024,
        max_output_tokens=32, upstream_route_digest="a" * 64,
        upstream_origin="https://eu-aiplatform.googleapis.com",
        upstream_model="test-model", upstream_path="/v1/pinned-route",
    )
    broker = Broker(permit, credential="controller-secret",
                    send=lambda *_: BrokerResponse(200, {}, b""))
    workspace, session = private_volume_names("run-one", "task-one", "target-one", "seat-one")
    spec = ContainerSpec("run-one", "task-one", "target-one", "seat-one", IMAGE,
                         workspace, session, "/run/fanout/seat-one.sock", CLI)
    with pytest.raises(RuntimeError, match="primary relay-open failure") as raised:
        boundary_module.PinnedDockerTransport.start_live(
            controller, spec, ledger=None, broker=broker, permit_id="a" * 64,
            epoch=1)
    assert any("cleanup" in note.lower() for note in raised.value.__notes__)
    receipts = controller.root / "receipts"
    cleanup = [path for path in receipts.glob("*.json") if
               json.loads(path.read_text()).get("category") ==
               "container-seat-cleanup-failure"]
    assert len(cleanup) == 1
    proof = read_evidence(controller, "container-seat-cleanup-failure",
                          cleanup[0].name, cleanup[0].stem)
    assert proof["phase"] == "relay-start"


def test_close_refuses_to_stop_changed_owner_labels(boundary_module, tmp_path):
    _, _, transport, docker, _, _ = _launched_seat(boundary_module, tmp_path)
    docker.inspect["Config"]["Labels"]["io.khenrix.fanout.run"] = "foreign-run"
    with pytest.raises(ProviderRequestError, match="ownership|identity"):
        transport.close()
    assert docker.inspect["State"]["Running"] is True
    assert "stop-seat" not in docker.events


def test_close_refuses_extra_unissued_owner_label(boundary_module, tmp_path):
    _, _, transport, docker, _, _ = _launched_seat(boundary_module, tmp_path)
    docker.inspect["Config"]["Labels"]["io.khenrix.fanout.extra"] = "unissued"
    with pytest.raises(ProviderRequestError, match="ownership"):
        transport.close()
    assert docker.inspect["State"]["Running"] is True
    assert "stop-seat" not in docker.events


def test_close_preserves_stop_failure_when_relay_close_also_fails(
    boundary_module, tmp_path,
):
    _, _, transport, docker, _, _ = _launched_seat(boundary_module, tmp_path)
    docker.fail_stop = True

    class BrokenRelay:
        def close(self):
            raise RuntimeError("secondary relay close failure")

    transport._control = BrokenRelay()
    with pytest.raises(ProviderRequestError, match="Docker stop operation failed") as raised:
        transport.close()
    assert any("relay" in note.lower() for note in raised.value.__notes__)
    assert docker.inspect["State"]["Running"] is True


def test_transport_retains_relay_control_after_failed_close(
    boundary_module, tmp_path,
):
    _, _, transport, docker, _, _ = _launched_seat(boundary_module, tmp_path)

    class RetryRelay:
        calls = 0

        def close(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("relay remains live")

    relay = RetryRelay()
    transport._control = relay
    with pytest.raises(RuntimeError, match="relay remains live"):
        transport.close()
    assert transport._control is relay
    assert docker.inspect["State"]["Running"] is False
    transport.close()
    assert relay.calls == 2
    assert transport._control is None


def _partial_ssh_control(module, tmp_path, monkeypatch, process):
    class Lease:
        entered = False
        released = False

        def __init__(self, *_args):
            pass

        def __enter__(self):
            type(self).entered = True
            return self

        def __exit__(self, *_args):
            type(self).released = True

    controller = create_lifecycle_controller(tmp_path / "controller")
    control = object.__new__(module.PinnedSSHSeatControl)
    control.controller = controller
    control.spec = SimpleNamespace(seat_id="seat-test", socket_path="/run/fanout/seat-test.sock")
    control.broker = SimpleNamespace(permit=SimpleNamespace(
        provider="claude", model="test-model", max_requests=1,
        max_request_bytes=1024, max_response_bytes=1024))
    control.ledger = object()
    control.permit_id = "a" * 64
    control.epoch = 1
    control._lease = None
    control._process = None
    control._thread = None
    control._relay_error = None
    control._guest_relay_path = None
    control._socket_at_open = None
    control._bootstrap = lambda: setattr(control, "_guest_relay_path", "/run/fanout/relay.py")
    control._argv = lambda _guest: ("ssh", "pinned")
    monkeypatch.setattr(module, "HostSessionLease", Lease)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    return control, Lease


def test_relay_thread_start_failure_preserves_primary_and_releases_lease(
    boundary_module, tmp_path, monkeypatch,
):
    class Process:
        terminated = False
        stdin = object()
        stdout = object()

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, *, timeout):
            assert timeout == 3
            return 0

    class UnstartedThread:
        ident = None

        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("primary thread start failure")

        def join(self, *, timeout):
            raise RuntimeError("cannot join thread before it is started")

        def is_alive(self):
            return False

    process = Process()
    control, lease = _partial_ssh_control(boundary_module, tmp_path,
                                           monkeypatch, process)
    monkeypatch.setattr(boundary_module.threading, "Thread", UnstartedThread)
    with pytest.raises(RuntimeError, match="primary thread start failure"):
        control.open()
    assert process.terminated
    assert control._lease is None
    assert lease.entered and lease.released


def test_agy_high_guest_receives_attempt_ceiling_while_permit_keeps_spend_limit(
    boundary_module, tmp_path, monkeypatch,
):
    class Process:
        stdin = object()
        stdout = object()

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, *, timeout):
            return 0

    class UnstartedThread:
        ident = None

        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("stop before relay thread")

        def join(self, *, timeout):
            pass

        def is_alive(self):
            return False

    process = Process()
    control, _ = _partial_ssh_control(boundary_module, tmp_path, monkeypatch, process)
    control.broker.permit = BrokerPermit(
        provider="agy", run_id="run", task_id="task", target_id="target",
        seat_id="seat-test", route="initial", model="gemini-3.8-flash-high",
        placeholder_key="disposable-placeholder", max_requests=32,
        max_request_bytes=1024, max_response_bytes=1024, max_output_tokens=32)
    captured = []
    monkeypatch.setattr(boundary_module, "_as_guest_relay_uid", lambda *argv: (
        captured.append(argv), argv)[1])
    monkeypatch.setattr(boundary_module.threading, "Thread", UnstartedThread)
    with pytest.raises(RuntimeError, match="stop before relay thread"):
        control.open()
    assert captured
    arguments = captured[0]
    assert arguments[arguments.index("--max-requests") + 1] == "35"
    assert control.broker.permit.max_requests == 32


def test_relay_open_preserves_primary_when_process_cleanup_also_fails(
    boundary_module, tmp_path, monkeypatch,
):
    class Process:
        terminated = False
        stdin = object()
        stdout = object()

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True
            raise OSError("secondary terminate failure")

        def wait(self, *, timeout):
            return 0

    class UnstartedThread:
        ident = None

        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("primary thread start failure")

        def is_alive(self):
            return False

    control, lease = _partial_ssh_control(boundary_module, tmp_path,
                                           monkeypatch, Process())
    monkeypatch.setattr(boundary_module.threading, "Thread", UnstartedThread)
    with pytest.raises(RuntimeError, match="primary thread start failure") as raised:
        control.open()
    assert lease.released
    assert any("cleanup" in note.lower() for note in raised.value.__notes__)
    receipts = control.controller.root / "receipts"
    cleanup = [path for path in receipts.glob("*.json") if
               json.loads(path.read_text()).get("category") ==
               "container-seat-cleanup-failure"]
    assert len(cleanup) == 1
    proof = read_evidence(control.controller, "container-seat-cleanup-failure",
                          cleanup[0].name, cleanup[0].stem)
    assert proof["phase"] == "relay-open"
    assert proof["primary_type"] == "RuntimeError"
    assert proof["cleanup_type"] == "OSError"


def test_live_relay_thread_prevents_successful_close_and_lease_release(
    boundary_module, tmp_path, monkeypatch,
):
    class Process:
        def poll(self):
            return 0

        def wait(self, *, timeout):
            return 0

    control, lease = _partial_ssh_control(boundary_module, tmp_path,
                                           monkeypatch, Process())
    control._lease = lease()
    control._lease.__enter__()
    control._process = Process()
    release = threading.Event()
    thread = threading.Thread(target=lambda: release.wait(10), daemon=True)
    thread.start()
    control._thread = thread
    try:
        with pytest.raises(ProviderRequestError, match="relay|thread"):
            control.close()
        assert thread.is_alive()
        assert control._thread is thread
        assert control._lease is not None
        assert not lease.released
    finally:
        release.set()
        thread.join(timeout=1)
        control.close()
    assert lease.released
    assert control._lease is None


@pytest.fixture
def local_drain_control(boundary_module, tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        control, lease = _partial_ssh_control(boundary_module, tmp_path, patch, None)
    # A short native path fits Unix sockets even under a long pytest TMPDIR.
    with tempfile.TemporaryDirectory(prefix="fanout-drain-") as directory:
        path = str(Path(directory) / "relay.sock")
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(path)
            info = os.lstat(path)
            control._socket_at_open = GuestSocketAttestation(
                path, info.st_uid, info.st_mode, info.st_dev,
                info.st_ino, info.st_ctime_ns)
            control.broker.permit.max_requests = 4
            yield control, lease


def _execute_drain_guest_locally(control, monkeypatch, error, stage, events):
    class Exchange:
        def settimeout(self, value):
            assert value == 2

        def connect(self, path):
            assert path == control._socket_at_open.path
            self.exchange("connect")

        def sendall(self, data):
            assert data == (b"POST http://invalid.invalid/ HTTP/1.1\r\n"
                            b"Host: 127.0.0.1:43187\r\nContent-Length: 0\r\n\r\n")
            self.exchange("sendall")

        def recv(self, size):
            assert size == 1024
            self.exchange("recv")
            return b""

        def exchange(self, current):
            events.append(current)
            if current == stage:
                raise error

        def close(self):
            events.append("connection_closed")

    def ssh(guest, data=b"", *, operation, timeout=30):
        assert guest[:3] == ("sudo", "-n", "python3")
        assert guest[5] == "-c" and data == b"" and timeout == 30
        events.append(operation)
        with monkeypatch.context() as patch:
            patch.setattr(sys, "argv", ["-c", *guest[7:]])
            if operation == "relay_drain":
                def connection(family):
                    assert family == socket.AF_UNIX
                    return Exchange()
                patch.setattr(socket, "socket", connection)
            else:
                assert operation == "socket_closed_check"
                assert control._process is None and control._thread is None
            try:
                exec(compile(guest[6], "<guest-control>", "exec"), {})
            except SystemExit as failure:
                if failure.code:
                    raise ProviderRequestError("local guest control failed") from failure
        return b""

    monkeypatch.setattr(control, "_ssh", ssh, raising=False)


@pytest.mark.parametrize("provider,model,slots", [
    ("claude", "test-model", 4),
    ("codex", "test-model", 4),
    ("agy", "gemini-3.8-flash-high", 6),
])
def test_owner_drain_closes_idle_relay_including_final_refusal(
    boundary_module, tmp_path, monkeypatch, provider, model, slots,
):
    control, _ = _partial_ssh_control(boundary_module, tmp_path, monkeypatch, None)
    control.broker.permit.provider = provider
    control.broker.permit.model = model
    control.broker.permit.max_requests = 4
    guest = broker_tests._guest_module()
    with tempfile.TemporaryDirectory(prefix="fanout-drain-", dir="/tmp") as directory:
        monkeypatch.setattr(guest, "GUEST_SOCKET_ROOT", directory)
        frames = broker_tests._Frames([{"type": "ready_ack"}, {"type": "closed"}])
        relay = guest.GuestRelay("seat-test", "a" * 64, 1, frames,
                                 max_requests=slots, max_request_bytes=1024,
                                 max_response_bytes=1024)
        relay.timeout = 1
        info = os.lstat(guest.seat_socket_path("seat-test"))
        proof = GuestSocketAttestation(
            guest.seat_socket_path("seat-test"), info.st_uid, info.st_mode,
            info.st_dev, info.st_ino, info.st_ctime_ns)
        failures = []

        def serve():
            try:
                relay.serve()
            except Exception as error:
                failures.append(error)

        def ssh(argv, data=b"", *, operation, timeout=30):
            assert operation == "relay_drain" and data == b"" and timeout == 30
            with monkeypatch.context() as patch:
                patch.setattr(sys, "argv", ["-c", *argv[7:]])
                exec(compile(argv[6], "<guest-control>", "exec"), {})
            return b""

        monkeypatch.setattr(control, "_ssh", ssh, raising=False)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            control._drain_relay(proof)
            thread.join(timeout=0.1)
            assert not thread.is_alive()
            assert not os.path.lexists(proof.path)
            assert not failures
            assert [frame["type"] for frame in frames.sent] == ["ready", "close"]
        finally:
            thread.join(timeout=2)


@pytest.mark.parametrize("stage,exchanges", [
    ("connect", ["connect"]),
    ("sendall", ["connect", "sendall"]),
    ("recv", ["connect", "sendall", "recv"]),
])
def test_drain_connection_reset_stops_without_another_exchange(
    local_drain_control, monkeypatch, stage, exchanges,
):
    control, _ = local_drain_control
    proof = control._socket_at_open
    events = []
    _execute_drain_guest_locally(control, monkeypatch,
                                ConnectionResetError(104, "reset"), stage, events)
    control._drain_relay(proof)
    assert events == ["relay_drain", *exchanges, "connection_closed"]
    assert os.path.lexists(proof.path)
    assert control._socket_at_open == proof


@pytest.mark.parametrize("socket_removed", (True, False), ids=("absent", "remaining"))
def test_reset_close_requires_independent_socket_absence(
    local_drain_control, monkeypatch, socket_removed,
):
    control, lease = local_drain_control
    proof = control._socket_at_open
    events = []
    _execute_drain_guest_locally(control, monkeypatch,
                                ConnectionResetError(104, "reset"), "recv", events)
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys;sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    control._process = process
    control._lease = lease()
    control._lease.__enter__()

    def finish():
        process.wait()
        if socket_removed:
            os.unlink(proof.path)

    control._thread = threading.Thread(target=finish, daemon=True)
    control._thread.start()
    try:
        if socket_removed:
            control.close()
            assert control._socket_at_open is None
        else:
            with pytest.raises(ProviderRequestError, match="did not close cleanly") as raised:
                control.close()
            assert isinstance(raised.value.__cause__, ProviderRequestError)
            assert control._socket_at_open == proof
        assert events == ["relay_drain", "connect", "sendall", "recv",
                          "connection_closed", "socket_closed_check"]
        assert process.poll() is not None
        assert control._process is None and control._thread is None
        assert lease.released and control._lease is None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        process.stdin.close()
        if os.path.lexists(proof.path):
            os.unlink(proof.path)
        control.close()


def test_drain_changed_socket_identity_prevents_exchange(local_drain_control, monkeypatch):
    control, _ = local_drain_control
    proof = control._socket_at_open
    events = []
    _execute_drain_guest_locally(control, monkeypatch,
                                ConnectionResetError(104, "reset"), "recv", events)
    os.unlink(proof.path)
    with socket.socket(socket.AF_UNIX) as replacement:
        replacement.bind(proof.path)
        with pytest.raises(ProviderRequestError, match="local guest control failed"):
            control._drain_relay(proof)
    assert events == ["relay_drain"]
    assert os.path.lexists(proof.path)


@pytest.mark.parametrize("error", (TimeoutError("timed out"), OSError(5, "I/O error")),
                         ids=("timeout", "unrelated-oserror"))
def test_drain_timeout_and_unrelated_errors_remain_failures(
    local_drain_control, monkeypatch, error,
):
    control, _ = local_drain_control
    events = []
    _execute_drain_guest_locally(control, monkeypatch, error, "recv", events)
    with pytest.raises(type(error)) as raised:
        control._drain_relay(control._socket_at_open)
    assert raised.value is error
    assert events == ["relay_drain", "connect", "sendall", "recv", "connection_closed"]
    assert os.path.lexists(control._socket_at_open.path)


@pytest.mark.parametrize("stream", (1, 2))
def test_host_capture_reaps_child_on_excess_output(boundary_module, tmp_path, stream):
    pid_file = tmp_path / "child.pid"
    script = (
        "import os,sys,time;"
        "open(sys.argv[1],'w').write(str(os.getpid()));"
        "[os.write(int(sys.argv[2]),b'x'*8192) for _ in range(100)];"
        "time.sleep(30)"
    )
    command = ProcessCommand(
        (sys.executable, "-c", script, str(pid_file), str(stream)), b"",
        cwd=tmp_path, timeout=5, slot_root=tmp_path / "slots",
        term_grace=0.01, reap_timeout=1,
    )
    with pytest.raises(ProviderRequestError, match="output limit"):
        boundary_module._run_bounded_command(command, stdout_cap=1024,
                                             stderr_cap=1024)
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_host_capture_timeout_is_bounded_terminal(boundary_module, tmp_path):
    command = ProcessCommand(
        (sys.executable, "-c", "import os,time;os.write(1,b'partial');time.sleep(30)"),
        b"", cwd=tmp_path, timeout=0.1, slot_root=tmp_path / "slots",
        term_grace=0.01, reap_timeout=1,
    )
    result = boundary_module._run_bounded_command(command, stdout_cap=1024,
                                                  stderr_cap=1024)
    assert result.status == ProcessStatus.TIMEOUT
    assert result.stdout == b"partial"
    assert result.timeout is not None and result.timeout.reaped


@pytest.mark.parametrize("stream,cap", [(1, 1024 * 1024), (2, 512 * 1024)])
def test_named_ssh_capture_stops_reading_and_reaps_on_overflow(
    boundary_module, tmp_path, monkeypatch, stream, cap,
):
    control = _private_failure_control(boundary_module, tmp_path)
    pid_file = tmp_path / "ssh-client.pid"
    script = ("import os,sys,time;"
              "open(sys.argv[1],'w').write(str(os.getpid()));"
              "[os.write(int(sys.argv[2]),b'x'*8192) for _ in range(256)];"
              "time.sleep(30)")
    control._argv = lambda _: (sys.executable, "-c", script, str(pid_file), str(stream))
    popen, read = subprocess.Popen, os.read
    output_fds = set()
    observed = 0

    def spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        output_fds.add((process.stdout if stream == 1 else process.stderr).fileno())
        return process

    def counted_read(fd, size):
        nonlocal observed
        raw = read(fd, size)
        if fd in output_fds:
            observed += len(raw)
        return raw

    monkeypatch.setattr(boundary_module.subprocess, "Popen", spawn)
    monkeypatch.setattr(boundary_module.os, "read", counted_read)
    with pytest.raises(ProviderRequestError):
        control._ssh(("trusted",), operation="vm_nonce", timeout=3)
    assert observed <= cap + 1
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@pytest.mark.parametrize("operation", ("linux-cli", "agy-settings", "vm-probe"))
@pytest.mark.parametrize("failure", (None, "stdout", "stderr", "deadline"))
def test_named_control_joins_bound_capture_and_never_accept_partial_success(
    boundary_module, tmp_path, monkeypatch, operation, failure,
):
    from scripts.fanout_container_vm_probe import Evidence
    digest = (CLI if operation != "agy-settings" else
              __import__("hashlib").sha256(boundary_module._AGY_SETTINGS_BYTES).hexdigest())
    script = "import os,time;os.write(1," + repr((digest + "\n").encode()) + ");"
    if failure in ("stdout", "stderr"):
        script += f"[os.write({1 if failure == 'stdout' else 2},b'x'*8192) for _ in range(256)];time.sleep(30)"
    elif failure == "deadline":
        script += "time.sleep(30)"
    original = boundary_module._run_control_command
    popen, read = subprocess.Popen, os.read
    processes, selected_fds, calls = [], set(), []
    observed = 0
    def spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        selected_fds.add((process.stderr if failure == "stderr" else process.stdout).fileno())
        return process
    def counted_read(fd, size):
        nonlocal observed
        raw = read(fd, size)
        if fd in selected_fds:
            observed += len(raw)
        return raw
    def local_command(argv, *, data=b"", timeout=30):
        calls.append(tuple(argv))
        return original((sys.executable, "-c", script), data=data,
                        timeout=0.05 if failure == "deadline" else timeout)
    monkeypatch.setattr(boundary_module.subprocess, "Popen", spawn)
    monkeypatch.setattr(boundary_module.os, "read", counted_read)
    monkeypatch.setattr(boundary_module, "_run_control_command", local_command)
    spec = ContainerSpec("run", "task", "target", "seat", IMAGE, *private_volume_names("run", "task", "target", "seat"),
                         "/run/fanout/seat.sock", CLI)
    handle = ContainerHandle("c" * 64, "d" * 64, spec.workspace_volume, spec.session_volume)
    def invoke():
        if operation == "linux-cli":
            return boundary_module.PinnedDockerTransport._attest_linux_cli(spec, "claude", handle)
        if operation == "agy-settings":
            return boundary_module.PinnedDockerTransport._provision_agy_settings(handle)
        return Evidence.run(SimpleNamespace(data={"commands": []}, save=lambda: None), ("colima", "status"))
    if failure is None:
        result = invoke()
        assert result == (None if operation == "agy-settings" else digest + ("\n" if operation == "vm-probe" else ""))
    else:
        with pytest.raises(ProviderRequestError):
            invoke()
    assert calls and len(processes) == 1 and processes[0].poll() is not None
    assert observed <= (512 * 1024 if failure == "stderr" else 1024 * 1024) + 1
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)


@pytest.mark.parametrize("drift", ["socket", "container", "permit"])
def test_model_slot_wait_rechecks_authority_before_settings_or_spawn(
    boundary_module, tmp_path, monkeypatch, drift,
):
    _, boundary, transport, docker, socket, _ = _launched_seat(boundary_module, tmp_path)
    transport._production_edge = True
    transport._control = SimpleNamespace()
    effects = []

    @contextmanager
    def waited_slot(**_kwargs):
        if drift == "socket":
            socket.proof = replace(socket.proof, inode=socket.proof.inode + 1)
        elif drift == "container":
            docker.inspect["Config"]["Labels"][runtime_tests.BOUNDARY + "seat"] = "foreign"
        else:
            boundary.admission.ledger.fence_epoch(boundary.admission.permit_id,
                                                  boundary.admission.epoch)
        yield

    monkeypatch.setattr(boundary_module, "executor_slot", waited_slot)
    monkeypatch.setattr(transport, "_provision_agy_settings", lambda *_: effects.append("settings"))
    def spawn(*_args, **_kwargs):
        effects.append("spawn")
        raise AssertionError("prompt handoff reached after authority drift")
    monkeypatch.setattr(boundary_module.subprocess, "Popen", spawn)
    with pytest.raises(ProviderRequestError):
        transport.run(ProcessCommand(("agy", "--prompt"), b"private prompt", cwd=tmp_path), boundary)
    assert effects == []


def test_host_capture_preserves_slot_and_fixed_environment(
    boundary_module, tmp_path, monkeypatch,
):
    observed = []
    real_slot = boundary_module.executor_slot

    @contextmanager
    def recording_slot(**kwargs):
        observed.append(kwargs)
        with real_slot(**kwargs):
            yield

    monkeypatch.setattr(boundary_module, "executor_slot", recording_slot)
    monkeypatch.setenv("TASK5_FAKE_SECRET", "must-not-reach-child")
    command = ProcessCommand(
        (sys.executable, "-c", "import os;print(os.getenv('TASK5_FAKE_SECRET','absent'));"
         "print(os.getenv('LLM_FANOUT_DEPTH'));print(os.getenv('GIT_CONFIG_GLOBAL'))"),
        b"", cwd=tmp_path, timeout=2, slot_cap=2, slot_timeout=0.4,
        slot_root=tmp_path / "slots",
    )
    result = boundary_module._run_bounded_command(command)
    assert result.status == ProcessStatus.EXIT and result.returncode == 0
    assert result.stdout.splitlines() == [b"absent", b"1", os.devnull.encode()]
    assert observed == [{"root": command.slot_root, "cap": 2, "timeout": 0.4}]


def test_selector_construction_failure_reaps_host_client(
    boundary_module, tmp_path, monkeypatch,
):
    pid_file = tmp_path / "client.pid"
    command = ProcessCommand(
        (sys.executable, "-c", "import os,sys,time;"
         "open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(30)",
         str(pid_file)), b"", cwd=tmp_path, timeout=3,
        slot_root=tmp_path / "slots", term_grace=0.01, reap_timeout=1,
    )

    def selector_fails():
        deadline = time.monotonic() + 1
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise OSError("synthetic selector failure")

    monkeypatch.setattr(boundary_module.selectors, "DefaultSelector", selector_fails)
    try:
        with pytest.raises(OSError, match="selector failure"):
            boundary_module._run_bounded_command(command)
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if pid_file.exists():
            pid = int(pid_file.read_text())
            try:
                os.killpg(pid, 9)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass


def test_launch_receipt_failure_quarantines_known_owned_seat(
    boundary_module, tmp_path, monkeypatch,
):
    docker = []
    original = boundary_module.write_evidence

    def fail_launch_receipt(controller, category, payload):
        if category == "container-seat-launch":
            raise OSError("synthetic receipt write failure")
        return original(controller, category, payload)

    monkeypatch.setattr(boundary_module, "write_evidence", fail_launch_receipt)
    with pytest.raises(OSError, match="synthetic receipt"):
        _launched_seat(boundary_module, tmp_path,
                       before_start=docker.append)
    assert len(docker) == 1
    assert docker[0].inspect["State"]["Running"] is False


def test_real_launcher_reinspects_running_container_before_spawn(boundary_module, tmp_path):
    request, boundary, _, fake_docker, _, sent = _launched_seat(boundary_module, tmp_path)
    admission_inspects = fake_docker.inspect_count
    assert admission_inspects >= 3
    fake_docker.inspect["HostConfig"]["NetworkMode"] = "bridge"
    with pytest.raises(ProviderRequestError, match="attestation|inspect|network"):
        boundary_module.run_container_turn(request, boundary)
    assert fake_docker.inspect_count > admission_inspects
    assert sent == []


def test_same_metadata_socket_replacement_is_refused_before_spawn(boundary_module, tmp_path):
    request, boundary, _, _, attestor, sent = _launched_seat(boundary_module, tmp_path)
    attestor.proof = replace(attestor.proof, inode=attestor.proof.inode + 1,
                             ctime_ns=attestor.proof.ctime_ns + 1)
    with pytest.raises(ProviderRequestError, match="socket"):
        boundary_module.run_container_turn(request, boundary)
    assert sent == []


def test_unissued_handle_is_not_admitted(boundary_module, tmp_path):
    request, boundary, transport, docker, _, sent = _launched_seat(boundary_module, tmp_path)
    forged = replace(transport.handle, inspect_sha256="e" * 64)
    with pytest.raises(ProviderRequestError, match="handle|provenance|association"):
        boundary_module.issue_container_boundary(
            boundary.controller, request, transport.attest_vm(), forged,
            boundary.admission,
        )
    assert sent == []
    assert docker.inspect["State"]["Running"] is True


def test_changed_target_issue_cannot_close_original_seat(boundary_module, tmp_path):
    request, boundary, transport, docker, _, sent = _launched_seat(
        boundary_module, tmp_path)
    changed = replace(request, target_id="different-target")
    with pytest.raises(ProviderRequestError):
        boundary_module.issue_container_boundary(
            boundary.controller, changed, transport.attest_vm(),
            transport.handle, boundary.admission)
    assert sent == []
    assert docker.inspect["State"]["Running"] is True
    assert "stop-seat" not in docker.events


def test_codex_explicit_loopback_provider_on_initial_and_resume(boundary_module, tmp_path):
    request, boundary, transport, store, ledger, controller = _seat(
        boundary_module, tmp_path, "codex", terminal_id=SESSION_IDS["codex"],
    )
    assert boundary_module.run_container_turn(request, boundary).valid
    ledger.fence_epoch(boundary.permit_id, 1)
    resumed, resume_boundary, resume_transport, *_ = _seat(
        boundary_module, tmp_path, "codex", controller=controller,
        store=store, ledger=ledger, epoch=2, resume=True,
        session_id=SESSION_IDS["codex"], terminal_id=SESSION_IDS["codex"],
    )
    assert boundary_module.run_container_turn(resumed, resume_boundary).valid
    for observed in (transport.calls[0], resume_transport.calls[0]):
        argv = observed.argv
        assert argv[:3] == ("codex", "exec", "--ignore-user-config")
        assert 'model_provider="fanout"' in argv
        assert any('base_url="http://127.0.0.1:43187/v1"' in arg
                   and 'x-fanout-seat="disposable-placeholder"' in arg
                   and "request_max_retries=0" in arg for arg in argv)
        assert "--ignore-user-config" in argv
        assert "--strict-config" in argv


def test_guest_relay_numeric_uid_drop_avoids_passwd_lookup(boundary_module):
    argv = boundary_module._as_guest_relay_uid("-c", "print(1)")
    assert argv[:4] == ("sudo", "-n", "python3", "-c")
    assert "-u" not in argv
    assert "os.setgid(65532)" in argv[4]
    assert "os.setuid(65532)" in argv[4]


def test_agy_session_settings_are_exact_and_fail_closed(boundary_module, tmp_path):
    root = tmp_path / "session"
    root.mkdir(mode=0o700)
    settings = root / ".gemini/antigravity-cli/settings.json"
    command = (sys.executable, "-c", boundary_module._AGY_SETTINGS_SCRIPT,
               str(root), str(os.getuid()))
    assert subprocess.run(command, capture_output=True, check=False).returncode == 0
    assert settings.read_bytes() == b'{"modelProvider":"gemini"}\n'
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    assert subprocess.run(command, capture_output=True, check=False).returncode == 0
    settings.write_bytes(b'{"modelProvider":"other"}\n')
    assert subprocess.run(command, capture_output=True, check=False).returncode != 0
    settings.unlink()
    settings.symlink_to(tmp_path / "other")
    assert subprocess.run(command, capture_output=True, check=False).returncode != 0


@pytest.fixture
def agy_existing_settings(boundary_module, tmp_path):
    root = tmp_path / "session"
    root.mkdir(mode=0o700)
    (root / ".gemini").mkdir(mode=0o700)
    directory = root / ".gemini/antigravity-cli"
    directory.mkdir(mode=0o700)
    settings = directory / "settings.json"
    settings.write_bytes(b'{"modelProvider":"gemini"}\n')
    settings.chmod(0o600)
    command = (sys.executable, "-c", boundary_module._AGY_SETTINGS_SCRIPT,
               str(root), str(os.getuid()))
    return root, settings, command


@pytest.mark.parametrize("document", [
    b'{\n  "modelProvider": "gemini"\n}\n',
    b' \t{\r\n\t"modelProvider" \t: "gemini"\r\n}\t \n',
], ids=["agy-pretty-json", "json-whitespace"])
def test_agy_existing_settings_accept_whitespace_without_rewriting(
    agy_existing_settings, document,
):
    root, settings, command = agy_existing_settings
    settings.write_bytes(document)
    session = root / "agy-conversation-1.json"
    session.write_bytes(b'{"conversationId":"agy-conversation-1"}\n')
    before = settings.stat()
    session_before = session.stat()

    result = subprocess.run(command, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"1727eda32c4479358fc61b7ee0963d0100a6a7b0c91adc208e6bc728a8bd8e86\n"
    assert settings.read_bytes() == document
    after = settings.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_ino, before.st_mtime_ns, before.st_ctime_ns,
    )
    assert session.read_bytes() == b'{"conversationId":"agy-conversation-1"}\n'
    session_after = session.stat()
    assert (session_after.st_ino, session_after.st_mtime_ns, session_after.st_ctime_ns) == (
        session_before.st_ino, session_before.st_mtime_ns, session_before.st_ctime_ns,
    )


@pytest.mark.parametrize("document", [
    b'{\n  "modelProvider": "other"\n}\n',
    b'{"modelProvider":"gemini","extra":true}\n',
    b'{"modelProvider":"gemini","modelProvider":"gemini"}\n',
    b'{"modelProvider":"other","modelProvider":"gemini"}\n',
    b'{"modelProvider":"gemini","modelProvider":"other"}\n',
    b'[["modelProvider","gemini"]]\n',
    b'["gemini"]\n',
    b'"gemini"\n',
    b'null\n',
    b'true\n',
    b'42\n',
    b'{"modelProvider":"gemini",}\n',
    b'{"modelProvider":"gemini"}\n{}\n',
    b'{"modelProvider":"gemini"}\xff',
    b'{"modelProvider":"gemini"}' + b' ' * 4096,
], ids=["wrong-provider", "extra-key", "duplicate-approved-key",
        "duplicate-last-approved", "duplicate-last-wrong", "array-of-pairs",
        "array", "string", "null", "boolean", "number", "malformed",
        "trailing-document", "invalid-encoding", "oversized"])
def test_agy_existing_settings_reject_unapproved_documents(
    agy_existing_settings, document,
):
    _, settings, command = agy_existing_settings
    settings.write_bytes(document)
    assert subprocess.run(command, capture_output=True, check=False).returncode != 0
    assert settings.read_bytes() == document


@pytest.mark.parametrize("unsafe", ["hardlink", "symlink", "directory", "mode", "owner"])
def test_agy_existing_settings_reject_unsafe_files(agy_existing_settings, unsafe):
    root, settings, command = agy_existing_settings
    if unsafe == "hardlink":
        os.link(settings, root / "settings-alias.json")
    elif unsafe == "symlink":
        target = root / "settings-target.json"
        settings.rename(target)
        settings.symlink_to(target)
    elif unsafe == "directory":
        settings.unlink()
        settings.mkdir(mode=0o600)
    elif unsafe == "mode":
        settings.chmod(0o644)
    else:
        command = (*command[:-1], str(os.getuid() + 1))
    assert subprocess.run(command, capture_output=True, check=False).returncode != 0


@pytest.mark.parametrize("directory", [".", ".gemini", ".gemini/antigravity-cli"])
def test_agy_settings_reject_unsafe_directory_modes(agy_existing_settings, directory):
    root, _, command = agy_existing_settings
    (root / directory).chmod(0o755)
    assert subprocess.run(command, capture_output=True, check=False).returncode != 0


def test_colima_receives_owned_nonwritable_home_without_ambient_secrets(tmp_path, monkeypatch):
    from shared.lib.fanout import container_boundary as boundary
    home = tmp_path / 'host-home'
    home.mkdir(mode=0o750)
    home.chmod(0o750)
    monkeypatch.setenv('HOME', str(home))
    for name in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS',
                 'GIT_DIR', 'GIT_CONFIG_COUNT', 'HTTPS_PROXY', 'DOCKER_HOST', 'DOCKER_CONTEXT',
                 'DOCKER_CONFIG', 'COLIMA_HOME', 'SSH_AUTH_SOCK'):
        monkeypatch.setenv(name, 'TEST_ONLY_DO_NOT_INHERIT')
    child = boundary.build_host_control_environment(('colima', 'list', '--json'))
    assert child['HOME'] == str(home)
    assert 'TEST_ONLY_DO_NOT_INHERIT' not in child.values()
    assert child['GIT_CONFIG_GLOBAL'] == os.devnull
    assert home.stat().st_mode & 0o777 == 0o750


@pytest.mark.parametrize('binary', ('docker', 'ssh', 'ssh-keyscan', '/usr/bin/ssh', 'codex', 'claude', 'agy', 'git', '/private/tmp/colima', 'sh'))
def test_other_commands_remain_home_blind(tmp_path, monkeypatch, binary):
    from shared.lib.fanout import container_boundary as boundary
    monkeypatch.setenv('HOME', str(tmp_path))
    assert 'HOME' not in boundary.build_host_control_environment((binary, 'fixture-operation'))


@pytest.mark.parametrize('kind', ('symlink', 'writable', 'relative', 'missing', 'file', 'foreign-owner'))
def test_host_controls_reject_unsafe_home(tmp_path, monkeypatch, kind):
    from shared.lib.fanout import container_boundary as boundary
    home = tmp_path / 'host-home'
    if kind == 'symlink':
        home.symlink_to(tmp_path, target_is_directory=True)
    elif kind == 'writable':
        home.mkdir(mode=0o700)
        home.chmod(0o775)
    elif kind == 'relative':
        home = Path('relative-host-home')
    elif kind == 'file':
        home.touch()
    elif kind == 'foreign-owner':
        home.mkdir(mode=0o750)
        monkeypatch.setattr(boundary.os, 'getuid', lambda: home.stat().st_uid + 1)
    monkeypatch.setenv('HOME', str(home))
    with pytest.raises(boundary.ProviderRequestError):
        boundary.build_host_control_environment(('colima', 'list', '--json'))


def test_provider_private_home_contract_is_unchanged(tmp_path, monkeypatch):
    from shared.lib.fanout import container_boundary as boundary
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    child = boundary.build_child_environment(overrides={'HOME': str(private)})
    assert child['HOME'] == str(private)
    assert boundary.build_host_control_environment(('codex',), overrides={'HOME': str(private)})['HOME'] == str(private)
    with pytest.raises(boundary.ProviderRequestError, match='cannot be overridden'):
        boundary.build_host_control_environment(('colima',), overrides={'HOME': str(private)})
    private.chmod(0o750)
    with pytest.raises(Exception):
        boundary.build_child_environment(overrides={'HOME': str(private)})


def test_colima_control_spawn_receives_home(tmp_path, monkeypatch):
    from shared.lib.fanout import container_boundary as boundary
    home = tmp_path / 'host-home'
    home.mkdir(mode=0o750)
    monkeypatch.setenv('HOME', str(home))
    seen = {}

    def refused_spawn(argv, **kwargs):
        seen.update(kwargs['env'])
        raise OSError('synthetic spawn refusal; no host command runs')

    monkeypatch.setattr(boundary.subprocess, 'Popen', refused_spawn)
    result = boundary._run_control_command(('colima', 'list', '--json'))
    assert result.status is boundary.ProcessStatus.SPAWN_ERROR
    assert seen['HOME'] == str(home)
