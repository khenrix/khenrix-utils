"""Consumer-level native read evidence, using real inert CLI subprocesses."""
import json
import hashlib
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import eval_harness
import eval_trigger


@pytest.fixture
def native_cli(tmp_path, monkeypatch):
    home = tmp_path / "account"
    skill = home / ".agents/skills/read-evidence/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("synthetic installed read guidance")
    config = home / ".codex"
    config.mkdir()
    (config / "config.toml").write_text('[mcp_servers.reader]\ncommand="fixture-reader"\n')
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    shim = binary_dir / "shim.py"
    shim.write_text("#!" + sys.executable + "\n" + r'''
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
if '--help' in args:
    print('--disable-slash-commands' if os.environ.get('FIXTURE_AGY_MISSING_FLAG') != '1' else 'unqualified help')
    raise SystemExit(0)
assert (Path(os.environ['HOME'])/'.agents/skills/read-evidence/SKILL.md').read_text() == 'synthetic installed read guidance'
assert os.environ['READ_SERVICE_AUTH'] == 'synthetic-auth'
assert '--ignore-user-config' not in args and '--strict-mcp-config' not in args
raw = sys.stdin.read()
prompt = json.loads(raw)['message']['content'] if name == 'agy' else raw
record = Path(os.environ['HOME'])/'launches.jsonl'
with record.open('a') as stream:
    stream.write(json.dumps({'argv':args, 'cwd':os.getcwd(), 'prompt':prompt})+'\n')
count = len(record.read_text().splitlines())
if 'malformed-case' in prompt:
    print('malformed protocol', flush=True)
    raise SystemExit(0)
if 'slow-case' in prompt:
    import time
    print('partial protocol', flush=True)
    time.sleep(60)
error = 'provider-error' in prompt
answer = ('malformed verdict' if count == 1 else '{"expectations":[{"passed":true,"evidence":"verified"}]}') if 'judge-retry-fixture' in prompt else 'installed read verified'
if name == 'claude':
    print(json.dumps({'type':'result','subtype':'error' if error else 'success','is_error':error,
        'session_id':args[args.index('--session-id')+1], 'result':'quota' if error else answer}))
elif name == 'codex':
    print(json.dumps({'type':'thread.started','thread_id':'native-consumer-thread'}))
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':answer}}))
    print(json.dumps({'type':'turn.failed','error':{'message':'quota'}} if error else {'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':2}}))
else:
    tool_steps = []
    conversation = 'native-consumer-conversation-' + str(count)
    native_rows = [{'type':'USER_INPUT','step_index':0,'status':'DONE','content':prompt}]
    if 'installed-read-attempts:' in prompt:
        import shlex, subprocess
        if '--conversation' in args:
            conversation = args[args.index('--conversation')+1]
        base = count * 20
        native_rows = [{'type':'USER_INPUT','step_index':base,'status':'DONE','content':prompt}]
        calls = json.loads(prompt.split('installed-read-attempts:',1)[1])
        native_rows.append({'type':'PLANNER_RESPONSE','step_index':base+1,'status':'DONE','tool_calls':calls})
        hooks = json.loads((Path.cwd()/'.agents/hooks.json').read_text())
        command = next(iter(hooks.values()))['PreToolUse'][0]['hooks'][0]['command']
        for index, call in reversed(list(enumerate(calls))):
            step = base + index + 2
            payload = {'conversationId':conversation,'stepIdx':step,'toolCall':call}
            if 'wrong-command-hook-fixture' in prompt and call['name'] == 'run_command':
                payload = json.loads(json.dumps(payload))
                payload['toolCall']['args']['WaitMsBeforeAsync'] = 7000
            hook = subprocess.run(shlex.split(command), input=json.dumps(payload), capture_output=True,text=True)
            decision = json.loads(hook.stdout)
            denied = decision['decision'] == 'deny'
            parameters = call['args']
            if 'reduced-command-parameters-fixture' in prompt and call['name'] == 'run_command':
                parameters = {'CommandLine': call['args']['CommandLine']}
                if 'wrong-command-display-fixture' in prompt:
                    parameters = {'CommandLine': 'different command'}
            info = {'name':call['name'],'parameters':parameters}
            row = {'type':'GENERIC','step_index':step,'status':'ERROR' if denied else 'DONE'}
            if denied:
                row['error'] = 'tool call denied by pre-tool hook: '+decision['reason']
                info['error'] = {'message':row['error']}
            else:
                lines = Path(call['args']['Path']).read_text().splitlines()
                start = call['args'].get('StartLine',1)
                end = min(call['args'].get('EndLine',len(lines)),len(lines))
                row['content'] = 'Showing lines %d-%d of %d\n' % (start,end,len(lines)) + '\n'.join('%d: %s' % (n,lines[n-1]) for n in range(start,end+1))
                if 'corrupt-read-fixture' in prompt:
                    row['content'] += '\n999: fabricated'
                if 'extra-read-content-fixture' in prompt:
                    row['content'] += '\nUnnumbered hidden oracle data'
                if 'admission-drift-fixture' in prompt:
                    admission = Path(shlex.split(command)[3]).parent/'agy-installed-read-admission.json'
                    admission.write_text(admission.read_text()+'\n')
                info['output'] = '%d lines' % (end-start+1)
            native_rows.append(row)
            tool_steps.append({'conversation_id':conversation,'step_index':step,'step_type':'tool',
                               'state':row['status'],'tool_name':call['name'],'tool_info':info})
        if 'missing-installed-hook-fixture' in prompt:
            policy = json.loads(Path(shlex.split(command)[3]).read_text())
            log = Path(policy['hook_log'])
            log.write_text('\n'.join(log.read_text().splitlines()[:-1])+'\n')
        native_rows.append({'type':'PLANNER_RESPONSE','step_index':base+19,'status':'DONE','content':answer})
    elif 'answer-only-tool-attempt:' in prompt:
        import shlex, subprocess
        tool_name = prompt.split('answer-only-tool-attempt:',1)[1].split()[0]
        payload = {'conversationId':conversation,'stepIdx':2,
                   'toolCall':{'name':tool_name,'args':{'Path':'synthetic-canary'}}}
        payloads = [payload]
        if 'parallel-denied-fixture' in prompt:
            payload['stepIdx'] = 4
            payloads.append({'conversationId':conversation,'stepIdx':8,
                             'toolCall':{'name':tool_name,'args':{'Path':'second-canary'}}})
        native_rows.append({'type':'PLANNER_RESPONSE','step_index':1,'status':'DONE',
                            'tool_calls':[item['toolCall'] for item in payloads]})
        hooks_path = Path.cwd()/'.agents/hooks.json'
        for payload in reversed(payloads):
            denied, reason = False, ''
            if hooks_path.is_file() and 'ignore-denial-fixture' not in prompt:
                for group in json.loads(hooks_path.read_text()).values():
                    for hook in group.get('PreToolUse',[]) if group.get('enabled') else []:
                        if hook.get('matcher') == '*':
                            for command in hook['hooks']:
                                hook_input = json.loads(json.dumps(payload))
                                if 'wrong-hook-path-fixture' in prompt:
                                    hook_input['toolCall']['args']['Path'] = 'unrelated-path'
                                result = subprocess.run(shlex.split(command['command']),input=json.dumps(hook_input),capture_output=True,text=True)
                                decision = json.loads(result.stdout)
                                denied = result.returncode == 0 and decision.get('decision') == 'deny'
                                reason = decision.get('reason','')
            if denied:
                answer = 'answer-only verified'
                native_rows.append({'type':'GENERIC','step_index':payload['stepIdx'],'status':'ERROR',
                                    'error':'tool call denied by pre-tool hook: '+reason})
            else:
                (Path(os.environ['HOME'])/'model-tool-effect').write_text('tool executed')
                native_rows.append({'type':'GENERIC','step_index':payload['stepIdx'],'status':'DONE','content':'tool succeeded'})
            tool_steps.append({'conversation_id':conversation,'step_index':payload['stepIdx'],
                         'step_type':'tool','state':'ERROR' if denied else 'DONE','tool_name':tool_name,
                         'tool_info':{'name':tool_name,'parameters':payload['toolCall']['args'],
                                      **({'error':{'message':'tool call denied by pre-tool hook: '+reason}} if denied else {})}})
        if 'missing-hook-fixture' in prompt:
            log = Path(shlex.split(command['command'])[-1]).parent/'agy-answer-only-hook-events.jsonl'
            lines = log.read_text().splitlines()
            log.write_text('\n'.join(lines[:-1])+'\n')
        native_rows.append({'type':'PLANNER_RESPONSE','step_index':9,'status':'DONE','content':answer})
    else:
        native_rows.append({'type':'PLANNER_RESPONSE','step_index':1,'status':'DONE','content':answer})
    transcript = Path(os.environ['HOME'])/'.gemini/antigravity-cli/brain'/conversation/'.system_generated/logs/transcript_full.jsonl'
    transcript.parent.mkdir(parents=True,exist_ok=True)
    previous = transcript.read_text() if '--conversation' in args and transcript.exists() else ''
    transcript.write_text(previous+''.join(json.dumps(row)+'\n' for row in native_rows))
    print(json.dumps({'event':'init','conversation_id':conversation,'init':{}}))
    for tool_step in tool_steps:
        print(json.dumps({'event':'step_update','step_update':tool_step}))
    print(json.dumps({'event':'result','result':{'conversation_id':conversation,'status':'ERROR' if error else 'SUCCESS','response':answer,**({'error':'quota'} if error else {})}}))
''')
    shim.chmod(0o700)
    for name in ("claude", "codex", "agy"):
        (binary_dir / name).symlink_to(shim)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(config))
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("READ_SERVICE_AUTH", "synthetic-auth")
    monkeypatch.setenv("LLM_FANOUT_DEPTH", "0")
    registry = eval_harness.eval_runtime.ProviderRegistry.default(
        version_probe=lambda _: "1.0.0", contract_probe=lambda _: None)
    monkeypatch.setattr(eval_harness.eval_runtime.ProviderRegistry, "default", classmethod(lambda cls: registry))
    return home


