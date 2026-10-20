"""Consumer-level native read evidence, using real inert CLI subprocesses."""
import json
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
    print(json.dumps({'event':'init','conversation_id':'native-consumer-conversation','init':{}}))
    print(json.dumps({'event':'result','result':{'conversation_id':'native-consumer-conversation','status':'ERROR' if error else 'SUCCESS','response':answer,**({'error':'quota'} if error else {})}}))
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
