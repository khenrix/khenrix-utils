"""Real offline kernel fences and opt-in transport; native catalog proof is separate."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests/support"))
import eval_claude_scratch_policy as policy


@pytest.fixture
def policy_case(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("macOS kernel fence")
    cwd = Path(tempfile.mkdtemp(prefix="cq", dir="/Users/Shared"))
    home = tmp_path / "home"
    (home / ".claude/debug").mkdir(parents=True)
    (home / ".npm/_logs").mkdir(parents=True)
    try:
        yield cwd, home, tmp_path / "controls"
    finally:
        shutil.rmtree(cwd)


def prepare(case):
    cwd, home, controls = case
    return policy.prepare(cwd, controls, home=home, environment=dict(os.environ))


def run(context, code, **extra):
    return policy.call_tool(context, {"argv": [sys.executable, "-I", "-B", "-c", code], **extra}, request_id=1)


def test_independent_rpc_executes_cwd_and_temp_writes(policy_case):
    context = prepare(policy_case)
    result = run(context, "import os; from pathlib import Path; Path('inside').write_text('fenced'); "
                 "Path(os.environ['TMPDIR'], 'inside').write_text('temporary')")
    assert result["isError"] is False
    assert (policy_case[0] / "inside").read_text() == "fenced"
    assert (policy_case[0] / "t/inside").read_text() == "temporary"
    assert context["native_enforcement_verified"] is False
    assert set(policy._load_policy(context["policy"])["environment"]) == {"HOME", "PATH", "TMPDIR", "LC_ALL"}


@pytest.mark.parametrize("outside", ["tmp", "private_tmp", "home_debug", "home_logs", "sibling", "symlink"])
def test_kernel_denies_writes_outside_cwd(policy_case, tmp_path, outside):
    context = prepare(policy_case)
    cwd, home, _ = policy_case
    with tempfile.TemporaryDirectory(prefix="cq-out-", dir="/tmp") as temporary:
        directory = tmp_path / "outside"
        directory.mkdir()
        if outside == "tmp":
            target = Path(temporary) / "sentinel"
        elif outside == "private_tmp":
            target = Path(temporary).resolve() / "sentinel"
        elif outside == "home_debug":
            target = home / ".claude/debug/sentinel"
        elif outside == "home_logs":
            target = home / ".npm/_logs/sentinel"
        elif outside == "symlink":
            (cwd / "link").symlink_to(directory, target_is_directory=True)
            target = cwd / "link/sentinel"
        else:
            target = directory / "sentinel"
        result = run(context, "from pathlib import Path; Path(" + repr(str(target)) + ").write_text('escaped')")
        assert result["isError"] is True
        assert not target.exists()
        assert "Operation not permitted" in result["content"][0]["text"]


def test_rejects_unsandboxed_argument_before_execution(policy_case):
    context = prepare(policy_case)
    result = run(context, "from pathlib import Path; Path('bypass').write_text('bypass')", dangerouslyDisableSandbox=True)
    assert result["isError"] is True
    assert not (policy_case[0] / "bypass").exists()
    log = Path(policy._load_policy(context["policy"])["hook_log"])
    assert json.loads(log.read_text())["decision"] == "deny"


@pytest.mark.parametrize("argv", [[], [""], [None], ["/bin/true", "\x00"],
                                  ["a"] * 257, ["a" * 32769, "b" * 32768]])
def test_rejects_invalid_or_oversized_argv_before_execution(policy_case, argv):
    context = prepare(policy_case)
    result = policy.call_tool(context, {"argv": argv}, request_id=1)
    assert result["isError"] is True
    log = Path(policy._load_policy(context["policy"])["hook_log"])
    assert json.loads(log.read_text())["decision"] == "deny"


def test_kernel_denies_detached_fork_before_program_returns(policy_case):
    context = prepare(policy_case)
    result = run(context, """import os,time,json
from pathlib import Path
Path('executed').write_text('executed')
try:
    child = os.fork()
except OSError as error:
    print(json.dumps({'fork_errno': error.errno}))
else:
    if child == 0:
        os.setsid()
        for fd in (0, 1, 2): os.close(fd)
        time.sleep(0.25)
        Path('late').write_text('late')
        os._exit(0)
    print(json.dumps({'fork_errno': None}))