@pytest.mark.parametrize("provider", ["claude", "codex", "agy"])
@pytest.mark.parametrize("role", ["executor", "evaluator"])
def test_harness_uses_native_read_roles(tmp_path, native_cli, provider, role):
    effort = "high" if provider == "agy" else "xhigh" if role == "evaluator" else "medium"
    cfg = {provider: {"model": "fixture-model", "thinking": effort}}
    answer, record = eval_harness.run_text(provider, "Read installed evidence.", cfg,
        tmp_path / "captures", timeout=3, retries=0, readonly=True,
        exec_cwd=tmp_path / "cwd", real_home=native_cli, role=role)
    assert record["valid"], record
    assert answer == "installed read verified"
    launches = [json.loads(line) for line in (native_cli / "launches.jsonl").read_text().splitlines()]
    assert len(launches) == 1
    assert "you have no tools" not in launches[0]["prompt"]
    assert effort in " ".join(launches[0]["argv"])
    assert "synthetic-auth" not in json.dumps(record)


def test_judge_keeps_two_fresh_native_attempts_at_review_effort(tmp_path, native_cli):
    cfg = {"codex": {"model": "fixture-model", "thinking": "medium"}}
    answer, record = eval_harness._run_judge("codex", "judge-retry-fixture", cfg,
        tmp_path / "judge", timeout=3,
        verdict_ok=lambda text: eval_harness._grading_verdict_ok(text, ["verified"]))
    assert record["valid"] and json.loads(answer)["expectations"][0]["passed"]
    launches = [json.loads(line) for line in (native_cli / "launches.jsonl").read_text().splitlines()]
    assert len(launches) == 2
    assert len({entry["cwd"] for entry in launches}) == 2
    assert all('model_reasoning_effort="xhigh"' in entry["argv"] for entry in launches)
    assert len(list(tmp_path.glob("judge*/*.stdout.attempt-1.txt"))) == 2


