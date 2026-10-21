"""Fixture-only contracts for resumable fanout executor adapters."""
from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import os
import sys
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
# Council's immutable compatibility tests intentionally import their legacy
# `fanout.py` as ``fanout``. Load this package under its own test-only name so
# the aggregate deterministic gate has no collection-order dependency.
SPEC = importlib.util.spec_from_file_location(
    "_fanout_provider_contracts",
    FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)

ArtifactStore = fanout.ArtifactStore
ArtifactQuotaError = fanout.ArtifactQuotaError
ProcessResult = fanout.ProcessResult
ProcessStatus = fanout.ProcessStatus
ProviderProtocolError = fanout.ProviderProtocolError
ProviderRequestError = fanout.ProviderRequestError
UnsupportedExecutorError = fanout.UnsupportedExecutorError
ProviderRegistry = fanout.ProviderRegistry
ProviderCapabilities = fanout.ProviderCapabilities
ProviderAdapter = fanout.ProviderAdapter
ExecutorProfile = fanout.ExecutorProfile
ProviderRequest = fanout.ProviderRequest
ProviderResult = fanout.ProviderResult
RoutingTargetRegistry = fanout.RoutingTargetRegistry
run_many = fanout.run_many
run_provider = fanout.run_provider
providers = sys.modules[f"{SPEC.name}.providers"]


def _trace_bytes(events):
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


def _claude_skill_companion_rows():
    # Native Skill expansion is a separate user event after its tool result.
    return [
        {"type": "user", "sessionId": "exact", "promptId": "prompt-1",
         "message": {"content": "task\n"}},
        {"type": "assistant", "sessionId": "exact", "message": {"content": [
            {"type": "tool_use", "id": "skill-1", "name": "Skill", "input": {"skill": "using-superpowers"}}]}},
        {"type": "assistant", "sessionId": "exact", "message": {"content": [
            {"type": "tool_use", "id": "read-before", "name": "Read", "input": {"file_path": "/safe/SKILL.md"}}]}},
        {"type": "user", "sessionId": "exact", "promptId": "prompt-1", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "skill-1", "content": "Launching skill: using-superpowers"}]}},
        {"type": "user", "sessionId": "exact", "promptId": "prompt-1", "isMeta": True,
         "turnCompanion": True, "sourceToolUseID": "skill-1", "message": {"content": [
             {"type": "text", "text": "Base directory for this skill: /safe/using-superpowers\nSkill instructions"}]}},
        {"type": "user", "sessionId": "exact", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "read-before", "content": "guidance"}]}},
        {"type": "assistant", "sessionId": "exact", "message": {"content": [
            {"type": "tool_use", "id": "read-after", "name": "Read", "input": {"file_path": "/run/sibling/answer.txt"}}]}},
        {"type": "user", "sessionId": "exact", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "read-after", "content": "private text"}]}},
        {"type": "assistant", "sessionId": "exact", "message": {"stop_reason": "end_turn",
            "content": [{"type": "text", "text": "answer"}]}},
    ]


@pytest.mark.parametrize("string_content", [False, True])
def test_claude_skill_companion_preserves_complete_turn_and_real_next_input(tmp_path, string_content):
    native = importlib.import_module(f"{SPEC.name}.native_trace")
    rows = _claude_skill_companion_rows()
    if string_content:
        rows[4]["message"]["content"] = "Skill instructions"
    rows.extend([
        {"type": "user", "sessionId": "exact", "promptId": "prompt-2", "message": {"content": "next task"}},
        {"type": "assistant", "sessionId": "exact", "message": {"content": [
            {"type": "tool_use", "id": "next-read", "name": "Read", "input": {"file_path": "/run/next.txt"}}]}},
    ])
    raw = _trace_bytes(rows)
    trace = native.project_native_trace("claude", raw, prompt=b"task\n", session_id="exact",
        cwd=tmp_path, answer="answer")
    assert trace["coverage"] == "complete", trace
    assert trace["bounds"] == [0, 8]
    assert trace["prompt_sha256"] == hashlib.sha256(b"task\n").hexdigest()
    assert trace["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert [call["id"] for call in trace["calls"]] == ["skill-1", "read-before", "read-after"]
    assert all(call["status"] == "completed" for call in trace["calls"])
    assert native.read_boundary_violation(trace, cwd=tmp_path, forbidden=(Path("/run"),),
        allowed_trees=(tmp_path,), allowed_files=()) == "read-after"
    assert "Skill instructions" not in json.dumps(trace)


@pytest.mark.parametrize("field,value", [
    ("isMeta", False), ("isMeta", "true"), ("turnCompanion", False), ("turnCompanion", "true"),
    ("sessionId", "foreign"), ("sessionId", None), ("promptId", "foreign"), ("promptId", None),
    ("sourceToolUseID", "unknown"), ("sourceToolUseID", "read-before"), ("sourceToolUseID", None),
    ("sourceToolUseID", []),
])
def test_claude_invalid_skill_companion_cannot_qualify_a_truncated_read_interval(tmp_path, field, value):
    native = importlib.import_module(f"{SPEC.name}.native_trace")
    rows = _claude_skill_companion_rows()
    rows[4][field] = value
    # A prior matching answer must not let malformed metadata hide later access.
    rows = rows[:4] + [rows[5], rows[-1], rows[4]] + rows[6:]
    trace = native.project_native_trace("claude", _trace_bytes(rows), prompt=b"task\n",
        session_id="exact", cwd=tmp_path, answer="answer")
    assert trace["coverage"] == "incomplete", trace


def test_claude_skill_companion_needs_a_preceding_skill_in_this_turn(tmp_path):
    native = importlib.import_module(f"{SPEC.name}.native_trace")
    rows = _claude_skill_companion_rows()
    rows.insert(1, rows.pop(4))
    trace = native.project_native_trace("claude", _trace_bytes(rows), prompt=b"task\n",
        session_id="exact", cwd=tmp_path, answer="answer")
    assert trace["coverage"] == "incomplete"


def test_claude_skill_companion_does_not_suppress_embedded_tool_calls(tmp_path):
    native = importlib.import_module(f"{SPEC.name}.native_trace")
    rows = _claude_skill_companion_rows()
    rows[4]["message"]["content"].append({"type": "tool_use", "id": "embedded-read", "name": "Read",
        "input": {"file_path": "/run/embedded.txt"}})
    rows.insert(5, {"type": "user", "sessionId": "exact", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "embedded-read", "content": "private text"}]}})
    trace = native.project_native_trace("claude", _trace_bytes(rows), prompt=b"task\n",
        session_id="exact", cwd=tmp_path, answer="answer")
    assert trace["coverage"] == "complete", trace
    assert [call["id"] for call in trace["calls"]] == ["skill-1", "read-before", "embedded-read", "read-after"]
    assert native.read_boundary_violation(trace, cwd=tmp_path, forbidden=(Path("/run"),),
        allowed_trees=(tmp_path,), allowed_files=()) == "embedded-read"


def test_claude_api_request_blobs_do_not_replay_calls_or_results(tmp_path):
    native = importlib.import_module(f"{SPEC.name}.native_trace")
    rows = _claude_skill_companion_rows()
    # Native request diagnostics copy prior assistant calls and user results.
    rows[6:6] = [
        {"type": "api-request-blob", "sessionId": "exact", "hash": "assistant-context",
         "message": {"role": "assistant", "content": rows[1]["message"]["content"] + rows[2]["message"]["content"]}},
        {"type": "api-request-blob", "sessionId": "exact", "hash": "user-context",
         "message": {"role": "user", "content": rows[3]["message"]["content"] + rows[5]["message"]["content"]}},
    ]
    trace = native.project_native_trace("claude", _trace_bytes(rows), prompt=b"task\n",
        session_id="exact", cwd=tmp_path, answer="answer")
    assert trace["coverage"] == "complete", trace
    assert [call["id"] for call in trace["calls"]] == ["skill-1", "read-before", "read-after"]
    assert native.read_boundary_violation(trace, cwd=tmp_path, forbidden=(Path("/run"),),
        allowed_trees=(tmp_path,), allowed_files=()) == "read-after"


@pytest.mark.parametrize("failure", ["snapshot-terminal", "foreign-snapshot", "actual-duplicate",
                                    "actual-unmatched-result", "actual-unsupported-block"])
def test_claude_request_diagnostics_keep_actual_message_and_session_validation(tmp_path, failure):
    native = importlib.import_module(f"{SPEC.name}.native_trace")
    rows = _claude_skill_companion_rows()
    if failure == "snapshot-terminal":
        rows[-1]["type"] = "api-request-blob"
    elif failure == "foreign-snapshot":
        rows.append({"type": "api-request-blob", "sessionId": "foreign", "message": {}})
    elif failure == "actual-duplicate":
        rows.insert(6, rows[1])
    elif failure == "actual-unmatched-result":
        rows.insert(6, {"type": "user", "sessionId": "exact", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "unmatched", "content": "text"}]}})
    else:
        rows.insert(6, {"type": "assistant", "sessionId": "exact", "message": {"content": [
            {"type": "unknown_tool_use", "arguments": {"path": "/run/answer.txt"}}]}})
    trace = native.project_native_trace("claude", _trace_bytes(rows), prompt=b"task\n",
        session_id="exact", cwd=tmp_path, answer="answer")
    assert trace["coverage"] == "incomplete", trace


