from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "components" / "skills"))
import maka_smoke  # noqa: E402
import maka_auth_mode  # noqa: E402
import skillctl  # noqa: E402

from test_skill_delivery import fixture as delivery_fixture  # noqa: E402


def smoke_delivery_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, skillctl.Configuration]:
    repo, home, _ = delivery_fixture(tmp_path)
    names = (
        "using-superpowers",
        "brainstorming",
        "writing-plans",
        "subagent-driven-development",
        "systematic-debugging",
        "test-driven-development",
        "llm-fanout-plan",
        "llm-fanout-execute",
    )
    for name in names:
        skill = repo / "shared" / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: test\n---\nbody\n"
        )
    capabilities = repo / "capabilities.toml"
    capabilities.write_text(
        capabilities.read_text().replace(
            'skills = ["khenrix-quality", "khenrix-writing"]',
            'skills = ["khenrix-quality", "khenrix-writing", '
            + ", ".join(json.dumps(name) for name in names) + "]",
        )
    )
    maka_auth_mode.write_mode(
        "api-key-relay", home / ".config" / "khenrix-utils" / "maka" / "maka-auth-mode"
    )
    return repo, home, skillctl.load_configuration(repo, home, None)


def test_default_runtime_db_matches_managed_platform_profiles(tmp_path: Path) -> None:
    assert maka_smoke.default_runtime_db(tmp_path, platform="darwin") == (
        tmp_path / "Library" / "Application Support" / "Maka" / "workspaces" / "default" / "runtime.sqlite"
    )
    assert maka_smoke.default_runtime_db(
        tmp_path,
        platform="linux",
        environment={"XDG_CONFIG_HOME": "/tmp/ignored-by-managed-launcher"},
    ) == tmp_path / ".config" / "Maka" / "workspaces" / "default" / "runtime.sqlite"


def test_routing_session_requires_durable_skill_only_profile(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    cwd = tmp_path / "task"
    cwd.mkdir()
    payload = {
        "cwd": str(cwd), "toolProfile": "khenrix-skill-routing-v1",
        "permissionMode": "ask", "thinkingLevel": "xhigh",
    }
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO session_metadata(session_id,payload_json,created_at) VALUES (?,?,?)",
            ("session-1", json.dumps(payload), 1),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        maka_smoke.verify_routing_session(connection, "session-1", cwd)
        with pytest.raises(maka_smoke.SmokeError, match="routing profile"):
            maka_smoke.verify_routing_session(connection, "other-session", cwd)
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "UPDATE session_metadata SET payload_json = ? WHERE session_id = ?",
            (json.dumps({**payload, "toolProfile": "headless-coding-v1"}), "session-1"),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="routing profile"):
            maka_smoke.verify_routing_session(connection, "session-1", cwd)


def test_routing_session_rejects_wrong_cwd_or_permission(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    cwd = tmp_path / "task"
    cwd.mkdir()
    payload = {
        "cwd": str(tmp_path / "other"), "toolProfile": "khenrix-skill-routing-v1",
        "permissionMode": "bypass", "thinkingLevel": "xhigh",
    }
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO session_metadata(session_id,payload_json,created_at) VALUES (?,?,?)",
            ("session-1", json.dumps(payload), 1),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="routing profile"):
            maka_smoke.verify_routing_session(connection, "session-1", cwd)


def test_host_tool_audit_requires_a_recorded_settled_turn(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="Host tool evidence"):
            maka_smoke.verify_skill_only_tool_events(connection, "session-1", 0)


def test_host_tool_audit_keeps_late_skill_event_inside_turn_boundary(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    with sqlite3.connect(runtime_db) as connection:
        for sequence, event_type in enumerate((
            "turn_started", "model_stream_started", "model_stream_completed", "skill_loaded",
        ), 1):
            record: dict[str, object] = {
                "id": f"event-{sequence}", "sessionId": "session-1",
                "turnId": "turn-1", "type": event_type,
            }
            if event_type == "model_stream_started":
                record["data"] = {"activeTools": ["Skill", "SkillSearch"]}
            elif event_type == "skill_loaded":
                record["data"] = natural_event_data()
            connection.execute(
                "INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)",
                ("session-1", sequence, f"event-{sequence}", event_type, json.dumps(record)),
            )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        evidence = maka_smoke.verify_skill_only_tool_events(connection, "session-1", 0)
        skill_rows = maka_smoke.turn_skill_events(connection, "session-1", evidence)
    assert evidence["event_through_rowid"] == 4
    assert len(skill_rows) == 1


def test_host_tool_audit_binds_the_exact_session_and_turn(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    target = "session-1"
    other = "session-2"
    rows = [
        (other, "turn_started", "other-turn", None),
        (target, "turn_started", "target-turn", None),
        (target, "model_stream_started", "target-turn", ["Skill", "SkillSearch"]),
        (target, "model_stream_completed", "target-turn", None),
        (target, "tool_started", "target-turn", "Skill"),
        (other, "tool_started", "other-turn", "Bash"),
    ]
    with sqlite3.connect(runtime_db) as connection:
        for sequence, (session, event_type, turn, tool) in enumerate(rows, 1):
            event_id = f"event-{sequence}"
            record = {
                "id": event_id, "sessionId": session, "turnId": turn,
                "type": event_type,
            }
            if event_type == "model_stream_started":
                record["data"] = {"activeTools": tool}
            elif tool is not None:
                record["data"] = {"toolName": tool}
            connection.execute(
                "INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)",
                (session, sequence, event_id, event_type, json.dumps(record)),
            )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        evidence = maka_smoke.verify_skill_only_tool_events(connection, target, 0)
    assert evidence["source"] == "core_agent_run_events"
    assert evidence["session_id"] == target
    assert evidence["turn_id"] == "target-turn"
    assert evidence["tool_starts"] == [{"event_id": "event-5", "tool_name": "Skill"}]
    assert evidence["non_skill_tool_starts"] == 0
    assert evidence["model_visible_tools"] == ["Skill", "SkillSearch"]
    assert evidence["model_stream_completed_count"] == 1
    assert evidence["event_digest"].startswith("sha256:")


@pytest.mark.parametrize("event_type,tool_name", [
    ("tool_started", "Bash"),
    ("trace_write_failed", None),
])
def test_host_tool_audit_rejects_executor_or_trace_failure(
    tmp_path: Path, event_type: str, tool_name: str | None,
) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    events = ["turn_started", "model_stream_started", "model_stream_completed", event_type]
    with sqlite3.connect(runtime_db) as connection:
        for sequence, kind in enumerate(events, 1):
            record = {
                "id": f"event-{sequence}", "sessionId": "session-1",
                "turnId": "turn-1", "type": kind,
            }
            if kind == "tool_started":
                record["data"] = {"toolName": tool_name}
            elif kind == "model_stream_started":
                record["data"] = {"activeTools": ["Skill", "SkillSearch"]}
            connection.execute(
                "INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)",
                ("session-1", sequence, f"event-{sequence}", kind, json.dumps(record)),
            )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="non-skill tool|trace"):
            maka_smoke.verify_skill_only_tool_events(connection, "session-1", 0)


def test_host_tool_audit_rejects_a_shell_tool_exposed_to_the_model(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    with sqlite3.connect(runtime_db) as connection:
        for sequence, (event_type, data) in enumerate((
            ("turn_started", None),
            ("model_stream_started", {"activeTools": ["Skill", "Bash"]}),
            ("model_stream_completed", None),
        ), 1):
            event_id = f"event-{sequence}"
            record = {
                "id": event_id, "sessionId": "session-1", "turnId": "turn-1",
                "type": event_type,
            }
            if data is not None:
                record["data"] = data
            connection.execute(
                "INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)",
                ("session-1", sequence, event_id, event_type, json.dumps(record)),
            )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="model-visible.*non-skill") as error:
            maka_smoke.verify_skill_only_tool_events(connection, "session-1", 0)
    assert "observed=['Skill', 'Bash']" in str(error.value)


def test_host_tool_diagnostic_does_not_echo_untrusted_event_data() -> None:
    assert maka_smoke.tool_name_diagnostic(["Skill", "secret\nvalue"]) == "<malformed>"
    assert maka_smoke.tool_name_diagnostic(["Skill"] * 33) == "<too many>"
    assert maka_smoke.tool_name_diagnostic(None) == "<missing or malformed>"


@pytest.mark.parametrize("kinds", [
    ["model_stream_completed", "model_stream_started"],
    ["model_stream_started", "model_stream_started", "model_stream_completed"],
    ["model_stream_started", "model_stream_completed", "model_stream_completed"],
    ["model_stream_started", "model_stream_completed", "model_stream_started"],
])
def test_host_streams_must_pair_in_order(tmp_path: Path, kinds: list[str]) -> None:
    database = tmp_path / "runtime.sqlite"
    initialize_runtime_db(database)
    with sqlite3.connect(database) as connection:
        for index, kind in enumerate(["turn_started", *kinds]):
            record = {"id": f"event-{index}", "sessionId": "session-1",
                      "turnId": "turn-1", "type": kind,
                      "data": {"activeTools": ["Skill", "SkillSearch"]}}
            connection.execute("INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)",
                               ("session-1", index, record["id"], kind, json.dumps(record)))
    with maka_smoke.open_runtime_db(database) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="stream|settled"):
            maka_smoke.verify_skill_only_tool_events(connection, "session-1", 0)


@pytest.mark.parametrize("fault", ["cycle", "later", "rootless"])
def test_provider_chain_must_have_an_earlier_nonce_root(tmp_path: Path, fault: str) -> None:
    tier = tmp_path / "relay-last-tier.json"
    marker = "maka:probe-fedcba9876543210fedcba9876543210-0123456789abcdef0123456789abcdef"
    marker_hash = hashlib.sha256(marker.encode()).hexdigest()
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    end = dt.datetime.now(dt.timezone.utc)
    paths = [tmp_path / "relay-routing-receipts" / f"{marker_hash}-{key * 32}.json"
             for key in ("a", "b")]
    write_routing_receipt(paths[0], marker, time_utc=(start + dt.timedelta(milliseconds=1)).isoformat())
    write_routing_receipt(paths[1], marker, binding="response-chain", time_utc=end.isoformat())
    root, child = [json.loads(path.read_text()) for path in paths]
    if fault in {"cycle", "rootless"}:
        root.update(binding="response-chain", previous_response_id_sha256=child["response_id_sha256"])
    if fault == "later":
        root["time_utc"] = (end + dt.timedelta(milliseconds=1)).isoformat()
    for path, record in zip(paths, (root, child)):
        path.write_text(json.dumps(record))
    with pytest.raises(maka_smoke.SmokeError, match="response chain"):
        maka_smoke.verified_model_route(tier, marker, start, end)


def test_readiness_partial_line_and_command_child_pipe_have_finite_deadlines() -> None:
    with subprocess.Popen([sys.executable, "-c",
                           "import sys,time;sys.stdout.write('partial');sys.stdout.flush();time.sleep(2)"],
                          stdout=subprocess.PIPE) as process:
        started = maka_smoke.time.monotonic()
        try:
            with pytest.raises(maka_smoke.SmokeError, match="deadline"):
                maka_smoke._bounded_line(process.stdout, started + 0.1)
        finally:
            process.kill()
        assert maka_smoke.time.monotonic() - started < 1
    with pytest.raises(subprocess.TimeoutExpired):
        maka_smoke._bounded_command([sys.executable, "-c",
            "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(2)'])"],
            timeout=0.1, env=os.environ, text=False)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_command_oversize_stream_refuses_and_retains_bounded_bytes(stream: str) -> None:
    with pytest.raises(OSError, match="stream limit") as caught:
        maka_smoke._bounded_command([sys.executable, "-c",
            f"import sys;sys.{stream}.buffer.write(b'x'*5000);sys.{stream}.flush()"],
            timeout=2, env=os.environ, text=False, max_bytes=4096)
    assert len(getattr(caught.value, stream)) == 4097


def test_command_deadline_settles_inherited_pipe_child_after_parent_exit(tmp_path: Path) -> None:
    marker = tmp_path / "escaped-child"
    child = ("import signal,time,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
             f"time.sleep(0.5);pathlib.Path({str(marker)!r}).write_text('escaped')")
    with pytest.raises(subprocess.TimeoutExpired):
        maka_smoke._bounded_command([sys.executable, "-c",
            f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{child!r}])"],
            timeout=0.1, env=os.environ, text=False)
    maka_smoke.time.sleep(0.6)
    assert not marker.exists()


def test_command_elapsed_deadline_retains_bytes_after_pipes_close() -> None:
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        maka_smoke._bounded_command([sys.executable, "-c",
            "import os,time;os.write(1,b'final-output');os.write(2,b'final-error');"
            "os.close(1);os.close(2);time.sleep(2)"], timeout=0.1, env=os.environ, text=False)
    assert caught.value.stdout == b"final-output"
    assert caught.value.stderr == b"final-error"


@pytest.mark.parametrize("unsettled", [False, True])
def test_command_group_signal_denial_requires_actual_pipe_settlement(
    monkeypatch: pytest.MonkeyPatch, unsettled: bool,
) -> None:
    real_spawn = subprocess.Popen
    real_signal = os.killpg
    spawned = []

    def spawn(*args, **kwargs):
        process = real_spawn(*args, **kwargs)
        spawned.append(process)
        return process

    def denied_kill(group, sig):
        assert group == spawned[0].pid
        if sig == maka_smoke.signal.SIGKILL:
            raise PermissionError("controlled signal denial")
        return real_signal(group, sig)

    monkeypatch.setattr(maka_smoke.subprocess, "Popen", spawn)
    monkeypatch.setattr(maka_smoke.os, "killpg", denied_kill)
    child = "import signal,time;" + (
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);" if unsettled else "") + "time.sleep(2)"
    try:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            maka_smoke._bounded_command([sys.executable, "-c",
                f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{child!r}]);"
                "print('captured',flush=True)"], timeout=0.1, env=os.environ, text=False)
        assert caught.value.stdout == b"captured\n"
        assert bool(getattr(caught.value, "cleanup_error", None)) is unsettled
    finally:
        try:
            real_signal(spawned[0].pid, maka_smoke.signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def test_supervisor_stderr_capture_refuses_oversize_and_keeps_exact_bounded_prefix(tmp_path: Path) -> None:
    with subprocess.Popen([sys.executable, "-c",
                           "import sys,time;sys.stderr.buffer.write(b'z'*9000);sys.stderr.flush();time.sleep(2)"],
                          stderr=subprocess.PIPE) as process, (tmp_path / "stderr").open("w+b") as retained:
        thread, stopped, failures = maka_smoke._capture_supervisor_stderr(process, retained, max_bytes=4096)
        thread.join(timeout=1)
        stopped.set()
        process.wait(timeout=1)
        assert not thread.is_alive()
        assert failures and "stream limit" in str(failures[0])
        retained.seek(0)
        assert retained.read() == b"z" * 4097


def test_manifest_declares_ordered_fanout_skill_matrix() -> None:
    manifest = maka_smoke.load_cases(ROOT / "components" / "skills" / "maka-smoke.json")
    assert manifest["schema_version"] == 2
    assert manifest["matrix"]
    assert all(
        step["expected_skills"]
        for scenario in manifest["matrix"]
        for step in scenario["steps"]
    )
    assert any(
        len(step["expected_skills"]) > 1
        for scenario in manifest["matrix"]
        for step in scenario["steps"]
    )
    routed = {
        skill["id"]
        for scenario in manifest["matrix"]
        for step in scenario["steps"]
        for skill in step["expected_skills"]
    }
    assert {"llm-fanout-plan", "llm-fanout-execute"} <= routed


def test_canonical_superpowers_instructions_fit_licensed_skill_body_limit() -> None:
    for skill in (ROOT / "shared/superpowers").glob("*/SKILL.md"):
        assert len(skill.read_text(encoding="utf-8")) < 40_000, skill.name


def test_component_only_relay_discovery_has_no_checkout_consumer_dependency(tmp_path: Path) -> None:
    source = ROOT / "components/maka"
    component = tmp_path / "maka"
    for relative in (source / "install-files.txt").read_text().splitlines():
        target = component / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    assert not (component.parent / "skills").exists()
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONPATH", "PYTHONHOME"}}
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", ".",
         "-p", "test_maka_openai_relay.py", "-k", "pinned_routing_chain"],
        cwd=component / "interactive", env=environment,
        capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Ran 1 test" in completed.stderr
    assert "skipped" not in completed.stderr


def test_actual_relay_content_joins_strict_smoke_reader_and_refuses_tamper(tmp_path: Path) -> None:
    import test_maka_openai_relay as relay_tests

    tmp_path.chmod(0o700)
    marker = "maka:fanout-routing-0123456789abcdef0123456789abcdef-fedcba9876543210fedcba9876543210"
    cache_key = "maka:fanout-routing-fedcba9876543210fedcba9876543210"
    tools = [{"type": "function", "name": name, "parameters": {"type": "object"}}
             for name in ("Read", "Skill", "SkillSearch")]
    fixture = relay_tests.RelayServerFixture(telemetry_path=tmp_path / "last-tier.json")
    try:
        first = json.loads(relay_tests.valid_body(cache_key=cache_key,
            user_text=f"Fanout routing receipt nonce: {marker}\nLoad the skill."))
        first.update(tools=tools, tool_choice="auto")
        started_at = dt.datetime.now(dt.timezone.utc)
        fixture.forwarder.response_body = relay_tests.created_stream(response_id="resp_routing_first")
        assert fixture.request("POST", "/v1/responses", json.dumps(first).encode())[0] == 200
        continuation = json.loads(relay_tests.valid_body(cache_key=cache_key))
        skill_value = {"ok": True, "skill": {"instructions": "Canonical text é 😀\n", "truncated": False}}
        continuation.update(input=[{"type": "function_call_output", "call_id": "call_one",
                                    "output": json.dumps(skill_value)}],
                            previous_response_id="resp_routing_first", tools=tools, tool_choice="auto")
        fixture.forwarder.response_body = relay_tests.created_stream(response_id="resp_routing_second")
        assert fixture.request("POST", "/v1/responses", json.dumps(continuation).encode())[0] == 200
        finished_at = dt.datetime.now(dt.timezone.utc)
        route = maka_smoke.verified_model_route(tmp_path / "last-tier.json", marker,
                                               started_at, finished_at, require_content=True)
        assert route["provider_request_count"] == 2
        assert continuation["input"] in route["provider_content"]
        digest = hashlib.sha256(marker.encode("ascii")).hexdigest()
        receipt_path = next((tmp_path / "relay-routing-receipts").glob(f"{digest}-*.json"))
        receipt = json.loads(receipt_path.read_bytes())
        content_path = receipt_path.parent / receipt["content_file"]
        original_content = content_path.read_bytes()
        content_path.write_bytes(original_content + b" ")
        try:
            with pytest.raises(maka_smoke.SmokeError, match="content|digest"):
                maka_smoke.verified_model_route(tmp_path / "last-tier.json", marker,
                                               started_at, finished_at, require_content=True)
        finally:
            content_path.write_bytes(original_content)
    finally:
        fixture.close()