def test_trigger_judge_uses_native_review_role(tmp_path, native_cli):
    answer, record = eval_trigger.run_isolated_judge("codex", "Route supplied metadata.",
        {"codex": {"model": "fixture-model", "thinking": "medium"}}, tmp_path / "trigger", 3)
    assert record["valid"] and answer == "installed read verified"
    assert record["thinking"] == "xhigh"
    assert record["attempts"] == 1


@pytest.mark.parametrize("explicit", [False, True])
def test_generated_config_selects_role_models_without_losing_explicit_overrides(tmp_path, native_cli, explicit):
    from types import SimpleNamespace
    cfg = eval_harness.eval_runtime.resolve_mode_config(SimpleNamespace(
        mode="normal", model_agy="gemini-3.8-flash-high" if explicit else None))
    cfg = json.loads(json.dumps(cfg))
    assert cfg["agy"]["model"] == "gemini-3.8-flash-high"
    assert cfg["codex"]["thinking"] == "medium"
    expected = "gemini-3.8-flash-high" if explicit else "gemini-4-argon-high"
    judge_cfg = eval_harness._judge_config("agy", cfg)
    assert judge_cfg["agy"]["model"] == expected
    assert eval_harness._checks()._judge_config_valid(judge_cfg["agy"], "agy")
    assert eval_harness._judge_config("agy", json.loads(json.dumps(judge_cfg))) == judge_cfg
    _, record = eval_trigger.run_isolated_judge("agy", "Route supplied metadata.", cfg,
        tmp_path / "trigger-role", 3)
    assert record["model"] == expected and record["thinking"] == "high"


