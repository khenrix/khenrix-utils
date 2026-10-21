"""Hermetic installed-read policy gates; no model or service calls."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import eval_harness


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def policy(tmp_path):
    root = tmp_path / 'installed'
    root.mkdir()
    skill = root / 'SKILL.md'
    skill.write_text('first line\nsecond line\n')
    cwd = tmp_path / 'cwd'
    cwd.mkdir()
    context = eval_harness.prepare_agy_installed_read_policy(cwd, tmp_path / 'controls',
        installed_trees={root: {'SKILL.md': digest(skill)}})
    return context, skill, cwd


def invoke(context, tool='read_file', args=None, step=2):
    payload = {'conversationId': 'test-session', 'stepIdx': step,
               'toolCall': {'name': tool, 'args': args}}
    result = subprocess.run(shlex.split(context['command']), input=json.dumps(payload),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_exact_installed_read_allows_and_logs_only_minimal_input(policy):
    context, skill, _ = policy
    assert invoke(context, args={'Path': str(skill)})['decision'] == 'allow'
    assert context['qualified'] is False
    log = [json.loads(line) for line in Path(context['hook_log']).read_text().splitlines()]
    assert len(log) == 1
    assert log[0]['input']['toolCall']['args'] == {'Path': str(skill)}
    assert 'first line' not in json.dumps(log)
    assert log[0]['source_sha256'] == digest(skill)


@pytest.mark.parametrize('args', [None, {}, {'Path': '../outside'}, {'Path': 1},
    {'Path': 'REPLACE', 'ByteOffset': 0}, {'Path': 'REPLACE', 'StartLine': True},
    {'Path': 'REPLACE', 'StartLine': 0}, {'Path': 'REPLACE', 'EndLine': 0},
    {'Path': 'REPLACE', 'StartLine': 2, 'EndLine': 1},
    {'Path': 'REPLACE', 'EndLine': '2'}])
def test_rejects_unknown_path_or_malformed_arguments(policy, args):
    context, skill, _ = policy
    args = {k: str(skill) if v == 'REPLACE' else v for k, v in args.items()} if args else args
    assert invoke(context, args=args)['decision'] == 'deny'


@pytest.mark.parametrize('tool', ['write_file', 'run_command', 'Read', 'search_web'])
def test_denies_every_other_tool_and_does_not_log_secret_arguments(policy, tool):
    context, skill, _ = policy
    assert invoke(context, tool, {'Path': str(skill), 'Content': 'private-content'})['decision'] == 'deny'
    assert 'private-content' not in Path(context['hook_log']).read_text()


def test_ranges_and_unique_denial_reasons(policy):
    context, skill, _ = policy
    assert invoke(context, args={'Path': str(skill), 'StartLine': 2, 'EndLine': 8})['decision'] == 'allow'
    a = invoke(context, args={'Path': '/not-authorized'}, step=3)
    b = invoke(context, args={'Path': '/not-authorized'}, step=4)
    assert a['reason'] != b['reason']


@pytest.mark.parametrize('drift', ['content', 'symlink', 'parent-symlink', 'handler', 'config', 'policy'])
def test_denies_source_or_control_drift(policy, tmp_path, drift):
    context, skill, cwd = policy
    if drift == 'content':
        skill.write_text('changed')
    elif drift == 'symlink':
        other = tmp_path / 'same-content'
        other.write_bytes(skill.read_bytes())
        skill.unlink()
        skill.symlink_to(other)
    elif drift == 'parent-symlink':
        root = skill.parent
        moved = root.with_name('moved')
        root.rename(moved)
        root.symlink_to(moved, target_is_directory=True)
    else:
        target = Path(context[drift]['path'])
        target.write_text(target.read_text() + '\n')
    assert invoke(context, args={'Path': str(skill)})['decision'] == 'deny'


def test_membership_is_checked_before_opening_unknown_content(policy, tmp_path):
    context, _, _ = policy
    fifo = tmp_path / 'unapproved'
    import os
    os.mkfifo(fifo)
    result = subprocess.run(shlex.split(context['command']), input=json.dumps({
        'conversationId': 'test', 'stepIdx': 1,
        'toolCall': {'name': 'read_file', 'args': {'Path': str(fifo)}}}),
        capture_output=True, text=True, timeout=2)
    assert json.loads(result.stdout)['decision'] == 'deny'


def test_preparation_checks_expected_hashes_and_preserves_existing_hooks(tmp_path):
    cwd = tmp_path / 'cwd'
    hooks = cwd / '.agents/hooks.json'
    hooks.parent.mkdir(parents=True)
    hooks.write_text('user hook')
    with pytest.raises(RuntimeError, match='already exists'):
        eval_harness.prepare_agy_installed_read_policy(cwd, tmp_path / 'controls', installed_trees={})
    assert hooks.read_text() == 'user hook'
    hooks.unlink()
    source = tmp_path / 'fixture'
    source.write_text('fixture')
    with pytest.raises(RuntimeError, match='hash'):
        eval_harness.prepare_agy_installed_read_policy(cwd, tmp_path / 'other-controls',
            installed_trees={}, fixtures={source: '0' * 64})


@pytest.mark.parametrize('raw', ['[]', 'null', '"string"', '{bad json'])
def test_malformed_hook_payload_always_returns_deny(policy, raw):
    context, _, _ = policy
    result = subprocess.run(shlex.split(context['command']), input=raw,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['decision'] == 'deny'


def test_hook_implementation_drift_stales_instrument_proof(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/lib'))
    import checks
    (tmp_path / 'capabilities.toml').write_text('')
    helper = tmp_path / 'scripts/eval_agy_read_policy.py'
    helper.parent.mkdir()
    helper.write_text('# original read gate\n')
    manifest = checks.instrument_manifest(tmp_path)
    behavior = checks.delivered_behavior_manifest(tmp_path, 'skill-tuneup')
    unrelated = checks.delivered_behavior_manifest(tmp_path, 'alpha')
    proof = {'schema_version': 1, 'instrument_manifest': manifest,
             'instrument_hash': checks.manifest_hash(manifest),
             'tests': [{'command': ['pytest', 'tests/test_contract.py'],
                        'counts': {'tests_run': 1, 'failed': 0, 'skipped': 0},
                        'evidence_sha256': 'a' * 64}],
             'native_sessions': {provider: {'valid': True, 'session_id': provider + '-session',
                 'role': 'evaluator', 'judge_config': {'model': 'fixture-model',
                     'thinking': 'high' if provider == 'agy' else 'xhigh'},
                 'cli_version': 'fixture-version', 'raw_capture_sha256': 'b' * 64,
                 'read_tool_evidence_sha256': 'c' * 64} for provider in ('claude', 'codex', 'agy')},
             'private_artifact_manifest_sha256': 'd' * 64}
    path = tmp_path / 'evals/instrument-proof.json'
    path.parent.mkdir()
    path.write_text(json.dumps(proof))
    assert checks.instrument_proof_problems(tmp_path) == []
    helper.write_text('# altered read gate\n')
    assert checks.instrument_manifest(tmp_path) != manifest
    assert checks.delivered_behavior_manifest(tmp_path, 'skill-tuneup') != behavior
    assert checks.delivered_behavior_manifest(tmp_path, 'alpha') == unrelated
    assert any('does not bind the current instrument' in problem
               for problem in checks.instrument_proof_problems(tmp_path))