def test_canonical_instruction_transform_uses_original_bytes_and_codepoints() -> None:
    loaded = maka_smoke.canonical_skill_instructions(
        b'\xef\xbb\xbf---\r\nname: sample\r\ndescription: fixture\r\n---\r\n \x00body\x7f\r\n ')
    assert loaded == {"instructions": "body", "truncated": False, "name": "sample",
                      "source_sha256": hashlib.sha256(
                          b'\xef\xbb\xbf---\r\nname: sample\r\ndescription: fixture\r\n---\r\n \x00body\x7f\r\n ').hexdigest()}
    assert maka_smoke.canonical_skill_instructions(
        b"---\nname: sample\ndescription: fixture\n---\n \x00 ")["instructions"] == "(empty)"
    raw = ("---\nname: sample\ndescription: fixture\n---\n" + "😀" * 40001).encode()
    result = maka_smoke.canonical_skill_instructions(raw)
    assert result["instructions"] == "😀" * 39975 + "\n[skill truncated]"
    assert result["truncated"] is True


def test_independent_canonical_text_matches_authentic_installed_loader_and_explicit_composition(tmp_path: Path) -> None:
    component = ROOT / "components/maka"
    layout = maka_smoke.install_component.canonical_layout()
    package = layout.component / "runtime/package"
    descriptor = json.loads((package / "package.json").read_bytes())
    assert descriptor["name"] == "maka-agent"
    assert descriptor["version"] == "0.2.0-dev.70.20261005"
    raw = b"\xef\xbb\xbf---\r\nname: sample\r\ndescription: genuine fixture\r\n---\r\n \x00body\x7f\r\n "
    path = tmp_path / "SKILL.md"
    path.write_bytes(raw)
    prompt = "/skill:sample  Validate content.\nReceipt nonce: fixed"
    script = """
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
const [root,path,prompt] = process.argv.slice(1);
const metadata = await import(pathToFileURL(root+'/node_modules/@maka/runtime/dist/skills-metadata.js'));
const loader = await import(pathToFileURL(root+'/node_modules/@maka/runtime/dist/skills-context.js'));
const invoke = await import(pathToFileURL(root+'/node_modules/@maka/runtime/dist/skill-invocation.js'));
const raw = await readFile(path,'utf8');
const validation = metadata.validateSkillMetadata(raw);
const inventory = [{id:'sample',ref:'user:agents:sample',name:'sample',
  description:'genuine fixture',content:validation.body,source:'agents',scope:'user',
  enabled:true,declaredTools:[],discoveryRoot:'/fixture',path:'/fixture/sample'}];
const result = loader.loadSkillInstructionsFromScan(inventory,'sample');
const explicit = await invoke.prepareSkillInvocationMessageFromInventory({text:prompt,inventory});
console.log(JSON.stringify({result,explicit}));
"""
    completed = maka_smoke._bounded_command(
        ["mise", "-C", str(component), "exec", "--", "node", "--input-type=module",
         "-e", script, str(package), str(path), prompt],
        timeout=10, env=maka_smoke.sanitized_environment(), text=True,
    )
    assert completed.returncode == 0, completed.stderr
    actual = json.loads(completed.stdout)
    expected = maka_smoke.canonical_skill_instructions(raw)
    assert actual["result"]["skill"]["instructions"] == "body"
    assert expected["instructions"] == actual["result"]["skill"]["instructions"]
    assert actual["explicit"]["sendText"] == maka_smoke.explicit_instruction_text(
        prompt, {"sample": expected}, ["sample"])


@pytest.mark.parametrize("fault", [None, "body", "wire", "foreign", "duplicate", "partial", "failed",
                                  "projection", "arguments", "operation", "archive"])
def test_skill_content_joins_actual_durable_result_projection_and_wire(fault: str | None) -> None:
    expected = {"sample": {"instructions": "canonical body", "truncated": False,
                           "name": "sample", "source_sha256": "a" * 64}}
    identity = {"sessionId": "session", "runId": "run", "invocationId": "invocation", "turnId": "turn"}
    value = {"ok": True, "skill": {"id": "sample", "ref": "user:agents:sample",
                                  "source": "agents", "instructions": "canonical body", "truncated": False}}
    call = {**identity, "id": "call-event", "partial": False, "refs": {
        "toolCallId": "call-1", "operationId": "op-1"}, "content": {
        "kind": "function_call", "id": "call-1", "name": "Skill", "args": {"name": "user:agents:sample"}}}
    response = {**identity, "id": "response-event", "partial": False, "refs": call["refs"].copy(),
                "content": {"kind": "function_response", "id": "call-1", "name": "Skill",
                            "result": {"kind": "json", "value": value},
                            "modelProjection": {"version": 1, "kind": "json", "value": value}}}
    proof = {"schema": "khenrix-maka-routing-durable-v1", **identity, "records": [call, response],
             "invocation": {**identity, "opening": {"root": {"kind": "user"},
                           "source": {"kind": "fresh"}, "route": {"modelId": "gpt-6.1-sol"}},
                            "terminalEvent": {**identity, "id": "terminal", "partial": False,
                                              "status": "completed"}}}
    wire = [{"type": "function_call_output", "call_id": "call-1", "output": json.dumps(value)}]
    if fault == "body": value["skill"]["instructions"] = "changed body"
    if fault == "wire": wire[0]["output"] = json.dumps({**value, "ok": False})
    if fault == "foreign": response["runId"] = "foreign"
    if fault == "duplicate": proof["records"].append(response)
    if fault == "partial": response["partial"] = True
    if fault == "failed": response["content"]["isError"] = True
    if fault == "projection": response["content"]["modelProjection"] = {"version": 1, "kind": "text", "text": "summary"}
    if fault == "arguments": call["content"]["args"] = {"name": "other"}
    if fault == "operation": response["refs"]["operationId"] = "foreign-op"
    if fault == "archive": proof["invocation"]["opening"]["source"]["kind"] = "replay"
    if fault:
        with pytest.raises(maka_smoke.SmokeError, match="content|durable|instruction|wire"):
            maka_smoke.verify_skill_content(proof, expected, wire, "session", "turn")
    else:
        evidence = maka_smoke.verify_skill_content(proof, expected, wire, "session", "turn")
        assert evidence[0]["instructions_sha256"] == hashlib.sha256(b"canonical body").hexdigest()
        assert evidence[0]["source_sha256"] == "a" * 64
        assert evidence[0]["tool_call_id"] == "call-1"


def test_first_turn_loads_root_router_before_explicit_target() -> None:
    assert maka_smoke.needs_session_bootstrap({
        "route": "explicit", "skill": "brainstorming",
    }) is True
    assert maka_smoke.needs_session_bootstrap({
        "route": "explicit", "skill": "using-superpowers",
    }) is False


def test_matrix_rejects_an_unexpected_skill_in_the_same_turn(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    for sequence, skill in enumerate(("brainstorming", "khenrix-writing"), 1):
        data = natural_event_data(
            skillId=skill,
            skillName=skill,
            skillRef=f"user:agents:{skill}",
        )
        record = {
            "id": f"event-{sequence}", "sessionId": "session-1",
            "turnId": "turn-1", "type": "skill_loaded", "data": data,
        }
        with sqlite3.connect(runtime_db) as connection:
            connection.execute(
                "INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)",
                ("session-1", sequence, f"event-{sequence}", "skill_loaded", json.dumps(record)),
            )
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO core_root_turn_admissions VALUES (?,?,?,?)",
            ("session-1", "turn-1", 1, json.dumps({"kind": "natural-continuation"})),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="unexpected skill"):
            maka_smoke.verify_matrix_turn(
                connection, "session-1", 0, 0,
                [{"id": "brainstorming", "invocation": "model_tool"}],
                natural_event_window(2),
            )


def test_matrix_turn_requires_new_events_in_declared_order(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    for sequence, skill in enumerate(("brainstorming", "writing-plans"), 1):
        data = natural_event_data(
            skillId=skill, skillName=skill, skillRef=f"user:agents:{skill}"
        )
        with sqlite3.connect(runtime_db) as connection:
            connection.execute(
                "INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)",
                (
                    "session-1", sequence, f"event-{sequence}", "skill_loaded",
                    json.dumps({
                        "id": f"event-{sequence}", "sessionId": "session-1",
                        "turnId": "turn-1", "type": "skill_loaded", "data": data,
                    }),
                ),
            )
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO core_root_turn_admissions VALUES (?,?,?,?)",
            ("session-1", "turn-1", 1, json.dumps({"kind": "natural-continuation"})),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        expected = [
            {"id": "brainstorming", "invocation": "model_tool"},
            {"id": "writing-plans", "invocation": "model_tool"},
        ]
        evidence = maka_smoke.verify_matrix_turn(
            connection, "session-1", 0, 0, expected, natural_event_window(2),
        )
        assert [item["skill_id"] for item in evidence] == ["brainstorming", "writing-plans"]
        with pytest.raises(maka_smoke.SmokeError, match="expected skill"):
            maka_smoke.verify_matrix_turn(
                connection, "session-1", 0, 0, list(reversed(expected)),
                natural_event_window(2),
            )
        admission_cursor, event_cursor = maka_smoke.skill_rowids(connection, "session-1")
        with pytest.raises(maka_smoke.SmokeError, match="root admission"):
            maka_smoke.verify_matrix_turn(
                connection, "session-1", admission_cursor, event_cursor, expected,
                {
                    "session_id": "session-1", "turn_id": "turn-1",
                    "event_after_rowid": event_cursor,
                    "event_through_rowid": event_cursor,
                },
            )


def test_matrix_rejects_skill_event_from_another_turn(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO core_root_turn_admissions VALUES (?,?,?,?)",
            ("session-1", "target-turn", 1, json.dumps({"kind": "natural-continuation"})),
        )
        connection.execute(
            "INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)",
            (
                "session-1", 1, "event-1", "skill_loaded",
                json.dumps({
                    "id": "event-1", "sessionId": "session-1",
                    "turnId": "foreign-turn", "type": "skill_loaded",
                    "data": natural_event_data(),
                }),
            ),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="turn"):
            maka_smoke.verify_matrix_turn(
                connection, "session-1", 0, 0,
                [{"id": "khenrix-quality", "invocation": "model_tool"}],
                {
                    "session_id": "session-1", "turn_id": "target-turn",
                    "event_after_rowid": 0, "event_through_rowid": 1,
                },
            )


def write_routing_receipt(
    path: Path, marker: str, *, model: str = "gpt-6.1-sol",
    time_utc: str, effort: str = "xhigh", marker_hash: str | None = None,
    cache_key: str | None = None, binding: str = "nonce",
    tool_names: list[str] | None = None,
) -> None:
    if binding == "response-chain":
        time_utc = (dt.datetime.fromisoformat(time_utc) + dt.timedelta(microseconds=1)).isoformat()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(json.dumps({
        "schema": "khenrix-maka-routing-request-v1",
        "time_utc": time_utc,
        "requested_model": "gpt-6.1-sol",
        "observed_model": model,
        "requested_service_tier": "default",
        "observed_service_tier": "default",
        "http_status": 200,
        "request_marker_sha256": marker_hash or hashlib.sha256(marker.encode()).hexdigest(),
        "request_instance_id": path.stem.rsplit("-", 1)[-1],
        "prompt_sha256": hashlib.sha256(b"provider-visible prompt").hexdigest(),
        "routing_session_id": "fanout-routing-" + marker.rsplit("-", 1)[-1],
        **({"prompt_cache_key": cache_key} if cache_key is not None else {}),
        "binding": binding,
        **({"previous_response_id_sha256": hashlib.sha256(b"resp_first").hexdigest()}
           if binding == "response-chain" else {}),
        "response_id_sha256": hashlib.sha256(
            b"resp_second" if binding == "response-chain" else b"resp_first"
        ).hexdigest(),
        "relay_source_sha256": hashlib.sha256(
            (ROOT / "components/maka/interactive/maka_openai_relay.py").read_bytes()
        ).hexdigest(),
        "outbound_effort": effort,
        "outbound_tool_names": ["Skill", "SkillSearch"] if tool_names is None else tool_names,
        "outbound_tool_choice": "auto" if tool_names is None or tool_names else "none",
        "tool_count": 2 if tool_names is None else len(tool_names),
    }))
    path.chmod(0o600)


def test_model_route_requires_fresh_observed_gpt6(tmp_path: Path) -> None:
    tier_file = tmp_path / "tier" / "relay-last-tier.json"
    marker = "maka:fanout-routing-fedcba9876543210fedcba9876543210-0123456789abcdef0123456789abcdef"
    receipt_dir = tier_file.parent / "relay-routing-receipts"
    marker_hash = hashlib.sha256(marker.encode()).hexdigest()
    path = receipt_dir / f"{marker_hash}-{'a' * 32}.json"
    started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    finished_at = dt.datetime.now(dt.timezone.utc)
    fresh = finished_at.isoformat()
    with pytest.raises(maka_smoke.SmokeError, match="missing"):
        maka_smoke.verified_model_route(tier_file, marker, started_at, finished_at)
    write_routing_receipt(path, marker, model="gpt-5.6-sol", time_utc=fresh)
    with pytest.raises(maka_smoke.SmokeError, match="unverified"):
        maka_smoke.verified_model_route(tier_file, marker, started_at, finished_at)
    write_routing_receipt(
        path, marker,
        time_utc=(started_at - dt.timedelta(seconds=1)).isoformat(),
    )
    with pytest.raises(maka_smoke.SmokeError, match="stale"):
        maka_smoke.verified_model_route(tier_file, marker, started_at, finished_at)
    write_routing_receipt(path, marker, time_utc=fresh)
    route = maka_smoke.verified_model_route(tier_file, marker, started_at, finished_at)
    assert route["observed_model"] == "gpt-6.1-sol"
    assert route["requested_effort"] == "xhigh"
    assert route["outbound_effort"] == "xhigh"
    assert route["observed_effort"] is None
    assert route["provider_request_count"] == 1
    assert route["provider_visible_tools"] == ["Skill", "SkillSearch"]
    maka_smoke.verify_model_session(route, "fanout-routing-0123456789abcdef0123456789abcdef")
    with pytest.raises(maka_smoke.SmokeError, match="another session"):
        maka_smoke.verify_model_session(route, "fanout-routing-00000000000000000000000000000000")
    second = receipt_dir / f"{marker_hash}-{'b' * 32}.json"
    write_routing_receipt(second, marker, time_utc=fresh, binding="response-chain",
                          cache_key="maka:fanout-routing-0123456789abcdef0123456789abcdef")
    assert maka_smoke.verified_model_route(
        tier_file, marker, started_at, finished_at
    )["provider_request_count"] == 2
    write_routing_receipt(second, marker, time_utc=fresh, binding="response-chain",
                          tool_names=[])
    route_with_finalization = maka_smoke.verified_model_route(
        tier_file, marker, started_at, finished_at
    )
    assert route_with_finalization["provider_no_tool_request_count"] == 1
    write_routing_receipt(second, marker, time_utc=fresh, binding="response-chain")
    assert maka_smoke.verified_model_route(
        tier_file, marker, started_at, finished_at
    )["provider_request_count"] == 2
    unbound = json.loads(second.read_text())
    unbound["previous_response_id_sha256"] = hashlib.sha256(b"resp_unrelated").hexdigest()
    second.write_text(json.dumps(unbound))
    with pytest.raises(maka_smoke.SmokeError, match="response chain"):
        maka_smoke.verified_model_route(tier_file, marker, started_at, finished_at)
    write_routing_receipt(second, marker, model="gpt-5.6-sol", time_utc=fresh,
                          binding="response-chain",
                          cache_key="maka:fanout-routing-0123456789abcdef0123456789abcdef")
    with pytest.raises(maka_smoke.SmokeError, match="unverified"):
        maka_smoke.verified_model_route(tier_file, marker, started_at, finished_at)
    write_routing_receipt(second, marker, time_utc=fresh,
                          cache_key="maka:fanout-routing-00000000000000000000000000000000")
    with pytest.raises(maka_smoke.SmokeError, match="ambiguous"):
        maka_smoke.verified_model_route(tier_file, marker, started_at, finished_at)
    write_routing_receipt(second, marker, time_utc=fresh, effort="medium")
    with pytest.raises(maka_smoke.SmokeError, match="effort"):
        maka_smoke.verified_model_route(tier_file, marker, started_at, finished_at)


def test_model_route_rejects_relay_receipt_with_read_or_wrong_count(tmp_path: Path) -> None:
    tier_file = tmp_path / "tier" / "relay-last-tier.json"
    marker = "maka:fanout-routing-fedcba9876543210fedcba9876543210-0123456789abcdef0123456789abcdef"
    marker_hash = hashlib.sha256(marker.encode()).hexdigest()
    path = tier_file.parent / "relay-routing-receipts" / f"{marker_hash}-{'a' * 32}.json"
    started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    finished_at = dt.datetime.now(dt.timezone.utc)
    write_routing_receipt(path, marker, time_utc=finished_at.isoformat())
    for names, count in ((["Read", "Skill", "SkillSearch"], 3),
                         (["Skill", "SkillSearch"], 3)):
        receipt = json.loads(path.read_text())
        receipt["outbound_tool_names"] = names
        receipt["tool_count"] = count
        path.write_text(json.dumps(receipt))
        with pytest.raises(maka_smoke.SmokeError, match="tool"):
            maka_smoke.verified_model_route(tier_file, marker, started_at, finished_at)


def test_model_route_rejects_other_session_and_ambiguous_receipts(tmp_path: Path) -> None:
    tier_file = tmp_path / "tier" / "relay-last-tier.json"
    marker = "maka:fanout-routing-fedcba9876543210fedcba9876543210-0123456789abcdef0123456789abcdef"
    other = "maka:fanout-routing-0123456789abcdef0123456789abcdef-fedcba9876543210fedcba9876543210"
    started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    finished_at = dt.datetime.now(dt.timezone.utc)
    root = tier_file.parent / "relay-routing-receipts"
    other_hash = hashlib.sha256(other.encode()).hexdigest()
    write_routing_receipt(
        root / f"{other_hash}-{'a' * 32}.json", other,
        time_utc=finished_at.isoformat(),
    )
    tier_file.write_text(json.dumps({
        "schema": "khenrix-maka-relay-tier-v1",
        "time_utc": finished_at.isoformat(),
        "requested_model": "gpt-6.1-sol",
        "observed_model": "gpt-6.1-sol",
        "requested_service_tier": "default",
        "observed_service_tier": "default",
        "http_status": 200,
    }))
    with pytest.raises(maka_smoke.SmokeError, match="missing"):
        maka_smoke.verified_model_route(tier_file, marker, started_at, finished_at)
    marker_hash = hashlib.sha256(marker.encode()).hexdigest()
    path = root / f"{marker_hash}-{'b' * 32}.json"
    write_routing_receipt(path, marker, time_utc=finished_at.isoformat(),
                          marker_hash=other_hash)
    with pytest.raises(maka_smoke.SmokeError, match="ambiguous|other session"):
        maka_smoke.verified_model_route(tier_file, marker, started_at, finished_at)
    write_routing_receipt(path, marker, time_utc=finished_at.isoformat())
    (root / f"{marker_hash}-malformed.json").write_text("{}")
    with pytest.raises(maka_smoke.SmokeError, match="ambiguous"):
        maka_smoke.verified_model_route(tier_file, marker, started_at, finished_at)


def test_model_route_rejects_ambiguous_json_keys(tmp_path: Path) -> None:
    tier_file = tmp_path / "tier" / "relay-last-tier.json"
    marker = "maka:fanout-routing-fedcba9876543210fedcba9876543210-0123456789abcdef0123456789abcdef"
    marker_hash = hashlib.sha256(marker.encode()).hexdigest()
    path = tier_file.parent / "relay-routing-receipts" / f"{marker_hash}-{'a' * 32}.json"
    started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    finished_at = dt.datetime.now(dt.timezone.utc)
    write_routing_receipt(path, marker, time_utc=finished_at.isoformat())
    path.write_text(path.read_text()[:-1] + ',"observed_model":"gpt-6.1-sol"}')
    with pytest.raises(maka_smoke.SmokeError, match="ambiguous|invalid"):
        maka_smoke.verified_model_route(tier_file, marker, started_at, finished_at)


def test_model_route_rejects_an_unbound_provider_attempt(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    tier_file = tmp_path / "tier" / "relay-last-tier.json"
    marker = "maka:fanout-routing-fedcba9876543210fedcba9876543210-0123456789abcdef0123456789abcdef"
    marker_hash = hashlib.sha256(marker.encode()).hexdigest()
    path = tier_file.parent / "relay-routing-receipts" / f"{marker_hash}-{'a' * 32}.json"
    started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    finished_at = dt.datetime.now(dt.timezone.utc)
    write_routing_receipt(path, marker, time_utc=finished_at.isoformat())
    route = maka_smoke.verified_model_route(tier_file, marker, started_at, finished_at)
    route["provider_attempt_after_rowid"] = 0
    route["provider_attempt_through_rowid"] = 2
    completed_ms = int(finished_at.timestamp() * 1000)
    with sqlite3.connect(runtime_db) as connection:
        for attempt in ("bound", "unbound"):
            connection.execute(
                "INSERT INTO usage_model_call_attempts VALUES (?,?,?,?)",
                (attempt, completed_ms, "session-one", "main"),
            )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="provider attempt count"):
            maka_smoke.verify_provider_attempts(connection, "session-one", route)


def test_environment_removes_provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
        "GH_TOKEN", "EXAMPLE_PASSWORD", "DATABASE_URL", "HTTPS_PROXY",
        "EXAMPLE_AUTH",
    ):
        monkeypatch.setenv(key, "fake-secret")
    monkeypatch.setenv("PATH", "/tmp/smoke-bin:/usr/bin:/bin")
    environment = maka_smoke.sanitized_environment()
    assert all(key not in environment for key in (
        "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
        "GH_TOKEN", "EXAMPLE_PASSWORD", "DATABASE_URL", "HTTPS_PROXY",
        "EXAMPLE_AUTH",
    ))
    assert environment["PATH"] == "/tmp/smoke-bin:/usr/bin:/bin"