def test_judge_refuses_malformed_override_provenance_before_spending(tmp_path, native_cli):
    cfg = {"agy": {"model": "gemini-3.8-flash-high", "model_explicit": "false", "thinking": "high"}}
    with pytest.raises(RuntimeError, match="model.*provenance"):
        eval_trigger.run_isolated_judge("agy", "Route metadata.", cfg, tmp_path / "bad-provenance", 3)
    assert not (native_cli / "launches.jsonl").exists()


def test_paired_conditions_keep_private_workspaces_with_native_reads(tmp_path, native_cli, monkeypatch):
    skill = tmp_path / "shared/skills/private-cwd"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Read supplied synthetic evidence.")
    monkeypatch.setattr(eval_harness, "ROOT", tmp_path)
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", tmp_path / "evals")
    original = eval_harness.run_text
    modes = []

    def native_turn(*args, **kwargs):
        modes.append(kwargs["exec_cwd"].stat().st_mode & 0o777)
        return original(*args, **kwargs, real_home=native_cli)

    monkeypatch.setattr(eval_harness, "run_text", native_turn)
    monkeypatch.setattr(eval_harness, "grade", lambda *a, **k:
        {"passed": 1, "total": 1, "judge_ok": True, "expectations": []})
    monkeypatch.setattr(eval_harness, "compare", lambda *a, **k: {"judge_ok": True})
    old_umask = os.umask(0o022)
    try:
        runs, comparison = eval_harness.run_eval_for_provider(
            "private-cwd", "codex", {"id": 1, "name": "private-cwd", "prompt": "Read evidence.",
            "assertions": ["Read evidence."]}, "claude",
            {"codex": {"model": "fixture-model", "thinking": "medium"}},
            tmp_path / "captures", timeout=3, retries=0, readonly=True)
    finally:
        os.umask(old_umask)
    assert modes == [0o700, 0o700]
    assert all(run["result"]["errors"] == 0 for run in runs)
    assert comparison["judge_ok"]


@pytest.mark.parametrize("filename", ["cli.stdout.txt", "cli.stderr.txt", "answer.md", "manifest.json"])
def test_native_artifact_screening_rejects_credentials_in_every_capture(tmp_path, filename):
    workdir = tmp_path / "capture"
    workdir.mkdir()
    secret = "SYNTHETIC_CREDENTIAL_VALUE"
    (workdir / filename).write_text(secret)
    with pytest.raises(RuntimeError, match="redacted"):
        eval_harness.guard_provider_artifacts(workdir, {"SERVICE_API_KEY": secret}, "")
    assert secret not in "".join(path.read_text() for path in workdir.iterdir())