""")
    assert result["isError"] is False
    output = json.loads(result["content"][0]["text"])["output"]
    assert json.loads(output)["fork_errno"] == 1
    assert (policy_case[0] / "executed").exists()
    time.sleep(0.5)
    assert not (policy_case[0] / "late").exists()


def test_kernel_denies_posix_spawn(policy_case):
    context = prepare(policy_case)
    result = run(context, """import os,sys,json
try:
    child = os.posix_spawn(sys.executable, [sys.executable, '-I', '-B', '-c',
        "from pathlib import Path; Path('spawned').write_text('spawned')"], dict(os.environ))
except OSError as error:
    print(json.dumps({'spawn_errno': error.errno}))
else:
    os.waitpid(child, 0)
    print(json.dumps({'spawn_errno': None}))
""")
    assert result["isError"] is False
    assert json.loads(json.loads(result["content"][0]["text"])["output"])["spawn_errno"] == 1
    assert not (policy_case[0] / "spawned").exists()


@pytest.mark.parametrize("target", ["cwd", "temp"])
@pytest.mark.parametrize("change", ["symlink", "inode", "mode"])
def test_rechecks_native_directory_identity(policy_case, target, change):
    context = prepare(policy_case)
    path = policy_case[0] if target == "cwd" else policy_case[0] / "t"
    if change == "mode":
        path.chmod(0o755)
        try:
            with pytest.raises(ValueError):
                policy.check(context)
        finally:
            path.chmod(0o700)
        return
    saved = path.with_name(path.name + "-saved")
    path.rename(saved)
    if change == "symlink":
        path.symlink_to(saved, target_is_directory=True)
    else:
        path.mkdir(mode=0o700)
    try:
        with pytest.raises(ValueError):
            policy.check(context)
    finally:
        path.unlink() if path.is_symlink() else path.rmdir()
        saved.rename(path)


@pytest.mark.parametrize("field", ["policy", "mcp_config", "sbpl"])
def test_rejects_control_drift_before_execution(policy_case, field):
    context = prepare(policy_case)
    reference = context[field] if field != "sbpl" else policy._load_policy(context["policy"])["references"][field]
    Path(reference["path"]).write_text("{}")
    with pytest.raises((ValueError, OSError)):
        policy.check(context)
    assert not (policy_case[0] / "inside").exists()


def test_stdio_mcp_catalog_execution_and_strict_arguments(policy_case):
    context = prepare(policy_case)
    config = json.loads(Path(context["mcp_config"]["path"]).read_text())["mcpServers"]["scratch"]
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "run", "arguments": {"argv": [sys.executable, "-I", "-B", "-c",
                "import os; from pathlib import Path; Path('home').write_text(os.environ['HOME'])"]}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "run", "arguments": {"argv": [sys.executable, "-I", "-B", "-c",
                "from pathlib import Path; Path('bypass').touch()"], "dangerouslyDisableSandbox": True}}},
    ]
    result = subprocess.run([config["command"], *config["args"]],
        input="".join(json.dumps(row) + "\n" for row in requests), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    responses = {row["id"]: row for row in map(json.loads, result.stdout.splitlines())}
    tools = responses[2]["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["run"]
    assert tools[0]["inputSchema"]["additionalProperties"] is False
    assert tools[0]["inputSchema"]["required"] == ["argv"]
    assert responses[3]["result"]["isError"] is False
    assert (policy_case[0] / "home").read_text() == str(policy_case[1])
    assert responses[4]["result"]["isError"] is True
    assert not (policy_case[0] / "bypass").exists()


@pytest.mark.parametrize("fault", [None, "pre-control-drift", "final-control-drift", "private-artifact"])
def test_qualification_adapter_removes_native_writers_and_preserves_shared_profile(policy_case, tmp_path, monkeypatch, fault):
    import eval_harness
    sys.path.insert(0, str(ROOT / "tests/support"))
    import claude_fixture_executor
    home = policy_case[1]
    directory = tmp_path / "bin"
    directory.mkdir()
    cli = directory / "claude"
    cli.write_text("#!" + sys.executable + "\n" + '''
import sys, os, json
from pathlib import Path
args = sys.argv[1:]
Path(os.environ['HOME'],'launch.json').write_text(json.dumps({'args':args,'home':os.environ['HOME']}))
if os.environ.get('FIXTURE_CONTROL_DRIFT'):
 Path(os.environ['FIXTURE_CONTROL_DRIFT']).write_text('{}')
print(json.dumps({'type':'result','subtype':'success','is_error':False,
 'session_id':args[args.index('--session-id')+1], 'result':os.environ.get('FIXTURE_OUTPUT','fixture')}))
''')
    cli.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    registry = eval_harness.eval_runtime.ProviderRegistry.default(
        version_probe=lambda _: policy.CLI_VERSION, contract_probe=lambda _: None)
    profile = registry.select("claude", "read-only", "standard", role="executor")
    before = profile.initial_argv
    context = prepare(policy_case)
    capture = tmp_path / "capture"
    if fault == "pre-control-drift":
        Path(context["policy"]["path"]).write_text("{}")
    elif fault == "final-control-drift":
        monkeypatch.setenv("FIXTURE_CONTROL_DRIFT", context["policy"]["path"])
    elif fault == "private-artifact":
        monkeypatch.setenv("FIXTURE_OUTPUT", "qualification-artifact-canary")
    if fault:
        with pytest.raises((ValueError, RuntimeError), match="drift|protected|canary"):
            claude_fixture_executor.run_text("Read supplied evidence.",
                {"claude": {"model": "fixture-model", "thinking": "medium"}}, capture,
                timeout=3, real_home=home, registry=registry, claude_scratch_policy=context,
                tool_canary="qualification-artifact-canary")
        assert profile.initial_argv == before
        if fault == "pre-control-drift":
            assert not (home / "launch.json").exists()
        elif fault == "private-artifact":
            assert [path.name for path in capture.iterdir()] == ["SECURITY_FAILURE.txt"]
        else:
            assert (home / "launch.json").exists()
            assert (capture / "claude.stdout.attempt-1.txt").exists()
        return
    answer, record = claude_fixture_executor.run_text("Read supplied evidence.",
        {"claude": {"model": "fixture-model", "thinking": "medium"}}, capture,
        timeout=3, real_home=home, registry=registry, claude_scratch_policy=context)
    assert answer == "fixture"
    args = json.loads((home / "launch.json").read_text())["args"]
    assert args[args.index("--tools") + 1] == "Skill,Read"
    assert "--strict-mcp-config" in args
    assert args[args.index("--mcp-config") + 1] == context["mcp_config"]["path"]
    assert "--settings" not in args
    assert profile.initial_argv == before
    assert record["claude_scratch_policy"]["controls_unchanged"] is True
    assert record["claude_scratch_policy"]["native_enforcement_verified"] is False
    assert record["attempts"] == 1
    assert record["session_id"] == args[args.index("--session-id") + 1]
    assert Path(record["stdout_file"]).exists()
    assert Path(record["stderr_file"]).exists()
    assert json.loads((capture / "manifest.json").read_text())["providers"] == [record]


def test_policy_changes_stale_instrument_without_changing_delivered_sources(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts/lib"))
    import checks
    (tmp_path / "capabilities.toml").write_text("")
    source = tmp_path / "tests/support/eval_claude_scratch_policy.py"
    source.parent.mkdir(parents=True)
    source.write_text("policy = 1\n")
    before_instrument = checks.manifest_hash(checks.instrument_manifest(tmp_path))
    before_tuneup = checks.manifest_hash(checks.delivered_behavior_manifest(tmp_path, "skill-tuneup"))
    before_quality = checks.manifest_hash(checks.delivered_behavior_manifest(tmp_path, "khenrix-quality"))
    source.write_text("policy = 2\n")
    assert checks.manifest_hash(checks.instrument_manifest(tmp_path)) != before_instrument
    assert checks.manifest_hash(checks.delivered_behavior_manifest(tmp_path, "skill-tuneup")) == before_tuneup
    assert checks.manifest_hash(checks.delivered_behavior_manifest(tmp_path, "khenrix-quality")) == before_quality