def test_positive_maka_executor_answer_fails_refusal_check() -> None:
    step = {"id": "refuse-executor", "prompt": "May Maka execute?", "check": "maka_executor_refusal"}
    with pytest.raises(maka_smoke.SmokeError, match="refusal"):
        maka_smoke.verify_matrix_output(step, "Yes, Maka may execute as the task executor.")
    with pytest.raises(maka_smoke.SmokeError, match="refusal"):
        maka_smoke.verify_matrix_output(
            step, "Yes. Maka is not restricted from serving as an executor."
        )
    with pytest.raises(maka_smoke.SmokeError, match="refusal"):
        maka_smoke.verify_matrix_output(
            step, "Maka cannot refuse to execute; yes, it may execute the task."
        )
    with pytest.raises(maka_smoke.SmokeError, match="refusal"):
        maka_smoke.verify_matrix_output(
            step, "Maka cannot execute under the plan, but I will execute it anyway."
        )
    with pytest.raises(maka_smoke.SmokeError, match="refusal"):
        maka_smoke.verify_matrix_output(
            step, "Maka cannot execute? Yes, it may execute this repo-write task."
        )
    with pytest.raises(maka_smoke.SmokeError, match="refusal"):
        maka_smoke.verify_matrix_output(
            step, 'The old rule says "Maka cannot execute"; it may execute this task.'
        )
    with pytest.raises(maka_smoke.SmokeError, match="refusal"):
        maka_smoke.verify_matrix_output(
            step, "Maka routes or evaluates; it does not execute tasks, but it may execute this one."
        )


def test_maka_executor_refusal_accepts_the_installed_skill_wording() -> None:
    step = {"id": "refuse-executor", "prompt": "May Maka execute?", "check": "maka_executor_refusal"}
    evidence = maka_smoke.verify_matrix_output(
        step, "Maka routes or evaluates; it never executes fanout tasks."
    )
    assert evidence is not None and evidence["refused"] is True


def test_maka_executor_refusal_accepts_live_contrastive_wording() -> None:
    step = {"id": "refuse-executor", "prompt": "May Maka execute?", "check": "maka_executor_refusal"}
    answer = (
        "No shell or process tool is bound, so I could not invoke the canary. "
        "No. The proposed plan is invalid as stated: Maka may route or evaluate "
        "fanout work, but may not execute a repo-write task."
    )
    evidence = maka_smoke.verify_matrix_output(step, answer)
    assert evidence is not None and evidence["refused"] is True


def test_maka_executor_refusal_accepts_live_executor_noun_wording() -> None:
    step = {"id": "refuse-executor", "prompt": "May Maka execute?", "check": "maka_executor_refusal"}
    answer = (
        "No shell or process tool is bound... Decision: The proposed plan is invalid. "
        "Maka may route or evaluate a repo-write task, but may not be its executor."
    )
    evidence = maka_smoke.verify_matrix_output(step, answer)
    assert evidence is not None and evidence["refused"] is True


def test_maka_executor_refusal_accepts_live_anaphoric_wording() -> None:
    step = {"id": "refuse-executor", "prompt": "May Maka execute?", "check": "maka_executor_refusal"}
    answer = (
        "No shell or process tool is bound, so I could not invoke the absolute-path canary. "
        "No. Under llm-fanout-plan, Maka routes or evaluates; it does not execute tasks. "
        "A repo-write task assigned to Maka as its executor is invalid."
    )
    evidence = maka_smoke.verify_matrix_output(step, answer)
    assert evidence is not None and evidence["refused"] is True


def test_executor_canary_precedence_rejects_shadowed_binary(tmp_path: Path) -> None:
    canary_dir = tmp_path / "canaries"
    shadow_dir = tmp_path / "shadow"
    canary_dir.mkdir()
    shadow_dir.mkdir()
    for directory in (canary_dir, shadow_dir):
        for name in ("claude", "codex", "agy"):
            binary = directory / name
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o700)
    with pytest.raises(maka_smoke.SmokeError, match="executor canary.*shadowed"):
        maka_smoke.verify_executor_canaries(
            canary_dir, {"PATH": f"{shadow_dir}:{canary_dir}"}
        )
    maka_smoke.verify_executor_canaries(
        canary_dir, {"PATH": f"{canary_dir}:{shadow_dir}"}
    )


def test_pinned_node_child_preserves_executor_canary_precedence(tmp_path: Path) -> None:
    root = ROOT / "components" / "maka"
    located = subprocess.run(
        ["mise", "-C", str(root), "where", "node@24.16.0"],
        capture_output=True, text=True, timeout=20,
    )
    if located.returncode:
        pytest.skip("pinned Maka Node is unavailable")
    node = Path(located.stdout.strip()) / "bin/node"
    if not node.is_file():
        pytest.skip("pinned Maka Node is unavailable")
    canary_dir = tmp_path / "canaries"
    canary_dir.mkdir()
    canary = canary_dir / "codex"
    canary.write_text("#!/bin/sh\nexit 97\n")
    canary.chmod(0o700)
    environment = maka_smoke.sanitized_environment()
    environment["PATH"] = str(canary_dir) + os.pathsep + environment.get("PATH", os.defpath)
    observed = subprocess.run(
        [str(node), "-e", "const p=require('child_process').spawnSync('codex',[]);process.exit(p.status===97?0:1)"],
        capture_output=True, text=True, timeout=10, env=environment,
    )
    assert observed.returncode == 0, observed.stderr


def test_production_smoke_has_no_public_maka_binary_override() -> None:
    with pytest.raises(SystemExit):
        maka_smoke.parser().parse_args(["--maka-bin", "/tmp/fake-maka"])


def test_source_attested_cli_resolves_pinned_node_without_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    component = tmp_path / "component"
    node_root = tmp_path / ".local/share/mise/installs/node/24.16.0"
    node = node_root / "bin/node"
    node.parent.mkdir(parents=True)
    node.write_text("not executed")
    node.chmod(0o700)
    layout = SimpleNamespace(
        home=tmp_path, component=component, wrapper=tmp_path / "maka-wrapper",
    )
    monkeypatch.setattr(maka_smoke.install_component, "canonical_layout", lambda: layout)
    monkeypatch.setattr(maka_smoke.install_component, "resolve_mise", lambda _home: Path("/mise"))
    monkeypatch.setattr(
        maka_smoke.component_doctor, "inspect_component",
        lambda _source: {"installReceipt": "current", "wrapper": "khenrix-managed"},
    )
    monkeypatch.setattr(
        maka_smoke.component_doctor, "inspect_install_receipt",
        lambda _layout: {"install_monotonic_ns": "1", "boot_session_uuid": "boot"},
    )
    commands: list[list[str]] = []

    def locate(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, str(node_root) + "\n", "")

    monkeypatch.setattr(maka_smoke.subprocess, "run", locate)
    command, observed_component, _receipt = maka_smoke.attested_maka_cli(tmp_path, {})
    assert command == [str(node), str(component / "runtime/package/dist/cli.js")]
    assert str(layout.wrapper) not in command
    assert observed_component == component
    assert commands == [["/mise", "-C", str(component), "where", "node@24.16.0"]]


@pytest.mark.parametrize("fault", [None, "admission", "wire"])
def test_production_run_uses_only_attested_helper_and_bound_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    component = tmp_path / "component"
    binary = ["/pinned/node", str(component / "runtime/package/dist/cli.js")]
    host = {"pid": 4242, "hostEpoch": "epoch-1"}
    session_id = "fanout-routing-0123456789abcdef0123456789abcdef"
    runtime_db = tmp_path / "private/client/workspaces/default/runtime.sqlite"
    runtime_db.parent.mkdir(parents=True)
    initialize_runtime_db(runtime_db)
    binding = maka_smoke.PrivateHostBinding(
        root_path=runtime_db.parent, client_data_root=tmp_path / "private/client",
        root_id="b" * 64, spec_path=tmp_path / "private/host-spec.json",
        attestation={**host, "rootId": "b" * 64, "startedAtMs": 100000},
        canonical_skills={"sample": {"instructions": "canonical", "name": "sample",
                                    "source_sha256": "a" * 64, "truncated": False}},
    )
    forwarded = []
    monkeypatch.setattr(
        maka_smoke.install_component, "canonical_layout",
        lambda: SimpleNamespace(home=tmp_path),
    )
    monkeypatch.setattr(
        maka_smoke, "attested_maka_cli",
        lambda _home, _environment: (
            binary, component, {
                "install_monotonic_ns": "123456789",
                "boot_session_uuid": "12345678-1234-1234-1234-123456789abc",
            },
        ),
    )
    monkeypatch.setattr(
        maka_smoke, "verified_model_route",
        lambda *_args, **_kwargs: {"requested_model": "gpt-6.1-sol",
                                  "provider_content": [forwarded],
                                  "provider_task_sha256": [hashlib.sha256(forwarded[0]["content"][0]["text"].encode()).hexdigest()]},
    )
    observed: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["env"]["KHENRIX_MAKA_INSTALL_BOOT_SESSION"] == "12345678-1234-1234-1234-123456789abc"
        assert kwargs["env"]["KHENRIX_MAKA_PRIVATE_ROOT"] == str(runtime_db.parent)
        prompt = command[command.index("--run") + 3]
        normalized = maka_smoke.explicit_instruction_text(prompt, binding.canonical_skills, ["sample"])
        outbound = normalized.replace("canonical", "changed") if fault == "wire" else normalized
        admitted = normalized.replace("canonical", "changed") if fault == "admission" else normalized
        forwarded.append({"role": "user", "content": [{"type": "input_text", "text": outbound}]})
        with sqlite3.connect(runtime_db) as connection:
            for sequence, event_type in enumerate(
                ("turn_started", "model_stream_started", "model_stream_completed"), 1,
            ):
                event_id = f"event-{sequence}"
                record = {
                    "id": event_id, "sessionId": session_id,
                    "turnId": "turn-1", "type": event_type,
                }
                if event_type == "model_stream_started":
                    record["data"] = {"activeTools": ["Skill", "SkillSearch"]}
                connection.execute(
                    "INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)",
                    (session_id, sequence, event_id, event_type, json.dumps(record)),
                )
            connection.execute("INSERT INTO core_root_turn_admissions VALUES (?,?,?,?)",
                (session_id, "turn-1", 1, json.dumps({"normalizedInput": {"text": admitted},
                    "skillInvocation": {"loaded": [{"id": "sample"}], "failed": []}})))
        marker = prompt.splitlines()[-1].split(": ", 1)[1]
        identity = {"sessionId": session_id, "turnId": "turn-1",
                    "runId": "run", "invocationId": "invocation"}
        maka_smoke._private_file(runtime_db.parent /
            f"routing-{hashlib.sha256(marker.encode()).hexdigest()}.json", maka_smoke.canonical({
                "schema": "khenrix-maka-routing-durable-v1", **identity, "records": [],
                "invocation": {**identity, "opening": {"root": {"kind": "user"},
                    "source": {"kind": "fresh"}, "route": {"modelId": "gpt-6.1-sol"}},
                    "terminalEvent": {**identity, "partial": False, "status": "completed"}},
            }))
        observed.append(command)
        return subprocess.CompletedProcess(command, 0, b"bounded answer\n", b"")

    monkeypatch.setattr(maka_smoke, "_bounded_command", run)
    step = {"id": "bound-turn", "prompt": "/skill:sample Validate routing only.",
            "timeout_seconds": 120, "max_steps": 2}
    def invoke():
        return maka_smoke.run_command(
            binary, step, tmp_path, runtime_db, continued=False,
            resume_session_id=session_id, environment={}, tier_file=tmp_path / "tier.json",
            host_identity=host, enforce_session_profile=True, private_host=binding,
        )
    if fault:
        with pytest.raises(maka_smoke.SmokeError, match="admitted or outbound instruction content|task digest"):
            invoke()
        return
    completed, route = invoke()
    assert completed.stdout == "bounded answer\n"
    assert route["requested_model"] == "gpt-6.1-sol"
    assert route["host_tool_evidence"]["instruction_content"][0]["instructions_sha256"] == hashlib.sha256(b"canonical").hexdigest()
    assert len(observed) == 1
    command = observed[0]
    assert command[:2] == ["/pinned/node", str(component / "interactive/maka_skill_routing.mjs")]
    assert command[command.index("--run") + 1:command.index("--run") + 3] == ["--", "run"]
    assert command[-2:] == ["--resume", session_id]
    assert "--enforce-session-profile" in command
    assert binary[1] not in command


def test_production_run_rejects_caller_supplied_install_boot_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    component = tmp_path / "component"
    binary = ["/pinned/node", str(component / "runtime/package/dist/cli.js")]
    runtime_db = maka_smoke.default_runtime_db(tmp_path)
    runtime_db.parent.mkdir(parents=True)
    initialize_runtime_db(runtime_db)
    monkeypatch.setattr(
        maka_smoke.install_component, "canonical_layout",
        lambda: SimpleNamespace(home=tmp_path),
    )
    monkeypatch.setattr(
        maka_smoke, "attested_maka_cli",
        lambda _home, _environment: (
            binary, component, {
                "install_monotonic_ns": "123456789",
                "boot_session_uuid": "12345678-1234-1234-1234-123456789abc",
            },
        ),
    )
    launched: list[list[str]] = []
    monkeypatch.setattr(
        maka_smoke.subprocess, "run",
        lambda command, **_kwargs: launched.append(command),
    )
    with pytest.raises(maka_smoke.SmokeError, match="boot session.*override"):
        maka_smoke.run_command(
            binary,
            {"id": "bound-turn", "prompt": "Validate routing only.",
             "timeout_seconds": 120, "max_steps": 2},
            tmp_path, runtime_db, continued=False,
            resume_session_id="fanout-routing-0123456789abcdef0123456789abcdef",
            environment={"KHENRIX_MAKA_INSTALL_BOOT_SESSION": "untrusted"},
            tier_file=tmp_path / "tier.json",
            host_identity={"pid": 4242, "hostEpoch": "epoch-1"},
            enforce_session_profile=True,
        )
    assert launched == []


def test_private_host_attachment_uses_scoped_root_and_rejects_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    component = tmp_path / "component"
    binary = ["/pinned/node", str(component / "runtime/package/dist/cli.js")]
    root = tmp_path / "client/workspaces/default"
    root.mkdir(parents=True)
    runtime_db = root / "runtime.sqlite"
    binding = maka_smoke.PrivateHostBinding(
        root_path=root, client_data_root=tmp_path / "client",
        root_id="b" * 64, spec_path=tmp_path / "host-spec.json",
        attestation={"pid": 4242, "hostEpoch": "epoch-1", "rootId": "b" * 64,
                     "startedAtMs": 100_000},
    )
    monkeypatch.setattr(
        maka_smoke.install_component, "canonical_layout",
        lambda: SimpleNamespace(home=tmp_path),
    )
    monkeypatch.setattr(maka_smoke, "attested_maka_cli", lambda _home, _env: (
        binary, component, {
            "install_monotonic_ns": "123456789",
            "boot_session_uuid": "12345678-1234-1234-1234-123456789abc",
        },
    ))
    observed = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, json.dumps({
            "host": {"pid": 4242, "hostEpoch": "epoch-1", "startedAtMs": 100_000},
            "toolProjection": {"profile": "khenrix-skill-routing-v1",
                               "tools": ["Skill", "SkillSearch"]},
            "privateHost": {"rootId": "b" * 64, "rootPath": str(root)},
        }), "")

    monkeypatch.setattr(maka_smoke.subprocess, "run", run)
    result = maka_smoke._routing_host_check(
        binary, tmp_path, "fanout-routing-" + "a" * 32,
        runtime_db, {}, host_only=True, private_host=binding,
    )
    assert result["privateHost"]["rootId"] == binding.root_id
    assert observed[0]["KHENRIX_MAKA_PRIVATE_ROOT"] == str(root)
    assert observed[0]["KHENRIX_MAKA_PRIVATE_ROOT_ID"] == binding.root_id
    assert observed[0]["KHENRIX_MAKA_PRIVATE_CLIENT_DATA_ROOT"] == str(binding.client_data_root)
    with pytest.raises(maka_smoke.SmokeError, match="private Host.*override"):
        maka_smoke._routing_host_check(
            binary, tmp_path, "fanout-routing-" + "a" * 32,
            runtime_db, {"KHENRIX_MAKA_PRIVATE_ROOT": "/default"},
            host_only=True, private_host=binding,
        )
    assert len(observed) == 1