@pytest.mark.parametrize("failure", ["changed-prompt", "duplicate-user", "duplicate-companion", "missing-prompt-id",
                                    "wrong-answer", "missing-terminal", "pending-skill"])
def test_claude_skill_companion_keeps_exact_input_and_completion_requirements(tmp_path, failure):
    native = importlib.import_module(f"{SPEC.name}.native_trace")
    rows = _claude_skill_companion_rows()
    if failure == "changed-prompt":
        rows[0]["message"]["content"] = "task"
    elif failure == "duplicate-user":
        rows.append({**rows[0], "promptId": "prompt-2"})
    elif failure == "duplicate-companion":
        rows[4]["message"]["content"] = "task\n"
    elif failure == "missing-prompt-id":
        rows[0].pop("promptId")
        rows[4].pop("promptId")
    elif failure == "wrong-answer":
        rows[-1]["message"]["content"][0]["text"] = "other answer"
    elif failure == "missing-terminal":
        rows.pop()
    else:
        rows.pop(3)
    trace = native.project_native_trace("claude", _trace_bytes(rows), prompt=b"task\n",
        session_id="exact", cwd=tmp_path, answer="answer")
    assert trace["coverage"] == "incomplete", trace


@pytest.mark.parametrize("separator", ["", "\n"])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("literal_closer", [False, True])
def test_agy_exact_prompt_frame_preserves_task_lf_and_turn_bounds(tmp_path, separator, resume, literal_closer):
    native = importlib.import_module(f"{SPEC.name}.native_trace")
    quoted = " with literal\n</USER_REQUEST>\nin task 雪" if literal_closer else ""
    prompts = [f"initial task{quoted}\n", f"resumed task{quoted}\n"]
    rows = []
    for index, prompt in enumerate(prompts):
        rows.extend([
            {"type": "USER_INPUT", "step_index": index * 3, "content":
                "<USER_REQUEST>\n/plan " + prompt + separator + "</USER_REQUEST>\n<ADDITIONAL_METADATA>native</ADDITIONAL_METADATA>"},
            {"type": "PLANNER_RESPONSE", "step_index": index * 3 + 1, "status": "DONE",
             "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": f"/workspace/turn-{index}.txt"}}]},
            {"type": "PLANNER_RESPONSE", "step_index": index * 3 + 2, "status": "DONE", "content": f"answer-{index}"},
        ])
    selected = int(resume)
    prompt = prompts[selected].encode()
    trace = native.project_native_trace("agy", _trace_bytes(rows), prompt=prompt,
        session_id="exact-session", cwd=tmp_path, answer=f"answer-{selected}")
    assert trace["coverage"] == "complete"
    assert trace["prompt_sha256"] == hashlib.sha256(prompt).hexdigest()
    assert trace["bounds"] == ([3, 5] if resume else [0, 2])
    assert [call["arguments"] for call in trace["calls"]] == [{"AbsolutePath": f"/workspace/turn-{selected}.txt"}]


@pytest.mark.parametrize("failure", ["changed", "foreign", "ambiguous", "extra-separator", "foreign-plan-prefix"])
def test_agy_exact_prompt_frame_rejects_changed_foreign_or_ambiguous_input(tmp_path, failure):
    native = importlib.import_module(f"{SPEC.name}.native_trace")
    prompt = "task\n"
    observed = {"changed": "task", "foreign": "another task\n"}.get(failure, prompt)
    prefix = "/other " if failure == "foreign-plan-prefix" else "/plan "
    separator = "\n\n" if failure == "extra-separator" else ""
    row = {"type": "USER_INPUT", "step_index": 0, "content":
           "<USER_REQUEST>\n" + prefix + observed + separator + "</USER_REQUEST>\n"}
    rows = [row, {"type": "PLANNER_RESPONSE", "step_index": 1, "status": "DONE", "content": "answer"}]
    if failure == "ambiguous":
        rows.extend([{**row, "step_index": 2}, {**rows[1], "step_index": 3}])
    trace = native.project_native_trace("agy", _trace_bytes(rows), prompt=prompt.encode(),
        session_id="exact-session", cwd=tmp_path, answer="answer")
    assert trace["coverage"] == "incomplete"
    assert trace["calls"] == []