def test_native_artifact_screening_rejects_symlinks(tmp_path):
    (tmp_path / "outside").write_text("synthetic private material")
    workdir = tmp_path / "capture"
    workdir.mkdir()
    (workdir / "stdout").symlink_to(tmp_path / "outside")
    with pytest.raises(RuntimeError, match="failed closed"):
        eval_harness.guard_provider_artifacts(workdir, {}, "")
    assert (tmp_path / "outside").read_text() == "synthetic private material"


@pytest.mark.parametrize("observed", [None, [], ["chrome-devtools/take_snapshot", "shell"]])
def test_benchmark_does_not_invent_zero_tool_calls(observed):
    record = {"valid": True, "usage": {"input": 123, "output": 45}}
    if observed is not None:
        record["launch_metadata"] = {"observed_tools": observed}
    result = eval_harness.build_run_result(record, {"passed": 1, "total": 1, "judge_ok": True})
    assert result["tool_calls"] is None
    assert result["observed_tools"] == observed
    assert result["usage"] == record["usage"]


@pytest.fixture
def answer_only_cli(native_cli):
    eval_harness.eval_runtime.ProviderRegistry.default()._version_probe = lambda _: "1.4.2"
    return native_cli


@pytest.mark.parametrize("tool", ["read_file", "mcp__fixture__lookup", "write_file"])
def test_agy_answer_only_denies_model_tools_before_effects(tmp_path, answer_only_cli, tool):
    answer, record = eval_harness.run_text("agy", "answer-only-tool-attempt:" + tool,
        {"agy": {"model": "fixture-model", "thinking": "high", "answer_only": True}},
        tmp_path / "capture", timeout=3, retries=0, readonly=True,
        exec_cwd=tmp_path / "cwd", real_home=answer_only_cli)
    assert answer == "answer-only verified"
    assert not (answer_only_cli / "model-tool-effect").exists()
    policy = record["agy_answer_only"]
    assert policy["configured"] is True and policy["observed_hook_execution"] is True
    assert policy["denied_tool_calls"] == 1
    for key in ("config", "handler", "policy"):
        artifact = policy[key]
        path = Path(artifact["path"])
        assert path.is_file() and path.stat().st_mode & 0o777 == 0o600
        assert artifact["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_agy_answer_only_zero_call_does_not_invent_hook_execution(tmp_path, answer_only_cli):
    _, record = eval_harness.run_text("agy", "Answer from inline facts.",
        {"agy": {"model": "fixture-model", "thinking": "high", "answer_only": True}},
        tmp_path / "capture", timeout=3, retries=0, readonly=True,
        exec_cwd=tmp_path / "cwd", real_home=answer_only_cli)
    assert record["agy_answer_only"]["configured"] is True
    assert record["agy_answer_only"]["observed_hook_execution"] is False
    assert record["agy_answer_only"]["denied_tool_calls"] == 0


def test_agy_answer_only_accepts_relative_capture_directory(tmp_path, answer_only_cli, monkeypatch):
    monkeypatch.chdir(tmp_path)
    answer, record = eval_harness.run_text("agy", "answer-only-tool-attempt:read_file",
        {"agy": {"model": "fixture-model", "thinking": "high", "answer_only": True}},
        Path("capture"), timeout=3, retries=0, readonly=True,
        exec_cwd=tmp_path / "cwd", real_home=answer_only_cli)
    assert answer == "answer-only verified"
    assert record["agy_answer_only"]["denied_tool_calls"] == 1
    assert not (answer_only_cli / "model-tool-effect").exists()


@pytest.mark.parametrize("unsupported", ["version", "flag", "preexisting-hook"])
def test_agy_answer_only_refuses_unqualified_or_existing_control(tmp_path, answer_only_cli, monkeypatch, unsupported):
    cwd = tmp_path / "cwd"
    if unsupported == "version":
        eval_harness.eval_runtime.ProviderRegistry.default()._version_probe = lambda _: "1.4.1"
    elif unsupported == "flag":
        monkeypatch.setenv("FIXTURE_AGY_MISSING_FLAG", "1")
    else:
        hook = cwd / ".agents/hooks.json"
        hook.parent.mkdir(parents=True)
        hook.write_text("existing user hooks")
    with pytest.raises(RuntimeError, match="UNQUALIFIED|already exists"):
        eval_harness.run_text("agy", "Answer inline.",
            {"agy": {"model": "fixture-model", "thinking": "high", "answer_only": True}},
            tmp_path / "capture", timeout=3, retries=0, readonly=True,
            exec_cwd=cwd, real_home=answer_only_cli)
    assert not (answer_only_cli / "launches.jsonl").exists()
    if unsupported == "preexisting-hook":
        assert hook.read_text() == "existing user hooks"


def test_agy_answer_only_rejects_a_tool_success_without_denial_join(tmp_path, answer_only_cli):
    with pytest.raises(RuntimeError, match="denial evidence"):
        eval_harness.run_text("agy", "answer-only-tool-attempt:read_file ignore-denial-fixture",
            {"agy": {"model": "fixture-model", "thinking": "high", "answer_only": True}},
            tmp_path / "capture", timeout=3, retries=0, readonly=True,
            exec_cwd=tmp_path / "cwd", real_home=answer_only_cli)


def test_agy_answer_only_option_is_explicit(tmp_path):
    assert eval_harness.parse_args(["--skill", "khenrix-quality", "--agy-answer-only"]).agy_answer_only is True
    assert eval_harness.parse_args(["--skill", "khenrix-quality"]).agy_answer_only is False


def test_agy_answer_only_derives_initial_and_resume_profiles(tmp_path, answer_only_cli):
    registry = eval_harness.eval_runtime.ProviderRegistry.default()
    original_digest = registry.select("agy", "read-only", "standard").digest
    _, record = eval_harness.run_text("agy", "Answer inline.",
        {"agy": {"model": "fixture-model", "thinking": "high", "answer_only": True}},
        tmp_path / "capture", timeout=3, retries=0, readonly=True,
        exec_cwd=tmp_path / "cwd", real_home=answer_only_cli)
    policy = record["agy_answer_only"]
    profile = eval_harness.eval_runtime.ExecutorProfile.from_dict(
        policy["profile"], adapter=eval_harness.eval_runtime.ADAPTERS["agy"])
    request = eval_harness.eval_runtime.ProviderRequest("agy", "Continue inline.",
        cwd=tmp_path / "cwd", profile=profile, session_id="resume-fixture", resume=True)
    command = profile.adapter.build_command(request)
    assert command.argv.count("--disable-slash-commands") == 1
    assert command.argv[command.argv.index("--conversation") + 1] == "resume-fixture"
    assert profile.initial_argv.count("--disable-slash-commands") == 1
    assert profile.digest == policy["profile_sha256"] != policy["base_profile_sha256"]
    assert registry.select("agy", "read-only", "standard").digest == original_digest


def test_agy_judge_semantic_retry_preserves_explicit_answer_only_policy(tmp_path, answer_only_cli):
    answer, record = eval_harness._run_judge("agy", "judge-retry-fixture",
        {"agy": {"model": "fixture-model", "thinking": "high", "answer_only": True}},
        tmp_path / "judge", timeout=3,
        verdict_ok=lambda text: eval_harness._grading_verdict_ok(text, ["verified"]))
    assert json.loads(answer)["expectations"][0]["passed"] is True
    policies = list(tmp_path.glob("judge*/agy-answer-only-policy.json"))
    assert len(policies) == 2
    assert all(json.loads(path.read_text())["answer_only"] is True for path in policies)
    launches = [json.loads(line) for line in (answer_only_cli / "launches.jsonl").read_text().splitlines()]
    assert len({entry["cwd"] for entry in launches}) == 2
    assert all("--disable-slash-commands" in entry["argv"] for entry in launches)
    assert record["agy_answer_only"]["observed_hook_execution"] is False


def test_agy_answer_only_accepts_out_of_order_denied_parallel_calls(tmp_path, answer_only_cli):
    answer, record = eval_harness.run_text("agy", "answer-only-tool-attempt:read_file parallel-denied-fixture",
        {"agy": {"model": "fixture-model", "thinking": "high", "answer_only": True}},
        tmp_path / "capture", timeout=3, retries=0, readonly=True,
        exec_cwd=tmp_path / "cwd", real_home=answer_only_cli)
    assert answer == "answer-only verified"
    assert record["agy_answer_only"]["denied_tool_calls"] == 2
    assert not (answer_only_cli / "model-tool-effect").exists()


@pytest.mark.parametrize("damage", ["wrong-hook-path-fixture", "missing-hook-fixture"])
def test_agy_answer_only_rejects_unjoined_parallel_denials(tmp_path, answer_only_cli, damage):
    with pytest.raises(RuntimeError, match="denial evidence"):
        eval_harness.run_text("agy", "answer-only-tool-attempt:read_file parallel-denied-fixture " + damage,
            {"agy": {"model": "fixture-model", "thinking": "high", "answer_only": True}},
            tmp_path / "capture", timeout=3, retries=0, readonly=True,
            exec_cwd=tmp_path / "cwd", real_home=answer_only_cli)


@pytest.mark.parametrize("skill", ["llm-council", "llm-forge"])
@pytest.mark.parametrize("operation", ["load", "receipt"])
def test_retired_skill_cannot_start_eval_or_publish_receipt(tmp_path, monkeypatch, skill, operation):
    directory = tmp_path / skill
    directory.mkdir()
    (directory / "evals.json").write_text('{"evals": []}')
    monkeypatch.setattr(eval_harness, "EVALS_ROOT", tmp_path)
    monkeypatch.setattr(eval_harness.subprocess, "run", lambda *args, **kwargs:
        pytest.fail("retired target launched a command"))
    with pytest.raises(SystemExit, match="retired"):
        if operation == "load":
            eval_harness.load_evals(skill)
        else:
            eval_harness._write_receipt(skill, providers=["codex"], mode="normal",
                judge="claude", delta=0, seeded=False)
    assert not (directory / "receipt.json").exists()


@pytest.fixture
def installed_policy(tmp_path, answer_only_cli):
    skill_root = answer_only_cli / ".agents/skills/read-evidence"
    skill = skill_root / "SKILL.md"
    cwd = tmp_path / "installed-cwd"
    cwd.mkdir()
    context = eval_harness.prepare_agy_installed_read_policy(cwd, tmp_path / "read-controls",
        installed_trees={skill_root: {"SKILL.md": hashlib.sha256(skill.read_bytes()).hexdigest()}})
    return context, skill, cwd


def installed_prompt(skill, prefix=""):
    calls = [{"name": "read_file", "args": {"Path": str(skill)}},
             {"name": "read_file", "args": {"Path": "/harmless-oracle.json"}},
             {"name": "write_file", "args": {"Path": "/scratch", "Content": "harmless"}}]
    return prefix + "installed-read-attempts:" + json.dumps(calls)


def test_installed_policy_joins_parallel_reads_denials_and_reuses_resume(tmp_path, answer_only_cli, installed_policy):
    context, skill, cwd = installed_policy
    cfg = {"agy": {"model": "fixture-model", "thinking": "high"}}
    answer, first = eval_harness.run_text("agy", installed_prompt(skill), cfg,
        tmp_path / "initial", timeout=3, retries=0, readonly=True, exec_cwd=cwd,
        real_home=answer_only_cli, installed_read_policy=context)
    answer, second = eval_harness.run_text("agy", installed_prompt(skill, "Continue. "), cfg,
        tmp_path / "resume", timeout=3, retries=0, readonly=True, exec_cwd=cwd,
        real_home=answer_only_cli, installed_read_policy=context, session_id=first["session_id"])
    for record in (first, second):
        policy = record["agy_installed_read"]
        assert policy["qualified"] is False
        assert policy["allowed_tool_calls"] == 1
        assert policy["denied_tool_calls"] == 2
        assert len(policy["joins"]) == 3
        assert policy["joins"][0]["complete_source_read"] is True
        assert policy["policy"]["sha256"] == context["policy"]["sha256"]
    assert first["agy_installed_read"]["hook_interval"] == [0, 3]
    assert second["agy_installed_read"]["hook_interval"] == [3, 6]
    launches = [json.loads(line) for line in (answer_only_cli / "launches.jsonl").read_text().splitlines()]
    assert all("--disable-slash-commands" not in entry["argv"] for entry in launches)
    assert "--conversation" not in launches[0]["argv"]
    assert launches[1]["argv"][launches[1]["argv"].index("--conversation") + 1] == first["session_id"]


@pytest.mark.parametrize("damage", ["corrupt-read-fixture ", "extra-read-content-fixture ", "missing-installed-hook-fixture ", "admission-drift-fixture "])
def test_installed_policy_rejects_unproven_read_or_missing_join(tmp_path, answer_only_cli, installed_policy, damage):
    context, skill, cwd = installed_policy
    with pytest.raises(RuntimeError, match="installed-read evidence"):
        eval_harness.run_text("agy", installed_prompt(skill, damage),
            {"agy": {"model": "fixture-model", "thinking": "high"}}, tmp_path / "capture",
            timeout=3, retries=0, readonly=True, exec_cwd=cwd, real_home=answer_only_cli,
            installed_read_policy=context)


def test_installed_policy_zero_call_and_mutual_exclusion(tmp_path, answer_only_cli, installed_policy):
    context, _, cwd = installed_policy
    cfg = {"agy": {"model": "fixture-model", "thinking": "high"}}
    _, record = eval_harness.run_text("agy", "No tools needed.", cfg, tmp_path / "zero",
        timeout=3, retries=0, readonly=True, exec_cwd=cwd, real_home=answer_only_cli,
        installed_read_policy=context)
    assert record["agy_installed_read"]["observed_hook_execution"] is False
    assert record["agy_installed_read"]["qualified"] is False
    cfg["agy"]["answer_only"] = True
    with pytest.raises(RuntimeError, match="mutually exclusive"):
        eval_harness.run_text("agy", "Inline.", cfg, tmp_path / "both", timeout=3,
            retries=0, readonly=True, exec_cwd=cwd, real_home=answer_only_cli,
            installed_read_policy=context)


@pytest.mark.parametrize("damage", ["source", "hook-log", "config", "admission"])
def test_installed_policy_rejects_between_turn_drift_before_launch(tmp_path, answer_only_cli, installed_policy, damage):
    context, skill, cwd = installed_policy
    cfg = {"agy": {"model": "fixture-model", "thinking": "high"}}
    _, first = eval_harness.run_text("agy", installed_prompt(skill), cfg, tmp_path / "first",
        timeout=3, retries=0, readonly=True, exec_cwd=cwd, real_home=answer_only_cli,
        installed_read_policy=context)
    path = skill if damage == "source" else Path(context["hook_log"]) if damage == "hook-log" else Path(context[damage]["path"])
    path.write_text(path.read_text() + "\n")
    with pytest.raises(RuntimeError, match="drift"):
        eval_harness.run_text("agy", installed_prompt(skill, "Resume. "), cfg, tmp_path / "second",
            timeout=3, retries=0, readonly=True, exec_cwd=cwd, real_home=answer_only_cli,
            installed_read_policy=context, session_id=first["session_id"])
    assert len((answer_only_cli / "launches.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("damage", ["", "wrong-command-hook-fixture ", "wrong-command-display-fixture "])
def test_installed_policy_joins_observed_denied_run_command_display_projection(tmp_path, answer_only_cli, installed_policy, damage):
    context, skill, cwd = installed_policy
    calls = [{"name": "read_file", "args": {"Path": str(skill)}},
             {"name": "run_command", "args": {"CommandLine": "echo harmless > denied-scratch.txt",
                  "WaitMsBeforeAsync": 5000, "toolSummary": "Canary write attempt"}}]
    prompt = damage + "reduced-command-parameters-fixture installed-read-attempts:" + json.dumps(calls)
    kwargs = dict(timeout=3, retries=0, readonly=True, exec_cwd=cwd,
                  real_home=answer_only_cli, installed_read_policy=context)
    cfg = {"agy": {"model": "fixture-model", "thinking": "high"}}
    if damage:
        with pytest.raises(RuntimeError, match="installed-read evidence"):
            eval_harness.run_text("agy", prompt, cfg, tmp_path / "capture", **kwargs)
    else:
        _, record = eval_harness.run_text("agy", prompt, cfg, tmp_path / "capture", **kwargs)
        assert record["agy_installed_read"]["allowed_tool_calls"] == 1
        assert record["agy_installed_read"]["denied_tool_calls"] == 1