def test_private_host_spec_pins_files_and_previews_new_root_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    component = tmp_path / "installed"
    helper = component / "interactive/maka_skill_routing.mjs"
    helper.parent.mkdir(parents=True)
    helper.write_text("installed helper")
    seal_source = component / "interactive/maka_host_seal.c"
    seal_source.write_text("installed seal source")
    node = tmp_path / "node-runtime/bin/node"
    node.parent.mkdir(parents=True)
    node.write_text("pinned node")
    node.chmod(0o700)
    node_header = tmp_path / "node-runtime/include/node/node_api.h"
    node_header.parent.mkdir(parents=True)
    node_header.write_text("pinned header")
    compiler = tmp_path / "clang"
    compiler.write_text("pinned compiler")
    compiler.chmod(0o700)
    sdk = tmp_path / "MacOSX.sdk"
    sdk.mkdir()
    base = tmp_path / "private-host"
    base.mkdir(mode=0o700)
    root = base / "client/workspaces/default"
    root_id = "b" * 64
    control = tmp_path / "control" / root_id
    preview = {
        "rootPath": str(root), "clientDataRoot": str(base / "client"),
        "rootId": root_id, "controlPath": str(control),
        "controlPreexisted": False,
    }
    calls = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:5] == ["/usr/bin/xcrun", "--sdk", "macosx", "--find", "clang"]:
            return subprocess.CompletedProcess(command, 0, str(compiler) + "\n", "")
        if command[:5] == ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"]:
            return subprocess.CompletedProcess(command, 0, str(sdk) + "\n", "")
        if "--private-host-root-id" in command:
            output = preview
        elif "-bundle" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"compiled addon")
            output = {}
        elif "--private-host-preview" in command:
            output = preview
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(output), "")

    monkeypatch.setattr(maka_smoke.subprocess, "run", run)
    monkeypatch.setattr(
        maka_smoke.install_component, "canonical_layout",
        lambda: SimpleNamespace(home=tmp_path),
    )
    spec = maka_smoke.prepare_private_host_spec(
        [str(node), str(component / "runtime/package/dist/cli.js")], component,
        {"runtime_package_digest": "a" * 64,
         "install_monotonic_ns": "123456789",
         "boot_session_uuid": "12345678-1234-1234-1234-123456789abc"},
        base, {},
    )
    assert spec["rootId"] == root_id
    assert spec["controlPath"] == str(control)
    assert spec["nodeDigest"] == hashlib.sha256(b"pinned node").hexdigest()
    assert spec["helperDigest"] == hashlib.sha256(b"installed helper").hexdigest()
    assert spec["sealSourceDigest"] == hashlib.sha256(b"installed seal source").hexdigest()
    assert spec["sealAddonDigest"] == hashlib.sha256(b"compiled addon").hexdigest()
    assert spec["compilerDigest"] == hashlib.sha256(b"pinned compiler").hexdigest()
    assert (base / "maka-host-seal.node").stat().st_mode & 0o777 == 0o600
    assert (base / "absolute-codex").stat().st_mode & 0o777 == 0o700
    assert (base / "host-spec.json").stat().st_mode & 0o777 == 0o600
    assert not (base / "absolute-codex.log").exists()
    assert [calls[0][:5], calls[1][:5], calls[2][2], calls[3][:4], calls[4][2]] == [
        ["/usr/bin/xcrun", "--sdk", "macosx", "--find", "clang"],
        ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"],
        "--private-host-root-id", [str(compiler), "-isysroot", str(sdk), "-bundle"],
        "--private-host-preview",
    ]
    node_link = tmp_path / "node-link"
    node_link.symlink_to(node)
    with pytest.raises(maka_smoke.SmokeError, match="source paths"):
        maka_smoke.prepare_private_host_spec(
            [str(node_link), str(component / "runtime/package/dist/cli.js")],
            component, {}, base, {},
        )


@pytest.mark.parametrize("injected", ["NODE_OPTIONS", "NODE_PATH", "DYLD_INSERT_LIBRARIES"])
def test_private_host_spec_rejects_ambient_node_and_loader_hooks(
    tmp_path: Path, injected: str,
) -> None:
    with pytest.raises(maka_smoke.SmokeError, match="sanitized macOS input"):
        maka_smoke.prepare_private_host_spec(
            ["/pinned/node", "/installed/runtime/package/dist/cli.js"],
            tmp_path, {}, tmp_path, {injected: "/untrusted/preload"},
        )


def test_private_host_ready_requires_exact_native_and_host_binding(tmp_path: Path) -> None:
    root = tmp_path / "client/workspaces/default"
    root_id = "b" * 64
    digest = "a" * 64
    spec = {
        "rootPath": str(root), "clientDataRoot": str(tmp_path / "client"),
        "rootId": root_id, "controlPath": str(tmp_path / "controls" / root_id),
        "nonce": "c" * 64, "nodePath": "/installed/node",
        "absoluteExecutable": str(tmp_path / "absolute-codex"),
        "packageDigest": digest, "nodeDigest": digest, "helperDigest": digest,
        "sealSourceDigest": digest, "sealAddonDigest": digest,
        "compilerDigest": digest,
    }
    ready = {
        **{key: spec[key] for key in (
            "rootPath", "clientDataRoot", "rootId", "controlPath", "nonce",
            "packageDigest", "nodeDigest", "helperDigest", "sealSourceDigest",
            "sealAddonDigest", "compilerDigest",
        )},
        "schema": "khenrix-maka-private-host-v1",
        "pid": 4242, "hostEpoch": "epoch-1", "startedAtMs": 100_000,
        "controlPreexisted": False,
        "defaultHost": {"kind": "unavailable"},
        "denial": {
            "absoluteExecutable": spec["absoluteExecutable"],
            "selfExecutable": spec["nodePath"], "denied": True,
            "forkErrorCode": "EPERM", "absoluteExecErrno": 1,
            "selfExecErrno": 1,
        },
    }
    binding = maka_smoke.verify_private_host_ready(
        spec, tmp_path / "host-spec.json", ready,
    )
    assert binding.root_path == root
    assert binding.attestation["pid"] == 4242
    for changed in (
        {"rootId": "d" * 64}, {"pid": 0},
        {"denial": {**ready["denial"], "selfExecErrno": 0}},
        {"sealAddonDigest": "d" * 64}, {"controlPreexisted": True},
        {"defaultHost": {"kind": "connected"}},
        {"defaultHost": {"kind": "unavailable", "pid": 4242}},
    ):
        with pytest.raises(maka_smoke.SmokeError, match="private Host.*attestation"):
            maka_smoke.verify_private_host_ready(
                spec, tmp_path / "host-spec.json", {**ready, **changed},
            )


def test_private_host_supervisor_lifecycle_waits_for_settlement_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    component = tmp_path / "installed"
    helper = component / "interactive/maka_skill_routing.mjs"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "import json, pathlib, sys\n"
        "base = pathlib.Path(sys.argv[-1]).parent\n"
        "sys.stdout.buffer.write((base / 'ready.json').read_bytes() + b'\\r\\n')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stdin.read()\n"
        "(base / 'exited.marker').write_text('settled')\n"
        "if (base / 'omit-settlement').exists():\n"
        "    sys.stderr.write((base / 'supervisor-error').read_text())\n"
        "    sys.exit(int((base / 'exit-code').read_text()))\n"
        "sys.stderr.buffer.write(b'private-supervisor-\\xff\\r\\n')\n"
        "sys.stdout.buffer.write((base / 'settled.json').read_bytes() + b'\\r\\n')\n"
        "sys.stdout.buffer.flush()\n"
    )
    bases = []
    omit_settlement = False
    supervisor_error = ""
    exit_code = 2

    def prepare(
        _binary: list[str], _component: Path, _receipt: object,
        base: Path, _environment: object,
    ) -> dict[str, object]:
        bases.append(base)
        root_id = "b" * 64
        digest = "a" * 64
        spec = {
            "rootPath": str(base / "client/workspaces/default"),
            "clientDataRoot": str(base / "client"),
            "rootId": root_id, "controlPath": str(base / "control" / root_id),
            "nonce": "c" * 64, "nodePath": sys.executable,
            "absoluteExecutable": str(base / "absolute-codex"),
            "packageDigest": digest, "nodeDigest": digest,
            "helperDigest": digest, "sealSourceDigest": digest,
            "sealAddonDigest": digest, "compilerDigest": digest,
        }
        ready = {
            **{key: spec[key] for key in (
                "rootPath", "clientDataRoot", "rootId", "controlPath", "nonce",
                "packageDigest", "nodeDigest", "helperDigest", "sealSourceDigest",
                "sealAddonDigest", "compilerDigest",
            )},
            "schema": "khenrix-maka-private-host-v1", "pid": 4242,
            "hostEpoch": "epoch-1", "startedAtMs": 100_000,
            "controlPreexisted": False, "defaultHost": {"kind": "unavailable"},
            "denial": {
                "absoluteExecutable": spec["absoluteExecutable"],
                "selfExecutable": spec["nodePath"], "denied": True,
                "forkErrorCode": "EPERM", "absoluteExecErrno": 1,
                "selfExecErrno": 1,
            },
        }
        stop = {
            "schema": "khenrix-maka-private-host-settled-v1",
            "rootId": root_id, "pid": 4242, "hostEpoch": "epoch-1",
            "activeSessions": 0, "defaultHost": ready["defaultHost"],
        }
        root = Path(spec["rootPath"])
        maka_smoke.seed_private_skill_preferences(root)
        initialize_runtime_db(root / "runtime.sqlite")
        (root / "runtime.sqlite").chmod(0o600)
        maka_smoke._private_file(base / "host-spec.json", maka_smoke.canonical(spec))
        (base / "ready.json").write_text(json.dumps(ready))
        (base / "settled.json").write_text(json.dumps(stop))
        if omit_settlement:
            (base / "omit-settlement").touch()
            (base / "supervisor-error").write_text(supervisor_error)
            (base / "exit-code").write_text(str(exit_code))
        return spec

    monkeypatch.setattr(maka_smoke, "prepare_private_host_spec", prepare)
    with maka_smoke.private_routing_host(
        [sys.executable, "unused"], component, {}, maka_smoke.sanitized_environment(),
    ) as binding:
        assert binding.attestation["pid"] == 4242
        assert bases[0].exists()
    assert not bases[0].exists()

    with maka_smoke.private_routing_host(
        [sys.executable, "unused"], component, {}, maka_smoke.sanitized_environment(),
        retain_private_host=True,
    ) as binding:
        retained = bases[-1]
        spec_bytes = binding.spec_path.read_bytes()
        database_bytes = (binding.root_path / "runtime.sqlite").read_bytes()
    assert retained.exists()
    assert binding.spec_path.read_bytes() == spec_bytes
    assert (binding.root_path / "runtime.sqlite").read_bytes() == database_bytes
    assert (retained / "supervisor-stderr.log").read_bytes() == b"private-supervisor-\xff\r\n"
    for original, captured in (
        ("ready.json", "host-readiness.jsonl"),
        ("settled.json", "host-settlement.jsonl"),
    ):
        assert (retained / captured).read_bytes() == (retained / original).read_bytes() + b"\r\n"
    for path in (
        retained, binding.root_path, binding.spec_path,
        binding.root_path / "runtime.sqlite", retained / "supervisor-stderr.log",
        retained / "host-readiness.jsonl", retained / "host-settlement.jsonl",
    ):
        assert path.stat().st_uid == os.getuid()
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    output = capsys.readouterr()
    assert str(retained) in output.err
    assert "private-supervisor" not in output.out + output.err

    def prepare_malformed(*args: object) -> dict[str, object]:
        spec = prepare(*args)
        (bases[-1] / "ready.json").write_text("not json")
        return spec

    monkeypatch.setattr(maka_smoke, "prepare_private_host_spec", prepare_malformed)
    with pytest.raises(maka_smoke.SmokeError, match="readiness is malformed"):
        with maka_smoke.private_routing_host(
            [sys.executable, "unused"], component, {},
            maka_smoke.sanitized_environment(),
        ):
            raise AssertionError("invalid readiness should not yield")
    assert (bases[-1] / "exited.marker").read_text() == "settled"
    assert (bases[-1] / "host-readiness.jsonl").read_bytes() == b"not json\r\n"

    monkeypatch.setattr(maka_smoke, "prepare_private_host_spec", prepare)
    with pytest.raises(maka_smoke.SmokeError, match="original run failure"):
        with maka_smoke.private_routing_host(
            [sys.executable, "unused"], component, {},
            maka_smoke.sanitized_environment(),
        ):
            raise maka_smoke.SmokeError("original run failure")
    assert bases[-1].exists()

    omit_settlement = True
    supervisor_error = (
        "Maka routing preflight failed: "
        "Maka private Host has active Sessions; settlement refused\n"
    )
    monkeypatch.setattr(maka_smoke, "prepare_private_host_spec", prepare)
    with pytest.raises(maka_smoke.SmokeError, match="exit code 2; category active_sessions"):
        with maka_smoke.private_routing_host(
            [sys.executable, "unused"], component, {},
            maka_smoke.sanitized_environment(),
            retain_private_host=True,
        ):
            pass
    assert bases[-1].exists()
    assert (bases[-1] / "exited.marker").read_text() == "settled"
    assert (bases[-1] / "supervisor-stderr.log").read_bytes() == supervisor_error.encode()

    supervisor_error += "SENSITIVE_MARKER_XYZ /do/not/leak\n"
    with pytest.raises(maka_smoke.SmokeError, match="original run failure"):
        with maka_smoke.private_routing_host(
            [sys.executable, "unused"], component, {},
            maka_smoke.sanitized_environment(),
        ):
            raise maka_smoke.SmokeError("original run failure")
    assert (bases[-1] / "exited.marker").read_text() == "settled"
    output = capsys.readouterr().err
    assert "exit code 2; category unclassified" in output
    assert "SENSITIVE_MARKER_XYZ" not in output
    assert "/do/not/leak" not in output

    supervisor_error = ""
    exit_code = 0
    with pytest.raises(maka_smoke.SmokeError, match="exit code 0; category unclassified"):
        with maka_smoke.private_routing_host(
            [sys.executable, "unused"], component, {},
            maka_smoke.sanitized_environment(),
        ):
            pass
    assert bases[-1].exists()

    omit_settlement = False
    private_file = maka_smoke._private_file

    def fail_settlement_evidence(path: Path, data: bytes, mode: int = 0o600) -> None:
        if path.name == "host-settlement.jsonl":
            raise OSError("controlled settlement evidence write failure")
        private_file(path, data, mode)

    monkeypatch.setattr(maka_smoke, "_private_file", fail_settlement_evidence)
    with pytest.raises(maka_smoke.SmokeError, match="original run failure"):
        with maka_smoke.private_routing_host(
            [sys.executable, "unused"], component, {},
            maka_smoke.sanitized_environment(), retain_private_host=True,
        ):
            raise maka_smoke.SmokeError("original run failure")
    assert bases[-1].exists()
    assert "teardown also failed" in capsys.readouterr().err


def test_private_host_pins_foundational_skills_in_its_own_root(tmp_path: Path) -> None:
    root = tmp_path / "client/workspaces/default"
    digest = maka_smoke.seed_private_skill_preferences(root)
    state = root / ".maka/skills-state.json"
    assert digest == hashlib.sha256(state.read_bytes()).hexdigest()
    assert state.stat().st_mode & 0o777 == 0o600
    assert all(path.stat().st_mode & 0o777 == 0o700 for path in (
        root.parent.parent, root.parent, root, state.parent,
    ))
    assert json.loads(state.read_text()) == {
        "schemaVersion": 2,
        "skills": {
            name: {"enabled": True, "pinned": True}
            for name in ("using-superpowers", "writing-plans", "writing-skills")
        },
    }
    with pytest.raises(maka_smoke.SmokeError, match="skill preferences already exist"):
        maka_smoke.seed_private_skill_preferences(root)


def test_production_run_refuses_default_profile_before_process_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(
        maka_smoke.subprocess, "run",
        lambda command, **_kwargs: launched.append(command),
    )
    with pytest.raises(maka_smoke.SmokeError, match="precreated Session"):
        maka_smoke.run_command(
            ["/pinned/node", "/installed/cli.js"],
            {"id": "unsafe", "prompt": "test", "timeout_seconds": 120, "max_steps": 2},
            tmp_path, tmp_path / "runtime.sqlite", continued=False,
            environment={}, tier_file=tmp_path / "tier.json",
            host_identity={"pid": 4242, "hostEpoch": "epoch-1"},
        )
    assert launched == []


def test_routing_preflight_rejects_missing_installed_tool_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    component = tmp_path / "component"
    binary = ["/pinned/node", str(component / "runtime/package/dist/cli.js")]
    monkeypatch.setattr(
        maka_smoke.install_component, "canonical_layout",
        lambda: SimpleNamespace(home=tmp_path),
    )
    monkeypatch.setattr(
        maka_smoke, "attested_maka_cli",
        lambda _home, _environment: (
            binary, component,
            {"install_monotonic_ns": "123456789", "boot_session_uuid": "boot"},
        ),
    )
    incomplete = {"sessionId": "fanout-routing-0123456789abcdef0123456789abcdef",
                  "host": {"pid": 4242, "hostEpoch": "epoch-1"},
                  "boundary": {"kind": "managed"}}
    monkeypatch.setattr(
        maka_smoke.subprocess, "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps(incomplete), "",
        ),
    )
    with pytest.raises(maka_smoke.SmokeError, match="unbound evidence"):
        maka_smoke.prepare_routing_session(
            binary, tmp_path, incomplete["sessionId"],
            maka_smoke.default_runtime_db(tmp_path), {},
        )


def test_matrix_detects_executor_canary_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = fake_maka(tmp_path)
    runtime_db = tmp_path / "runtime.sqlite"
    tier_file = tmp_path / "tier" / "relay-last-tier.json"
    initialize_runtime_db(runtime_db)
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(tmp_path / "argv.log"))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(runtime_db))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_TIER_FILE", str(tier_file))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_LAUNCH_EXECUTOR", "1")
    allow_fake_maka_environment(monkeypatch)
    scenario = maka_smoke.load_cases(
        ROOT / "components" / "skills" / "maka-smoke.json"
    )["matrix"][-1]
    with pytest.raises(maka_smoke.SmokeError, match="launched an executor canary"):
        maka_smoke.run_matrix_case(
            str(binary), scenario, runtime_db, maka_smoke.sanitized_environment(), tier_file
        )