@pytest.mark.parametrize("executor", ["claude", "codex", "agy"])
def test_native_trace_projects_only_exact_turn_arguments(executor, tmp_path):
    native = importlib.import_module(f"{SPEC.name}.native_trace")
    prompt, session, answer = b"assigned task", "exact-session", "answer"
    arguments = {"AbsolutePath": "/run/artifacts/sibling/answer.txt"}
    if executor == "claude":
        events = [
            {"type": "user", "sessionId": session, "message": {"content": prompt.decode()}},
            {"type": "assistant", "sessionId": session, "message": {"content": [
                {"type": "thinking", "thinking": "DO NOT COPY"},
                {"type": "tool_use", "id": "call-1", "name": "Read", "input": arguments}]}},
            {"type": "user", "sessionId": session, "message": {"content": [
                {"type": "tool_result", "tool_use_id": "call-1", "content": "DO NOT COPY"}]}},
            {"type": "assistant", "sessionId": session, "message": {"stop_reason": "end_turn",
                "content": [{"type": "text", "text": answer}]}},
            {"type": "user", "sessionId": session, "message": {"content": "later turn"}},
        ]
    elif executor == "agy":
        events = [
            {"type": "USER_INPUT", "step_index": 0, "status": "DONE", "content":
                "<USER_REQUEST>\n/plan assigned task\n</USER_REQUEST>\n<ADDITIONAL_METADATA>native</ADDITIONAL_METADATA>"},
            {"type": "PLANNER_RESPONSE", "step_index": 1, "status": "DONE", "thinking": "DO NOT COPY",
                "tool_calls": [{"name": "view_file", "args": arguments}]},
            {"type": "GENERIC", "step_index": 2, "status": "DONE", "content": "DO NOT COPY"},
            {"type": "PLANNER_RESPONSE", "step_index": 3, "status": "DONE", "content": answer},
            {"type": "USER_INPUT", "step_index": 4, "content": "later turn"},
        ]
    else:
        events = [{"type": "thread.started", "thread_id": session}, {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "call-1", "type": "mcp_tool_call",
                "server": "filesystem", "tool": "read", "arguments": arguments,
                "status": "completed", "result": "DO NOT COPY"}},
            {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": answer}},
            {"type": "turn.completed"}]
    raw = _trace_bytes(events)
    trace = native.project_native_trace(executor, raw, prompt=prompt,
        session_id=session, cwd=tmp_path, answer=answer)
    assert trace["coverage"] == "complete"
    assert trace["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert [call["arguments"] for call in trace["calls"]] == [arguments]
    assert "DO NOT COPY" not in json.dumps(trace)
    assert native.read_boundary_violation(trace, cwd=tmp_path, forbidden=(Path("/run"),),
        allowed_trees=(tmp_path,), allowed_files=())


@pytest.mark.parametrize("failure", ["foreign-session", "foreign-prompt", "missing-terminal", "truncated-arguments"])
def test_native_trace_never_qualifies_missing_or_foreign_evidence(tmp_path, failure):
    native = importlib.import_module(f"{SPEC.name}.native_trace")
    events = [{"type": "thread.started", "thread_id": "other" if failure == "foreign-session" else "exact"},
        {"type": "turn.started"}, {"type": "item.completed", "item": {
            "id": "call", "type": "command_execution", "status": "completed",
            "command": "cat /safe/…" if failure == "truncated-arguments" else "pwd"}},
        {"type": "turn.completed"}]
    if failure == "missing-terminal":
        events.pop()
    if failure == "foreign-prompt":
        trace = native.project_native_trace("agy", _trace_bytes([
            {"type": "USER_INPUT", "step_index": 0, "content": "foreign"},
            {"type": "PLANNER_RESPONSE", "step_index": 1, "content": "answer", "status": "DONE"}]),
            prompt=b"task", session_id="exact", cwd=tmp_path, answer="answer")
    else:
        trace = native.project_native_trace("codex", _trace_bytes(events), prompt=b"task",
            session_id="exact", cwd=tmp_path, answer="answer")
    assert trace["coverage"] != "complete"


def test_agy_capture_uses_full_exact_native_args_when_stdout_is_truncated(tmp_path, monkeypatch):
    native = importlib.import_module(f"{SPEC.name}.native_trace")
    monkeypatch.setenv("HOME", str(tmp_path))
    session = "exact-session"
    transcript = tmp_path / ".gemini/antigravity-cli/brain" / session / ".system_generated/logs/transcript_full.jsonl"
    transcript.parent.mkdir(parents=True)
    command = "printf harmless " + "x" * 2200 + "; cat /run/artifacts/sibling/answer.txt"
    transcript.write_bytes(_trace_bytes([
        {"type": "USER_INPUT", "step_index": 0, "content": "task"},
        {"type": "PLANNER_RESPONSE", "step_index": 1, "status": "DONE", "tool_calls": [
            {"name": "run_command", "args": {"CommandLine": command, "Cwd": str(tmp_path)}}]},
        {"type": "PLANNER_RESPONSE", "step_index": 2, "status": "DONE", "content": "answer"}]))
    request = _request("agy", tmp_path, prompt="task")
    from types import SimpleNamespace
    result = SimpleNamespace(session_id=session, answer="answer", valid=True,
        stdout=b'{"tool_info":{"parameters":"printf harmless xxx...\xe2\x80\xa6"}}',
        launch_metadata={"native_roots": providers.resolve_native_roots().to_dict()})
    trace = native.capture_native_trace(request, result)
    assert trace["coverage"] == "complete"
    assert trace["calls"][0]["arguments"]["CommandLine"] == command
    assert native.read_boundary_violation(trace, cwd=tmp_path, forbidden=(Path("/run"),),
        allowed_trees=(tmp_path,), allowed_files=()) == "1:0"
    result.session_id = "foreign-session"
    assert native.capture_native_trace(request, result)["coverage"] == "unavailable"


def test_unknown_native_tool_block_is_unqualified(tmp_path):
    native = importlib.import_module(f"{SPEC.name}.native_trace")
    raw = _trace_bytes([
        {"type": "user", "sessionId": "exact", "message": {"content": "task"}},
        {"type": "assistant", "sessionId": "exact", "message": {"content": [
            {"type": "unknown_tool_use", "arguments": {"path": "/run/answer.txt"}}]}},
        {"type": "assistant", "sessionId": "exact", "message": {"stop_reason": "end_turn",
            "content": [{"type": "text", "text": "answer"}]}},
    ])
    assert native.project_native_trace("claude", raw, prompt=b"task", session_id="exact",
        cwd=tmp_path, answer="answer")["coverage"] == "incomplete"


@pytest.mark.parametrize("arguments,denied", [
    ({"command": "cat /run/artifacts/sibling/answer.txt"}, True),
    ({"command": "ls /run/artifacts"}, True),
    ({"command": "rg proposal ../sibling"}, True),
    ({"Cwd": "/run/seats/sibling", "CommandLine": "pwd"}, True),
    ({"command": "cat /run/seats/own/README.md"}, False),
    ({"command": "cat /run/artifacts/dependency.txt"}, False),
    ({"command": "cat /run/artifacts-similar/README.md"}, False),
    ({"command": "pwd", "description": "/run/artifacts/sibling/answer.txt"}, False),
])
def test_observed_direct_path_boundary(arguments, denied):
    native = importlib.import_module(f"{SPEC.name}.native_trace")
    trace = {"calls": [{"id": "call", "arguments": arguments}]}
    actual = native.read_boundary_violation(trace, cwd=Path("/run/seats/own"),
        forbidden=(Path("/run/artifacts"), Path("/run/seats")),
        allowed_trees=(Path("/run/seats/own"),), allowed_files=(Path("/run/artifacts/dependency.txt"),))
    assert bool(actual) is denied


FIXTURES = ROOT / "tests" / "fixtures" / "fanout_providers"


@pytest.fixture(autouse=True)
def _pin_fixture_cli_versions(monkeypatch):
    # Deterministic provider tests never invoke installed CLIs.
    versions = {"claude": "2.1.284", "codex": "0.160.0", "agy": "1.2.13"}
    monkeypatch.setattr(providers, "_installed_cli_version", lambda executor: versions[executor])
    monkeypatch.setattr(providers, "_installed_cli_contract", lambda profile: None)


@pytest.fixture
def _mock_clean_codex_host(tmp_path, monkeypatch):
    home = tmp_path / "native-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))


def _result(*, stdout: bytes, stderr: bytes = b"", code: int = 0,
            status: ProcessStatus = ProcessStatus.EXIT) -> ProcessResult:
    return ProcessResult(status=status, returncode=code, stdout=stdout, stderr=stderr)


def _request(executor_id: str, tmp_path: Path, **changes: object) -> ProviderRequest:
    execution_class = changes.get(
        "execution_class", "repo-write" if executor_id == "agy" else "read-only",
    )
    values: dict[str, object] = {
        "executor_id": executor_id,
        "prompt": "secret prompt only in stdin",
        "cwd": tmp_path,
        "execution_class": execution_class,
        "timeout": 3,
        "artifact_store": ArtifactStore(tmp_path / "artifacts"),
        "artifact_prefix": f"turns/{executor_id}",
        "profile": ProviderRegistry.default().select(
            executor_id, execution_class, "standard",
        ),
    }
    values.update(changes)
    return ProviderRequest(**values)


def _fake_provider(request: ProviderRequest, runner, *, registry=None) -> ProviderResult:
    if request.executor_id == "codex" and request.execution_class == "read-only":
        source = registry or ProviderRegistry.default()
        profile = source.select("codex", "read-only", request.profile.quality_tier)
        registry = _fixture_certified_codex_read(source)
        if request.profile.digest == profile.digest:
            request = replace(request, profile=registry.select(*profile.key))
    if request.executor_id == "agy" and request.execution_class == "repo-write":
        registry = _fixture_certified_agy_write(registry)
        request = replace(request, profile=registry.select("agy", "repo-write", "standard"))
    return providers._run_provider_with_runner(request, registry=registry, runner=runner)


def test_public_admitted_turn_without_native_transcript_cannot_be_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    request = _request("claude", tmp_path, run_id="run", session_id="11111111-1111-4111-8111-111111111111")
    stdout = json.dumps({"type": "result", "subtype": "success", "is_error": False,
        "session_id": request.session_id, "result": "transport succeeded"}).encode()
    monkeypatch.setattr(providers, "run_command", lambda _: _result(stdout=stdout))
    result = run_provider(request)
    assert not result.valid
    assert result.reason == "read-trace-unqualified"
    assert result.launch_metadata["native_tool_trace"]["coverage"] == "unavailable"


def _fake_many(requests, runner, *, max_workers: int) -> tuple[ProviderResult, ...]:
    if any(request.executor_id == "agy" and request.execution_class == "repo-write"
           for request in requests):
        registry = _fixture_certified_agy_write()
        requests = tuple(
            replace(request, profile=registry.select("agy", "repo-write", "standard"))
            if request.executor_id == "agy" and request.execution_class == "repo-write"
            else request for request in requests
        )
    else:
        registry = None
    if any(request.executor_id == "codex" and request.execution_class == "read-only"
           for request in requests):
        source = registry or ProviderRegistry.default()
        registry = _fixture_certified_codex_read(source)
        requests = tuple(
            replace(request, profile=registry.select(*request.profile.key))
            if request.executor_id == "codex" and request.execution_class == "read-only"
            and request.profile.digest == source.select(*request.profile.key).digest
            else request for request in requests
        )
    return providers._run_many_with_runner(
        requests, max_workers=max_workers, registry=registry, runner=runner,
    )


def _fixture_certified_codex_read(source) -> ProviderRegistry:
    profiles = tuple(
        replace(profile, characterized=True, profile_sha256=None)
        if profile.executor_id == "codex" and profile.execution_class == "read-only"
        else profile for profile in source._profiles.values()
    )
    return ProviderRegistry(profiles, version_probe=source._version_probe)


def _fixture_certified_agy_write(registry=None) -> ProviderRegistry:
    """Protocol fixtures simulate a certified route; production defaults stay closed."""
    source = registry or ProviderRegistry.default()
    profiles = tuple(
        replace(profile, characterized=True, profile_sha256=None)
        if profile.key == ("agy", "repo-write", "standard") else profile
        for profile in source._profiles.values()
    )
    return ProviderRegistry(profiles, version_probe=source._version_probe)


@pytest.mark.parametrize("executor_id", ("claude", "codex", "agy"))
@pytest.mark.parametrize("resume", (False, True))
def test_repo_write_requires_invoking_owner_before_any_subprocess(
    executor_id: str, resume: bool, tmp_path: Path, monkeypatch,
) -> None:
    launched = []
    monkeypatch.setattr(providers, "_installed_cli_version", lambda _executor: pytest.fail(
        "version probe ran before the repo-write boundary gate",
    ))
    monkeypatch.setattr(providers, "run_command", launched.append)
    request = _request(
        executor_id, tmp_path, execution_class="repo-write", resume=resume,
        session_id=("11111111-1111-4111-8111-111111111111" if executor_id == "claude"
                    else f"exact-{executor_id}-session") if resume else None,
    )

    with pytest.raises(ProviderRequestError, match="invoking owner"):
        run_provider(request)
    assert launched == []




def test_run_many_rejects_mixed_repo_write_batch_before_any_subprocess(
    tmp_path: Path, monkeypatch,
) -> None:
    launched = []
    monkeypatch.setattr(providers, "_installed_cli_version", lambda _executor: pytest.fail(
        "version probe ran before the repo-write batch boundary gate",
    ))
    monkeypatch.setattr(providers, "run_command", launched.append)
    requests = (_request("claude", tmp_path), _request("agy", tmp_path))

    with pytest.raises(ProviderRequestError, match="invoking owner"):
        run_many(requests, max_workers=2)
    assert launched == []


def _synthetic_claude_adc(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    config = home / ".claude"
    config.mkdir(mode=0o700)
    settings = config / "settings.json"
    settings.write_text(json.dumps({"env": {
        "CLAUDE_CODE_USE_VERTEX": "1",
        "ANTHROPIC_VERTEX_PROJECT_ID": "disposable-test-project",
        "CLOUD_ML_REGION": "eu",
    }}))
    settings.chmod(0o600)
    adc = home / ".config" / "gcloud" / "application_default_credentials.json"
    adc.parent.mkdir(parents=True)
    adc.write_text("synthetic credential")
    adc.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    return adc


class _Adapter(ProviderAdapter):
    """Minimal compatible adapter used only to exercise registry admission."""

    def __init__(self, executor_id: str, capabilities: ProviderCapabilities) -> None:
        self.executor_id = executor_id
        self.capabilities = capabilities

    def build_command(self, request):  # pragma: no cover - registry-only test double.
        raise AssertionError(request)

    def parse(self, stdout):  # pragma: no cover - registry-only test double.
        raise AssertionError(stdout)


class _IncompleteAdapter(ProviderAdapter):
    executor_id = "custom"

    def build_command(self, request):  # pragma: no cover - registry-only test double.
        raise AssertionError(request)

    def parse(self, stdout):  # pragma: no cover - registry-only test double.
        raise AssertionError(stdout)


def _profile(executor_id, adapter, capabilities):
    return ExecutorProfile(
        executor_id, adapter, capabilities, "read-only", "standard", "test-model",
        "high", "test-cli-1", ("custom", "--model", "test-model"),
        ("custom", "--resume", "{session_id}", "--model", "test-model"),
        900, 900,
    )


def test_registry_has_only_three_default_executors_and_maka_is_routing_only():
    """Registering Maka as an executor would let an evaluation host spend a seat."""
    registry = ProviderRegistry.default()

    assert registry.executor_ids == ("claude", "codex", "agy")
    assert RoutingTargetRegistry.default().target_ids == ("maka",)
    with pytest.raises(UnsupportedExecutorError, match="maka"):
        registry.require("maka")


def test_new_readonly_profile_binds_installed_codex_without_version_allowlist():
    registry = ProviderRegistry.for_installed(
        ("codex",), version_probe=lambda _executor: "0.160.0",
    )
    profile = registry.admit("codex", "read-only", "standard")
    assert profile.cli_version == "0.160.0"
    registry.assert_installed_version(profile)
    assert not profile.characterized
    assert registry.select("codex", "repo-write", "standard").digest == (
        ProviderRegistry.default().select("codex", "repo-write", "standard").digest
    )


@pytest.mark.parametrize("executor,version", [
    ("claude", "2.2.0"), ("codex", "0.159.0"), ("codex", "0.160.0"),
    ("codex", "0.161.0"), ("agy", "1.3.0"),
])
@pytest.mark.parametrize("tier", ("standard", "deep"))
def test_installed_readonly_admission_preserves_invocation_and_historical_evidence(
    executor, version, tier,
):
    registry = ProviderRegistry.for_installed((executor,), version_probe=lambda _executor: version)
    profile = registry.admit(executor, "read-only", tier)
    template = ProviderRegistry.default().select(executor, "read-only", tier)
    assert profile.cli_version == version
    assert profile.to_dict() == {**template.to_dict(), "cli_version": version, "characterized": False}
    registry.assert_installed_version(profile)
    with pytest.raises(ProviderRequestError, match="characterization"):
        registry.admit(executor, "repo-write", "standard")


def test_installed_snapshot_probes_only_selected_executor():
    calls = []

    def probe(executor):
        calls.append(executor)
        assert executor == "codex"
        return "0.160.0"

    ProviderRegistry.for_installed(("codex",), version_probe=probe)
    assert calls == ["codex"]


@pytest.mark.parametrize("selected", [(), ("codex", "codex"), ("unknown",), ("maka",)])
def test_installed_snapshot_rejects_invalid_selection_before_probing(selected):
    with pytest.raises((ProviderRequestError, UnsupportedExecutorError)):
        ProviderRegistry.for_installed(
            selected, version_probe=lambda _executor: pytest.fail("invalid selection probed a CLI"),
        )


def test_recorded_run_refuses_drift_but_new_run_can_bind_upgrade(tmp_path, monkeypatch):
    current = {"version": "0.160.0"}
    probe = lambda _executor: current["version"]
    old = ProviderRegistry.for_installed(("codex",), version_probe=probe)
    recorded = old.select("codex", "read-only", "deep")
    current["version"] = "0.161.0"
    with pytest.raises(ProviderRequestError, match="recorded CLI version changed"):
        run_provider(_request("codex", tmp_path, execution_class="read-only",
                     profile=recorded, run_id="recorded", inputs_digest="a" * 64),
                     registry=old)
    new = ProviderRegistry.for_installed(("codex",), version_probe=probe)
    assert new.admit("codex", "read-only", "deep").cli_version == "0.161.0"
    assert recorded.cli_version == "0.160.0"


@pytest.mark.parametrize("capability", ("explicit_resume", "stdin_prompt", "structured_output"))
def test_admission_refuses_missing_operation_capability(capability):
    capabilities = replace(ProviderCapabilities(False, True, True), **{capability: False})
    adapter = _Adapter("custom", capabilities)
    registry = ProviderRegistry((_profile("custom", adapter, capabilities),))
    with pytest.raises(ProviderRequestError, match="capabilit"):
        registry.admit("custom", "read-only", "standard")


def test_uncharacterized_custom_adapter_stays_closed():
    capabilities = ProviderCapabilities(False, True, True)
    adapter = _Adapter("custom", capabilities)
    registry = ProviderRegistry((replace(
        _profile("custom", adapter, capabilities), characterized=False, profile_sha256=None,
    ),))
    with pytest.raises(ProviderRequestError, match="characterization"):
        registry.admit("custom", "read-only", "standard")






@pytest.mark.parametrize("executor_id", ["", 1, None])
def test_registry_rejects_non_string_or_empty_profile_ids(executor_id):
    """A non-string registry key defeats the open-ended string executor contract."""
    adapter = _Adapter("custom", ProviderCapabilities(False, True, True))

    with pytest.raises(ProviderRequestError):
        ProviderRegistry((_profile(executor_id, adapter, adapter.capabilities),))


def test_registry_rejects_incompatible_profiles_and_maka_adapter_bypasses():
    """Profile aliases must not smuggle Maka or a different capability surface into seats."""
    capabilities = ProviderCapabilities(False, True, True)
    adapter = _Adapter("custom", capabilities)
    with pytest.raises(ProviderRequestError, match="ProviderAdapter"):
        ProviderRegistry((_profile("custom", object(), capabilities),))
    with pytest.raises(ProviderRequestError, match="capabilities"):
        ProviderRegistry((_profile("custom", _IncompleteAdapter(), capabilities),))
    with pytest.raises(ProviderRequestError, match="match adapter"):
        ProviderRegistry((_profile("other", adapter, capabilities),))
    with pytest.raises(ProviderRequestError, match="capabilities"):
        ProviderRegistry((_profile("custom", adapter, ProviderCapabilities(True, True, True)),))
    maka = _Adapter("maka", capabilities)
    with pytest.raises(UnsupportedExecutorError, match="maka"):
        ProviderRegistry((_profile("maka", maka, capabilities),))
    with pytest.raises(ProviderRequestError, match="match adapter"):
        ProviderRegistry((_profile("not-maka", maka, capabilities),))


@pytest.mark.parametrize("executor_id", ("claude", "codex", "agy"))
@pytest.mark.parametrize("resume", (False, True))
def test_native_commands_pin_explicit_sessions_and_stdin_only(
    executor_id, resume, tmp_path, _mock_clean_codex_host,
):
    session = {"claude": "11111111-1111-4111-8111-111111111111",
               "codex": "codex-thread-1", "agy": "agy-conversation-1"}[executor_id]
    request = _request(executor_id, tmp_path, profile=None, resume=resume,
                       session_id=session if resume or executor_id == "claude" else None)
    command = ProviderRegistry.default().require(executor_id).build_command(request)
    if resume:
        assert session in command.argv
    elif executor_id == "claude":
        assert command.argv[command.argv.index("--session-id") + 1] == session
    assert not {"--continue", "--last", "--ignore-user-config", "--strict-mcp-config"}.intersection(command.argv)
    assert "secret prompt only in stdin" not in repr(command)
    if executor_id == "agy":
        assert json.loads(command.stdin) == {
            "event": "user", "message": {"content": "secret prompt only in stdin"}}
    else:
        assert command.stdin == b"secret prompt only in stdin"


































@pytest.mark.parametrize("execution_class,mode", [
    ("read-only", "plan"),
    ("repo-write", "accept-edits"),
])
@pytest.mark.parametrize("resume", [False, True])
def test_agy_profile_uses_stream_json_stdin_on_initial_and_exact_resume(
    tmp_path, execution_class, mode, resume,
):
    """A bare --print consumes a flag as its prompt; user text belongs in one JSON stdin event."""
    registry = ProviderRegistry.default()
    profile = registry.select("agy", execution_class, "standard")
    prompt = 'Say "héllo"\n世界'
    session_id = "agy-conversation-1" if resume else None
    request = ProviderRequest(
        "agy", prompt, cwd=tmp_path, execution_class=execution_class,
        session_id=session_id, resume=resume, profile=profile,
    )

    command = registry.require("agy").build_command(request)

    expected = ("agy",) + (("--conversation", session_id) if resume else ()) + (
        "--dangerously-skip-permissions", "--mode", mode,
        "--input-format", "stream-json", "--output-format", "stream-json",
        "--model", "gemini-3.8-flash-high", "--effort", "high",
    )
    assert command.argv == expected
    assert "--print" not in command.argv
    assert prompt not in repr(command)
    assert command.stdin.endswith(b"\n")
    assert command.stdin.count(b"\n") == 1
    assert b"\\n" in command.stdin
    assert "héllo".encode("utf-8") in command.stdin
    assert json.loads(command.stdin) == {
        "event": "user", "message": {"content": prompt},
    }


def test_only_agy_commands_enable_the_literal_adc_mode(
    tmp_path, monkeypatch, _mock_clean_codex_host,
):
    """The parent auth-mode switch must reach agy without reaching other executors."""
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    registry = ProviderRegistry.default()

    for resume in (False, True):
        command = registry.require("agy").build_command(ProviderRequest(
            "agy", "question", cwd=tmp_path, execution_class="repo-write",
            session_id="agy-conversation-1" if resume else None, resume=resume,
            profile=registry.select("agy", "repo-write", "standard"),
        ))
        assert command.environment == {"AGY_ADC_AUTH": "true"}
        assert "AGY_ADC_AUTH" not in command.argv
    for executor, session_id in (
        ("claude", "11111111-1111-4111-8111-111111111111"),
        ("codex", None),
    ):
        command = registry.require(executor).build_command(ProviderRequest(
            executor, "question", cwd=tmp_path, session_id=session_id,
            execution_class="repo-write" if executor == "agy" else "read-only",
            profile=registry.select(
                executor, "repo-write" if executor == "agy" else "read-only", "standard",
            ),
        ))
        assert "AGY_ADC_AUTH" not in command.environment




@pytest.mark.parametrize("value", [None, "false", "TRUE", "key-sentinel"])
def test_agy_uses_default_auth_when_adc_mode_is_not_literal_true(tmp_path, monkeypatch, value):
    """Unknown parent values cannot become child auth settings."""
    if value is None:
        monkeypatch.delenv("AGY_ADC_AUTH", raising=False)
    else:
        monkeypatch.setenv("AGY_ADC_AUTH", value)
    registry = ProviderRegistry.default()

    command = registry.require("agy").build_command(ProviderRequest(
        "agy", "question", cwd=tmp_path, execution_class="repo-write",
        profile=registry.select("agy", "repo-write", "standard"),
    ))

    assert command.environment == {}


@pytest.mark.parametrize("resume", [False, True])
def test_claude_native_command_passes_private_default_adc_path(tmp_path, monkeypatch, resume):
    """The installed CLI owns its route; the adapter forwards only eligible ADC."""
    adc = _synthetic_claude_adc(tmp_path, monkeypatch)
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_QUOTA_PROJECT", "ambient-quota")
    monkeypatch.setenv("CLAUDE_MEM_TOKEN", "ambient-memory-token")
    registry = ProviderRegistry.default()
    request = ProviderRequest(
        "claude", "question", cwd=tmp_path,
        session_id="11111111-1111-4111-8111-111111111111", resume=resume,
        profile=registry.select("claude", "read-only", "standard"),
    )

    command = registry.require("claude").build_command(request)

    assert command.environment == {
        "GOOGLE_APPLICATION_CREDENTIALS": str(adc),
    }
    assert "synthetic credential" not in repr(command)


@pytest.mark.parametrize("state", ["missing", "public", "symlink", "directory"])
def test_claude_native_command_does_not_inject_ineligible_adc(tmp_path, monkeypatch, state):
    adc = _synthetic_claude_adc(tmp_path, monkeypatch)
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    if state == "missing":
        adc.unlink()
    elif state == "public":
        adc.chmod(0o644)
    elif state == "symlink":
        target = tmp_path / "target.json"
        target.write_text("synthetic credential")
        target.chmod(0o600)
        adc.unlink()
        adc.symlink_to(target)
    else:
        adc.unlink()
        adc.mkdir()
    registry = ProviderRegistry.default()
    request = ProviderRequest(
        "claude", "question", cwd=tmp_path,
        profile=registry.select("claude", "read-only", "standard"),
    )

    assert registry.require("claude").build_command(request).environment == {}


@pytest.mark.parametrize("auth_mode", [None, "false", "TRUE"])
@pytest.mark.parametrize("resume", [False, True])
def test_claude_default_adc_requires_literal_opt_in(tmp_path, monkeypatch, auth_mode, resume):
    """A private default file alone does not authorize forwarding its path."""
    _synthetic_claude_adc(tmp_path, monkeypatch)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    if auth_mode is None:
        monkeypatch.delenv("AGY_ADC_AUTH", raising=False)
    else:
        monkeypatch.setenv("AGY_ADC_AUTH", auth_mode)
    registry = ProviderRegistry.default()
    request = ProviderRequest(
        "claude", "question", cwd=tmp_path,
        session_id="11111111-1111-4111-8111-111111111111", resume=resume,
        profile=registry.select("claude", "read-only", "standard"),
    )

    assert registry.require("claude").build_command(request).environment == {}


@pytest.mark.parametrize("explicit", ["", "/explicit/credentials.json"])
@pytest.mark.parametrize("resume", [False, True])
def test_claude_native_command_does_not_replace_explicit_parent_choice(
    tmp_path, monkeypatch, explicit, resume,
):
    """The adapter must not replace the installed CLI's explicit parent choice."""
    _synthetic_claude_adc(tmp_path, monkeypatch)
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", explicit)
    registry = ProviderRegistry.default()
    request = ProviderRequest(
        "claude", "question", cwd=tmp_path,
        session_id="11111111-1111-4111-8111-111111111111", resume=resume,
        profile=registry.select("claude", "read-only", "standard"),
    )

    assert registry.require("claude").build_command(request).environment == {}




def test_agy_stream_result_preserves_exact_identity_usage_and_unobserved_model(tmp_path):
    """The init model repeats the requested override; it is not independent model evidence."""
    stdout = (
        b'{"event":"init","conversation_id":"agy-conversation-1","init":{"cwd":"/tmp",'
        b'"tools":[],"permission_mode":"always-proceed","model":"gemini-3.8-flash-high"}}\n'
        b'{"event":"step_update","step_update":{"conversation_id":"agy-conversation-1",'
        b'"step_index":0,"state":"DONE","step_type":"agent_response","text_delta":"READY"}}\n'
        b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
        b'"status":"SUCCESS","response":"READY","duration_seconds":1.0,"num_turns":1,'
        b'"usage":{"input_tokens":17,"output_tokens":9,"thinking_tokens":2,'
        b'"cache_read_tokens":4,"total_tokens":32}}}\n'
    )
    registry = ProviderRegistry.default()

    for resume in (False, True):
        result = _fake_provider(ProviderRequest(
            "agy", "reply READY", cwd=tmp_path, execution_class="repo-write",
            session_id="agy-conversation-1" if resume else None, resume=resume,
            profile=registry.select("agy", "repo-write", "standard"),
        ), lambda _command: _result(stdout=stdout), registry=registry)

        assert result.valid
        assert result.session_id == "agy-conversation-1"
        assert result.answer == "READY"
        assert result.usage == {
            "input": 17, "output": 9, "thinking": 2, "cache_read": 4, "total": 32,
        }
        assert result.requested_model == "gemini-3.8-flash-high"
        assert result.observed_model is None
        assert result.stdout == stdout


def test_codex_stream_uses_final_agent_message_not_intermediate_commentary():
    stdout = (
        b'{"type":"thread.started","thread_id":"codex-thread-1"}\n'
        b'{"type":"item.completed","item":{"type":"agent_message",'
        b'"text":"I will read the source and return JSON."}}\n'
        b'{"type":"item.completed","item":{"type":"agent_message",'
        b'"text":"{\\"fact\\":\\"verified\\"}"}}\n'
        b'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}\n'
    )

    parsed = ProviderRegistry.default().require("codex").parse(stdout)

    assert parsed.answer == '{"fact":"verified"}'


@pytest.mark.parametrize("status,code,complete,valid", [
    (ProcessStatus.OUTPUT_LIMIT, 0, True, False),
    (ProcessStatus.EXIT, 0, False, False),
    (ProcessStatus.EXIT, None, True, False),
    (ProcessStatus.EXIT, 0, True, True),
])
def test_provider_success_requires_complete_zero_exit(tmp_path, status, code, complete, valid):
    stdout = (FIXTURES / "claude-success.json").read_bytes()
    request = _request("claude", tmp_path, session_id=json.loads(stdout)["session_id"])
    result = _fake_provider(request, lambda _: ProcessResult(status, code, stdout, b"failure capture",
                                                            capture_complete=complete))
    assert result.valid is valid
    assert result.stdout == stdout and result.stderr == b"failure capture"
    assert request.artifact_store.read_bytes(result.stdout_ref) == stdout


@pytest.mark.parametrize("status,execution_class,valid", [
    ("completed", "read-only", False), ("failed", "read-only", True),
    ("completed", "repo-write", True),
])
def test_codex_successful_file_change_requires_write_class(tmp_path, _mock_clean_codex_host,
                                                         status, execution_class, valid):
    events = [
        {"type": "thread.started", "thread_id": "codex-thread-1"},
        {"type": "item.completed", "item": {"id": "item_1", "type": "file_change",
            "changes": [{"path": "shared/example.py", "kind": "update"}], "status": status}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "command_execution",
            "command": "ls", "aggregated_output": "example.py", "exit_code": 0, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_3", "type": "mcp_tool_call",
            "server": "test", "tool": "read", "arguments": {}, "result": None,
            "error": {"message": "unavailable"}, "status": "failed"}},
        {"type": "item.completed", "item": {"id": "item_4", "type": "agent_message", "text": "READY"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    stdout = b"\n".join(json.dumps(event).encode() for event in events) + b"\n"
    registry = ProviderRegistry.default()
    if execution_class == "repo-write":
        registry = ProviderRegistry(tuple(replace(profile, characterized=True, profile_sha256=None)
            if profile.executor_id == "codex" and profile.execution_class == "repo-write" else profile
            for profile in registry._profiles.values()))
    request = _request("codex", tmp_path, execution_class=execution_class,
                       profile=registry.select("codex", execution_class, "standard"))
    result = _fake_provider(request, lambda _: _result(stdout=stdout), registry=registry)
    assert result.valid is valid
    assert result.stdout == stdout


@pytest.mark.parametrize("tail", [
    {"type": "item.completed", "item": {"type": "agent_message", "text": "replacement"}},
    {"type": "turn.completed", "usage": {}},
    {"type": "turn.failed", "error": {"message": "late failure"}},
])
def test_codex_terminal_cannot_be_overwritten(tail):
    events = [{"type": "thread.started", "thread_id": "thread-1"},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "READY"}},
              {"type": "turn.completed", "model": "mismatched-model", "usage": {}}, tail]
    with pytest.raises(ProviderProtocolError):
        providers.CodexAdapter().parse(b"\n".join(json.dumps(event).encode() for event in events))


def test_known_unstarted_retry_rechecks_cli_version_without_second_launch(tmp_path):
    version = ["2.1.284"]
    registry = ProviderRegistry.default(version_probe=lambda _: version[0])
    launched = []
    def unstarted(command):
        launched.append(command)
        version[0] = "2.2.0"
        return ProcessResult(ProcessStatus.SPAWN_ERROR, None, b"", b"", error="not started")
    with pytest.raises(ProviderRequestError, match="recorded CLI version changed"):
        providers._run_provider_with_runner(_request("claude", tmp_path, retries=1,
                                            run_id="recorded", inputs_digest="a" * 64),
                                            registry=registry, runner=unstarted)
    assert len(launched) == 1


@pytest.mark.parametrize("executor", ["claude", "codex", "agy"])
def test_ordinary_provider_commands_share_finite_artifact_capture_cap(tmp_path, executor,
                                                                    _mock_clean_codex_host):
    command = ProviderRegistry.default().require(executor).build_command(_request(executor, tmp_path))
    assert command.max_output_bytes == 64 * 1024 * 1024


def test_codex_empty_final_message_does_not_promote_intermediate_commentary():
    stdout = (
        b'{"type":"thread.started","thread_id":"codex-thread-1"}\n'
        b'{"type":"item.completed","item":{"type":"agent_message",'
        b'"text":"I will answer shortly."}}\n'
        b'{"type":"item.completed","item":{"type":"agent_message","text":""}}\n'
        b'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}\n'
    )

    parsed = ProviderRegistry.default().require("codex").parse(stdout)

    assert parsed.answer == ""


@pytest.mark.parametrize("stdout", [
    b'{"conversation_id":"agy-conversation-1","status":"SUCCESS","response":"legacy"}',
    b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n',
    (b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n'
     b'{"event":"step_update","step_update":{"conversation_id":"agy-conversation-2",'
     b'"step_index":0,"state":"DONE","step_type":"agent_response"}}\n'
     b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
     b'"status":"SUCCESS","response":"wrong step"}}\n'),
    (b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n'
     b'{"event":"result","result":{"conversation_id":"agy-conversation-2",'
     b'"status":"SUCCESS","response":"wrong session"}}\n'),
    (b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n'
     b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
     b'"status":"RUNNING","response":"unfinished"}}\n'),
    (b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n'
     b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
     b'"status":"SUCCESS","response":"first"}}\n'
     b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
     b'"status":"SUCCESS","response":"second"}}\n'),
    (b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n'
     b'not json\n'
     b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
     b'"status":"SUCCESS","response":"ignored"}}\n'),
    (b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n\n'
     b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
     b'"status":"SUCCESS","response":"ignored"}}\n'),
])
def test_agy_stream_rejects_incomplete_mismatched_or_malformed_terminal_events(stdout):
    """A partial or contradictory stream cannot establish a resumable result."""
    with pytest.raises(ProviderProtocolError):
        ProviderRegistry.default().require("agy").parse(stdout)


def test_agy_result_only_error_keeps_provider_diagnostic(tmp_path):
    """The CLI can fail before init (for example, a rejected model override)."""
    stdout = (b'{"event":"result","result":{"conversation_id":"",'
              b'"status":"ERROR","response":"","error":"invalid model selection"}}\n')
    result = _fake_provider(_request("agy", tmp_path),
                            lambda _command: _result(stdout=stdout, code=1))

    assert not result.valid
    assert result.reason == "provider-error"
    assert result.hint == "invalid model selection"


def test_agy_success_with_error_field_fails_closed(tmp_path):
    """A contradictory terminal result is not evidence of a successful turn."""
    stdout = (
        b'{"event":"init","conversation_id":"agy-conversation-1","init":{}}\n'
        b'{"event":"result","result":{"conversation_id":"agy-conversation-1",'
        b'"status":"SUCCESS","response":"READY","error":"provider failed"}}\n'
    )
    result = _fake_provider(_request("agy", tmp_path), lambda _command: _result(stdout=stdout))

    assert not result.valid
    assert result.reason == "protocol-error"
    assert result.stdout == stdout


def test_agy_stream_keeps_unicode_separators_inside_a_crlf_delimited_result():
    """U+2028 and U+2029 inside JSON strings are content, not NDJSON boundaries."""
    stdout = (
        '{"event":"init","conversation_id":"agy-conversation-1","init":{}}\r\n'
        '{"event":"result","result":{"conversation_id":"agy-conversation-1",'
        '"status":"SUCCESS","response":"north\u2028south\u2029end"}}\r\n'
    ).encode("utf-8")

    parsed = ProviderRegistry.default().require("agy").parse(stdout)

    assert parsed.final
    assert parsed.session_id == "agy-conversation-1"
    assert parsed.answer == "north\u2028south\u2029end"


def test_adapters_reject_an_unusable_explicit_session_identity(tmp_path):
    """Treating arbitrary text as a fresh Claude UUID or ignored Codex ID breaks exact resume."""
    with pytest.raises(ProviderRequestError, match="UUID"):
        ProviderRegistry.default().require("claude").build_command(
            _request("claude", tmp_path, session_id="not-a-uuid", profile=None),
        )
    with pytest.raises(ProviderRequestError, match="only valid when resuming"):
        ProviderRegistry.default().require("codex").build_command(
            _request("codex", tmp_path, session_id="thread-that-would-be-ignored", profile=None),
        )


@pytest.mark.parametrize(
    ("executor_id", "fixture", "session_id", "answer", "usage"),
    [
        ("claude", "claude-success.json", "11111111-1111-4111-8111-111111111111", "Claude answer", {"input": 11, "output": 7, "cache_read": 3, "cache_write": 2, "cost_usd": 0.12}),
        ("codex", "codex-success.ndjson", "codex-thread-1", "Codex answer", {"input": 13, "output": 8, "cache_read": 5}),
        ("agy", "agy-success.json", "agy-conversation-1", "agy answer", {"input": 17, "output": 9, "thinking": 2, "cache_read": 4, "total": 32}),
    ],
)
def test_run_provider_parses_fixture_usage_identity_and_raw_artifacts(
    executor_id, fixture, session_id, answer, usage, tmp_path,
    _mock_clean_codex_host,
):
    """Dropping final envelopes or raw bytes would make a settled turn unverifiable."""
    stdout = (FIXTURES / fixture).read_bytes()
    seen = []

    def fake_run(command):
        seen.append(command)
        return _result(stdout=stdout)

    request = _request(
        executor_id,
        tmp_path,
        session_id=session_id if executor_id == "claude" else None,
    )

    result = _fake_provider(request, fake_run)

    assert result.valid is True
    assert result.session_id == session_id
    assert result.answer == answer
    assert result.usage == usage
    assert result.attempt_count == 1
    assert result.answer_ref and result.stdout_ref and result.stderr_ref
    assert request.artifact_store.read_bytes(result.stdout_ref) == stdout
    assert result.stdout_digest == hashlib.sha256(stdout).hexdigest()
    if executor_id == "agy":
        assert json.loads(seen[0].stdin) == {
            "event": "user", "message": {"content": "secret prompt only in stdin"},
        }
    else:
        assert seen[0].stdin == b"secret prompt only in stdin"


@pytest.fixture
def codex_model_metadata_warning_events():
    events = [json.loads(line) for line in (FIXTURES / "codex-success.ndjson").read_bytes().splitlines()]
    events.insert(1, {
        "type": "item.completed",
        "item": {
            "type": "error",
            "message": "Model metadata for `gpt-6.1-sol` not found. Defaulting to fallback metadata; this can degrade performance and cause issues.",
        },
    })
    return events


def test_codex_model_metadata_warning_preserves_success_and_raw_evidence(
    tmp_path, _mock_clean_codex_host, codex_model_metadata_warning_events,
):
    stdout = b"\n".join(json.dumps(event).encode() for event in codex_model_metadata_warning_events)
    request = _request("codex", tmp_path)

    result = _fake_provider(request, lambda _command: _result(stdout=stdout))

    assert result.valid is True
    assert result.reason == "ok"
    assert result.hint is None
    assert result.session_id == "codex-thread-1"
    assert result.answer == "Codex answer"
    assert result.usage == {"input": 13, "output": 8, "cache_read": 5}
    assert result.observed_model is None
    assert result.stdout == stdout
    assert request.artifact_store.read_bytes(result.stdout_ref) == stdout
    assert result.stdout_digest == hashlib.sha256(stdout).hexdigest()


@pytest.mark.parametrize("message", [
    "quota exhausted",
    "model rerouted: gpt-6.1-sol -> another-model",
    "Model metadata for `gpt-6.1-sol` not found.",
    "prefix Model metadata for `gpt-6.1-sol` not found. Defaulting to fallback metadata; this can degrade performance and cause issues.",
    "Model metadata for `gpt-6.1-sol` not found. Defaulting to fallback metadata; this can degrade performance and cause issues. suffix",
    "Model metadata for `` not found. Defaulting to fallback metadata; this can degrade performance and cause issues.",
    "Model metadata for `gpt-6.1-sol\nother` not found. Defaulting to fallback metadata; this can degrade performance and cause issues.",
    "Model metadata for `gpt-6.1-sol` not found. Defaulting to fallback metadata; this can degrade performance and cause issues.\n",
    None,
    7,
])
def test_codex_model_metadata_warning_does_not_accept_other_item_errors(
    message, tmp_path, _mock_clean_codex_host, codex_model_metadata_warning_events,
):
    events = codex_model_metadata_warning_events
    events[1]["item"]["message"] = message
    stdout = b"\n".join(json.dumps(event).encode() for event in events)

    result = _fake_provider(_request("codex", tmp_path), lambda _command: _result(stdout=stdout))

    assert result.valid is False
    assert result.reason == "provider-error"


@pytest.mark.parametrize("error_kind", ["item.completed", "error", "turn.failed"])
@pytest.mark.parametrize("before_warning", [True, False])
def test_codex_model_metadata_warning_cannot_hide_fatal_events(
    error_kind, before_warning, tmp_path, _mock_clean_codex_host,
    codex_model_metadata_warning_events,
):
    events = codex_model_metadata_warning_events
    message = "quota exhausted" if error_kind == "item.completed" else events[1]["item"]["message"]
    fatal = {"type": error_kind}
    if error_kind == "item.completed":
        fatal["item"] = {"type": "error", "message": message}
    elif error_kind == "turn.failed":
        fatal["error"] = {"message": message}
    else:
        fatal["message"] = message
    events.insert(1 if before_warning else 2, fatal)
    stdout = b"\n".join(json.dumps(event).encode() for event in events)

    result = _fake_provider(_request("codex", tmp_path), lambda _command: _result(stdout=stdout))

    assert result.valid is False
    assert result.reason == "provider-error"
    assert result.hint == message


@pytest.mark.parametrize("guard,reason", [
    ("missing-session", "protocol-error"),
    ("conflicting-session", "protocol-error"),
    ("missing-terminal", "protocol-error"),
    ("missing-answer", "protocol-error"),
    ("model-mismatch", "model-drift"),
    ("process-exit", "process-error"),
    ("timeout", "timeout"),
    ("spawn-error", "spawn-error"),
    ("closed-artifacts", "artifact-storage-error"),
])
def test_codex_model_metadata_warning_preserves_failure_guards(
    guard, reason, tmp_path, _mock_clean_codex_host, codex_model_metadata_warning_events,
):
    events = codex_model_metadata_warning_events
    request = _request("codex", tmp_path)
    status, code = ProcessStatus.EXIT, 0
    if guard == "missing-session":
        events.pop(0)
    elif guard == "conflicting-session":
        events.insert(1, {"type": "thread.started", "thread_id": "other-thread"})
    elif guard == "missing-terminal":
        events.pop()
    elif guard == "missing-answer":
        events.pop(2)
    elif guard == "model-mismatch":
        events[-1]["model"] = "another-model"
    elif guard == "process-exit":
        code = 1
    elif guard == "timeout":
        status = ProcessStatus.TIMEOUT
    elif guard == "spawn-error":
        status = ProcessStatus.SPAWN_ERROR
    elif guard == "closed-artifacts":
        request.artifact_store.close()
    stdout = b"\n".join(json.dumps(event).encode() for event in events)

    result = _fake_provider(request, lambda _command: _result(stdout=stdout, status=status, code=code))

    assert result.valid is False
    assert result.reason == reason


def test_structured_error_precedes_nonzero_exit_and_preserves_provider_identity(
    tmp_path, _mock_clean_codex_host,
):
    """Replacing a provider error with a generic exit code loses the actionable cause."""
    result = _fake_provider(
        _request("codex", tmp_path),
        lambda _command: _result(
            stdout=(FIXTURES / "codex-error.ndjson").read_bytes(),
            stderr=b"generic launcher failure",
            code=1,
        ),
    )

    assert result.valid is False
    assert result.reason == "provider-error"
    assert result.session_id == "codex-thread-1"
    assert "quota exhausted" in (result.hint or "")


def test_malformed_or_missing_session_or_final_result_fails_closed(
    tmp_path, _mock_clean_codex_host,
):
    """Accepting a fragment without identity or a terminal event permits unsafe resume."""
    result = _fake_provider(
        _request("codex", tmp_path),
        lambda _command: _result(stdout=b'{"type":"item.completed","item":{"type":"agent_message","text":"partial"}}\n'),
    )

    assert result.valid is False
    assert result.reason == "protocol-error"
    assert result.session_id is None
    with pytest.raises(ProviderProtocolError):
        ProviderRegistry.default().require("claude").parse(b"not json")


@pytest.mark.parametrize("envelope", [
    {"subtype": "success"},
    {"type": "message", "subtype": "success"},
    {"type": "result", "subtype": "failure"},
])
def test_claude_requires_the_documented_terminal_success_discriminators(tmp_path, envelope):
    """A result-looking fragment without Claude's terminal success shape is not resumable evidence."""
    envelope.update({
        "session_id": "11111111-1111-4111-8111-111111111111",
        "result": "answer",
        "is_error": False,
    })
    result = _fake_provider(_request(
        "claude", tmp_path, session_id="11111111-1111-4111-8111-111111111111",
    ), lambda _command: _result(stdout=json.dumps(envelope).encode("utf-8")))

    assert result.valid is False
    assert result.reason == "protocol-error"


@pytest.mark.parametrize("executor_id, fixture", [
    ("claude", "claude-success.json"),
    ("codex", "codex-error.ndjson"),
    ("agy", "agy-error.json"),
])
def test_each_provider_structured_error_precedes_nonzero_process_exit(
    executor_id, fixture, tmp_path, _mock_clean_codex_host,
):
    """An authoritative provider envelope must outrank generic child exit classification."""
    runner = lambda _command: _result(
        stdout=(FIXTURES / fixture).read_bytes(), code=1,
    )
    request = _request(
        executor_id,
        tmp_path,
        session_id="11111111-1111-4111-8111-111111111111" if executor_id == "claude" else None,
    )
    if executor_id == "claude":
        runner = lambda _command: _result(
            stdout=b'{"type":"result","subtype":"success","is_error":true,"session_id":"11111111-1111-4111-8111-111111111111","result":"quota exhausted"}', code=1,
        )

    result = _fake_provider(request, runner)

    assert result.reason == "provider-error"


@pytest.mark.parametrize("executor,fixture", [
    ("claude", "claude-success.json"),
    ("codex", "codex-success.ndjson"),
    ("agy", "agy-success.json"),
])
def test_harmless_additive_provider_metadata_preserves_success(executor, fixture):
    events = [json.loads(line) for line in (FIXTURES / fixture).read_bytes().splitlines()]
    for event in events:
        event["future_metadata"] = {"progress_schema": 7}
    parsed = ProviderRegistry.default().require(executor).parse(
        b"\n".join(json.dumps(event).encode() for event in events),
    )
    assert parsed.answer
    assert parsed.final
    assert parsed.error is None


@pytest.mark.parametrize("executor,fixture", [
    ("claude", "claude-success.json"),
    ("codex", "codex-error.ndjson"),
    ("agy", "agy-error.json"),
])
def test_harmless_additive_provider_metadata_cannot_hide_errors(
    executor, fixture, tmp_path, _mock_clean_codex_host,
):
    events = [json.loads(line) for line in (FIXTURES / fixture).read_bytes().splitlines()]
    if executor == "claude":
        events[0].update(is_error=True, result="quota exhausted")
    for event in events:
        event["future_metadata"] = {"progress_schema": 7}
    stdout = b"\n".join(json.dumps(event).encode() for event in events)
    result = _fake_provider(_request(executor, tmp_path), lambda _command: _result(stdout=stdout, code=1))
    assert not result.valid
    assert result.reason == "provider-error"


def test_provider_result_freezes_usage_and_redacts_prompt_derived_values():
    """Frozen dataclass fields alone would still expose mutable usage or answer text."""
    usage = {"input": 3}
    result = ProviderResult(
        executor_id="custom",
        valid=True,
        reason="ok",
        hint=None,
        attempt_count=1,
        duration=0,
        usage=usage,
        session_id="s",
        answer="secret prompt only in stdin",
        stdout=b"secret prompt only in stdin",
        stderr=b"secret prompt only in stdin",
    )
    usage["input"] = 9

    assert result.usage == {"input": 3}
    assert isinstance(result.usage, MappingProxyType)
    with pytest.raises(TypeError):
        result.usage["input"] = 9  # type: ignore[index]
    assert "secret prompt only in stdin" not in repr(result)
    with pytest.raises(ProviderRequestError):
        ProviderResult(
            executor_id="custom", valid=True, reason="ok", hint=None, attempt_count=1,
            duration=0, usage={"nested": {}}, session_id="s", answer="", stdout=b"", stderr=b"",
        )


def test_retry_is_limited_to_known_pre_start_spawn_failure_and_attempts_are_visible(tmp_path):
    """Retrying after a started turn can duplicate spend; spawn failure cannot."""
    calls = []
    success = (FIXTURES / "agy-success.json").read_bytes()

    def fake_run(_command):
        calls.append(1)
        if len(calls) == 1:
            return _result(status=ProcessStatus.SPAWN_ERROR, stdout=b"", stderr=b"missing")
        return _result(stdout=success)

    result = _fake_provider(_request("agy", tmp_path, retries=1), fake_run)

    assert result.valid is True
    assert result.attempt_count == 2
    assert len(calls) == 2


def test_known_started_failure_is_not_retried(
    tmp_path, _mock_clean_codex_host,
):
    """A thread-start event is evidence that repeating the request could charge twice."""
    calls = []

    def fake_run(_command):
        calls.append(1)
        return _result(stdout=(FIXTURES / "codex-error.ndjson").read_bytes(), code=1)

    result = _fake_provider(_request("codex", tmp_path, retries=3), fake_run)

    assert result.reason == "provider-error"
    assert result.attempt_count == 1
    assert len(calls) == 1


def test_claude_assigns_one_session_id_before_a_safe_pre_start_retry(tmp_path):
    """Changing Claude's caller-assigned UUID between attempts would lose exact provenance."""
    seen = []

    def fake_run(command):
        seen.append(command)
        if len(seen) == 1:
            return _result(status=ProcessStatus.SPAWN_ERROR, stdout=b"", stderr=b"missing")
        return _result(stdout=(FIXTURES / "claude-success.json").read_bytes())

    _fake_provider(_request("claude", tmp_path, retries=1), fake_run)

    assert seen[0].argv[seen[0].argv.index("--session-id") + 1] == seen[1].argv[
        seen[1].argv.index("--session-id") + 1
    ]


def test_structured_error_precedes_a_timeout_when_stdout_contains_provider_evidence(tmp_path):
    """A timeout category cannot erase an authoritative provider error it already emitted."""
    result = _fake_provider(
        _request("agy", tmp_path),
        lambda _command: _result(
            stdout=(FIXTURES / "agy-error.json").read_bytes(), status=ProcessStatus.TIMEOUT,
        ),
    )

    assert result.reason == "provider-error"
    assert "quota exhausted" in (result.hint or "")


def test_run_many_is_input_ordered_bounded_and_retains_partial_failures(
    tmp_path, _mock_clean_codex_host,
):
    """Completion order must not change seat order or erase a failed provider result."""
    active = 0
    peak = 0

    def fake_run(command):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            time.sleep(0.02 if command.argv[0] == "claude" else 0)
            if command.argv[0] == "codex":
                return _result(stdout=(FIXTURES / "codex-error.ndjson").read_bytes(), code=1)
            if command.argv[0] == "claude":
                return _result(stdout=(FIXTURES / "claude-success.json").read_bytes())
            return _result(stdout=(FIXTURES / "agy-success.json").read_bytes())
        finally:
            active -= 1

    requests = [
        _request(
            name,
            tmp_path,
            artifact_prefix=f"batch/{name}",
            session_id="11111111-1111-4111-8111-111111111111" if name == "claude" else None,
        )
        for name in ("claude", "codex", "agy")
    ]

    results = _fake_many(requests, fake_run, max_workers=2)

    assert [result.executor_id for result in results] == ["claude", "codex", "agy"]
    assert [result.valid for result in results] == [True, False, True]
    assert peak <= 2


def test_artifact_storage_failure_returns_terminal_result_with_partial_refs(tmp_path, monkeypatch):
    """A durable-store failure must retain the process evidence instead of raising it away."""
    store = ArtifactStore(tmp_path / "store")
    original_write = store.write_bytes
    calls = 0

    def fail_after_answer(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ArtifactQuotaError("simulated quota")
        return original_write(path, data)

    monkeypatch.setattr(store, "write_bytes", fail_after_answer)
    result = _fake_provider(
        _request("agy", tmp_path, artifact_store=store),
        lambda _command: _result(stdout=(FIXTURES / "agy-success.json").read_bytes()),
    )

    assert result.valid is False
    assert result.reason == "artifact-storage-error"
    assert result.answer_ref is not None
    assert result.stdout_ref is result.stderr_ref is None
    assert result.session_id == "agy-conversation-1"
    assert result.usage == {"input": 17, "output": 9, "thinking": 2, "cache_read": 4, "total": 32}
    assert result.stdout_digest == hashlib.sha256((FIXTURES / "agy-success.json").read_bytes()).hexdigest()
    assert "simulated quota" not in (result.hint or "")


def test_closed_artifact_store_becomes_terminal_evidence_not_an_exception(tmp_path, monkeypatch):
    """A store closed after dispatch cannot erase an otherwise settled provider turn."""
    store = ArtifactStore(tmp_path / "closed-store")
    store.close()
    stdout = (FIXTURES / "agy-success.json").read_bytes()
    result = _fake_provider(_request("agy", tmp_path, artifact_store=store),
                            lambda _command: _result(stdout=stdout))

    assert result.valid is False
    assert result.reason == "artifact-storage-error"
    assert result.answer_ref is result.stdout_ref is result.stderr_ref is None
    assert result.attempt_count == 1
    assert result.session_id == "agy-conversation-1"
    assert result.usage == {"input": 17, "output": 9, "thinking": 2, "cache_read": 4, "total": 32}
    assert result.stdout == stdout
    assert result.stdout_digest == hashlib.sha256(stdout).hexdigest()
    assert "artifact store is closed" not in (result.hint or "")


def test_raw_artifact_io_failure_returns_partial_refs_and_raw_digests(tmp_path, monkeypatch):
    """A raw fsync/write error after one durable artifact must retain the partial evidence."""
    store = ArtifactStore(tmp_path / "store")
    original_write = store.write_bytes
    calls = 0

    def fail_after_answer(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated fsync failure")
        return original_write(path, data)

    monkeypatch.setattr(store, "write_bytes", fail_after_answer)
    stdout = (FIXTURES / "agy-success.json").read_bytes()
    result = _fake_provider(_request("agy", tmp_path, artifact_store=store),
                            lambda _command: _result(stdout=stdout))

    assert result.reason == "artifact-storage-error"
    assert result.answer_ref is not None
    assert result.stdout_ref is result.stderr_ref is None
    assert result.stdout_digest == hashlib.sha256(stdout).hexdigest()
    assert "simulated fsync failure" not in (result.hint or "")


def test_run_many_keeps_duplicate_artifact_prefix_as_an_ordered_storage_failure(tmp_path):
    """One colliding seat must not make the batch discard other completed evidence."""
    store = ArtifactStore(tmp_path / "store")
    requests = [
        _request("agy", tmp_path, artifact_store=store, artifact_prefix="same")
        for _ in range(2)
    ]

    results = _fake_many(requests,
                         lambda _command: _result(stdout=(FIXTURES / "agy-success.json").read_bytes()),
                         max_workers=1)

    assert [result.reason for result in results] == ["ok", "artifact-storage-error"]
    assert results[1].stdout_digest == hashlib.sha256((FIXTURES / "agy-success.json").read_bytes()).hexdigest()


def test_run_many_preserves_order_when_a_closed_store_fails_after_dispatch(tmp_path):
    """One closed output store must not prevent a later healthy seat from being reported."""
    closed = ArtifactStore(tmp_path / "closed-store")
    closed.close()
    healthy = ArtifactStore(tmp_path / "healthy-store")
    requests = (
        _request("agy", tmp_path, artifact_store=closed, artifact_prefix="closed"),
        _request("agy", tmp_path, artifact_store=healthy, artifact_prefix="healthy"),
    )

    results = _fake_many(requests,
                         lambda _command: _result(stdout=(FIXTURES / "agy-success.json").read_bytes()),
                         max_workers=1)

    assert [result.reason for result in results] == ["artifact-storage-error", "ok"]
    assert results[0].session_id == results[1].session_id == "agy-conversation-1"
