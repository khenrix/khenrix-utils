"""Real subprocess evidence for the eval's prompt-only provider boundary."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import eval_harness


@pytest.fixture
def isolated_cli(tmp_path):
    home = tmp_path / "real-home"
    home.mkdir(mode=0o700)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    shim = binary_dir / "shim.py"
    shim.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
provider = 'agy' if name == 'agy-bin' else name
raw = sys.stdin.read()
prompt = json.loads(raw)['message']['content'] if provider == 'agy' else raw
record = Path('launches.jsonl')
with record.open('a') as out:
    out.write(json.dumps({'argv': sys.argv[1:], 'prompt': prompt,
                         'environment': sorted(os.environ), 'home': os.environ['HOME'],
                         'depth': os.environ.get('LLM_FANOUT_DEPTH')}) + '\\n')
if 'malformed-case' in prompt:
    print('malformed provider output')
    sys.exit(0)
if 'slow-case' in prompt:
    import time
    print('partial output', flush=True)
    time.sleep(60)
error = 'provider-error' in prompt
if provider == 'claude':
    print(json.dumps({'type': 'result', 'subtype': 'error' if error else 'success',
                      'is_error': error, 'result': 'quota' if error else 'OK',
                      'usage': {'input_tokens': 8, 'output_tokens': 2},
                      'total_cost_usd': 0.002}))
elif provider == 'codex':
    print(json.dumps({'type': 'thread.started', 'thread_id': 'test-thread'}))
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'OK'}}))
    print(json.dumps({'type': 'turn.failed', 'error': {'message': 'quota'}} if error else
                     {'type': 'turn.completed', 'usage': {'input_tokens': 8, 'output_tokens': 2}}))
else:
    print(json.dumps({'event': 'init', 'conversation_id': 'test-thread', 'init': {}}))
    print(json.dumps({'event': 'result', 'result': {'conversation_id': 'test-thread',
                      'status': 'ERROR' if error else 'SUCCESS', 'response': 'OK',
                      **({'error': 'quota'} if error else {})}}))
""")
    shim.chmod(0o755)
    for name in ("claude", "codex"):
        (binary_dir / name).symlink_to(shim)
    agy = home / ".local/libexec/agy-bin"
    agy.parent.mkdir(parents=True)
    agy.symlink_to(shim)
    return home, {"PATH": str(binary_dir) + os.pathsep + os.environ["PATH"],
                  "HOME": str(home), "OPENAI_API_KEY": "TEST_ONLY_KEY",
                  "HOSTILE_SECRET": "SHOULD_NOT_CROSS", "LLM_FANOUT_DEPTH": "0"}


def run_fake(tmp_path, isolated_cli, provider, prompt="Answer briefly.", retries=2, timeout=3):
    home, env = isolated_cli
    cwd = tmp_path / "isolated" / "cwd"
    cfg = {name: {"model": "test-model", "thinking": effort}
           for name, effort in (("claude", "max"), ("codex", "xhigh"), ("agy", "high"))}
    answer, record = eval_harness.run_text(
        provider, prompt, cfg, tmp_path / "artifacts", timeout=timeout, retries=retries,
        readonly=True, exec_cwd=cwd, base_env=env, real_home=home,
    )
    launches = [json.loads(line) for line in (cwd / "launches.jsonl").read_text().splitlines()]
    return answer, record, launches


@pytest.mark.parametrize("provider", ["claude", "codex", "agy"])
def test_eval_uses_stdin_and_accepts_short_answers_without_council_policy(
    tmp_path, isolated_cli, provider,
):
    answer, record, launches = run_fake(tmp_path, isolated_cli, provider)
    assert answer == "OK"
    assert record["valid"] and record["reason"] == "ok"
    assert len(launches) == 1 and "Answer briefly." in launches[0]["prompt"]
    assert "Answer briefly." not in launches[0]["argv"]
    assert "HOSTILE_SECRET" not in launches[0]["environment"]
    assert not {"CODEX_COLLABORATION_MODE", "MEMORY_WORKER_TOKEN"} & set(launches[0]["environment"])
    assert launches[0]["depth"] == "2"
    expected_home = isolated_cli[0] if provider == "claude" else tmp_path / "isolated/home"
    assert launches[0]["home"] == str(expected_home)
    assert Path(record["result_file"]).read_text() == "OK"
    if provider == "claude":
        assert record["usage"]["cost_usd"] == 0.002