@pytest.mark.parametrize("reject_on_call", [1, 2])
def test_matrix_rechecks_host_before_and_after_a_settled_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reject_on_call: int,
) -> None:
    binary = fake_maka(tmp_path)
    runtime_db = tmp_path / "runtime.sqlite"
    tier_file = tmp_path / "tier" / "relay-last-tier.json"
    argv_log = tmp_path / "argv.log"
    initialize_runtime_db(runtime_db)
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(argv_log))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(runtime_db))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_TIER_FILE", str(tier_file))
    allow_fake_maka_environment(monkeypatch)
    calls = 0

    def attest(*_args: object) -> None:
        nonlocal calls
        calls += 1
        if calls == reject_on_call:
            raise maka_smoke.SmokeError("Maka routing Host changed between turns")

    monkeypatch.setattr(maka_smoke, "attest_routing_host", attest)
    scenario = maka_smoke.load_cases(
        ROOT / "components" / "skills" / "maka-smoke.json"
    )["matrix"][-1]
    with pytest.raises(maka_smoke.SmokeError, match="Host changed"):
        maka_smoke.run_matrix_case(
            str(binary), scenario, runtime_db, maka_smoke.sanitized_environment(), tier_file
        )
    assert calls == reject_on_call
    assert argv_log.exists() is (reject_on_call == 2)


def test_path_canaries_cannot_detect_absolute_launch_without_private_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = fake_maka(tmp_path)
    runtime_db = tmp_path / "runtime.sqlite"
    tier_file = tmp_path / "tier" / "relay-last-tier.json"
    initialize_runtime_db(runtime_db)
    absolute = tmp_path / "absolute-codex"
    absolute.write_text('#!/bin/sh\nprintf launched > "$0.log"\n')
    absolute.chmod(0o700)
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(tmp_path / "argv.log"))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(runtime_db))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_TIER_FILE", str(tier_file))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_ABSOLUTE_EXECUTOR", str(absolute))
    allow_fake_maka_environment(monkeypatch)
    scenario = maka_smoke.load_cases(
        ROOT / "components" / "skills" / "maka-smoke.json"
    )["matrix"][-1]
    result = maka_smoke.run_matrix_case(
        str(binary), scenario, runtime_db, maka_smoke.sanitized_environment(), tier_file
    )
    assert result["status"] == "pass"
    assert (tmp_path / "absolute-codex.log").read_text() == "launched"


@pytest.mark.parametrize("retain", [False, True])
def test_production_smoke_rejects_non_private_cli_before_any_model_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    retain: bool,
) -> None:
    repo, home, config = smoke_delivery_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    binary = fake_maka(tmp_path)
    log = tmp_path / "argv.log"
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(log))
    monkeypatch.setattr(
        maka_smoke, "attested_maka_cli", lambda *_args: (str(binary), tmp_path, {}),
    )
    result = maka_smoke.main([
        "--repo-root", str(repo), "--home", str(home),
        "--manifest", str(ROOT / "components/skills/maka-smoke.json"),
        *(["--retain-private-host"] if retain else []),
    ])
    assert result == 2
    assert "requires a private native Host" in capsys.readouterr().err
    assert not log.exists()


@pytest.mark.parametrize("retain", [False, True])
def test_production_smoke_rejects_missing_private_binding_before_any_model_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    retain: bool,
) -> None:
    repo, home, config = smoke_delivery_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    monkeypatch.setattr(
        maka_smoke, "attested_maka_cli",
        lambda *_args: (["/pinned/node", "/installed/cli.js"], tmp_path, {}),
    )
    monkeypatch.setattr(maka_smoke, "maka_version", lambda *_args: "test-version")
    def host_context(*_args: object, retain_private_host: bool = False) -> object:
        if retain_private_host != retain:
            raise AssertionError("main did not forward private Host retention")
        return maka_smoke.nullcontext(None)

    monkeypatch.setattr(maka_smoke, "private_routing_host", host_context)
    result = maka_smoke.main([
        "--repo-root", str(repo), "--home", str(home),
        "--manifest", str(ROOT / "components/skills/maka-smoke.json"),
        *(["--retain-private-host"] if retain else []),
    ])
    assert result == 2
    assert "requires a private native Host" in capsys.readouterr().err
    assert not (config.state_dir / "maka-smoke-receipt.json").exists()


def test_matrix_rejects_canary_log_removed_after_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = fake_maka(tmp_path)
    runtime_db = tmp_path / "runtime.sqlite"
    tier_file = tmp_path / "tier" / "relay-last-tier.json"
    initialize_runtime_db(runtime_db)
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(tmp_path / "argv.log"))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(runtime_db))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_TIER_FILE", str(tier_file))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_TAMPER_LOG", "1")
    allow_fake_maka_environment(monkeypatch)
    scenario = maka_smoke.load_cases(
        ROOT / "components" / "skills" / "maka-smoke.json"
    )["matrix"][-1]
    with pytest.raises(maka_smoke.SmokeError, match="executor.*launch|boundary"):
        maka_smoke.run_matrix_case(
            str(binary), scenario, runtime_db, maka_smoke.sanitized_environment(), tier_file
        )


def test_matrix_rejects_a_new_session_on_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = fake_maka(tmp_path)
    runtime_db = tmp_path / "runtime.sqlite"
    tier_file = tmp_path / "tier" / "relay-last-tier.json"
    initialize_runtime_db(runtime_db)
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(tmp_path / "argv.log"))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(runtime_db))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_TIER_FILE", str(tier_file))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_SPLIT_SESSION", "1")
    allow_fake_maka_environment(monkeypatch)
    scenario = maka_smoke.load_cases(
        ROOT / "components" / "skills" / "maka-smoke.json"
    )["matrix"][-1]
    with pytest.raises(maka_smoke.SmokeError, match="session"):
        maka_smoke.run_matrix_case(
            str(binary), scenario, runtime_db, maka_smoke.sanitized_environment(), tier_file
        )


def test_matrix_rejects_an_unbound_provider_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = fake_maka(tmp_path)
    runtime_db = tmp_path / "runtime.sqlite"
    tier_file = tmp_path / "tier" / "relay-last-tier.json"
    initialize_runtime_db(runtime_db)
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(tmp_path / "argv.log"))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(runtime_db))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_TIER_FILE", str(tier_file))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_EXTRA_ATTEMPT", "1")
    allow_fake_maka_environment(monkeypatch)
    scenario = maka_smoke.load_cases(
        ROOT / "components" / "skills" / "maka-smoke.json"
    )["matrix"][-1]
    with pytest.raises(maka_smoke.SmokeError, match="provider attempt count"):
        maka_smoke.run_matrix_case(
            str(binary), scenario, runtime_db, maka_smoke.sanitized_environment(), tier_file
        )


def test_matrix_rejects_a_non_skill_tool_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = fake_maka(tmp_path)
    runtime_db = tmp_path / "runtime.sqlite"
    tier_file = tmp_path / "tier" / "relay-last-tier.json"
    initialize_runtime_db(runtime_db)
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(tmp_path / "argv.log"))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(runtime_db))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_TIER_FILE", str(tier_file))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_TOOL_START", "Bash")
    allow_fake_maka_environment(monkeypatch)
    scenario = maka_smoke.load_cases(
        ROOT / "components" / "skills" / "maka-smoke.json"
    )["matrix"][-1]
    with pytest.raises(maka_smoke.SmokeError, match="non-skill tool"):
        maka_smoke.run_matrix_case(
            str(binary), scenario, runtime_db, maka_smoke.sanitized_environment(), tier_file
        )


def initialize_runtime_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE session_metadata (
                session_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                model TEXT NOT NULL DEFAULT 'gpt-6.1-sol',
                llm_connection_slug TEXT NOT NULL DEFAULT 'keychain-openai'
            );
            CREATE TABLE message_admissions (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                skill_invocation_json TEXT NOT NULL
            );
            CREATE TABLE core_root_turn_admissions (
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                admitted_at INTEGER NOT NULL,
                record_json TEXT NOT NULL
            );
            CREATE TABLE core_agent_run_events (
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                record_json TEXT NOT NULL
            );
            CREATE TABLE usage_model_call_attempts (
                attempt_id TEXT PRIMARY KEY,
                completed_at INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                call_kind TEXT NOT NULL
            );
            """
        )


def fake_maka(tmp_path: Path) -> Path:
    binary = tmp_path / "maka"
    source_hash = hashlib.sha256(
        (ROOT / "components/maka/interactive/maka_openai_relay.py").read_bytes()
    ).hexdigest()
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import datetime, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, time, uuid\n"
        "if sys.argv[1:2] == ['--version']:\n"
        "    print('Maka test')\n"
        "    raise SystemExit(0)\n"
        "assert '--yolo' not in sys.argv\n"
        "assert sys.argv[sys.argv.index('--thinking') + 1] == 'xhigh'\n"
        "assert 'OPENAI_API_KEY' not in os.environ\n"
        "assert 'AWS_SECRET_ACCESS_KEY' not in os.environ\n"
        "assert 'DATABASE_URL' not in os.environ\n"
        "assert 'HTTPS_PROXY' not in os.environ\n"
        "with open(os.environ['KHENRIX_MAKA_SMOKE_ARGV_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "prompt = sys.argv[2]\n"
        "markers = re.findall(r'(?m)^(?:Fanout routing receipt nonce: (maka:fanout-routing-[0-9a-f]{32}-[0-9a-f]{32})|Receipt nonce: (maka:probe-[0-9a-f]{32}-[0-9a-f]{32}))$', prompt)\n"
        "assert len(markers) == 1\n"
        "marker = markers[0][0] or markers[0][1]\n"
        f"source_hash = '{source_hash}'\n"
        "cwd = sys.argv[sys.argv.index('--cwd') + 1]\n"
        "if 'May Maka execute that task?' in prompt:\n"
        "    absolute = re.search(r'Absolute validation canary: (\\S+)', prompt)\n"
        "    assert absolute and os.path.isabs(absolute.group(1))\n"
        "    assert os.path.isfile(absolute.group(1))\n"
        "    assert os.path.commonpath((cwd, absolute.group(1))) != cwd\n"
        "if 'KHENRIX_MAKA_SMOKE_CANARY_LOG' in os.environ:\n"
        "    assert not os.path.commonpath((cwd, os.environ['KHENRIX_MAKA_SMOKE_CANARY_LOG'])) == cwd\n"
        "    canary_dir = os.environ['PATH'].split(os.pathsep)[0]\n"
        "    for name in ('claude', 'codex', 'agy'):\n"
        "        assert shutil.which(name) == os.path.join(canary_dir, name)\n"
        "db = sqlite3.connect(os.environ['KHENRIX_FAKE_MAKA_DB'])\n"
        "rows = db.execute('SELECT session_id,payload_json FROM session_metadata').fetchall()\n"
        "existing = [sid for sid,payload in rows if json.loads(payload)['cwd'] == cwd]\n"
        "if '--resume' in sys.argv and os.environ.get('KHENRIX_FAKE_MAKA_SPLIT_SESSION') == '1' and 'May Maka execute that task?' in prompt:\n"
        "    session_id = str(uuid.uuid4())\n"
        "    now = int(time.time() * 1000)\n"
        "    db.execute('INSERT INTO session_metadata(session_id,payload_json,created_at) VALUES (?,?,?)', (session_id, json.dumps({'cwd': cwd, 'createdAt': now}), now))\n"
        "elif '--resume' in sys.argv:\n"
        "    session_id = sys.argv[sys.argv.index('--resume') + 1]\n"
        "    assert session_id in existing\n"
        "elif '--continue' in sys.argv:\n"
        "    assert len(existing) == 1\n"
        "    session_id = existing[0]\n"
        "else:\n"
        "    assert not existing\n"
        "    session_id = str(uuid.uuid4())\n"
        "    now = int(time.time() * 1000)\n"
        "    db.execute('INSERT INTO session_metadata(session_id,payload_json,created_at) VALUES (?,?,?)', (session_id, json.dumps({'cwd': cwd, 'createdAt': now}), now))\n"
        "turn_id = str(uuid.uuid4())\n"
        "if os.environ.get('KHENRIX_FAKE_MAKA_SKIP_HOST_EVENTS') != '1':\n"
        "    event_id = str(uuid.uuid4())\n"
        "    record = {'id':event_id,'sessionId':session_id,'turnId':turn_id,'type':'turn_started'}\n"
        "    sequence = db.execute('SELECT count(*)+1 FROM core_agent_run_events WHERE session_id=?',(session_id,)).fetchone()[0]\n"
        "    db.execute('INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)',(session_id,sequence,event_id,'turn_started',json.dumps(record)))\n"
        "    event_id = str(uuid.uuid4())\n"
        "    record = {'id':event_id,'sessionId':session_id,'turnId':turn_id,'type':'model_stream_started','data':{'activeTools':['Skill','SkillSearch']}}\n"
        "    sequence = db.execute('SELECT count(*)+1 FROM core_agent_run_events WHERE session_id=?',(session_id,)).fetchone()[0]\n"
        "    db.execute('INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)',(session_id,sequence,event_id,'model_stream_started',json.dumps(record)))\n"
        "if '/skill:' in prompt:\n"
        "    skill = prompt.split('/skill:', 1)[1].split()[0]\n"
        "    receipt = {'invocation':'explicit','request':skill,'success':True,'ref':'user:agents:'+skill,'id':skill,'name':skill,'scope':'user','source':'agents','truncated':False}\n"
        "    invocation = {'loaded':[{'id':skill,'name':skill}],'failed':[],'receipts':[receipt]}\n"
        "    admission = {'skillInvocation':invocation}\n"
        "    db.execute('INSERT INTO core_root_turn_admissions VALUES (?,?,?,?)', (session_id,turn_id,int(time.time()*1000),json.dumps(admission)))\n"
        "    lower = prompt.lower()\n"
        "    if 'explore a small todo list' in lower: nested = 'brainstorming'\n"
        "    elif 'implementation has no accepted plan' in lower: nested = 'writing-plans'\n"
        "    elif 'an accepted implementation plan' in lower: nested = 'subagent-driven-development'\n"
        "    else: nested = None\n"
        "    if nested is not None:\n"
        "        data = {'invocation':'model_tool','success':True,'skillRef':'user:agents:'+nested,'skillId':nested,'skillName':nested,'skillScope':'user','skillSource':'agents','truncated':False,'shadowCandidateCount':0,'shadowHitAt1':False,'shadowHitAt5':False,'shadowHitAt20':False}\n"
        "        event_id = str(uuid.uuid4())\n"
        "        record = {'id':event_id,'sessionId':session_id,'turnId':turn_id,'type':'skill_loaded','data':data}\n"
        "        sequence = db.execute('SELECT count(*)+1 FROM core_agent_run_events WHERE session_id=?',(session_id,)).fetchone()[0]\n"
        "        db.execute('INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)', (session_id,sequence,event_id,'skill_loaded',json.dumps(record)))\n"
        "    if skill == 'using-superpowers' and os.environ.get('KHENRIX_FAKE_MAKA_BOOTSTRAP_EXTRA_SKILL'):\n"
        "        nested = os.environ['KHENRIX_FAKE_MAKA_BOOTSTRAP_EXTRA_SKILL']\n"
        "        event_id = str(uuid.uuid4())\n"
        "        data = {'invocation':'model_tool','success':True,'skillRef':'user:agents:'+nested,'skillId':nested,'skillName':nested,'skillScope':'user','skillSource':'agents','truncated':False,'shadowCandidateCount':0,'shadowHitAt1':False,'shadowHitAt5':False,'shadowHitAt20':False}\n"
        "        record = {'id':event_id,'sessionId':session_id,'turnId':turn_id,'type':'skill_loaded','data':data}\n"
        "        sequence = db.execute('SELECT count(*)+1 FROM core_agent_run_events WHERE session_id=?',(session_id,)).fetchone()[0]\n"
        "        db.execute('INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)', (session_id,sequence,event_id,'skill_loaded',json.dumps(record)))\n"
        "else:\n"
        "    db.execute('INSERT INTO core_root_turn_admissions VALUES (?,?,?,?)', (session_id,turn_id,int(time.time()*1000),json.dumps({'kind':'natural-continuation'})))\n"
        "    lower = prompt.lower()\n"
        "    if 'explore the requirements' in lower or 'react todo list' in lower: skill = 'brainstorming'\n"
        "    elif 'assume that task runner design' in lower: skill = 'writing-plans'\n"
        "    elif 'an accepted implementation plan' in lower: skill = 'subagent-driven-development'\n"
        "    elif 'a unit test for quote_total' in lower: skill = 'systematic-debugging'\n"
        "    elif 'now treat the quote_total fix' in lower: skill = 'test-driven-development'\n"
        "    elif 'supported tools and skills' in lower: skill = 'using-superpowers'\n"
        "    elif 'humanize' in lower: skill = 'khenrix-writing'\n"
        "    elif 'former ' in lower or 'show concise help' in lower: skill = 'khenrix-quality'\n"
        "    else: skill = None\n"
        "    if skill is not None:\n"
        "        event_id = str(uuid.uuid4())\n"
        "        data = {'invocation':'model_tool','success':True,'skillRef':'user:agents:'+skill,'skillId':skill,'skillName':skill,'skillScope':'user','skillSource':'agents','truncated':False,'shadowCandidateCount':0,'shadowHitAt1':False,'shadowHitAt5':False,'shadowHitAt20':False}\n"
        "        record = {'id':event_id,'sessionId':session_id,'turnId':turn_id,'type':'skill_loaded','data':data}\n"
        "        sequence = db.execute('SELECT count(*)+1 FROM core_agent_run_events WHERE session_id=?',(session_id,)).fetchone()[0]\n"
        "        db.execute('INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)',(session_id,sequence,event_id,'skill_loaded',json.dumps(record)))\n"
        "if os.environ.get('KHENRIX_FAKE_MAKA_SKIP_HOST_EVENTS') != '1':\n"
        "    event_id = str(uuid.uuid4())\n"
        "    record = {'id':event_id,'sessionId':session_id,'turnId':turn_id,'type':'model_stream_completed'}\n"
        "    sequence = db.execute('SELECT count(*)+1 FROM core_agent_run_events WHERE session_id=?',(session_id,)).fetchone()[0]\n"
        "    db.execute('INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)',(session_id,sequence,event_id,'model_stream_completed',json.dumps(record)))\n"
        "db.execute('INSERT INTO usage_model_call_attempts VALUES (?,?,?,?)', (uuid.uuid4().hex,int(time.time()*1000),session_id,'main'))\n"
        "if os.environ.get('KHENRIX_FAKE_MAKA_TOOL_START') and 'May Maka execute that task?' in prompt:\n"
        "    event_id = str(uuid.uuid4())\n"
        "    record = {'id':event_id,'sessionId':session_id,'turnId':turn_id,'type':'tool_started','data':{'toolName':os.environ['KHENRIX_FAKE_MAKA_TOOL_START']}}\n"
        "    sequence = db.execute('SELECT count(*)+1 FROM core_agent_run_events WHERE session_id=?',(session_id,)).fetchone()[0]\n"
        "    db.execute('INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)',(session_id,sequence,event_id,'tool_started',json.dumps(record)))\n"
        "if os.environ.get('KHENRIX_FAKE_MAKA_EXTRA_ATTEMPT') == '1' and 'May Maka execute that task?' in prompt:\n"
        "    db.execute('INSERT INTO usage_model_call_attempts VALUES (?,?,?,?)', (uuid.uuid4().hex,int(time.time()*1000),session_id,'main'))\n"
        "db.commit(); db.close()\n"
        "tier_file = os.environ['KHENRIX_MAKA_SMOKE_TIER_FILE']\n"
        "receipt_dir = os.path.join(os.path.dirname(tier_file), 'relay-routing-receipts')\n"
        "os.makedirs(receipt_dir, mode=0o700, exist_ok=True)\n"
        "marker_hash = hashlib.sha256(marker.encode()).hexdigest()\n"
        "instance = uuid.uuid4().hex\n"
        "route = {'schema':'khenrix-maka-routing-request-v1','time_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'requested_model':'gpt-6.1-sol','observed_model':'gpt-6.1-sol','requested_service_tier':'default','observed_service_tier':'default','http_status':200,'request_marker_sha256':marker_hash,'request_instance_id':instance,'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),'routing_session_id':session_id,'prompt_cache_key':'maka:'+session_id,'binding':'nonce','relay_source_sha256':source_hash,'outbound_effort':'xhigh','outbound_tool_names':['Skill','SkillSearch'],'tool_count':2}\n"
        "if os.environ.get('KHENRIX_FAKE_MAKA_SKIP_ROUTE_RECEIPT') != '1':\n"
        "    path = os.path.join(receipt_dir, marker_hash + '-' + instance + '.json')\n"
        "    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n"
        "    with os.fdopen(fd, 'w') as output: json.dump(route, output)\n"
        "if os.environ.get('KHENRIX_FAKE_MAKA_LAUNCH_EXECUTOR') == '1' and 'May Maka execute that task?' in prompt:\n"
        "    subprocess.run(['codex', 'exec', 'canary'], check=False)\n"
        "if os.environ.get('KHENRIX_FAKE_MAKA_ABSOLUTE_EXECUTOR') and 'May Maka execute that task?' in prompt:\n"
        "    subprocess.run([os.environ['KHENRIX_FAKE_MAKA_ABSOLUTE_EXECUTOR']], check=False)\n"
        "if os.environ.get('KHENRIX_FAKE_MAKA_TAMPER_LOG') == '1' and 'May Maka execute that task?' in prompt:\n"
        "    subprocess.run(['codex', 'exec', 'canary'], check=False)\n"
        "    os.unlink(os.environ['KHENRIX_MAKA_SMOKE_CANARY_LOG'])\n"
        "if 'three things on my plate' in prompt: print('Open the pull request and read the description.')\n"
        "elif 'Stop ADHD mode' in prompt: print('ADHD mode is now off.')\n"
        "elif 'May Maka execute that task?' in prompt: print('Maka cannot be a repo-write executor for this task.')\n"
        "else: print('bounded model output')\n"
    )
    binary.chmod(0o755)
    return binary


def allow_fake_maka_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass only fake-runner controls after the real production scrubber runs."""
    sanitize = maka_smoke.sanitized_environment
    names = (
        "KHENRIX_MAKA_SMOKE_ARGV_LOG", "KHENRIX_FAKE_MAKA_DB",
        "KHENRIX_MAKA_SMOKE_TIER_FILE", "KHENRIX_FAKE_MAKA_LAUNCH_EXECUTOR",
        "KHENRIX_FAKE_MAKA_SPLIT_SESSION", "KHENRIX_FAKE_MAKA_EXTRA_ATTEMPT",
        "KHENRIX_FAKE_MAKA_SKIP_ROUTE_RECEIPT",
        "KHENRIX_FAKE_MAKA_ABSOLUTE_EXECUTOR", "KHENRIX_FAKE_MAKA_TAMPER_LOG",
        "KHENRIX_FAKE_MAKA_TOOL_START",
        "KHENRIX_FAKE_MAKA_SKIP_HOST_EVENTS",
        "KHENRIX_FAKE_MAKA_BOOTSTRAP_EXTRA_SKILL",
    )
    controls = {name: os.environ[name] for name in names if name in os.environ}
    monkeypatch.setattr(
        maka_smoke, "sanitized_environment", lambda: {**sanitize(), **controls}
    )

    def prepare(
        _binary: str | list[str], cwd: Path, session_id: str, runtime_db: Path,
        _environment: dict[str, str],
        _private_host: object = None,
    ) -> dict[str, object]:
        with sqlite3.connect(runtime_db) as connection:
            connection.execute(
                "INSERT INTO session_metadata(session_id,payload_json,created_at) VALUES (?,?,?)",
                (session_id, json.dumps({
                    "cwd": str(cwd), "toolProfile": "khenrix-skill-routing-v1",
                    "permissionMode": "ask", "thinkingLevel": "xhigh",
                }), int(__import__("time").time() * 1000)),
            )
        return {"sessionId": session_id, "host": {"pid": 1, "hostEpoch": "test"},
                "boundary": {"kind": "managed"},
                "toolProjection": {"profile": "khenrix-skill-routing-v1",
                                   "tools": ["Skill", "SkillSearch"]}}

    monkeypatch.setattr(maka_smoke, "prepare_routing_session", prepare)
    monkeypatch.setattr(maka_smoke, "attest_routing_host", lambda *_args: None)
    monkeypatch.setattr(
        maka_smoke, "attest_existing_host",
        lambda *_args: {"pid": 1, "hostEpoch": "test"},
    )


