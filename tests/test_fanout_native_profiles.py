"""Native launches preserve discovery while binding roles and observed contracts."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "_fanout_native_profiles", ROOT / "shared/lib/fanout/__init__.py",
    submodule_search_locations=[str(ROOT / "shared/lib/fanout")],
)
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)
providers = sys.modules[SPEC.name + ".providers"]
process = sys.modules[SPEC.name + ".process"]


def test_native_runtime_and_diagnostics_work_without_retired_isolation_modules(tmp_path):
    probe = r'''
import importlib.abc, importlib.util, json, sys
from pathlib import Path
class RetiredIsolation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        leaf = fullname.rsplit(".", 1)[-1]
        if leaf.startswith("container_") or leaf in {"broker", "native_boundary", "agy_guard", "canary_proof"}:
            raise ImportError("retired isolation module unavailable: " + fullname)
sys.meta_path.insert(0, RetiredIsolation())
root, temporary = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location("native_without_isolation", root / "__init__.py", submodule_search_locations=[str(root)])
fanout = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fanout
spec.loader.exec_module(fanout)
from native_without_isolation.diagnostic import describe_native_launch
from native_without_isolation import eval_provider, maka_transport
profile = fanout.ProviderRegistry.default().select("claude", "read-only", "standard")
request = fanout.ProviderRequest("claude", "synthetic read", cwd=temporary, profile=profile)
native = profile.adapter.build_command(request)
metadata = describe_native_launch(profile, native, observed_cli_version=profile.cli_version)
result = fanout.run_command(fanout.ProcessCommand((sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"), b"retained native supervision\n", cwd=temporary, slot_root=temporary / "slots"))
assert result.status is fanout.ProcessStatus.EXIT and result.returncode == 0
assert result.stdout == b"retained native supervision\n" and result.capture_complete
assert metadata["role"] == "executor" and metadata["os_confinement"] is False
print(json.dumps({"answer": result.stdout.decode(), "confinement": metadata["os_confinement"]}))
'''
    result = subprocess.run([sys.executable, "-c", probe, str(ROOT / "shared/lib/fanout"), str(tmp_path)],
        capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"answer": "retained native supervision\n", "confinement": False}


@pytest.mark.parametrize("executor", ["claude", "agy"])
@pytest.mark.parametrize("role", ["executor", "evaluator"])
@pytest.mark.parametrize("resume", [False, True])
def test_native_headless_reads_do_not_require_unavailable_permission_prompts(tmp_path, executor, role, resume):
    profile = fanout.ProviderRegistry.default().select(executor, "read-only", "standard", role=role)
    session = "89ec5e4a-9888-4c09-aaab-93ad24144614" if executor == "claude" else "native-session"
    command = profile.adapter.build_command(fanout.ProviderRequest(
        executor, "read installed evidence", cwd=tmp_path, role=role, profile=profile,
        resume=resume, session_id=session if resume else None))
    assert "--dangerously-skip-permissions" in command.argv
    if executor == "claude":
        assert "--permission-mode" not in command.argv
        assert command.argv[command.argv.index("--permission-prompts") + 1] == "none"
        denied = command.argv[command.argv.index("--disallowedTools") + 1].split(",")
        assert {"Edit", "Write", "mcp__slack__conversations_add_message"} <= set(denied)
    else:
        assert command.argv[command.argv.index("--mode") + 1] == "plan"
        assert not {"--settings", "--allowedTools", "--disallowedTools"} & set(command.argv)


@pytest.mark.parametrize("name", ["chrome-devtools", "company_tools", "company:tools@team/read"])
def test_generated_codex_denials_load_the_exact_installed_mcp_server(tmp_path, monkeypatch, name):
    binary = shutil.which("codex")
    if binary is None:
        pytest.skip("installed Codex config parser unavailable")
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.toml").write_text(
        '[mcp_servers.' + json.dumps(name) + ']\ncommand="synthetic-not-started"\n'
        'disabled_tools=["already_denied"]\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(config))
    options = providers._codex_native_mcp_denials(tmp_path)
    result = subprocess.run([binary, *options, "mcp", "get", name, "--json"],
        capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    loaded = json.loads(result.stdout)
    assert loaded["transport"]["command"] == "synthetic-not-started"
    assert {"already_denied", "conversations_add_message"} <= set(loaded["disabled_tools"])


@pytest.mark.parametrize("name", ["company.tools", 'quoted"server', ""])
def test_unrepresentable_codex_server_policy_refuses_before_launch(tmp_path, monkeypatch, name):
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.toml").write_text('[mcp_servers.' + json.dumps(name) + ']\ncommand="not-started"\n')
    monkeypatch.setenv("CODEX_HOME", str(config))
    registry = fanout.ProviderRegistry.default(version_probe=lambda _: "0.161.0", contract_probe=lambda _: None)
    profile = registry.select("codex", "read-only", "standard")
    with pytest.raises(fanout.ProviderRequestError, match="server name"):
        providers._run_provider_with_runner(fanout.ProviderRequest(
            "codex", "read", cwd=tmp_path, profile=profile), registry=registry,
            runner=lambda _: pytest.fail("unrepresentable server policy spent a turn"))


@pytest.mark.parametrize("executor", ["claude", "codex", "agy"])
def test_native_roles_bind_explicit_effort_and_keep_normal_discovery(tmp_path, executor):
    registry = fanout.ProviderRegistry.default()
    commands = []
    for role in ("executor", "evaluator"):
        profile = registry.select(executor, "read-only", "standard", role=role)
        request = fanout.ProviderRequest(executor, "read evidence", cwd=tmp_path,
                                        profile=profile, role=role)
        command = profile.adapter.build_command(request)
        assert command.trusted_native
        assert profile.requested_effort == ("high" if executor == "agy" else
                                            "medium" if role == "executor" else "xhigh")
        assert not {"--strict-mcp-config", "--restricted", "--safe-mode",
                    "--ignore-user-config", "--disable-slash-commands"} & set(command.argv)
        assert "--disable" not in command.argv
        assert "HOME" not in command.environment
        assert "--effort" in command.argv if executor != "codex" else any(
            arg == 'model_reasoning_effort="' + profile.requested_effort + '"'
            for arg in command.argv)
        commands.append(profile)
    assert commands[0].digest != commands[1].digest
    assert commands[0].to_dict()["role"] == "executor"
    assert fanout.ExecutorProfile.from_dict(commands[1].to_dict(), adapter=commands[1].adapter).role == "evaluator"


def test_trusted_native_process_reads_installed_skill_and_credential_config(tmp_path):
    home = tmp_path / "account"
    (home / ".agents/skills/evidence").mkdir(parents=True)
    (home / ".agents/skills/evidence/SKILL.md").write_text("installed read skill")
    config = home / ".codex"
    config.mkdir()
    (config / "config.toml").write_text('[mcp_servers.mixed]\ncommand="native-mcp"\n')
    command = process.ProcessCommand(
        [sys.executable, "-c", "import os,pathlib; h=pathlib.Path(os.environ['HOME']); "
         "assert (h/'.agents/skills/evidence/SKILL.md').read_text() == 'installed read skill'; "
         "assert 'mixed' in (pathlib.Path(os.environ['CODEX_HOME'])/'config.toml').read_text(); "
         "assert os.environ['SERVICE_READ_TOKEN']=='synthetic-only'; print('native reads available')"],
        b"", cwd=tmp_path, trusted_native=True, slot_root=tmp_path / "slots",
    )
    result = process.run_command(command, base_environment={
        "PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(config),
        "SERVICE_READ_TOKEN": "synthetic-only", "LLM_FANOUT_DEPTH": "0",
        "GIT_DIR": "/unrelated/repository", "CODEX_COLLABORATION_MODE": "ambient",
    })
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"native reads available\n"


def test_codex_mixed_mcp_denies_known_writes_without_overwriting_existing_policy(tmp_path, monkeypatch):
    home = tmp_path / "account"
    config = home / ".codex"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(
        '[mcp_servers.slack]\ncommand="native-mcp"\ndisabled_tools=["already_denied"]\n'
        'enabled_tools=["slack_search_messages", "slack_send_message"]\n')
    monkeypatch.setenv("CODEX_HOME", str(config))
    profile = fanout.ProviderRegistry.default().select("codex", "read-only", "standard")
    command = profile.adapter.build_command(fanout.ProviderRequest(
        "codex", "read slack", cwd=tmp_path, profile=profile))
    policy = next(arg for arg in command.argv if arg.startswith('mcp_servers.slack.disabled_tools='))
    denied = json.loads(policy.split("=", 1)[1])
    assert "already_denied" in denied and "slack_send_message" in denied
    assert "slack_search_messages" not in denied
    assert "enabled_tools" not in " ".join(command.argv)


@pytest.mark.parametrize("recorded", [False, True])
def test_compatible_cli_drift_records_actual_launch_and_accepts_native_read_events(tmp_path, recorded):
    registry = (fanout.ProviderRegistry.for_installed(("codex",),
        version_probe=lambda _: "0.161.0", contract_probe=lambda _: None) if recorded else
        fanout.ProviderRegistry.default(version_probe=lambda _: "0.161.0", contract_probe=lambda _: None))
    profile = registry.select("codex", "read-only", "standard")
    request = fanout.ProviderRequest("codex", "read evidence", cwd=tmp_path, profile=profile,
                                    target_id="target" if recorded else None,
                                    inputs_digest="a" * 64 if recorded else None)
    output = b'\n'.join(json.dumps(item).encode() for item in [
        {"type": "thread.started", "thread_id": "native-thread"},
        {"type": "item.completed", "item": {"id": "tool-1", "type": "mcp_tool_call",
         "server": "slack", "tool": "slack_search_messages", "arguments": {},
         "result": {"content": [{"type": "text", "text": "evidence"}]}, "error": None,
         "status": "completed"}},
        {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": "grounded"}},
        {"type": "turn.completed", "usage": {"input_tokens": 12, "cached_input_tokens": 0,
         "cache_write_input_tokens": 0, "output_tokens": 3, "reasoning_output_tokens": 0}},
    ])
    commands = []
    def launch(command):
        commands.append(command)
        return process.ProcessResult(process.ProcessStatus.EXIT, 0, output, b"")
    result = providers._run_provider_with_runner(request, registry=registry, runner=launch)
    assert result.valid and result.answer == "grounded"
    assert result.observed_model is None
    assert result.launch_metadata["observed_cli_version"] == "0.161.0"
    assert result.launch_metadata["declared_cli_version"] == profile.cli_version
    assert result.launch_metadata["argv"] == list(commands[0].argv)
    assert result.launch_metadata["os_confinement"] is False
    assert result.launch_metadata["observed_tools"] == ["slack/slack_search_messages"]


def test_unsupported_native_contract_refuses_before_turn_launch(tmp_path):
    def unsupported(_profile):
        raise fanout.ProviderRequestError("native CLI lacks structured output")
    registry = fanout.ProviderRegistry.default(version_probe=lambda _: "0.161.0",
                                               contract_probe=unsupported)
    profile = registry.select("codex", "read-only", "standard")
    with pytest.raises(fanout.ProviderRequestError, match="structured output"):
        providers._run_provider_with_runner(fanout.ProviderRequest(
            "codex", "question", cwd=tmp_path, profile=profile), registry=registry,
            runner=lambda _: pytest.fail("unsupported CLI spent a turn"))


def test_owner_is_descriptive_and_cannot_be_launched_as_a_worker(tmp_path):
    registry = fanout.ProviderRegistry.default()
    profile = registry.select("codex", "repo-write", "standard", role="owner")
    with pytest.raises(fanout.ProviderRequestError, match="owner"):
        fanout.run_provider(fanout.ProviderRequest("codex", "apply", cwd=tmp_path,
            execution_class="repo-write", profile=profile, role="owner"), registry=registry)


def test_native_automatic_retry_budget_is_rejected_before_launch(tmp_path):
    registry = fanout.ProviderRegistry.default(version_probe=lambda _: pytest.fail("retry request probed CLI"))
    profile = registry.select("codex", "read-only", "standard")
    with pytest.raises(fanout.ProviderRequestError, match="retries.*zero"):
        fanout.run_provider(fanout.ProviderRequest("codex", "read", cwd=tmp_path,
            profile=profile, retries=1), registry=registry)


@pytest.mark.parametrize("executor", ["claude", "agy"])
def test_native_permission_bypass_must_be_supported_before_a_turn(tmp_path, monkeypatch, executor):
    registry = fanout.ProviderRegistry.default(version_probe=lambda _: "1.0.0")
    profile = registry.select(executor, "read-only", "standard")
    help_text = b"--print --session-id --resume --output-format --disallowedTools --effort medium xhigh high --conversation --input-format --mode"
    monkeypatch.setattr(providers.subprocess, "run", lambda *args, **kwargs:
        subprocess.CompletedProcess(args[0], 0, help_text, b""))
    with pytest.raises(fanout.ProviderRequestError, match="invocation flags"):
        providers._run_provider_with_runner(fanout.ProviderRequest(
            executor, "read", cwd=tmp_path, profile=profile), registry=registry,
            runner=lambda _: pytest.fail("unsupported native bypass spent a turn"))


def test_codex_denials_cover_known_write_tools_on_a_mixed_server_alias(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.toml").write_text('[mcp_servers.company_tools]\ncommand="mixed-mcp"\n')
    monkeypatch.setenv("CODEX_HOME", str(config))
    profile = fanout.ProviderRegistry.default().select("codex", "read-only", "standard")
    command = profile.adapter.build_command(fanout.ProviderRequest("codex", "read", cwd=tmp_path, profile=profile))
    policy = next(arg for arg in command.argv if arg.startswith('mcp_servers.company_tools.disabled_tools='))
    assert "slack_send_message" in json.loads(policy.split("=", 1)[1])


def test_native_agy_help_on_stderr_is_a_supported_no_spend_contract(monkeypatch):
    import subprocess
    profile = fanout.ProviderRegistry.default().select("agy", "read-only", "standard")
    help_text = b"--conversation --input-format --output-format --effort low|medium|high|xhigh|max --mode --dangerously-skip-permissions"
    monkeypatch.setattr(providers.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, b"", help_text))
    providers._installed_cli_contract(profile)


def test_trusted_codex_shell_preserves_network_backed_read_skills(tmp_path):
    profile = fanout.ProviderRegistry.default().select("codex", "read-only", "standard")
    first = profile.adapter.build_command(fanout.ProviderRequest("codex", "use gh read", cwd=tmp_path, profile=profile))
    resumed = profile.adapter.build_command(fanout.ProviderRequest("codex", "continue read", cwd=tmp_path,
        profile=profile, resume=True, session_id="native-thread"))
    assert first.argv[first.argv.index("--sandbox") + 1] == "danger-full-access"
    assert 'sandbox_mode="danger-full-access"' in resumed.argv
    assert 'approval_policy="never"' in first.argv and 'approval_policy="never"' in resumed.argv


def test_codex_nonrepository_read_contract_refuses_an_unsupported_cli(monkeypatch):
    profile = fanout.ProviderRegistry.default().select("codex", "read-only", "standard")
    monkeypatch.setattr(providers.subprocess, "run", lambda *args, **kwargs:
        subprocess.CompletedProcess(args[0], 0, b"--json --sandbox --model --config", b""))
    with pytest.raises(fanout.ProviderRequestError, match="invocation flags"):
        providers._installed_cli_contract(profile)


def test_native_evaluator_uses_shared_role_profile_and_supervisor(tmp_path, monkeypatch):
    import importlib
    runtime = importlib.import_module(SPEC.name + ".eval_provider")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("READ_CREDENTIAL", "synthetic")
    registry = fanout.ProviderRegistry.default(version_probe=lambda _: "0.161.0", contract_probe=lambda _: None)
    spec = runtime.build_native_spec("codex", "recompute from evidence", cwd=tmp_path, registry=registry)
    assert spec.thinking == "xhigh" and spec.profile.role == "evaluator"
    assert spec.stdin == b"recompute from evidence"
    assert "--ignore-user-config" not in spec.argv
    captured = []
    def launch(command, **kwargs):
        captured.append((command, kwargs))
        return process.ProcessResult(process.ProcessStatus.EXIT, 0,
            b'{"type":"thread.started","thread_id":"eval-thread"}\n'
            b'{"type":"item.completed","item":{"type":"agent_message","text":"verified"}}\n'
            b'{"type":"turn.completed","usage":{"input_tokens":4,"output_tokens":1}}\n', b"")
    monkeypatch.setattr(runtime, "run_command", launch)
    workdir = tmp_path / "artifacts"
    workdir.mkdir()
    record = runtime.run(spec, workdir, timeout=3, retries=0,
        environment=dict(os.environ), real_home=tmp_path)
    assert record["valid"] and record["observed_model"] is None
    assert record["launch_metadata"]["role"] == "evaluator"
    assert captured[0][0].trusted_native
    assert captured[0][1]["base_environment"]["HOME"] == str(tmp_path)


@pytest.mark.parametrize("mutation", ["model", "thinking", "role", "session-uuid", "argv-model", "provider"])
def test_native_evaluator_mutation_refuses_before_paid_runner(tmp_path, monkeypatch, mutation):
    import importlib
    runtime = importlib.import_module(SPEC.name + ".eval_provider")
    registry = fanout.ProviderRegistry.default(version_probe=lambda _: "2.2.0", contract_probe=lambda _: None)
    spec = runtime.build_native_spec("claude", "read evidence", cwd=tmp_path, registry=registry)
    if mutation == "model":
        spec.model = "different-model"
    elif mutation == "thinking":
        spec.thinking = "medium"
    elif mutation == "role":
        spec.role = None
    elif mutation == "session-uuid":
        spec.argv[spec.argv.index("--session-id") + 1] = "invalid-session"
    elif mutation == "argv-model":
        spec.argv[spec.argv.index("--model") + 1] = "different-model"
    else:
        spec.name = "codex"
    monkeypatch.setattr(runtime, "run_command", lambda *args, **kwargs: pytest.fail("mutated evaluator spent a turn"))
    with pytest.raises(runtime.ProcessValidationError, match="native evaluator"):
        runtime.run(spec, tmp_path, timeout=3, retries=0, environment={}, real_home=tmp_path)


def test_native_evaluator_exact_resume_preserves_bound_session(tmp_path, monkeypatch):
    import importlib
    runtime = importlib.import_module(SPEC.name + ".eval_provider")
    registry = fanout.ProviderRegistry.default(version_probe=lambda _: "2.2.0", contract_probe=lambda _: None)
    session = "11111111-1111-4111-8111-111111111111"
    spec = runtime.build_native_spec("claude", "continue evidence", cwd=tmp_path, registry=registry,
                                     session_id=session, resume=True)
    commands = []
    def launch(command, **kwargs):
        commands.append(command)
        output = {"type": "result", "subtype": "success", "is_error": False,
                  "session_id": session, "result": "verified"}
        return process.ProcessResult(process.ProcessStatus.EXIT, 0, json.dumps(output).encode(), b"")
    monkeypatch.setattr(runtime, "run_command", launch)
    result = runtime.run(spec, tmp_path, timeout=3, retries=0, environment=dict(os.environ), real_home=tmp_path)
    assert result["valid"]
    assert commands[0].argv[commands[0].argv.index("--resume") + 1] == session


@pytest.mark.parametrize("executor", ["claude", "codex", "agy"])
@pytest.mark.parametrize("resume", [False, True])
def test_native_evaluator_config_preserves_initial_or_exact_resume(tmp_path, monkeypatch, executor, resume):
    import importlib
    runtime = importlib.import_module(SPEC.name + ".eval_provider")
    registry = fanout.ProviderRegistry.default(version_probe=lambda _: "1.4.0", contract_probe=lambda _: None)
    session = "11111111-1111-4111-8111-111111111111"
    cfg = {executor: {"model": "fixture-native-model", "thinking": "high" if executor == "agy" else "xhigh"}}
    spec = runtime.build_native_spec(executor, "read configured evidence", cfg, cwd=tmp_path,
        registry=registry, resume=resume, session_id=session if resume else None)
    assert spec.native_request.resume is resume
    assert spec.profile.requested_model == "fixture-native-model"
    session = spec.native_request.session_id or "new-native-session"
    if executor == "claude":
        output = {"type": "result", "subtype": "success", "is_error": False,
                  "session_id": session, "result": "verified"}
        raw = json.dumps(output).encode()
    elif executor == "codex":
        raw = b"\n".join(json.dumps(event).encode() for event in [
            {"type": "thread.started", "thread_id": session},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "verified"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ])
    else:
        raw = b"\n".join(json.dumps(event).encode() for event in [
            {"event": "init", "conversation_id": session, "init": {}},
            {"event": "result", "result": {"conversation_id": session, "status": "SUCCESS", "response": "verified"}},
        ])
    commands = []
    def launch(command, **kwargs):
        commands.append(command)
        return process.ProcessResult(process.ProcessStatus.EXIT, 0, raw, b"")
    monkeypatch.setattr(runtime, "run_command", launch)
    result = runtime.run(spec, tmp_path, timeout=3, retries=0, environment=dict(os.environ), real_home=tmp_path)
    assert result["valid"] and result["observed_model"] is None
    assert "fixture-native-model" in commands[0].argv
    if resume:
        assert session in commands[0].argv


@pytest.mark.parametrize("mutation", ["repo-write", "rebound-model", "clear-native-binding"])
def test_native_evaluator_rejects_coherent_rebinding_before_launch(tmp_path, monkeypatch, mutation):
    import importlib
    runtime = importlib.import_module(SPEC.name + ".eval_provider")
    registry = fanout.ProviderRegistry.default(version_probe=lambda _: "2.2.0", contract_probe=lambda _: None)
    spec = runtime.build_native_spec("claude", "read", cwd=tmp_path, registry=registry)
    if mutation == "clear-native-binding":
        spec.native_request = spec.profile = spec.registry = spec.role = None
    else:
        execution_class = "repo-write" if mutation == "repo-write" else "read-only"
        model = "other-model" if mutation == "rebound-model" else spec.model
        initial, resumed = providers._default_argv("claude", execution_class, model, "xhigh")
        spec.profile = replace(spec.profile, execution_class=execution_class, requested_model=model,
                               initial_argv=initial, resume_argv=resumed, profile_sha256=None)
        spec.native_request = replace(spec.native_request, execution_class=execution_class, profile=spec.profile)
        spec.model = model
        spec.argv = list(spec.profile.adapter.build_command(spec.native_request).argv)
    monkeypatch.setattr(runtime, "run_command", lambda *args, **kwargs: pytest.fail("rebound evaluator spent a turn"))
    with pytest.raises(runtime.ProcessValidationError, match="native evaluator"):
        runtime.run(spec, tmp_path, timeout=3, retries=0, environment={}, real_home=tmp_path)


@pytest.mark.parametrize("response,valid,reason", [
    ("missing-session", False, "protocol-error"),
    ("wrong-session", False, "protocol-error"),
    ("wrong-model", False, "model-drift"),
    ("unreported-model", True, "ok"),
])
def test_native_evaluator_uses_provider_identity_and_model_acceptance(tmp_path, monkeypatch, response, valid, reason):
    import importlib
    runtime = importlib.import_module(SPEC.name + ".eval_provider")
    registry = fanout.ProviderRegistry.default(version_probe=lambda _: "2.2.0", contract_probe=lambda _: None)
    spec = runtime.build_native_spec("claude", "read", cwd=tmp_path, registry=registry)
    output = {"type": "result", "subtype": "success", "is_error": False, "result": "verified"}
    if response != "missing-session":
        output["session_id"] = ("22222222-2222-4222-8222-222222222222" if response == "wrong-session"
                                else spec.native_request.session_id)
    if response == "wrong-model":
        output["modelUsage"] = {"different-model": {"inputTokens": 1, "outputTokens": 1}}
    monkeypatch.setattr(runtime, "run_command", lambda *args, **kwargs:
        process.ProcessResult(process.ProcessStatus.EXIT, 0, json.dumps(output).encode(), b""))
    record = runtime.run(spec, tmp_path, timeout=3, retries=0, environment=dict(os.environ), real_home=tmp_path)
    assert (record["valid"], record["reason"]) == (valid, reason)
    assert record["observed_model"] == ("different-model" if response == "wrong-model" else None)


def test_native_evaluator_refuses_child_config_different_from_policy_builder(tmp_path, monkeypatch):
    import importlib
    runtime = importlib.import_module(SPEC.name + ".eval_provider")
    for account, alias in (("builder", "builder_alias"), ("child", "child_alias")):
        config = tmp_path / account / ".codex"
        config.mkdir(parents=True)
        (config / "config.toml").write_text(f'[mcp_servers.{alias}]\ncommand="mixed-mcp"\n')
    monkeypatch.setenv("HOME", str(tmp_path / "builder"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "builder/.codex"))
    registry = fanout.ProviderRegistry.default(version_probe=lambda _: "0.161.0", contract_probe=lambda _: None)
    spec = runtime.build_native_spec("codex", "read", cwd=tmp_path, registry=registry)
    assert any(arg.startswith('mcp_servers.builder_alias.disabled_tools=') for arg in spec.argv)
    child = dict(os.environ)
    child.update({"HOME": str(tmp_path / "child"), "CODEX_HOME": str(tmp_path / "child/.codex")})
    monkeypatch.setattr(runtime, "run_command", lambda *args, **kwargs: pytest.fail("mismatched config spent a turn"))
    with pytest.raises(runtime.ProcessValidationError, match="native evaluator.*environment"):
        runtime.run(spec, tmp_path, timeout=3, retries=0, environment=child, real_home=tmp_path / "child")


def test_native_evaluator_accepts_benign_per_run_environment_metadata(tmp_path, monkeypatch):
    import importlib
    runtime = importlib.import_module(SPEC.name + ".eval_provider")
    registry = fanout.ProviderRegistry.default(version_probe=lambda _: "0.161.0", contract_probe=lambda _: None)
    spec = runtime.build_native_spec("codex", "read", cwd=tmp_path, registry=registry)
    base = {**os.environ, "KHENRIX_EVAL_TOOL_CANARY": "synthetic-canary"}
    seen = []
    def launch(command, **kwargs):
        seen.append(kwargs["base_environment"])
        return process.ProcessResult(process.ProcessStatus.EXIT, 0,
            b'{"type":"thread.started","thread_id":"native-eval"}\n'
            b'{"type":"item.completed","item":{"type":"agent_message","text":"verified"}}\n'
            b'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}', b"")
    monkeypatch.setattr(runtime, "run_command", launch)
    result = runtime.run(spec, tmp_path, timeout=3, retries=0, environment=base, real_home=tmp_path)
    assert result["valid"]
    assert seen[0]["KHENRIX_EVAL_TOOL_CANARY"] == "synthetic-canary"


@pytest.mark.parametrize("executor,effort", [("claude", "medium"), ("codex", "medium"), ("agy", "high")])
def test_native_prompt_bridge_can_bind_executor_role_for_pressure_answers(tmp_path, executor, effort):
    import importlib
    runtime = importlib.import_module(SPEC.name + ".eval_provider")
    spec = runtime.build_native_spec(executor, "answer", {executor: {"model": "fixture-model", "thinking": effort}},
        cwd=tmp_path, role="executor")
    assert spec.role == spec.profile.role == spec.native_request.role == "executor"
    assert spec.thinking == effort


@pytest.mark.parametrize("consumer", ["executor", "evaluator"])
@pytest.mark.parametrize("status,code,complete,answer,provider_error,reason", [
    (process.ProcessStatus.OUTPUT_LIMIT, 0, False, "answer", False, "process-error"),
    (process.ProcessStatus.OUTPUT_LIMIT, 0, True, "answer", False, "process-error"),
    (process.ProcessStatus.EXIT, 0, False, "answer", False, "process-error"),
    (process.ProcessStatus.EXIT, None, True, "answer", False, "process-error"),
    (process.ProcessStatus.EXIT, False, True, "answer", False, "process-error"),
    (process.ProcessStatus.EXIT, 1, True, "answer", False, "process-error"),
    (process.ProcessStatus.EXIT, 0, True, " \n\t ", False, "protocol-error"),
    (process.ProcessStatus.TIMEOUT, None, False, "answer", False, "timeout"),
    (process.ProcessStatus.SPAWN_ERROR, None, False, "", False, "spawn-error"),
    (process.ProcessStatus.TIMEOUT, None, False, "answer", True, "provider-error"),
    (process.ProcessStatus.EXIT, 1, False, "answer", True, "provider-error"),
    (process.ProcessStatus.EXIT, 0, True, "answer", False, "ok"),
])
def test_native_results_require_complete_successful_process_and_nonblank_answer(
    tmp_path, monkeypatch, consumer, status, code, complete, answer, provider_error, reason,
):
    import importlib
    runtime = importlib.import_module(SPEC.name + ".eval_provider")
    registry = fanout.ProviderRegistry.default(version_probe=lambda _: "0.161.0", contract_probe=lambda _: None)
    raw = b"\n".join(json.dumps(event).encode() for event in [
        {"type": "thread.started", "thread_id": "native-result"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": answer}},
        ({"type": "turn.failed", "error": {"message": "structured failure"}} if provider_error else
         {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
    ])
    terminal = process.ProcessResult(status, code, raw, b"", capture_complete=complete)
    if consumer == "evaluator":
        spec = runtime.build_native_spec("codex", "read", cwd=tmp_path, registry=registry)
        monkeypatch.setattr(runtime, "run_command", lambda *args, **kwargs: terminal)
        result = runtime.run(spec, tmp_path, timeout=3, retries=0,
            environment=dict(os.environ), real_home=tmp_path)
        actual = result["valid"], result["reason"]
    else:
        profile = registry.select("codex", "read-only", "standard")
        result = providers._run_provider_with_runner(fanout.ProviderRequest(
            "codex", "read", cwd=tmp_path, profile=profile), registry=registry, runner=lambda _: terminal)
        actual = result.valid, result.reason
    assert actual == (reason == "ok", reason)


@pytest.mark.parametrize("executor", ["claude", "codex", "agy"])
@pytest.mark.parametrize("role", ["executor", "evaluator"])
@pytest.mark.parametrize("tier", ["standard", "deep"])
def test_whole_native_initial_and_resume_commands_preserve_installed_reads(tmp_path, monkeypatch, executor, role, tier):
    home = tmp_path / "account"
    skill = home / ".agents/skills/native-evidence/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("read-only evidence instructions")
    config = home / ".codex"
    config.mkdir()
    (config / "config.toml").write_text('[mcp_servers.slack]\ncommand="installed-transport"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / executor
    binary.write_text("#!" + sys.executable + "\n" + '''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if '--version' in args:
    print({'claude':'2.2.0 (Claude Code)', 'codex':'codex-cli 0.161.0', 'agy':'1.3.0'}[name])
    raise SystemExit(0)
if '--help' in args:
    print('--print --session-id --resume --output-format json stream-json --disallowedTools --effort medium high xhigh --json --sandbox --model --config --conversation --input-format --mode --dangerously-skip-permissions --skip-git-repo-check', file=sys.stderr if name == 'agy' else sys.stdout)
    raise SystemExit(0)
if name == 'codex' and '--skip-git-repo-check' not in args and not any(
        (parent/'.git').exists() for parent in (pathlib.Path.cwd(), *pathlib.Path.cwd().parents)):
    print('Not inside a trusted directory and --skip-git-repo-check was not specified.', file=sys.stderr)
    raise SystemExit(1)
assert (pathlib.Path(os.environ['HOME'])/'.agents/skills/native-evidence/SKILL.md').read_text() == 'read-only evidence instructions'
assert os.environ['READ_SERVICE_AUTH'] == 'synthetic-auth'
assert '--ignore-user-config' not in args and '--strict-mcp-config' not in args
prompt = sys.stdin.read()
if name == 'agy':
    assert json.loads(prompt)['message']['content'] in {'initial evidence', 'resume evidence'}
else:
    assert prompt in {'initial evidence', 'resume evidence'}
if name == 'claude':
    assert '--dangerously-skip-permissions' in args and '--permission-mode' not in args
    assert 'mcp__slack__conversations_add_message' in args[args.index('--disallowedTools')+1]
    flag = '--resume' if '--resume' in args else '--session-id'
    print(json.dumps({'type':'result', 'subtype':'success', 'is_error':False,
        'session_id':args[args.index(flag)+1], 'result':'grounded native read'}))
elif name == 'codex':
    print(json.dumps({'type':'thread.started','thread_id':'thread-native'}))
    print(json.dumps({'type':'item.completed','item':{'id':'read','type':'mcp_tool_call','server':'slack','tool':'slack_search_messages','arguments':{},'result':{'content':[]},'error':None,'status':'completed'}}))
    print(json.dumps({'type':'item.completed','item':{'id':'answer','type':'agent_message','text':'grounded native read'}}))
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':3,'cached_input_tokens':0,'cache_write_input_tokens':0,'output_tokens':2,'reasoning_output_tokens':0}}))
else:
    assert '--dangerously-skip-permissions' in args
    print(json.dumps({'event':'init','conversation_id':'conversation-native','init':{}}))
    print(json.dumps({'event':'step_update','step_update':{'conversation_id':'conversation-native','step_index':0,'state':'DONE','step_type':'agent_response','text_delta':'grounded native read'}}))
    print(json.dumps({'event':'result','result':{'conversation_id':'conversation-native','status':'SUCCESS','response':'grounded native read'}}))
''')
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(config))
    monkeypatch.setenv("READ_SERVICE_AUTH", "synthetic-auth")
    monkeypatch.setenv("LLM_FANOUT_DEPTH", "0")
    monkeypatch.delenv("AGY_ADC_AUTH", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    registry = fanout.ProviderRegistry.default()
    profile = registry.select(executor, "read-only", tier, role=role)
    initial = fanout.run_provider(fanout.ProviderRequest(executor, "initial evidence", cwd=tmp_path,
        role=role, profile=profile, slot_root=tmp_path / "slots"), registry=registry)
    assert initial.valid, initial.stderr
    resumed = fanout.run_provider(fanout.ProviderRequest(executor, "resume evidence", cwd=tmp_path,
        role=role, profile=profile, resume=True, session_id=initial.session_id,
        slot_root=tmp_path / "slots"), registry=registry)
    assert resumed.valid and resumed.session_id == initial.session_id
    assert resumed.answer == "grounded native read" and resumed.observed_model is None
    assert "synthetic-auth" not in json.dumps(resumed.launch_metadata)