@pytest.mark.parametrize("provider", ["claude", "codex", "agy"])
def test_paid_provider_failure_is_terminal_and_keeps_raw_capture(tmp_path, isolated_cli, provider):
    answer, record, launches = run_fake(tmp_path, isolated_cli, provider, "provider-error")
    assert answer == "" and not record["valid"]
    assert record["reason"] == "provider-error"
    assert len(launches) == 1 and record["attempts"] == 1
    assert "quota" in Path(record["stdout_file"]).read_text()


@pytest.mark.parametrize("provider", ["claude", "codex", "agy"])
@pytest.mark.parametrize("mutation", ["tool-override", "extra-directory", "ambient-env", "home", "native"])
def test_prompt_only_launch_refuses_boundary_overrides_before_spawn(
    tmp_path, isolated_cli, provider, mutation,
):
    from dataclasses import replace
    runtime = eval_harness.eval_runtime
    home, base = isolated_cli
    cwd = tmp_path / "isolated/cwd"
    env = eval_harness.isolated_provider_env(provider, cwd.parent / "home", base, home)
    env[eval_harness.TOOL_CANARY_ENV] = "TEST_ONLY_CANARY"
    spec = runtime.build_spec(provider, "Answer",
                              {provider: {"model": "test-model", "thinking": "high"}})
    eval_harness.apply_provider_isolation(spec, cwd, home)
    command = runtime.ProcessCommand(spec.argv, spec.stdin, cwd=cwd)
    if mutation == "tool-override":
        argv = {"claude": ["--tools", "Bash"], "codex": ["-c", "agents.enabled=true"],
                "agy": ["--agent", "default"]}[provider]
        command = replace(command, argv=(*command.argv, *argv))
    elif mutation == "extra-directory":
        command = replace(command, argv=(*command.argv, "--add-dir", str(home)))
    elif mutation == "ambient-env":
        env["MEMORY_WORKER_TOKEN"] = "TEST_ONLY_TOKEN"
    elif mutation == "home":
        env["HOME"] = str(tmp_path)
    else:
        command = replace(command, native_original=command)
    with pytest.raises(runtime.ProcessValidationError):
        runtime.run_prompt_only_command(command, provider=provider, environment=env, real_home=home)
    assert not (cwd / "launches.jsonl").exists()


def test_only_a_verified_unstarted_spawn_is_retried(tmp_path, isolated_cli, monkeypatch):
    process = sys.modules[eval_harness.eval_runtime.__package__ + ".process"]
    original = process.subprocess.Popen
    calls = []

    def first_unstarted(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise FileNotFoundError("test-only unstarted spawn")
        return original(*args, **kwargs)

    monkeypatch.setattr(process.subprocess, "Popen", first_unstarted)
    answer, record, launches = run_fake(tmp_path, isolated_cli, "codex")
    assert answer == "OK" and record["attempts"] == 2 and len(launches) == 1
    assert (tmp_path / "artifacts/codex.stdout.attempt-1.txt").read_bytes() == b""


@pytest.mark.parametrize("prompt,timeout,reason", [
    ("malformed-case", 3, "protocol-error"), ("slow-case", 0.15, "timeout"),
])
def test_possible_paid_protocol_failure_or_timeout_is_never_replayed(
    tmp_path, isolated_cli, prompt, timeout, reason,
):
    answer, record, launches = run_fake(tmp_path, isolated_cli, "codex", prompt, timeout=timeout)
    assert answer == "" and not record["valid"] and record["reason"] == reason
    assert record["attempts"] == 1 and len(launches) == 1
    assert Path(record["stdout_file"]).read_bytes()