def test_bootstrap_receipt_nonce_does_not_suggest_a_skill_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = fake_maka(tmp_path)
    runtime_db = tmp_path / "runtime.sqlite"
    tier_file = tmp_path / "tier/relay-last-tier.json"
    argv_log = tmp_path / "argv.log"
    initialize_runtime_db(runtime_db)
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(argv_log))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(runtime_db))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_TIER_FILE", str(tier_file))
    allow_fake_maka_environment(monkeypatch)
    case = next(
        item for item in maka_smoke.load_cases(
            ROOT / "components/skills/maka-smoke.json"
        )["cases"] if item["id"] == "explicit-brainstorming"
    )

    result = maka_smoke.run_case(
        str(binary), case, runtime_db, maka_smoke.sanitized_environment(),
        tier_file, {"pid": 1, "hostEpoch": "test"},
    )

    invocations = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert len(invocations) == 2
    bootstrap_prompt = invocations[0][1]
    assert bootstrap_prompt.startswith("/skill:using-superpowers Reply ready.\n")
    marker = bootstrap_prompt.splitlines()[-1].split(": ", 1)[1]
    route = result["session_bootstrap"]["model_route"]
    assert route["routing_session_id"] == result["session_id"]
    assert route["request_marker_sha256"] == hashlib.sha256(marker.encode()).hexdigest()
    assert marker.endswith(result["session_id"].removeprefix("fanout-routing-"))
    assert re.search(r"(?i)\b(?:fanout|routing|execute)\b", bootstrap_prompt) is None


def test_smoke_runs_bounded_cases_and_writes_hash_bound_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, home, config = smoke_delivery_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    binary = fake_maka(tmp_path)
    log = tmp_path / "argv.log"
    runtime_db = tmp_path / "runtime.sqlite"
    tier_file = tmp_path / "tier" / "relay-last-tier.json"
    initialize_runtime_db(runtime_db)
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(log))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(runtime_db))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_TIER_FILE", str(tier_file))
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key-must-not-reach-maka")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret-must-not-reach-maka")
    monkeypatch.setenv("DATABASE_URL", "fake-secret-must-not-reach-maka")
    monkeypatch.setenv("HTTPS_PROXY", "fake-secret-must-not-reach-maka")
    allow_fake_maka_environment(monkeypatch)
    manifest = ROOT / "components" / "skills" / "maka-smoke.json"

    result = maka_smoke.main(
        [
            "--repo-root",
            str(repo),
            "--home",
            str(home),
            "--manifest",
            str(manifest),
            "--runtime-db",
            str(runtime_db),
            "--tier-file",
            str(tier_file),
        ],
        _test_cli_command=[str(binary)],
    )

    assert result == 0
    invocations = [json.loads(line) for line in log.read_text().splitlines()]
    declared = json.loads(manifest.read_text())
    bootstrap_turns = sum(
        maka_smoke.needs_session_bootstrap(case) for case in declared["cases"]
    )
    assert len(invocations) == (
        len(declared["cases"])
        + bootstrap_turns
        + sum(1 + len(flow["steps"]) for flow in declared["flows"])
        + sum(len(scenario["steps"]) for scenario in declared["matrix"])
    )
    assert all("--yolo" not in line for line in invocations)
    assert all("--resume" in line for line in invocations)
    assert all("--cwd" in line and "--max-steps" in line and "--timeout" in line for line in invocations)
    assert all(line[line.index("--thinking") + 1] == "xhigh" for line in invocations)
    markers = []
    for line in invocations:
        match = re.search(
            r"(?m)^(?:Fanout routing receipt nonce: (maka:fanout-routing-[0-9a-f]{32}-[0-9a-f]{32})|Receipt nonce: (maka:probe-[0-9a-f]{32}-[0-9a-f]{32}))$",
            line[1],
        )
        assert match is not None
        markers.append(match.group(1) or match.group(2))
    assert len(markers) == len(set(markers))
    receipt = json.loads((config.state_dir / "maka-smoke-receipt.json").read_text())
    assert receipt["schema_version"] == 2
    assert receipt["model_route"] == {
        "auth_route": "api-key-relay",
        "requested_model": "gpt-6.1-sol",
        "observed_model": "gpt-6.1-sol",
        "requested_effort": "xhigh",
        "outbound_effort": "xhigh",
        "observed_effort": None,
        "observed_service_tier": "default",
    }
    assert receipt["input_hash"].startswith("sha256:")
    assert receipt["runner_hash"].startswith("sha256:")
    assert receipt["bounded"] == {
        "root_session_bootstrap": True,
        "temporary_cwd_per_case": True,
        "yolo": False,
        "provider_credentials_removed": True,
        "host_tool_event_audit": True,
        "skill_only_tool_profile": True,
        "existing_host_only": True,
    }
    assert all(case["validation_only"] is True and case["session_id"] for case in receipt["cases"])
    assert receipt["flows"][0]["validation_only"] is True
    assert {(case["skill"], case["route"]) for case in receipt["cases"]} == {
        ("khenrix-quality", "explicit"),
        ("khenrix-quality", "natural"),
        ("khenrix-writing", "explicit"),
        ("khenrix-writing", "natural"),
        ("using-superpowers", "explicit"),
        ("using-superpowers", "natural"),
        ("brainstorming", "explicit"),
        ("brainstorming", "natural"),
    }
    assert all(case["event_evidence"]["evidence_hash"].startswith("sha256:") for case in receipt["cases"])
    bootstrapped = [
        case
        for case in receipt["cases"]
        if case["skill"] != "using-superpowers"
    ]
    assert len(bootstrapped) == bootstrap_turns
    assert all(
        case["session_bootstrap"]["event_evidence"]["skill_id"]
        == "using-superpowers"
        for case in bootstrapped
    )
    assert all(
        case["session_bootstrap"] is None
        for case in receipt["cases"]
        if case not in bootstrapped
    )
    assert {
        case["event_evidence"]["kind"]
        for case in receipt["cases"]
        if case["route"] == "explicit"
    } == {"core_root_turn_admission"}
    natural_prompts = "\n".join(
        case["prompt"] for case in declared["cases"] if case["route"] == "natural"
    )
    assert {
        "i-have-adhd",
        "no-ai-slop",
        "ponytail",
        "ponytail-review",
        "ponytail-audit",
        "ponytail-debt",
        "ponytail-gain",
        "ponytail-help",
        "humanizer",
    } <= set(natural_prompts.replace(",", "").replace(".", "").split())
    flow = receipt["flows"][0]
    assert [step["continued"] for step in flow["steps"]] == [True, True, True]
    assert flow["activation_evidence"]["invocation"] == "explicit"
    assert flow["steps"][1]["behavior_evidence"]["opening_actionable"] is True
    assert flow["steps"][1]["behavior_evidence"]["action_line"] in {1, 2}
    assert flow["steps"][2]["behavior_evidence"] == {
        "check": "normal_mode_ack",
        "line_count": 1,
        "state_acknowledged": True,
    }
    assert len(receipt["matrix"]) == 4
    assert all("executor_canary_launches" not in scenario for scenario in receipt["matrix"])
    assert all(
        scenario["host_tool_boundary"] == {
            "tool_profile": "khenrix-skill-routing-v1",
            "profile_tools": ["Skill", "SkillSearch"],
            "audited_turns": len(scenario["steps"]),
            "non_skill_tool_starts": 0,
        }
        for scenario in receipt["matrix"]
    )
    all_routes = [
        case["model_route"] for case in receipt["cases"]
    ] + [
        case["session_bootstrap"]["model_route"]
        for case in receipt["cases"] if case["session_bootstrap"] is not None
    ] + [
        flow["session_bootstrap"]["model_route"] for flow in receipt["flows"]
    ] + [
        step["model_route"] for flow in receipt["flows"] for step in flow["steps"]
    ] + [
        step["model_route"] for scenario in receipt["matrix"] for step in scenario["steps"]
    ]
    assert all(
        route["host_tool_evidence"]["non_skill_tool_starts"] == 0
        and route["host_tool_evidence"]["model_stream_completed_count"] >= 1
        for route in all_routes
    )
    assert all(
        step["expected_skills"] == [
            {"id": evidence["skill_id"], "invocation": evidence["invocation"]}
            for evidence in step["skill_evidence"]
        ]
        for scenario in receipt["matrix"]
        for step in scenario["steps"]
    )
    lifecycle = next(scenario for scenario in receipt["matrix"] if scenario["id"] == "fanout-plan-lifecycle")
    nested = {step["id"]: step["skill_evidence"] for step in lifecycle["steps"]}
    assert [
        (item["skill_id"], item["invocation"])
        for item in nested["write-plan"]
    ] == [("llm-fanout-plan", "explicit"), ("writing-plans", "model_tool")]
    assert [
        (item["skill_id"], item["invocation"])
        for item in nested["delegate-plan"]
    ] == [("using-superpowers", "explicit"), ("subagent-driven-development", "model_tool")]
    assert [
        (item["skill_id"], item["invocation"])
        for item in nested["execute-route"]
    ] == [("llm-fanout-execute", "explicit")]
    assert receipt["matrix"][-1]["steps"][-1]["behavior_evidence"]["refused"] is True


@pytest.mark.parametrize("outcome,write_failure", [
    ("success", None), ("failure", None), ("timeout", None),
    ("timeout", ".stdout"), ("startup", ".stderr"),
    ("success", ".json"), ("failure", ".stdout"),
])
def test_private_turn_output_preserves_native_bytes_before_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], outcome: str, write_failure: str | None,
) -> None:
    binary = fake_maka(tmp_path)
    binary.write_text(
        binary.read_text()
        + "sys.stdout.flush()\n"
        + "sys.stdout.buffer.write(b'private-turn-output\\r\\n')\n"
        + "sys.stdout.buffer.flush()\n"
        + "sys.stderr.buffer.write(b'private-turn-stderr\\r\\n')\n"
        + "sys.stderr.buffer.flush()\n"
        + ("sys.exit(7)\n" if outcome == "failure" else "")
        + ("time.sleep(2)\n" if outcome == "timeout" else "")
    )
    base = tmp_path / "private-host"
    base.mkdir(mode=0o700)
    root = base / "client/workspaces/default"
    maka_smoke.seed_private_skill_preferences(root)
    runtime_db = root / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    binding = maka_smoke.PrivateHostBinding(
        root_path=root, client_data_root=base / "client", root_id="b" * 64,
        spec_path=base / "host-spec.json",
        attestation={"pid": 4242, "hostEpoch": "epoch-1", "rootId": "b" * 64,
                     "startedAtMs": 100_000},
    )
    tier_file = tmp_path / "tier/relay-last-tier.json"
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(tmp_path / "argv.log"))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(runtime_db))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_TIER_FILE", str(tier_file))
    allow_fake_maka_environment(monkeypatch)
    original_failures = []
    if outcome in {"timeout", "startup"}:
        run = maka_smoke._bounded_command

        def timed_run(command: list[str], **kwargs: object) -> object:
            if outcome == "startup":
                error = OSError("controlled CLI start failure")
                original_failures.append(error)
                raise error
            try:
                return run(command, **{**kwargs, "timeout": 0.5})
            except subprocess.TimeoutExpired as error:
                original_failures.append(error)
                raise

        monkeypatch.setattr(maka_smoke, "_bounded_command", timed_run)
    session_id = "fanout-routing-0123456789abcdef0123456789abcdef"
    case = {"id": "private-output", "prompt": "Validate routing only.",
            "timeout_seconds": 120, "max_steps": 2}
    maka_smoke.prepare_routing_session(
        str(binary), tmp_path, session_id, runtime_db, maka_smoke.sanitized_environment(),
    )
    arguments = {
        "continued": False, "resume_session_id": session_id,
        "environment": maka_smoke.sanitized_environment(), "tier_file": tier_file,
        "private_host": binding,
    }
    if write_failure is not None:
        private_file = maka_smoke._private_file

        def fail_turn_evidence(path: Path, data: bytes, mode: int = 0o600) -> None:
            if path.name.startswith("turn-") and path.suffix == write_failure:
                raise OSError("SENSITIVE_RETENTION_ERROR /private/do/not/leak")
            private_file(path, data, mode)

        monkeypatch.setattr(maka_smoke, "_private_file", fail_turn_evidence)
        expected = (
            "could not complete" if outcome in {"timeout", "startup"}
            else "output evidence could not be retained"
        )
        with pytest.raises(maka_smoke.SmokeError, match=expected) as caught:
            maka_smoke.run_command(str(binary), case, tmp_path, runtime_db, **arguments)
        if outcome in {"timeout", "startup"}:
            assert caught.value.__cause__ is original_failures[-1]
        assert base.is_dir() and runtime_db.is_file()
        output = capsys.readouterr()
        assert "SENSITIVE_RETENTION_ERROR" not in output.out + output.err
        assert "/private/do/not/leak" not in output.out + output.err
        if outcome in {"timeout", "startup"}:
            assert "turn output evidence could not be retained" in output.err

        repo, home, config = smoke_delivery_fixture(tmp_path)
        skillctl.apply(config, expect=None, as_json=False)
        monkeypatch.setattr(maka_smoke, "maka_version", lambda *_args: "test-version")

        def run_private_case(*_args: object) -> object:
            return maka_smoke.run_command(
                str(binary), case, tmp_path, runtime_db, **arguments,
            )

        monkeypatch.setattr(maka_smoke, "run_case", run_private_case)
        capsys.readouterr()
        result = maka_smoke.main([
            "--repo-root", str(repo), "--home", str(home),
            "--runtime-db", str(runtime_db), "--tier-file", str(tier_file),
            "--retain-private-host",
        ], _test_cli_command=[str(binary)])
        assert result == 2
        assert not (config.state_dir / "maka-smoke-receipt.json").exists()
        assert base.is_dir() and runtime_db.is_file()
        assert base.stat().st_mode & 0o777 == 0o700
        for path in base.glob("turn-*"):
            assert path.stat().st_uid == os.getuid()
            assert path.stat().st_mode & 0o777 == 0o600
        output = capsys.readouterr()
        assert expected in output.err
        assert "private-turn" not in output.out
        assert "SENSITIVE_RETENTION_ERROR" not in output.out + output.err
        assert "/private/do/not/leak" not in output.out + output.err
        return
    if outcome == "success":
        completed, _route = maka_smoke.run_command(
            str(binary), case, tmp_path, runtime_db, **arguments,
        )
        assert completed.stdout == "bounded model output\nprivate-turn-output\r\n"
    else:
        with pytest.raises(maka_smoke.SmokeError, match=(
            "failed with 7" if outcome == "failure" else "could not complete"
        )):
            maka_smoke.run_command(str(binary), case, tmp_path, runtime_db, **arguments)
    identities = list(base.glob("turn-*.json"))
    assert len(identities) == 1
    identity = json.loads(identities[0].read_bytes())
    invocation = json.loads((tmp_path / "argv.log").read_text())
    marker = invocation[1].splitlines()[-1].split(": ", 1)[1]
    assert identity["case_id"] == "private-output"
    assert identity["session_id"] == session_id
    assert identity["prompt_sha256"] == hashlib.sha256(invocation[1].encode()).hexdigest()
    assert identity["request_marker_sha256"] == hashlib.sha256(marker.encode()).hexdigest()
    assert identity["timed_out"] is (outcome == "timeout")
    assert identity["returncode"] == {"success": 0, "failure": 7, "timeout": None}[outcome]
    assert identities[0].with_suffix(".stdout").read_bytes() == (
        b"bounded model output\nprivate-turn-output\r\n"
    )
    assert identities[0].with_suffix(".stderr").read_bytes() == b"private-turn-stderr\r\n"
    for path in base.glob("turn-*"):
        assert path.stat().st_uid == os.getuid()
        assert path.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr()
    assert "private-turn" not in output.out


