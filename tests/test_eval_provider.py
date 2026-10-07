"""Real subprocess evidence for the eval's prompt-only provider boundary."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import eval_harness


@pytest.mark.parametrize("provider", ("claude", "codex", "agy"))
@pytest.mark.parametrize("effort", ("medium", "xhigh"))
def test_quality_session_grammar_preserves_exact_resume_and_stdin(tmp_path, provider, effort):
    runtime = eval_harness.eval_runtime
    from types import SimpleNamespace
    from dataclasses import replace
    profile = SimpleNamespace(requested_model={"claude": "claude-opus-5-5",
        "codex": "gpt-6.1-sol", "agy": "gemini-3.8-flash-high"}[provider],
        requested_effort="high" if provider == "agy" else effort)
    request = SimpleNamespace(executor_id=provider, profile=profile, resume=False,
        session_id="ed1e3f89-8ce9-44b7-9684-59f05d9c7f9a" if provider == "claude" else None,
        prompt_bytes="private exact prompt α".encode(), prompt="private exact prompt α",
        cwd=tmp_path, retries=0, timeout=30,
        slot_cap=2, slot_timeout=1.25, slot_root=tmp_path / "slots")
    catalog = "/workspace/inputs/" + "a" * 64 + "/published/context/quality-catalog.json"
    first = runtime.quality_session_command(request, catalog_path=catalog if provider == "codex" else None)
    request.resume = True
    request.session_id = "ed1e3f89-8ce9-44b7-9684-59f05d9c7f9a"
    second = runtime.quality_session_command(request, catalog_path=catalog if provider == "codex" else None)
    assert request.session_id in second.argv
    assert "private exact prompt α" not in first.argv
    if provider == "agy":
        assert json.loads(first.stdin)["message"]["content"] == request.prompt
        assert "--new-project" in first.argv and "--new-project" not in second.argv
        assert "eval-no-tools" in first.argv and "eval-no-tools" in second.argv
    else:
        assert first.stdin == second.stdin == request.prompt_bytes
    assert "--no-session-persistence" not in first.argv
    assert "--ephemeral" not in first.argv
    for argv in (first.argv, second.argv):
        if provider == "codex":
            assert f'model_reasoning_effort="{effort}"' in argv
            disabled = [argv[index + 1] for index, flag in enumerate(argv) if flag == "--disable"]
            assert "goals" in disabled
            assert "suppress_unstable_features_warning=true" in argv
        else:
            assert argv[argv.index("--effort") + 1] == profile.requested_effort
    if provider == "codex":
        assert 'agents.enabled=false' in first.argv and 'agents.enabled=false' in second.argv
        assert f'model_catalog_json="{catalog}"' in first.argv
        assert "multi_agent_v2" in first.argv and "request_permissions_tool" in first.argv
    request.retries = 1
    with pytest.raises(runtime.ProcessValidationError, match="quality"):
        runtime.quality_session_command(request, catalog_path=catalog if provider == "codex" else None)


@pytest.mark.parametrize("provider", ("claude", "codex", "agy"))
@pytest.mark.parametrize("resume", (False, True))
def test_quality_session_command_preserves_requested_slot_controls(tmp_path, provider, resume):
    from dataclasses import replace
    from importlib import import_module

    runtime = eval_harness.eval_runtime
    providers = import_module(runtime.__package__ + ".providers")
    boundary = import_module(runtime.__package__ + ".container_boundary")
    slots = tmp_path / "slots"
    slots.mkdir(mode=0o700)
    session = "ed1e3f89-8ce9-44b7-9684-59f05d9c7f9a"
    request = providers.ProviderRequest(provider, "private exact prompt α", cwd=tmp_path,
        profile=providers.ProviderRegistry.default().select(provider, "read-only", "standard"),
        session_id=session if resume or provider == "claude" else None, resume=resume,
        timeout=31, slot_cap=2, slot_timeout=1.25, slot_root=slots,
        run_id="quality-run", task_id="quality-task", target_id="quality-target",
        seat_id=provider + "-seat", inputs_digest="a" * 64)
    transport = import_module(runtime.__package__ + ".container_transfer_transport")
    workdir = transport._workdir(transport._identity(request))
    catalog = workdir.removesuffix("/repo") + "/context/quality-catalog.json" if provider == "codex" else None
    command = runtime.quality_session_command(request, catalog_path=catalog)
    if provider == "claude":
        argv = ("claude", "--print", "--output-format", "json",
            "--resume" if resume else "--session-id", session,
            "--model", "claude-opus-5-5", "--effort", "medium", "--safe-mode",
            "--disable-slash-commands", "--strict-mcp-config", "--no-chrome", "--tools", "")
        stdin = "private exact prompt α".encode()
    elif provider == "codex":
        argv = ("codex", "exec", "resume") if resume else ("codex", "exec")
        argv += ("--ignore-user-config", "--ignore-rules", "--json", "--skip-git-repo-check",
            "--enable", "skip_host_skill_discovery",
            "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "view_image",
            "--disable", "apps", "--disable", "plugins", "--disable", "remote_plugin",
            "--disable", "hooks", "--disable", "skill_search", "--disable", "multi_agent",
            "--disable", "multi_agent_v2", "--disable", "deferred_executor",
            "--disable", "token_budget", "--disable", "sleep_tool", "--disable", "current_time_reminder",
            "--disable", "send_message_to_user_async", "--disable", "request_permissions_tool",
            "--disable", "tool_suggest", "--disable", "goals", "-c", "agents.enabled=false",
            "-c", "suppress_unstable_features_warning=true", "-c", 'web_search="disabled"',
            "-c", "tools.experimental_request_user_input.enabled=false",
            "-c", "tools.update_plan.enabled=false", "-c", f'model_catalog_json="{catalog}"',
            "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="medium"')
        argv += (("-c", 'sandbox_mode="read-only"', session, "-") if resume
            else ("--sandbox", "read-only", "-C", str(tmp_path), "-"))
        stdin = "private exact prompt α".encode()
    else:
        argv = ("agy", "--conversation", session) if resume else ("agy", "--new-project")
        argv += ("--mode", "plan", "--input-format", "stream-json", "--output-format", "stream-json",
            "--model", "gemini-3.8-flash-high", "--effort", "high", "--agent", "eval-no-tools",
            "--disable-slash-commands")
        stdin = '{"event": "user", "message": {"content": "private exact prompt α"}}\n'.encode()
    assert command.argv == argv
    assert command.stdin == stdin
    assert command == runtime.ProcessCommand(argv, stdin, cwd=tmp_path, timeout=31,
        slot_cap=2, slot_timeout=1.25, slot_root=slots)
    canonical = replace(command, max_output_bytes=1024)
    pair = boundary.quality_container_command(request, canonical, workdir=workdir, catalog_path=catalog)
    assert pair.canonical is canonical
    assert (pair.guest.slot_cap, pair.guest.slot_timeout, pair.guest.slot_root) == (2, 1.25, slots)


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


def test_eval_conditions_create_private_cwd_before_launch_under_normal_umask(
    tmp_path, isolated_cli, monkeypatch,
):
    source = tmp_path / "shared/skills/private-cwd"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("Answer briefly.\n")
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", tmp_path / "evals")
    home, env = isolated_cli
    run_text = eval_harness.run_text
    validate_launch = eval_harness.eval_runtime.validate_launch
    modes, launches = {}, []

    def observe_validation(command, *args, **kwargs):
        modes[str(command.cwd)] = command.cwd.stat().st_mode & 0o777
        return validate_launch(command, *args, **kwargs)

    def run_isolated_cli(*args, **kwargs):
        result = run_text(*args, **kwargs, base_env=env, real_home=home)
        launches.extend(json.loads(line) for line in
                        (kwargs["exec_cwd"] / "launches.jsonl").read_text().splitlines())
        return result

    def judge_reply(judge, prompt, cfg, workdir, *, timeout, verdict_ok):
        verdict = ({"winner": "A", "reasoning": "Both answers are brief."}
                   if prompt.startswith("Two answers") else
                   {"expectations": [{"passed": True, "evidence": "OK is brief."}]})
        return json.dumps(verdict), {"valid": True, "reason": "ok"}

    monkeypatch.setattr(eval_harness.eval_runtime, "validate_launch", observe_validation)
    monkeypatch.setattr(eval_harness, "run_text", run_isolated_cli)
    monkeypatch.setattr(eval_harness, "_run_judge", judge_reply)
    old_umask = os.umask(0o022)
    try:
        runs, comparison = eval_harness.run_eval_for_provider(
            "private-cwd", "codex",
            {"id": 1, "name": "private-cwd", "prompt": "Answer briefly.",
             "assertions": ["Answer briefly."]},
            "claude", {"codex": {"model": "test-model", "thinking": "medium"}},
            tmp_path / "artifacts", timeout=3, retries=0, readonly=True,
        )
    finally:
        os.umask(old_umask)
    assert len(modes) == 2 and set(modes.values()) == {0o700}
    assert len(launches) == 2
    assert {run["configuration"] for run in runs} == {"with_skill", "without_skill"}
    assert all(run["result"]["errors"] == 0 for run in runs)
    assert comparison["judge_ok"]


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


def test_codex_launch_uses_private_tool_free_model_metadata(tmp_path, isolated_cli):
    answer, record, launches = run_fake(tmp_path, isolated_cli, "codex")
    assert answer == "OK" and record["valid"]
    argv = launches[0]["argv"]
    configs = eval_harness._argv_pairs(argv, "-c")
    assert "tools.view_image=false" not in configs
    assert "suppress_unstable_features_warning=true" in configs
    assert "view_image" in eval_harness._argv_pairs(argv, "--disable")
    assert "goals" in eval_harness._argv_pairs(argv, "--disable")
    catalog_config = next(value for value in configs if value.startswith("model_catalog_json="))
    catalog_path = Path(json.loads(catalog_config.split("=", 1)[1]))
    assert catalog_path.parent == tmp_path / "isolated/cwd"
    assert catalog_path.stat().st_mode & 0o777 == 0o600
    models = json.loads(catalog_path.read_text())["models"]
    assert len(models) == 1 and models[0]["slug"] == "test-model"
    assert models[0]["shell_type"] == "disabled"
    assert models[0]["apply_patch_tool_type"] is None
    assert models[0]["experimental_supported_tools"] == []
    assert models[0]["supports_search_tool"] is False
    assert models[0]["tool_mode"] == "direct"


def test_codex_launch_without_goals_disabled_is_rejected_before_spawn(tmp_path, isolated_cli):
    runtime = eval_harness.eval_runtime
    home, base = isolated_cli
    cwd = tmp_path / "isolated/cwd"
    env = eval_harness.isolated_provider_env("codex", cwd.parent / "home", base, home)
    env[eval_harness.TOOL_CANARY_ENV] = "TEST_ONLY_CANARY"
    spec = runtime.build_spec("codex", "Answer", {"codex": {"model": "test-model", "thinking": "medium"}})
    eval_harness.apply_provider_isolation(spec, cwd, home)
    argv = list(spec.argv)
    if "goals" in argv:
        index = argv.index("goals")
        assert argv[index - 1] == "--disable"
        del argv[index - 1:index + 1]
    command = runtime.ProcessCommand(argv, spec.stdin, cwd=cwd)
    with pytest.raises(runtime.ProcessValidationError):
        runtime.run_prompt_only_command(command, provider="codex", environment=env, real_home=home)
    assert not (cwd / "launches.jsonl").exists()


@pytest.mark.parametrize("mutation", ["patch", "clock", "model", "mode", "symlink", "hardlink", "fifo", "directory"])
def test_codex_catalog_changes_are_rejected_before_spawn(tmp_path, isolated_cli, mutation):
    runtime = eval_harness.eval_runtime
    home, base = isolated_cli
    cwd = tmp_path / "isolated/cwd"
    env = eval_harness.isolated_provider_env("codex", cwd.parent / "home", base, home)
    env[eval_harness.TOOL_CANARY_ENV] = "TEST_ONLY_CANARY"
    spec = runtime.build_spec("codex", "Answer", {"codex": {"model": "test-model", "thinking": "high"}})
    eval_harness.apply_provider_isolation(spec, cwd, home)
    config = next(value for value in eval_harness._argv_pairs(spec.argv, "-c")
                  if value.startswith("model_catalog_json="))
    path = Path(json.loads(config.split("=", 1)[1]))
    value = json.loads(path.read_text())
    if mutation == "patch":
        value["models"][0]["apply_patch_tool_type"] = "freeform"
    elif mutation == "clock":
        value["models"][0]["experimental_supported_tools"] = ["clock"]
    elif mutation == "model":
        value["models"][0]["slug"] = "other-model"
    elif mutation == "mode":
        path.chmod(0o644)
    elif mutation == "hardlink":
        path.with_suffix(".alias").hardlink_to(path)
    elif mutation in {"fifo", "directory"}:
        path.unlink()
        if mutation == "fifo":
            os.mkfifo(path, 0o600)
        else:
            path.mkdir(mode=0o700)
    else:
        original = path.with_suffix(".original")
        path.rename(original)
        path.symlink_to(original)
    if mutation in {"patch", "clock", "model"}:
        path.write_text(json.dumps(value))
    command = runtime.ProcessCommand(spec.argv, spec.stdin, cwd=cwd)
    with pytest.raises(runtime.ProcessValidationError):
        runtime.run_prompt_only_command(command, provider="codex", environment=env, real_home=home)
    assert not (cwd / "launches.jsonl").exists()


def test_codex_oversized_catalog_is_rejected_without_unbounded_read(tmp_path, isolated_cli, monkeypatch):
    runtime = eval_harness.eval_runtime
    cwd = tmp_path / "cwd"
    spec = runtime.build_spec("codex", "Answer", {"codex": {"model": "test-model", "thinking": "high"}})
    eval_harness.apply_provider_isolation(spec, cwd, isolated_cli[0])
    path = cwd / "eval-models.json"
    with path.open("r+b") as out:
        out.truncate(1024 * 1024)
    original = Path.read_bytes

    def forbid_unbounded_catalog_read(target):
        if target == path:
            pytest.fail("catalog validation used an unbounded path read")
        return original(target)

    monkeypatch.setattr(Path, "read_bytes", forbid_unbounded_catalog_read)
    with pytest.raises(runtime.ProcessValidationError):
        runtime.validate_codex_catalog(cwd, "test-model")


def test_codex_catalog_is_rechecked_after_executor_slot_wait(tmp_path, isolated_cli, monkeypatch):
    from contextlib import contextmanager

    runtime = eval_harness.eval_runtime
    process = sys.modules[runtime.__package__ + ".process"]
    home, base = isolated_cli
    cwd = tmp_path / "isolated/cwd"
    env = eval_harness.isolated_provider_env("codex", cwd.parent / "home", base, home)
    env[eval_harness.TOOL_CANARY_ENV] = "TEST_ONLY_CANARY"
    spec = runtime.build_spec("codex", "Answer", {"codex": {"model": "test-model", "thinking": "high"}})
    eval_harness.apply_provider_isolation(spec, cwd, home)

    @contextmanager
    def wait_then_admit(**kwargs):
        path = cwd / "eval-models.json"
        value = json.loads(path.read_text())
        value["models"][0]["apply_patch_tool_type"] = "freeform"
        path.write_text(json.dumps(value))
        yield

    monkeypatch.setattr(process, "executor_slot", wait_then_admit)
    command = runtime.ProcessCommand(spec.argv, spec.stdin, cwd=cwd)
    with pytest.raises(runtime.ProcessValidationError):
        runtime.run_prompt_only_command(command, provider="codex", environment=env, real_home=home)
    assert not (cwd / "launches.jsonl").exists()


def test_codex_ultra_refuses_unattested_effort_mapping_before_isolation(tmp_path, isolated_cli):
    home, base = isolated_cli
    cwd = tmp_path / "isolated/cwd"
    with pytest.raises(RuntimeError, match="ultra.*mapping"):
        eval_harness.run_text(
            "codex", "Answer", {"codex": {"model": "test-model", "thinking": "ultra"}},
            tmp_path / "artifacts", timeout=3, retries=0, readonly=True,
            exec_cwd=cwd, base_env=base, real_home=home,
        )
    assert not cwd.exists()