def test_smoke_attributes_extra_load_to_bootstrap_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, home, config = smoke_delivery_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    binary = fake_maka(tmp_path)
    runtime_db = tmp_path / "runtime.sqlite"
    tier_file = tmp_path / "tier" / "relay-last-tier.json"
    initialize_runtime_db(runtime_db)
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(tmp_path / "argv.log"))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(runtime_db))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_TIER_FILE", str(tier_file))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_BOOTSTRAP_EXTRA_SKILL", "llm-fanout-execute")
    allow_fake_maka_environment(monkeypatch)

    result = maka_smoke.main([
        "--repo-root", str(repo), "--home", str(home),
        "--runtime-db", str(runtime_db), "--tier-file", str(tier_file),
    ], _test_cli_command=[str(binary)])

    assert result == 2
    assert "bootstrap turn loaded an unexpected skill" in capsys.readouterr().err
    assert not (config.state_dir / "maka-smoke-receipt.json").exists()


def test_diagnostic_matrix_scenario_does_not_write_certification_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, home, config = smoke_delivery_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    binary = fake_maka(tmp_path)
    runtime_db = tmp_path / "runtime.sqlite"
    tier_file = tmp_path / "tier/relay-last-tier.json"
    log = tmp_path / "argv.log"
    initialize_runtime_db(runtime_db)
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(log))
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(runtime_db))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_TIER_FILE", str(tier_file))
    allow_fake_maka_environment(monkeypatch)

    result = maka_smoke.main([
        "--repo-root", str(repo), "--home", str(home),
        "--runtime-db", str(runtime_db), "--tier-file", str(tier_file),
        "--diagnostic-scenario", "ordered-bootstrap-and-design",
    ], _test_cli_command=[str(binary)])

    assert result == 0
    assert "DIAGNOSTIC PASS" in capsys.readouterr().out
    assert len(log.read_text().splitlines()) == 1
    assert not (config.state_dir / "maka-smoke-receipt.json").exists()


def test_partial_custom_manifest_cannot_issue_canonical_certification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, home, config = smoke_delivery_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    manifest = maka_smoke.load_cases(ROOT / "components/skills/maka-smoke.json")
    removed = manifest["cases"].pop()
    manifest["required_routes"][removed["skill"]].remove(removed["route"])
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(manifest))
    assert maka_smoke.load_cases(path)["cases"]
    monkeypatch.setattr(maka_smoke, "maka_version", lambda *_args: "fixture-version")
    assert maka_smoke.main(["--repo-root", str(repo), "--home", str(home),
                            "--manifest", str(path), "--dry-run"],
                           _test_cli_command=[str(fake_maka(tmp_path))]) == 2
    assert not (config.state_dir / "maka-smoke-receipt.json").exists()


@pytest.mark.parametrize("fault", [None, "bootstrap-extra", "bootstrap-failed", "new-session"])
def test_adhd_flow_bootstraps_before_continued_activation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None) -> None:
    binary = fake_maka(tmp_path)
    database = tmp_path / "runtime.sqlite"
    initialize_runtime_db(database)
    tier = tmp_path / "tier/relay-last-tier.json"
    log = tmp_path / "argv.log"
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(database))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_TIER_FILE", str(tier))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(log))
    if fault == "bootstrap-extra":
        monkeypatch.setenv("KHENRIX_FAKE_MAKA_BOOTSTRAP_EXTRA_SKILL", "brainstorming")
    allow_fake_maka_environment(monkeypatch)
    execute = maka_smoke._bounded_command

    def command(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        result = execute(argv, **kwargs)
        if fault == "bootstrap-failed" and len(argv) > 2 and argv[2].startswith("/skill:using-superpowers"):
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE core_root_turn_admissions SET record_json=?",
                    (json.dumps({"skillInvocation": {"loaded": [], "failed": [{"id": "using-superpowers"}], "receipts": []}}),))
        if fault == "new-session" and len(argv) > 2 and "I have three things" in argv[2]:
            with sqlite3.connect(database) as connection:
                row = connection.execute("SELECT session_id,payload_json,created_at FROM session_metadata").fetchone()
                connection.execute("UPDATE session_metadata SET payload_json=? WHERE session_id=?",
                    (json.dumps({"cwd": "elsewhere"}), row[0]))
                connection.execute("INSERT INTO session_metadata(session_id,payload_json,created_at) VALUES (?,?,?)", ("unexpected-session", row[1], row[2]))
        return result

    monkeypatch.setattr(maka_smoke, "_bounded_command", command)
    flow = maka_smoke.load_cases(ROOT / "components/skills/maka-smoke.json")["flows"][0]
    environment = maka_smoke.sanitized_environment()
    host = maka_smoke.attest_existing_host(str(binary), tmp_path, database, environment, None)
    if fault:
        with pytest.raises(maka_smoke.SmokeError, match="unexpected|failed|another|unverified|target|successful explicit admission|session"):
            maka_smoke.run_adhd_flow(str(binary), flow, database, environment, tier, host)
        invocations = [json.loads(line) for line in log.read_text().splitlines()]
        assert len(invocations) == (3 if fault == "new-session" else 1)
        return
    result = maka_smoke.run_adhd_flow(str(binary), flow, database, environment, tier, host)
    invocations = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(invocations) == 4
    assert invocations[0][1].startswith("/skill:using-superpowers Reply ready.\n")
    assert invocations[1][1].startswith(flow["steps"][0]["prompt"])
    assert all(row[row.index("--resume") + 1] == result["session_id"] for row in invocations)
    assert result["session_bootstrap"]["event_evidence"]["skill_id"] == "using-superpowers"
    assert result["activation_evidence"]["skill_id"] == "khenrix-quality"
    assert [step["continued"] for step in result["steps"]] == [True, True, True]


@pytest.mark.parametrize("fault", ["natural-failed", "natural-extra", "admission-failed", "admission-extra"])
def test_adhd_continuation_rejects_failed_or_extra_skill_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    binary = fake_maka(tmp_path)
    database = tmp_path / "runtime.sqlite"
    initialize_runtime_db(database)
    tier = tmp_path / "tier/relay-last-tier.json"
    monkeypatch.setenv("KHENRIX_FAKE_MAKA_DB", str(database))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_TIER_FILE", str(tier))
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(tmp_path / "argv.log"))
    allow_fake_maka_environment(monkeypatch)
    execute = maka_smoke._bounded_command

    def command(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        result = execute(argv, **kwargs)
        if len(argv) > 2 and "I have three things" in argv[2]:
            with sqlite3.connect(database) as connection:
                session, sequence, encoded = connection.execute(
                    "SELECT session_id,sequence,record_json FROM core_agent_run_events ORDER BY rowid DESC LIMIT 1").fetchone()
                record = json.loads(encoded)
                if fault.startswith("natural"):
                    record.update(id="failed-extra-load", type="skill_load_failed" if fault == "natural-failed" else "skill_loaded", data={"success": fault == "natural-extra"})
                    connection.execute("INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)",
                        (session, sequence + 1, record["id"], record["type"], json.dumps(record)))
                else:
                    invocation = {"loaded": [], "failed": [], "receipts": []}
                    invocation["failed" if fault == "admission-failed" else "loaded"] = [{"id": "unexpected"}]
                    connection.execute("UPDATE core_root_turn_admissions SET record_json=? WHERE session_id=? AND turn_id=?",
                        (json.dumps({"skillInvocation": invocation}), session, record["turnId"]))
        return result

    monkeypatch.setattr(maka_smoke, "_bounded_command", command)
    flow = maka_smoke.load_cases(ROOT / "components/skills/maka-smoke.json")["flows"][0]
    environment = maka_smoke.sanitized_environment()
    host = maka_smoke.attest_existing_host(str(binary), tmp_path, database, environment, None)
    with pytest.raises(maka_smoke.SmokeError, match="unexpected|failed"):
        maka_smoke.run_adhd_flow(str(binary), flow, database, environment, tier, host)


def test_flow_checks_behavior_without_prompting_for_the_expected_answer() -> None:
    action = {
        "id": "action-first-follow-up",
        "prompt": "I have three things on my plate: review a pull request, answer email, and prepare lunch.",
        "check": "adhd_action_first",
    }
    evidence = maka_smoke.verify_flow_output(
        action, "Open the pull request and read the description.\nThen decide whether to review it."
    )
    assert evidence["opening_actionable"] is True
    assert evidence["action_line"] == 1
    second_line = maka_smoke.verify_flow_output(
        action,
        "Order: lunch, then the pull request, then email.\n"
        "Now: set a 10-minute timer and make lunch.",
    )
    assert second_line["opening_actionable"] is True
    assert second_line["action_line"] == 2
    with pytest.raises(maka_smoke.SmokeError, match="concrete action"):
        maka_smoke.verify_flow_output(action, "Those tasks sound difficult. Here are some options.")

    opt_out = {
        "id": "normal-mode-opt-out",
        "prompt": "Normal mode. Stop ADHD mode. Confirm briefly.",
        "check": "normal_mode_ack",
    }
    with pytest.raises(maka_smoke.SmokeError, match="echoed"):
        maka_smoke.verify_flow_output(opt_out, opt_out["prompt"])
    with pytest.raises(maka_smoke.SmokeError, match="bounded state"):
        maka_smoke.verify_flow_output(opt_out, "Understood. I can help with anything else.")


@pytest.mark.parametrize("output", [
    "Do not open the pull request. Wait for someone else.",
    "Don't review the pull request yet.",
    "I will review the pull request at some point.",
    '"Open the pull request" is an example of an action.',
])
def test_action_flow_rejects_negated_promised_or_quoted_actions(output: str) -> None:
    step = {"id": "action-first-follow-up", "check": "adhd_action_first"}
    with pytest.raises(maka_smoke.SmokeError, match="concrete action"):
        maka_smoke.verify_flow_output(step, output)


@pytest.mark.parametrize("line,ordinal", [
    ("1. **Now — lunch:** take out the easiest food available. Leftovers, toast, or a sandwich count.", 2),
    ("**Open the pull request** and read its description.", 1),
    ("- **Email:** reply to the oldest email.", 1),
    ("2) **Next — pull request:** read its description.", 2),
    ("**First:** prepare lunch.", 1),
    ("**Lunch:** take out a sandwich.", 1),
    ("Now — email: open the oldest email.", 2),
])
def test_action_flow_accepts_bounded_formatted_imperatives(line: str, ordinal: int) -> None:
    step = {"id": "action-first-follow-up", "check": "adhd_action_first"}
    output = line if ordinal == 1 else "\nLunch, then the pull request, then email.\n\n" + line
    evidence = maka_smoke.verify_flow_output(step, output)
    assert evidence == {
        "check": "adhd_action_first",
        "opening_actionable": True,
        "action_line": ordinal,
        "action_line_hash": "sha256:" + hashlib.sha256(line.encode()).hexdigest(),
    }


@pytest.mark.parametrize("output", [
    "**Do not open the pull request.**",
    "1. **Now — lunch:** don't take out any food yet.",
    "**Next — email:** I will reply to the email later.",
    '**Now — pull request:** "Open the pull request" is an example.',
    "**Example:** open the pull request.",
    "**Now — email:** for example, reply to the oldest email.",
    "**Now — lunch:** `take out a sandwich` is sample wording.",
    "```text\nOpen the pull request.\n```",
    "**Now — lunch:** lunch is one of your tasks.",
    "**Now:** prepare a coffee.",
    "Lunch, then email.\nYou can start small.\n**Open the pull request.**",
    "I have three things on my plate: review a pull request, answer email, and prepare lunch.",
])
def test_action_flow_rejects_formatted_non_actions_and_late_actions(output: str) -> None:
    step = {"id": "action-first-follow-up", "check": "adhd_action_first"}
    with pytest.raises(maka_smoke.SmokeError, match="concrete action"):
        maka_smoke.verify_flow_output(step, output)


@pytest.mark.parametrize("introduction", [
    "For example:",
    "**For instance:**",
    "Here is an example:",
    "Example action:",
    "**Sample output:**",
    "I will:",
    "I'll do the following:",
    "I promise to:",
    "I plan to:",
    "**Next — email:** I will:",
    "**Now — lunch:** For example:",
    "Quoted instruction:",
    "Here is a quotation:",
    '"',
    "“",
])
def test_action_flow_rejects_action_under_non_action_introduction(introduction: str) -> None:
    step = {"id": "action-first-follow-up", "check": "adhd_action_first"}
    with pytest.raises(maka_smoke.SmokeError, match="concrete action"):
        maka_smoke.verify_flow_output(step, introduction + "\n**Open the pull request.**")


@pytest.mark.parametrize("output", [
    "**Lunch:** take out",
    "Email: reply",
    "Lunch: open",
    "PR: review.",
    "**Now — lunch:** take out ...",
    "**Email:** reply — !",
    "**Lunch:** open `...`",
    "Next — pull request: read ___",
])
def test_action_flow_rejects_task_label_with_objectless_action(output: str) -> None:
    step = {"id": "action-first-follow-up", "check": "adhd_action_first"}
    with pytest.raises(maka_smoke.SmokeError, match="concrete action"):
        maka_smoke.verify_flow_output(step, output)


@pytest.mark.parametrize("line,ordinal", [
    ("**First: get out something easy to eat.** Lunch doesn’t need to become a cooking project.", 1),
    ("**Lunch:** take a sandwich from the fridge.", 2),
    ("Grab a plate for lunch.", 1),
    ("**Now — lunch:** put a bowl on the table.", 2),
    ("Eat the leftovers for lunch.", 1),
    ("Heat the soup for lunch.", 2),
    ("Warm up the rice for lunch.", 1),
    ("Have a sandwich for lunch.", 2),
    ("Gather the lunch ingredients on the counter.", 1),
    ("Assemble a simple lunch from leftovers.", 2),
    ("Serve yourself a small lunch.", 1),
    ("**Email:** answer the oldest message.", 2),
    ("Send a brief reply to the email.", 1),
    ("Write the first sentence of your email reply.", 2),
    ("Check the pull request description.", 1),
    ("Skim the pull request diff.", 2),
    ("Scan the email subject lines.", 1),
    ("**Next — PR:** look\tat the changed files.", 2),
])
def test_action_flow_accepts_ordinary_task_action_family(line: str, ordinal: int) -> None:
    step = {"id": "action-first-follow-up", "check": "adhd_action_first"}
    output = line if ordinal == 1 else "Lunch, then email, then the pull request.\n" + line
    evidence = maka_smoke.verify_flow_output(step, output)
    assert evidence == {
        "check": "adhd_action_first",
        "opening_actionable": True,
        "action_line": ordinal,
        "action_line_hash": "sha256:" + hashlib.sha256(line.encode()).hexdigest(),
    }


@pytest.mark.parametrize("output", [
    "**Lunch:** do not get out any food.",
    "**Do not grab lunch yet.**",
    "Don't send the email.",
    "**Now — lunch:** I will heat the soup later.",
    "I can gather lunch ingredients if you want.",
    "Would you like me to assemble lunch?",
    '"Get out some food for lunch" is an example.',
    "**Example:** eat the leftovers for lunch.",
    "For example:\n**Skim the pull request.**",
    "I promise to:\n**Write the email reply.**",
    "Quoted instruction:\n**Serve lunch.**",
    "```text\nCheck the pull request.\n```",
    "**Lunch:** get",
    "**Lunch:** get out ...",
    "**Lunch:** take out",
    "**Lunch:** grab !!!",
    "**Lunch:** put down",
    "**Lunch:** heat up",
    "**Lunch:** have a",
    "**Email:** answer ...",
    "**Next — PR:** look at",
    "**Email:** send the",
    "Have you thought about lunch?",
    "Have not prepared lunch yet.",
    "Get a coffee.",
    "Lunch, then email.\nStart small.\nGrab the leftovers for lunch.",
    "Lunch, then email.\nStart small.\nLook at the pull request.",
])
def test_action_flow_rejects_non_actions_in_ordinary_task_action_family(output: str) -> None:
    step = {"id": "action-first-follow-up", "check": "adhd_action_first"}
    with pytest.raises(maka_smoke.SmokeError, match="concrete action"):
        maka_smoke.verify_flow_output(step, output)


@pytest.mark.parametrize("output", [
    "Have the lunch leftovers spoiled?",
    "Have your leftovers gone bad before lunch?",
    "**Lunch:** have a sandwich?",
    "Have lunch ingredients arrived?",
    "Have already eaten lunch.",
    "Have just warmed lunch.",
    "Have been preparing lunch.",
    "Have eaten lunch.",
    "**Lunch:** put together",
    "**Lunch:** get in",
    "**Lunch:** put together ...",
    "**Lunch:** take apart!",
    "**Lunch:** get through",
    "**Lunch:** put into the ...",
    "**Lunch:** take from",
    "**Lunch:** gather around",
    "**Lunch:** get over",
    "**Lunch:** put aside",
])
def test_action_flow_rejects_auxiliary_have_and_particle_only_objects(output: str) -> None:
    step = {"id": "action-first-follow-up", "check": "adhd_action_first"}
    with pytest.raises(maka_smoke.SmokeError, match="concrete action"):
        maka_smoke.verify_flow_output(step, output)


@pytest.mark.parametrize("output", [
    "Have lunch.",
    "Have some leftovers for lunch.",
    "**Lunch:** have your sandwich.",
    "Have the soup for lunch.",
    "**Lunch:** put the sandwich together.",
    "**Lunch:** get the leftovers in a bowl.",
])
def test_action_flow_keeps_affirmative_have_and_concrete_phrasal_actions(output: str) -> None:
    step = {"id": "action-first-follow-up", "check": "adhd_action_first"}
    evidence = maka_smoke.verify_flow_output(step, output)
    assert evidence["action_line"] == 1
    assert evidence["action_line_hash"] == "sha256:" + hashlib.sha256(output.encode()).hexdigest()


@pytest.mark.parametrize("line,ordinal", [
    ("**First: go to the kitchen and take out the easiest lunch option.** No need to solve all three tasks at once.", 1),
    ("**Lunch:** go into the kitchen and choose leftovers.", 2),
    ("Head to the counter to prepare lunch.", 1),
    ("**Next — lunch:** head into the kitchen.", 2),
    ("Walk to your desk and open the email.", 1),
    ("**Lunch:** walk into the kitchen.", 2),
    ("Step into the kitchen and make lunch.", 1),
    ("**PR:** move\tto your desk and open the changed files.", 2),
    ("Move into the kitchen to prepare lunch.", 1),
    ("Bring a plate to the table for lunch.", 2),
    ("Collect the ingredients for lunch.", 1),
    ("Fetch the leftovers for lunch.", 2),
    ("Return to your desk and open the pull request.", 1),
    ("Clear the kitchen counter for lunch.", 2),
    ("Stand at the counter and prepare lunch.", 1),
    ("Sit at your desk and read the email.", 2),
    ("Sit down at the table with your lunch.", 1),
    ("Reheat the soup for lunch.", 2),
    ("Microwave the leftovers for lunch.", 1),
    ("Cut a slice of bread for lunch.", 2),
    ("Slice the tomato for lunch.", 1),
    ("Pour the soup into a bowl for lunch.", 2),
])
def test_action_flow_accepts_concrete_movement_and_physical_task_actions(line: str, ordinal: int) -> None:
    step = {"id": "action-first-follow-up", "check": "adhd_action_first"}
    output = line if ordinal == 1 else "Lunch, then email, then the pull request.\n" + line
    evidence = maka_smoke.verify_flow_output(step, output)
    assert evidence == {
        "check": "adhd_action_first",
        "opening_actionable": True,
        "action_line": ordinal,
        "action_line_hash": "sha256:" + hashlib.sha256(line.encode()).hexdigest(),
    }


@pytest.mark.parametrize("output", [
    "**Lunch:** go to",
    "**Lunch:** go into the ...",
    "**Lunch:** head into",
    "**Lunch:** walk to the",
    "**Lunch:** step into ...",
    "**Lunch:** move to",
    "**Lunch:** bring the",
    "**Lunch:** collect ...",
    "**Lunch:** fetch from",
    "**PR:** return to the",
    "**Lunch:** clear",
    "**Lunch:** stand at",
    "**Email:** sit down at the",
    "**Lunch:** reheat ...",
    "**Lunch:** pour into",
    "Should I go to the kitchen for lunch?",
    "Can you head into the kitchen for lunch?",
    "Will you bring a plate for lunch?",
    "Do not go to the kitchen for lunch.",
    "**Lunch:** don't fetch the leftovers yet.",
    "**Email:** I will walk to my desk later.",
    "I can microwave lunch if you want.",
    '"Step into the kitchen for lunch" is sample wording.',
    "For example:\n**Move to the counter to prepare lunch.**",
    "I promise to:\n**Return to the desk and open the pull request.**",
    "```text\nCollect ingredients for lunch.\n```",
    "Lunch, then email.\nStart small.\nHead to the kitchen for lunch.",
    "Lunch, then email.\nStart small.\nFetch the lunch leftovers.",
    "Go to the front door.",
    "Bring a glass of water.",
    "Head movement can help with lunch stress.",
    "A walk may clear your head before lunch.",
    "Move forward with your lunch planning.",
])
def test_action_flow_rejects_non_actions_in_movement_family(output: str) -> None:
    step = {"id": "action-first-follow-up", "check": "adhd_action_first"}
    with pytest.raises(maka_smoke.SmokeError, match="concrete action"):
        maka_smoke.verify_flow_output(step, output)


@pytest.mark.parametrize("output", [
    "Normal mode is unavailable; ADHD mode remains active.",
    "Normal mode enabled. ADHD mode remains active.",
    "Normal mode enabled. ADHD mode is not off.",
    "Normal mode is not enabled. ADHD mode disabled.",
    "Normal mode is not enabled.",
    "ADHD mode is not off.",
    "I will switch to normal mode later.",
    '"Normal mode is enabled" is the acknowledgement you requested.',
])
def test_normal_mode_flow_rejects_denied_promised_or_quoted_state(output: str) -> None:
    step = {"id": "normal-mode-opt-out", "prompt": "Normal mode. Stop ADHD mode. Confirm briefly.",
            "check": "normal_mode_ack"}
    with pytest.raises(maka_smoke.SmokeError, match="bounded state"):
        maka_smoke.verify_flow_output(step, output)


@pytest.mark.parametrize("output", [
    "Normal mode is enabled.",
    "Normal mode enabled; ADHD mode is off.",
    "Done — normal mode is enabled.",
    "Normal mode is now on.",
    "Switched to normal mode.",
    "ADHD mode is now off.",
    "ADHD mode disabled.",
    "Understood.\nNormal mode is active.",
])
def test_normal_mode_flow_accepts_affirmative_state_acknowledgements(output: str) -> None:
    step = {"id": "normal-mode-opt-out", "prompt": "Normal mode. Stop ADHD mode. Confirm briefly.",
            "check": "normal_mode_ack"}
    evidence = maka_smoke.verify_flow_output(step, output)
    assert evidence["state_acknowledged"] is True


@pytest.mark.parametrize("retain", [False, True])
def test_dry_run_executes_no_model_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], retain: bool,
) -> None:
    repo, home, config = smoke_delivery_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    binary = fake_maka(tmp_path)
    log = tmp_path / "argv.log"
    monkeypatch.setenv("KHENRIX_MAKA_SMOKE_ARGV_LOG", str(log))
    monkeypatch.setattr(
        maka_smoke, "attested_maka_cli",
        lambda *_args: ([sys.executable, str(binary)], tmp_path, {}),
    )
    monkeypatch.setattr(maka_smoke, "maka_version", lambda *_args: "test-version")

    def unexpected_host(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("dry-run started a private Host")

    monkeypatch.setattr(maka_smoke, "private_routing_host", unexpected_host)

    result = maka_smoke.main(
        [
            "--repo-root",
            str(repo),
            "--home",
            str(home),
            "--manifest",
            str(ROOT / "components" / "skills" / "maka-smoke.json"),
            "--dry-run",
            *(["--retain-private-host"] if retain else []),
        ],
    )
    assert result == 0
    output = capsys.readouterr().out
    assert "32 bounded turns" in output
    assert "PLAN adhd-session-mode-bootstrap explicit using-superpowers" in output
    assert not log.exists()
    assert not (config.state_dir / "maka-smoke-receipt.json").exists()


def test_smoke_rejects_non_mapping_nested_install_receipt(tmp_path: Path) -> None:
    _, _, config = smoke_delivery_fixture(tmp_path)
    skillctl.apply(config, expect=None, as_json=False)
    receipt_path = config.state_dir / skillctl.RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text())
    receipt["skills"] = []
    receipt_path.write_text(json.dumps(receipt))

    manifest = maka_smoke.load_cases(ROOT / "components" / "skills" / "maka-smoke.json")
    with pytest.raises(skillctl.DeliveryError, match="skills must be an object"):
        maka_smoke.installed_closure(config, manifest)


def test_manifest_requires_explicit_and_natural_case_per_skill(tmp_path: Path) -> None:
    manifest = tmp_path / "cases.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "required_routes": {
                    "khenrix-quality": ["explicit", "natural"]
                },
                "cases": [
                    {
                        "id": "only-one",
                        "route": "explicit",
                        "skill": "khenrix-quality",
                        "prompt": "/skill:khenrix-quality test",
                        "timeout_seconds": 10,
                        "max_steps": 1,
                    }
                ],
            }
        )
    )
    with pytest.raises(maka_smoke.SmokeError, match="cover every required skill route"):
        maka_smoke.load_cases(manifest)


def test_manifest_refusal_requires_absolute_executor_canary(tmp_path: Path) -> None:
    manifest = json.loads((ROOT / "components/skills/maka-smoke.json").read_text())
    refusal = next(
        step for scenario in manifest["matrix"] for step in scenario["steps"]
        if step.get("check") == "maka_executor_refusal"
    )
    refusal["prompt"] = refusal["prompt"].replace("{absolute_canary_path}", "")
    path = tmp_path / "maka-smoke.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(maka_smoke.SmokeError, match="absolute executor canary"):
        maka_smoke.load_cases(path)


def test_natural_case_fails_when_model_tool_event_is_absent(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    now = 2_000
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO session_metadata(session_id,payload_json,created_at) VALUES (?,?,?)",
            ("session-1", json.dumps({"cwd": str(cwd.resolve()), "createdAt": now}), now),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        session_id = maka_smoke.locate_session(connection, cwd, now)
        with pytest.raises(maka_smoke.SmokeError, match="model_tool"):
            maka_smoke.verify_natural_event(
                connection, session_id, "khenrix-quality", natural_event_window(0),
            )


def test_explicit_case_rejects_wrong_source(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    invocation = {
        "loaded": [{"id": "khenrix-quality", "name": "khenrix-quality"}],
        "failed": [],
        "receipts": [
            {
                "invocation": "explicit",
                "success": True,
                "id": "khenrix-quality",
                "ref": "custom:khenrix-quality",
                "source": "custom",
            }
        ],
    }
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO message_admissions(session_id,skill_invocation_json) VALUES (?,?)",
            ("session-1", json.dumps(invocation)),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="explicit admission"):
            maka_smoke.verify_explicit_event(
                connection, "session-1", "khenrix-quality",
                {"session_id": "session-1", "turn_id": "turn-1"},
            )


def test_explicit_case_reads_canonical_root_turn_admission(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    skill = "khenrix-quality"
    receipt = {
        "invocation": "explicit",
        "request": skill,
        "success": True,
        "ref": f"user:agents:{skill}",
        "id": skill,
        "name": skill,
        "scope": "user",
        "source": "agents",
        "truncated": False,
    }
    record = {
        "skillInvocation": {
            "loaded": [{"id": skill, "name": skill}],
            "failed": [],
            "receipts": [receipt],
        }
    }
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO core_root_turn_admissions VALUES (?,?,?,?)",
            ("session-1", "turn-1", 1, json.dumps(record)),
        )
        connection.execute(
            "INSERT INTO core_root_turn_admissions VALUES (?,?,?,?)",
            ("session-1", "turn-2", 2, json.dumps({"kind": "natural-continuation"})),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        evidence = maka_smoke.verify_explicit_event(
            connection, "session-1", skill,
            {"session_id": "session-1", "turn_id": "turn-1"},
        )
    assert evidence["kind"] == "core_root_turn_admission"
    assert evidence["turn_id"] == "turn-1"


def test_explicit_target_cannot_reuse_bootstrap_admission(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    skill = "brainstorming"
    receipt = {
        "invocation": "explicit", "success": True, "id": skill,
        "ref": f"user:agents:{skill}", "source": "agents", "truncated": False,
    }
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO core_root_turn_admissions VALUES (?,?,?,?)",
            ("session-1", "bootstrap-turn", 1, json.dumps({
                "skillInvocation": {
                    "loaded": [{"id": skill}], "failed": [], "receipts": [receipt],
                },
            })),
        )
        connection.execute(
            "INSERT INTO core_root_turn_admissions VALUES (?,?,?,?)",
            ("session-1", "target-turn", 2, json.dumps({"kind": "natural-continuation"})),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="turn"):
            maka_smoke.verify_explicit_event(
                connection, "session-1", skill,
                {"session_id": "session-1", "turn_id": "target-turn"},
            )


def test_explicit_target_rejects_extra_root_admitted_skill(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    receipts = [
        {
            "invocation": "explicit", "success": True, "id": skill,
            "ref": f"user:agents:{skill}", "source": "agents", "truncated": False,
        }
        for skill in ("brainstorming", "llm-fanout-execute")
    ]
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO core_root_turn_admissions VALUES (?,?,?,?)",
            ("session-1", "target-turn", 1, json.dumps({
                "skillInvocation": {
                    "loaded": [{"id": item["id"]} for item in receipts],
                    "failed": [], "receipts": receipts,
                },
            })),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="another skill"):
            maka_smoke.verify_explicit_event(
                connection, "session-1", "brainstorming",
                {"session_id": "session-1", "turn_id": "target-turn"},
            )


def test_explicit_case_rejects_truncated_skill_instructions(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    skill = "khenrix-quality"
    receipt = {
        "invocation": "explicit",
        "request": skill,
        "success": True,
        "ref": f"user:agents:{skill}",
        "id": skill,
        "name": skill,
        "scope": "user",
        "source": "agents",
        "truncated": True,
    }
    invocation = {
        "loaded": [{"id": skill, "name": skill}],
        "failed": [],
        "receipts": [receipt],
    }
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO message_admissions(session_id,skill_invocation_json) VALUES (?,?)",
            ("session-1", json.dumps(invocation)),
        )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="truncated instructions"):
            maka_smoke.verify_explicit_event(
                connection, "session-1", skill,
                {"session_id": "session-1", "turn_id": "turn-1"},
            )


def add_natural_event(runtime_db: Path, data: dict[str, object]) -> None:
    record = {
        "id": "event-1", "sessionId": "session-1", "turnId": "turn-1",
        "type": "skill_loaded", "data": data,
    }
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)",
            ("session-1", 1, "event-1", "skill_loaded", json.dumps(record)),
        )


def natural_event_window(through: int = 1) -> dict[str, object]:
    return {
        "session_id": "session-1", "turn_id": "turn-1",
        "event_after_rowid": 0, "event_through_rowid": through,
    }


def natural_event_data(**overrides: object) -> dict[str, object]:
    skill = "khenrix-quality"
    data: dict[str, object] = {
        "invocation": "model_tool",
        "success": True,
        "skillRef": f"user:agents:{skill}",
        "skillId": skill,
        "skillName": skill,
        "skillScope": "user",
        "skillSource": "agents",
        "truncated": False,
        "shadowCandidateCount": 0,
        "shadowHitAt1": False,
        "shadowHitAt5": False,
        "shadowHitAt20": False,
    }
    data.update(overrides)
    return data


def test_natural_case_rejects_truncated_skill_instructions(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    add_natural_event(runtime_db, natural_event_data(truncated=True))
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="truncated instructions"):
            maka_smoke.verify_natural_event(
                connection, "session-1", "khenrix-quality", natural_event_window(),
            )


def test_natural_case_rejects_non_top_shadow_candidate(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    add_natural_event(
        runtime_db,
        natural_event_data(
            shadowCandidateCount=2,
            shadowRank=2,
            shadowHitAt1=False,
            shadowHitAt5=True,
            shadowHitAt20=True,
        ),
    )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="first-ranked"):
            maka_smoke.verify_natural_event(
                connection, "session-1", "khenrix-quality", natural_event_window(),
            )


def test_natural_case_accepts_first_ranked_shadow_candidate(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    add_natural_event(
        runtime_db,
        natural_event_data(
            shadowCandidateCount=3,
            shadowRank=1,
            shadowHitAt1=True,
            shadowHitAt5=True,
            shadowHitAt20=True,
        ),
    )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        evidence = maka_smoke.verify_natural_event(
            connection, "session-1", "khenrix-quality", natural_event_window(),
        )
    assert evidence["skill_id"] == "khenrix-quality"


def test_natural_event_uses_only_target_turn_window(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    with sqlite3.connect(runtime_db) as connection:
        for sequence, turn_id, skill in (
            (1, "bootstrap-turn", "llm-fanout-execute"),
            (2, "target-turn", "khenrix-quality"),
        ):
            data = natural_event_data(
                skillId=skill, skillRef=f"user:agents:{skill}"
            )
            connection.execute(
                "INSERT INTO core_agent_run_events VALUES (?,?,?,?,?)",
                (
                    "session-1", sequence, f"event-{sequence}", "skill_loaded",
                    json.dumps({
                        "id": f"event-{sequence}", "sessionId": "session-1",
                        "type": "skill_loaded", "turnId": turn_id, "data": data,
                    }),
                ),
            )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        evidence = maka_smoke.verify_natural_event(
            connection, "session-1", "khenrix-quality",
            {
                "session_id": "session-1", "turn_id": "target-turn",
                "event_after_rowid": 1, "event_through_rowid": 2,
            },
        )
    assert evidence["skill_id"] == "khenrix-quality"


def test_bootstrap_turn_rejects_extra_natural_skill_load(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite"
    initialize_runtime_db(runtime_db)
    add_natural_event(
        runtime_db,
        natural_event_data(
            skillId="llm-fanout-execute",
            skillRef="user:agents:llm-fanout-execute",
        ),
    )
    with maka_smoke.open_runtime_db(runtime_db) as connection:
        with pytest.raises(maka_smoke.SmokeError, match="bootstrap turn loaded an unexpected skill"):
            maka_smoke.verify_no_natural_skill_events(
                connection, "session-1", "bootstrap",
                natural_event_window(),
            )


def test_admitted_wire_task_requires_selected_native_task_digest():
    text = 'native admitted task\nReceipt nonce: maka:probe-' + 'a' * 32 + '-' + 'b' * 32
    expected = hashlib.sha256(text.encode()).hexdigest()
    assert hasattr(maka_smoke, 'verify_admitted_wire_task')
    maka_smoke.verify_admitted_wire_task(text, [text], [expected])
    for selected in ([], ['0' * 64], [expected, '0' * 64]):
        with pytest.raises(maka_smoke.SmokeError, match='task digest'):
            maka_smoke.verify_admitted_wire_task(text, [text], selected)
    with pytest.raises(maka_smoke.SmokeError, match='task digest'):
        maka_smoke.verify_admitted_wire_task(text, ['changed task'], [expected])


def test_smoke_closure_binds_each_normal_native_support_file(tmp_path, monkeypatch):
    module_path = tmp_path / 'components/skills/maka_smoke.py'
    module_path.parent.mkdir(parents=True)
    for relative in maka_smoke.NATIVE_SUPPORT_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
    monkeypatch.setattr(maka_smoke, '__file__', str(module_path))
    before = maka_smoke.native_support_hashes()
    assert set(before) == set(maka_smoke.NATIVE_SUPPORT_FILES)
    for relative in maka_smoke.NATIVE_SUPPORT_FILES:
        path = tmp_path / relative
        original = path.read_bytes()
        path.write_bytes(original + b' changed')
        after = maka_smoke.native_support_hashes()
        assert after[relative] != before[relative]
        assert all(after[other] == before[other] for other in before if other != relative)
        path.write_bytes(original)
