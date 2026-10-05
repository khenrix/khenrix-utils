from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MEMORY_ROOT = ROOT / "components" / "memory"

# The child records the actual hook boundary, including any transcript the
# upstream adapter could read. It never starts memory or contacts a provider.
WORKER = """
import json, os, pathlib, stat, sys
if sys.argv[1] == 'hook':
    raw = sys.stdin.read()
    payload = json.loads(raw)
    result = {'payload': payload, 'raw': raw,
              'provider': os.environ['CLAUDE_MEM_PROVIDER'],
              'exclusions': os.environ['CLAUDE_MEM_EXCLUDED_PROJECTS']}
    paths = [payload[k] for k in ('transcript_path', 'transcriptPath') if payload.get(k)]
    result['transcripts'] = [
        {'path': p, 'text': pathlib.Path(p).read_text(),
         'file_mode': stat.S_IMODE(pathlib.Path(p).stat().st_mode),
         'directory_mode': stat.S_IMODE(pathlib.Path(p).parent.stat().st_mode)}
        for p in paths]
    pathlib.Path(os.environ['MEMORY_TEST_SINK']).write_text(json.dumps(result))
    raise SystemExit(int(os.environ.get('MEMORY_TEST_EXIT', '0')))
"""

LAUNCHER = """
import pathlib, sys
sys.path.insert(0, sys.argv[1])
import memoryctl
trace = pathlib.Path(sys.argv[3])
def route():
    trace.write_text('route resolved')
    return {'schema_version': 2, 'route': 'claude-subscription'}
memoryctl.read_route = route
memoryctl._ensure_relay = lambda selected: None
memoryctl._ensure_gateway = lambda: None
memoryctl._bun_path = lambda: sys.executable
memoryctl._worker_script = lambda: pathlib.Path(sys.argv[2])
if __import__('os').environ.get('MEMORY_TEST_CHANGE_SOURCE'):
    source = pathlib.Path(__import__('os').environ['MEMORY_TEST_CHANGE_SOURCE'])
    read = memoryctl.memory_ingress.os.read
    changed = False
    def changing_read(descriptor, size):
        global changed
        chunk = read(descriptor, size)
        if not changed:
            changed = True
            source.write_bytes(b'changed concurrently')
        return chunk
    memoryctl.memory_ingress.os.read = changing_read
raise SystemExit(memoryctl.main(sys.argv[4:]))
"""


@pytest.fixture
def ingress(tmp_path: pathlib.Path):
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    sink = tmp_path / "sink.json"
    trace = tmp_path / "route.txt"
    # Do not inherit the host's provider configuration or credentials.
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "AGENTIC_MEMORY_HOME": str(home),
        "MEMORY_TEST_SINK": str(sink),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    def run(payload, platform="claude-code", event="session-init", **env):
        sink.unlink(missing_ok=True)
        trace.unlink(missing_ok=True)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        result = subprocess.run(
            [sys.executable, "-c", LAUNCHER, str(MEMORY_ROOT), str(worker),
             str(trace), "hook", platform, event],
            input=raw, capture_output=True, env={**environment, **env}, check=False, timeout=5,
        )
        captured = json.loads(sink.read_text()) if sink.exists() else None
        return result, captured, trace.exists()

    run.home = home
    return run


def _hook(**fields):
    return {"session_id": "synthetic-session", "cwd": "/synthetic/project", **fields}


@pytest.mark.parametrize("platform", ["claude-code", "codex"])
def test_managed_prompt_child_receives_safe_text_and_original_metadata(ingress, platform):
    payload = _hook(
        prompt="before <PRIVATE reason='test'>synthetic-secret</PRIVATE> after",
        hook_event_name="UserPromptSubmit", turn_id="turn-7", model="synthetic-model",
        stop_hook_active=False, source="resume",
    )
    result, captured, routed = ingress(payload, platform)
    assert result.returncode == 0, result.stderr.decode()
    assert routed
    assert captured["payload"] == {**payload, "prompt": "before  after"}
    assert "synthetic-secret" not in captured["raw"]
    assert captured["provider"] == "claude"


@pytest.mark.parametrize("text", [
    "<private>synthetic-secret</private>",
    "<PRIVATE>synthetic-secret",
    "<private>outer<system-reminder>inner</system-reminder>end</private>",
    "<claude-mem-context>synthetic-secret</claude-mem-context>",
    "<system_instruction>synthetic-secret</system_instruction>",
    "<system-instruction>synthetic-secret</system-instruction>",
    "<persisted-output>synthetic-secret</persisted-output>",
])
def test_entirely_private_prompt_skips_before_route_resolution(ingress, text):
    result, captured, routed = ingress(_hook(prompt=text))
    assert result.returncode == 0, result.stderr.decode()
    assert captured is None
    assert not routed
    assert b"synthetic-secret" not in result.stdout + result.stderr


def test_nested_tool_payload_is_sanitized_before_child_logging(ingress):
    payload = _hook(
        tool_name="synthetic-tool", tool_use_id="tool-9",
        tool_input={"nested": [{"command": "safe <private>synthetic-secret</private> end"}]},
        tool_response={"body": '{"text":"<SYSTEM-REMINDER>hidden</SYSTEM-REMINDER>visible"}'},
    )
    result, captured, _ = ingress(payload, event="observation")
    assert result.returncode == 0, result.stderr.decode()
    assert captured["payload"] == {
        **payload,
        "tool_input": {"nested": [{"command": "safe  end"}]},
        "tool_response": {"body": '{"text":"visible"}'},
    }
    assert "synthetic-secret" not in captured["raw"]
    assert "hidden" not in captured["raw"]


def test_wholly_private_tool_payload_skips_without_metadata_fallback(ingress):
    result, captured, routed = ingress(_hook(
        tool_name="synthetic-tool", tool_input={"command": "<private>secret</private>"},
        tool_response={"text": "<PRIVATE>secret</PRIVATE>"},
    ), event="observation")
    assert result.returncode == 0
    assert captured is None
    assert not routed


@pytest.mark.parametrize("raw", [
    b'{"prompt":"synthetic-secret",',
    b'{"session_id":"one","session_id":"two","prompt":"synthetic-secret"}',
    b'{"prompt":NaN}',
    b'{"prompt":' + b'[' * 64 + b'"synthetic-secret"' + b']' * 64 + b'}',
    b'{"prompt":"' + b's' * (2 * 1024 * 1024) + b'"}',
], ids=["malformed", "duplicate", "nonfinite", "deep", "oversized"])
def test_ambiguous_or_oversized_json_is_rejected_without_dispatch_or_content(ingress, raw):
    result, captured, routed = ingress(raw)
    assert result.returncode != 0
    assert captured is None
    assert not routed
    assert result.stdout == b""
    assert b"synthetic-secret" not in result.stderr
    assert len(result.stderr) < 200


def _transcript(tmp_path, entries):
    path = tmp_path / "synthetic-transcript.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    return path


@pytest.mark.parametrize("event,fields", [
    ("context", {}),
    ("session-init", {"prompt": "current <private>synthetic-secret</private> prompt"}),
    ("file-context", {"tool_input": {"command": "current <private>synthetic-secret</private> command"}}),
    ("observation", {"tool_input": {"command": "current <private>synthetic-secret</private> command"}}),
    ("summarize", {"last_assistant_message": "current <private>synthetic-secret</private> answer"}),
])
def test_codex_inline_event_does_not_read_or_forward_large_history(ingress, tmp_path, event, fields):
    path = tmp_path / "large-transcript.jsonl"
    with path.open("wb") as handle:
        handle.truncate(16 * 1024 * 1024 + 1)
    result, captured, routed = ingress(
        _hook(transcript_path=str(path), transcriptPath=str(path), **fields), "codex", event,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert routed
    assert captured is not None
    assert "transcript_path" not in captured["payload"]
    assert "transcriptPath" not in captured["payload"]
    assert captured["transcripts"] == []
    assert "synthetic-secret" not in captured["raw"]
    assert path.stat().st_size == 16 * 1024 * 1024 + 1


@pytest.mark.parametrize("event", ["context", "session-init", "file-context", "observation", "summarize"])
@pytest.mark.parametrize("raw", [b'{', b'{"prompt":"synthetic-secret </private>"}'])
def test_rejected_codex_capture_does_not_block_agent_or_reach_worker(ingress, event, raw):
    result, captured, routed = ingress(raw, "codex", event)
    assert result.returncode == 0
    assert captured is None
    assert not routed
    assert result.stdout == b""
    assert result.stderr == b"memory: capture skipped (input rejected)\n"


def test_codex_oversized_summary_fallback_skips_capture_without_blocking(ingress, tmp_path):
    path = tmp_path / "large-transcript.jsonl"
    with path.open("wb") as handle:
        handle.truncate(16 * 1024 * 1024 + 1)
    result, captured, routed = ingress(_hook(transcript_path=str(path)), "codex", "summarize")
    assert result.returncode == 0
    assert captured is None
    assert not routed
    assert result.stderr == b"memory: capture skipped (input rejected)\n"


def test_agy_transcript_capture_uses_private_sanitized_snapshot(ingress, tmp_path):
    path = _transcript(tmp_path, [
        {"step_index": 1, "source": "USER", "type": "USER_INPUT",
         "content": "<USER_REQUEST>question <private>synthetic-secret</private> end</USER_REQUEST>"},
        {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE",
         "content": "answer <SYSTEM_INSTRUCTION>hidden</SYSTEM_INSTRUCTION> end"},
    ])
    original = path.read_bytes()
    payload = {"conversationId": "agy-session", "workspacePaths": ["/synthetic/project"],
               "transcriptPath": str(path), "invocationNum": 3, "initialNumSteps": 1}
    result, captured, _ = ingress(payload, "antigravity-cli", "observation")
    assert result.returncode == 0, result.stderr.decode()
    assert captured["payload"]["conversationId"] == "agy-session"
    assert captured["payload"]["invocationNum"] == 3
    snapshot = captured["transcripts"][0]
    assert snapshot["path"] != str(path)
    assert snapshot["file_mode"] == 0o600
    assert snapshot["directory_mode"] == 0o700
    assert "synthetic-secret" not in snapshot["text"]
    assert "hidden" not in snapshot["text"]
    assert "question  end" in snapshot["text"]
    assert "answer  end" in snapshot["text"]
    assert not pathlib.Path(snapshot["path"]).exists()
    assert not pathlib.Path(snapshot["path"]).parent.exists()
    assert path.read_bytes() == original


@pytest.mark.parametrize("platform,event,entries", [
    ("antigravity-cli", "observation", [
        {"type": "USER_INPUT", "content": "<USER_REQUEST><PRIVATE>secret</PRIVATE></USER_REQUEST>"},
        {"type": "PLANNER_RESPONSE", "content": "<private>secret</private>"},
    ]),
    ("claude-code", "summarize", [
        {"type": "assistant", "message": {"content": "previous safe answer", "model": "synthetic-model"}},
        {"type": "assistant", "message": {"content": "<PRIVATE>secret</PRIVATE>", "model": "synthetic-model"}},
    ]),
])
def test_private_latest_transcript_turn_cannot_fall_back_to_previous_turn(ingress, tmp_path, platform, event, entries):
    path = _transcript(tmp_path, entries)
    payload = ({"conversationId": "agy-session", "workspacePaths": ["/synthetic/project"],
                "transcriptPath": str(path), "invocationNum": 3}
               if platform == "antigravity-cli" else _hook(transcript_path=str(path)))
    result, captured, routed = ingress(payload, platform, event)
    assert result.returncode == 0, result.stderr.decode()
    assert captured is None
    assert not routed


def test_claude_summary_child_sees_only_sanitized_transcript(ingress, tmp_path):
    path = _transcript(tmp_path, [
        {"type": "user", "uuid": "user-1", "message": {"content": "safe <private>secret</private>"}},
        {"type": "assistant", "uuid": "assistant-1", "message": {"model": "synthetic-model", "content": [
            {"type": "text", "text": "summary <persisted-output>secret</persisted-output> end"}]}},
    ])
    result, captured, _ = ingress(_hook(transcript_path=str(path)), event="summarize")
    assert result.returncode == 0, result.stderr.decode()
    entries = [json.loads(line) for line in captured["transcripts"][0]["text"].splitlines()]
    assert entries[1] == {"type": "assistant", "uuid": "assistant-1", "message": {
        "model": "synthetic-model", "content": [{"type": "text", "text": "summary  end"}]}}
    assert "secret" not in captured["transcripts"][0]["text"]


@pytest.mark.parametrize("kind", ["malformed", "duplicate", "oversized", "symlink", "parent-link", "parent-link-dotdot", "fifo"])
def test_unsafe_transcript_rejected_before_route_and_never_falls_back(ingress, tmp_path, kind):
    path = tmp_path / "synthetic-transcript.jsonl"
    if kind == "malformed":
        path.write_text('{"type":"assistant","message":"synthetic-secret"')
    elif kind == "duplicate":
        path.write_text('{"type":"user","type":"assistant","content":"synthetic-secret"}')
    elif kind == "oversized":
        path.write_bytes(b"s" * (16 * 1024 * 1024 + 1))
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        target = _transcript(tmp_path, [{"type": "assistant", "message": {"content": "synthetic-secret"}}])
        if kind == "symlink":
            path = tmp_path / "linked-transcript.jsonl"
            path.symlink_to(target)
        else:
            linked = tmp_path / "linked-directory"
            linked.symlink_to(tmp_path, target_is_directory=True)
            path = linked / ("../synthetic-transcript.jsonl" if kind.endswith("dotdot") else "synthetic-transcript.jsonl")
    result, captured, routed = ingress(_hook(transcript_path=str(path)), event="summarize")
    assert result.returncode != 0
    assert captured is None
    assert not routed
    assert b"synthetic-secret" not in result.stderr
    assert str(path).encode() not in result.stderr
    assert len(result.stderr) < 200


@pytest.mark.parametrize("sentinel", ["KHENRIX_NESTED_AGENT", "KHENRIX_MEMORY_ADAPTER"])
def test_nested_or_adapter_hook_does_not_read_input_or_resolve_route(ingress, sentinel):
    result, captured, routed = ingress(b"not JSON", **{sentinel: "1"})
    assert result.returncode == 0
    assert captured is None
    assert not routed


def test_exclusion_globs_remain_in_worker_settings_with_sanitized_payload(ingress):
    config = ingress.home / ".config" / "agentic-memory" / "excluded-projects.json"
    config.parent.mkdir(parents=True, mode=0o700)
    config.write_text(json.dumps({"schema_version": 1, "projects": ["**/private-*", "secret?"]}))
    config.chmod(0o600)
    result, captured, _ = ingress(_hook(prompt="safe <private>secret</private> end"))
    assert result.returncode == 0, result.stderr.decode()
    assert captured["exclusions"] == "**/private-*,secret?"
    assert captured["payload"]["prompt"] == "safe  end"


def test_agy_tool_call_args_and_error_are_sanitized(ingress):
    payload = {"conversationId": "agy-session", "workspacePaths": ["/synthetic/project"],
               "toolCall": {"name": "synthetic-tool", "args": {"command": "safe <private>secret</private>"}},
               "error": "error <SYSTEM-REMINDER>hidden</SYSTEM-REMINDER> end", "stepIdx": 9}
    result, captured, _ = ingress(payload, "antigravity-cli", "observation")
    assert result.returncode == 0, result.stderr.decode()
    assert captured["payload"] == {
        **payload,
        "toolCall": {"name": "synthetic-tool", "args": {"command": "safe "}},
        "error": "error  end",
    }


def test_json_encoded_tool_string_cannot_hide_private_tags_or_supply_empty_fallback(ingress):
    payload = _hook(tool_name="synthetic-tool", tool_input={
        "body": '{"command":"\\u003cprivate\\u003esecret\\u003c/private\\u003e"}'})
    result, captured, routed = ingress(payload, event="observation")
    assert result.returncode == 0, result.stderr.decode()
    assert captured is None
    assert not routed


def test_unclosed_tag_retains_only_text_before_private_block(ingress):
    result, captured, _ = ingress(_hook(prompt="keep <PrIvAtE>secret and remaining text"))
    assert result.returncode == 0, result.stderr.decode()
    assert captured["payload"]["prompt"] == "keep "


def test_codex_private_last_assistant_message_does_not_fall_back_to_transcript(ingress, tmp_path):
    path = _transcript(tmp_path, [{"type": "assistant", "message": {"content": "previous safe"}}])
    result, captured, routed = ingress(_hook(
        last_assistant_message="<PRIVATE>secret</PRIVATE>", transcript_path=str(path),
        hook_event_name="Stop", turn_id="turn-8"), "codex", "summarize")
    assert result.returncode == 0, result.stderr.decode()
    assert captured is None
    assert not routed


@pytest.mark.parametrize("tag", ["private", "SYSTEM-REMINDER"])
def test_transcript_tags_spanning_text_blocks_are_stripped_as_one_message(ingress, tmp_path, tag):
    path = _transcript(tmp_path, [{"type": "assistant", "uuid": "assistant-2", "message": {
        "model": "synthetic-model", "content": [
            {"type": "text", "text": f"keep <{tag}>"},
            {"type": "text", "text": "synthetic-secret"},
            {"type": "text", "text": f"</{tag}> end"},
        ]}}])
    original = path.read_bytes()
    result, captured, _ = ingress(_hook(transcript_path=str(path)), event="summarize")
    assert result.returncode == 0, result.stderr.decode()
    snapshot = captured["transcripts"][0]["text"]
    assert "synthetic-secret" not in snapshot
    entry = json.loads(snapshot)
    assert entry["uuid"] == "assistant-2"
    assert entry["message"]["model"] == "synthetic-model"
    content = entry["message"]["content"]
    assert content == [
        {"type": "text", "text": "keep "},
        {"type": "text", "text": ""},
        {"type": "text", "text": " end"},
    ]
    assert "\n".join(block["text"] for block in content) == "keep \n\n end"
    assert path.read_bytes() == original


@pytest.mark.parametrize("field", ["message", "tool_response", "encoded-response", "response-array", "encoded-array"])
@pytest.mark.parametrize("closed", [False, True])
def test_inline_logical_message_privacy_spans_blocks_without_joining_arguments(ingress, field, closed):
    content = [
        {"type": "text", "text": "keep <private>", "citations": [{"id": "citation-20"}]},
        {"type": "text", "text": "synthetic-secret"},
        {"type": "text", "text": "</private> end" if closed else "also-private"},
        {"type": "tool_use", "id": "tool-20", "name": "synthetic-tool", "input": {"command": "safe"}},
    ]
    response = {"id": "message-20", "model": "synthetic-model", "content": content}
    key = "message" if field == "message" else "tool_response"
    if field in {"response-array", "encoded-array"}:
        response = content
    payload = _hook(tool_name="synthetic-tool", tool_input={"command": "unrelated safe argument"},
                    **{key: json.dumps(response) if field in {"encoded-response", "encoded-array"} else response})
    result, captured, routed = ingress(payload, event="observation")
    assert result.returncode == 0, result.stderr.decode()
    assert routed and captured is not None
    safe = captured["payload"][key]
    if field in {"encoded-response", "encoded-array"}:
        safe = json.loads(safe)
    expected = [
        {"type": "text", "text": "keep ", "citations": [{"id": "citation-20"}]},
        {"type": "text", "text": ""},
        {"type": "text", "text": " end" if closed else ""},
        {"type": "tool_use", "id": "tool-20", "name": "synthetic-tool", "input": {"command": "safe"}},
    ]
    assert safe == (expected if field in {"response-array", "encoded-array"} else
                    {"id": "message-20", "model": "synthetic-model", "content": expected})
    assert captured["payload"]["tool_input"] == {"command": "unrelated safe argument"}
    assert "synthetic-secret" not in captured["raw"]


@pytest.mark.parametrize("position", ["uuid", "message-id", "message-model", "tool-id", "tool-name",
                                      "inline-model", "inline-tool-id", "inline-array-id", "inline-encoded-array-id", "agy-tool-name"])
def test_known_nested_identity_cannot_be_changed_by_redaction(ingress, tmp_path, position):
    entry = {"type": "assistant", "uuid": "record-21", "message": {
        "id": "message-21", "model": "synthetic-model", "content": [
            {"type": "text", "text": "safe answer"},
            {"type": "tool_use", "id": "tool-21", "name": "synthetic-tool", "input": {"command": "safe"}},
        ]}}
    private = "identity-<private>synthetic-secret</private>"
    if position == "uuid":
        entry["uuid"] = private
    elif position in {"message-id", "message-model", "inline-model"}:
        entry["message"]["model" if "model" in position else "id"] = private
    elif position != "agy-tool-name":
        entry["message"]["content"][1]["name" if position == "tool-name" else "id"] = private
    platform, event = "claude-code", "summarize"
    if position.startswith("inline"):
        response = entry["message"]
        if "array" in position:
            response = response["content"]
            if "encoded" in position:
                response = json.dumps(response)
        payload = _hook(tool_input={"command": "safe"}, tool_response=response)
        event = "observation"
    elif position == "agy-tool-name":
        payload = {"conversationId": "session-21", "workspacePaths": ["/synthetic/project"],
                   "toolCall": {"name": private, "args": {"command": "safe"}}}
        platform, event = "antigravity-cli", "observation"
    else:
        payload = _hook(transcript_path=str(_transcript(tmp_path, [entry])))
    result, captured, routed = ingress(payload, platform, event)
    assert result.returncode != 0
    assert captured is None and not routed
    assert result.stdout == b""
    assert b"synthetic-secret" not in result.stderr


def test_ordinary_argument_identity_named_keys_are_still_sanitizable(ingress):
    payload = _hook(tool_name="synthetic-tool", tool_input={
        "id": "id-<private>secret</private>", "model": "model-<private>secret</private>",
        "name": "name-<private>secret</private>", "nested": {"id": "nested-<private>secret</private>"}})
    result, captured, routed = ingress(payload, event="observation")
    assert result.returncode == 0, result.stderr.decode()
    assert routed
    assert captured["payload"]["tool_input"] == {
        "id": "id-", "model": "model-", "name": "name-", "nested": {"id": "nested-"}}


@pytest.mark.parametrize("platform", ["claude-code", "codex", "antigravity-cli"])
@pytest.mark.parametrize("latest", ["", [], [{"type": "image", "source": {"data": "synthetic-media"}}]])
def test_latest_matching_empty_or_media_turn_does_not_resurrect_old_answer(ingress, tmp_path, platform, latest):
    entries = ([{"type": "PLANNER_RESPONSE", "content": "earlier safe answer"},
                {"type": "PLANNER_RESPONSE", "content": latest}, {"type": "metadata", "content": "ignored"}]
               if platform == "antigravity-cli" else [
                   {"type": "assistant", "message": {"content": "earlier safe answer"}},
                   {"type": "assistant", "message": {"content": latest}},
                   {"type": "user", "message": {"content": "unrelated user"}}])
    path = _transcript(tmp_path, entries)
    payload = ({"conversationId": "session-22", "workspacePaths": ["/synthetic/project"], "transcriptPath": str(path)}
               if platform == "antigravity-cli" else _hook(transcript_path=str(path)))
    result, captured, routed = ingress(payload, platform, "summarize")
    assert result.returncode == 0, result.stderr.decode()
    assert captured is None and not routed


@pytest.mark.parametrize("latest", ["", "<USER_REQUEST></USER_REQUEST>", "<USER_REQUEST><private>secret</private></USER_REQUEST>"])
def test_agy_latest_matching_user_turn_cannot_resurrect_old_request(ingress, tmp_path, latest):
    path = _transcript(tmp_path, [
        {"type": "USER_INPUT", "content": "<USER_REQUEST>earlier safe question</USER_REQUEST>"},
        {"type": "USER_INPUT", "content": latest}, {"type": "PLANNER_RESPONSE", "content": "safe answer"}])
    payload = {"conversationId": "session-23", "workspacePaths": ["/synthetic/project"],
               "transcriptPath": str(path), "invocationNum": 3}
    result, captured, routed = ingress(payload, "antigravity-cli", "observation")
    assert result.returncode == 0, result.stderr.decode()
    assert captured is None and not routed


@pytest.mark.parametrize("limit,extra", [("lines", 0), ("lines", 1), ("record-bytes", 0),
                                       ("record-bytes", 1), ("nodes", 0), ("nodes", 1)])
def test_transcript_exact_limits_keep_at_limit_or_refuse_before_dispatch(ingress, tmp_path, limit, extra):
    if limit == "lines":
        raw = b"{}\n" * (9999 + extra) + b'{"type":"assistant","message":{"content":"safe answer"}}\n'
    elif limit == "record-bytes":
        prefix, suffix = b'{"type":"assistant","message":{"content":"', b'"}}'
        raw = prefix + b"s" * (1024 * 1024 - len(prefix) - len(suffix) + extra) + suffix + b"\n"
    else:
        entry = {"type": "assistant", "message": {"content": "safe answer"}, "padding": [None] * (99991 + extra)}
        raw = json.dumps(entry).encode() + b"\n"
    path = tmp_path / "bounded-transcript.jsonl"
    path.write_bytes(raw)
    result, captured, routed = ingress(_hook(transcript_path=str(path)), event="summarize")
    assert (result.returncode == 0) is (extra == 0), result.stderr.decode()
    assert routed is (extra == 0)
    assert (captured is not None) is (extra == 0)
    assert path.read_bytes() == raw


def test_private_tool_text_block_does_not_count_type_metadata_as_safe_text(ingress):
    result, captured, routed = ingress(_hook(
        tool_name="synthetic-tool", tool_response={"content": [
            {"type": "text", "text": "<private>secret</private>"}]},
    ), event="observation")
    assert result.returncode == 0, result.stderr.decode()
    assert captured is None
    assert not routed


def test_privacy_tags_in_session_metadata_are_rejected_without_changing_identity(ingress):
    result, captured, routed = ingress(_hook(
        session_id="session-<private>secret</private>", prompt="safe prompt",
    ))
    assert result.returncode != 0
    assert captured is None
    assert not routed
    assert b"secret" not in result.stderr


def test_snapshot_is_cleaned_when_child_returns_failure(ingress, tmp_path):
    path = _transcript(tmp_path, [{"type": "assistant", "message": {"content": "safe answer"}}])
    result, captured, _ = ingress(_hook(transcript_path=str(path)), event="summarize", MEMORY_TEST_EXIT="7")
    assert result.returncode == 7, result.stderr.decode()
    snapshot = pathlib.Path(captured["transcripts"][0]["path"])
    assert not snapshot.exists()
    assert not snapshot.parent.exists()


def test_private_json_object_key_is_rejected_before_worker_logging(ingress):
    result, captured, routed = ingress(_hook(
        tool_name="synthetic-tool", tool_input={"nested": {"<private>secret</private>": "safe"}},
    ), event="observation")
    assert result.returncode != 0
    assert captured is None
    assert not routed
    assert b"secret" not in result.stderr


def test_changing_transcript_is_rejected_before_route_resolution(ingress, tmp_path):
    path = _transcript(tmp_path, [{"type": "assistant", "message": {"content": "synthetic-secret"}}])
    result, captured, routed = ingress(_hook(transcript_path=str(path)), event="summarize",
                                        MEMORY_TEST_CHANGE_SOURCE=str(path))
    assert result.returncode != 0
    assert captured is None
    assert not routed
    assert b"synthetic-secret" not in result.stderr


def test_malformed_json_encoded_tool_payload_cannot_restore_raw_private_text(ingress):
    result, captured, routed = ingress(_hook(tool_name="synthetic-tool", tool_input={
        "body": '{"text":"\\u003cprivate\\u003esecret\\u003c/private\\u003e"} trailing'
    }), event="observation")
    assert result.returncode != 0
    assert captured is None
    assert not routed
    assert b"secret" not in result.stderr


def test_ordinary_non_json_tool_text_preserves_brackets_and_whitespace(ingress):
    payload = _hook(tool_name="synthetic-tool", tool_response="[Command result]  ordinary text\n")
    result, captured, _ = ingress(payload, event="observation")
    assert result.returncode == 0, result.stderr.decode()
    assert captured["payload"] == payload


@pytest.mark.parametrize("fields,expected_field,dispatch", [
    ({}, None, True),
    ({"last_assistant_message": None}, None, True),
    ({"last_assistant_message": ""}, "", True),
    ({"last_assistant_message": 42}, 42, True),
    ({"last_assistant_message": False}, False, True),
    ({"last_assistant_message": ["ignored"]}, ["ignored"], True),
    ({"last_assistant_message": {"text": "<private>secret</private>"}}, {"text": ""}, True),
    ({"last_assistant_message": "<PRIVATE>secret</PRIVATE>"}, "", False),
], ids=["absent", "null", "empty", "number", "boolean", "array", "object", "redacted-nonempty"])
def test_codex_original_assistant_field_controls_sanitized_transcript_fallback(
    ingress, tmp_path, fields, expected_field, dispatch,
):
    path = _transcript(tmp_path, [{"type": "assistant", "message": {
        "content": "safe transcript <private>secret</private> answer", "model": "synthetic-model"}}])
    original = path.read_bytes()
    result, captured, routed = ingress(
        _hook(transcript_path=str(path), hook_event_name="Stop", turn_id="turn-10", **fields),
        "codex", "summarize",
    )
    assert result.returncode == 0, result.stderr.decode()
    assert routed is dispatch
    assert (captured is not None) is dispatch
    if dispatch:
        assert captured["payload"].get("last_assistant_message") == expected_field
        assert ("last_assistant_message" in captured["payload"]) == bool(fields)
        assert captured["payload"]["session_id"] == "synthetic-session"
        assert captured["payload"]["turn_id"] == "turn-10"
        entry = json.loads(captured["transcripts"][0]["text"])
        assert entry["message"]["content"] == "safe transcript  answer"
        assert "secret" not in captured["raw"] + captured["transcripts"][0]["text"]
    assert path.read_bytes() == original


def test_transcript_redaction_retains_safe_block_fields_and_sanitizes_tool_input(ingress, tmp_path):
    citations = [{"type": "file", "id": "citation-1", "uri": "https://example.invalid/synthetic"}]
    path = _transcript(tmp_path, [{"type": "assistant", "uuid": "assistant-3", "message": {
        "model": "synthetic-model", "content": [
            {"type": "text", "text": "safe <private>secret</private> answer", "citations": citations},
            {"type": "tool_use", "id": "synthetic-tool-id", "name": "synthetic-tool", "input": {
                "command": "safe command", "details": "<private>secret</private>safe detail"}},
        ]}}])
    original = path.read_bytes()
    result, captured, _ = ingress(_hook(transcript_path=str(path)), event="summarize")
    assert result.returncode == 0, result.stderr.decode()
    entry = json.loads(captured["transcripts"][0]["text"])
    assert entry == {"type": "assistant", "uuid": "assistant-3", "message": {
        "model": "synthetic-model", "content": [
            {"type": "text", "text": "safe  answer", "citations": citations},
            {"type": "tool_use", "id": "synthetic-tool-id", "name": "synthetic-tool", "input": {
                "command": "safe command", "details": "safe detail"}},
        ]}}
    assert "secret" not in captured["transcripts"][0]["text"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("tag", ["private", "SYSTEM-REMINDER"])
def test_transcript_spans_blocks_retaining_citations_tool_id_and_text(ingress, tmp_path, tag):
    path = _transcript(tmp_path, [{"type": "assistant", "message": {"content": [
        {"type": "text", "text": f"keep <{tag}>", "citations": [{"id": "first-citation"}]},
        {"type": "text", "text": "secret", "citations": [{"id": "second-citation"}]},
        {"type": "text", "text": f"</{tag}> end", "citations": [{"id": "third-citation"}]},
        {"type": "tool_use", "id": "tool-11", "name": "synthetic-tool", "input": {"command": "safe command"}},
    ]}}])
    result, captured, _ = ingress(_hook(transcript_path=str(path)), event="summarize")
    assert result.returncode == 0, result.stderr.decode()
    content = json.loads(captured["transcripts"][0]["text"])["message"]["content"]
    assert content == [
        {"type": "text", "text": "keep ", "citations": [{"id": "first-citation"}]},
        {"type": "text", "text": "", "citations": [{"id": "second-citation"}]},
        {"type": "text", "text": " end", "citations": [{"id": "third-citation"}]},
        {"type": "tool_use", "id": "tool-11", "name": "synthetic-tool", "input": {"command": "safe command"}},
    ]
    assert "\n".join(block["text"] for block in content if block["type"] == "text") == "keep \n\n end"
    assert "secret" not in captured["transcripts"][0]["text"]


@pytest.mark.parametrize("response", [
    {"content": [
        {"type": "text", "text": "<private>secret</private>", "citations": [{"id": "citation-2"}]},
        {"type": "tool_use", "id": "tool-12", "name": "synthetic-tool", "input": {"command": "<private>secret</private>"}},
    ]},
    {"type": "metadata", "id": "event-13"},
    {"content": [{"text": "<private>secret</private>", "citations": [{"id": "citation-3"}]}]},
], ids=["private-text-and-tool", "type-and-id-only", "generic-private-text-block"])
def test_capture_metadata_cannot_supply_safe_text_after_redaction(ingress, response):
    result, captured, routed = ingress(_hook(tool_name="synthetic-tool", tool_response=response), event="observation")
    assert result.returncode == 0, result.stderr.decode()
    assert captured is None
    assert not routed


def test_private_latest_block_array_skips_despite_retained_citations_and_tool_id(ingress, tmp_path):
    path = _transcript(tmp_path, [
        {"type": "assistant", "message": {"content": "earlier safe answer"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "<private>", "citations": [{"id": "citation-4"}]},
            {"type": "text", "text": "secret"},
            {"type": "text", "text": "</private>"},
            {"type": "tool_use", "id": "tool-14", "name": "synthetic-tool", "input": {"command": "safe command"}},
        ]}},
    ])
    result, captured, routed = ingress(_hook(transcript_path=str(path)), event="summarize")
    assert result.returncode == 0, result.stderr.decode()
    assert captured is None
    assert not routed


@pytest.mark.parametrize("platform", ["claude-code", "codex", "antigravity-cli"])
@pytest.mark.parametrize("arguments", [
    {"id": "safe-id"},
    {"type": "safe-type"},
    {"text": "", "command": "safe command"},
    {"content": [{"id": "safe-id"}]},
    {"content": [{"type": "safe-type"}]},
    {"content": [{"text": "", "command": "safe command"}]},
    {"nested": {"content": [{"id": "safe-id"}]}},
    {"nested": {"content": [{"type": "safe-type"}]}},
    {"nested": {"content": [{"text": "", "command": "safe command"}]}},
], ids=["id-argument", "type-argument", "empty-text-with-safe-command",
        "content-id", "content-type", "content-empty-text-command",
        "nested-content-id", "nested-content-type", "nested-content-empty-text-command"])
def test_ordinary_tool_arguments_reach_managed_child_without_metadata_classification(
    ingress, platform, arguments,
):
    response = {"content": [
        {"type": "text", "id": "synthetic-block-id", "text": "<private>secret</private>"},
    ]}
    if platform == "antigravity-cli":
        payload = {"conversationId": "agy-session", "workspacePaths": ["/synthetic/project"],
                   "toolCall": {"name": "synthetic-tool", "args": arguments},
                   "error": "", "stepIdx": 14, "tool_response": response}
    else:
        payload = _hook(tool_name="synthetic-tool", tool_input=arguments,
                        hook_event_name="PostToolUse", tool_response=response)
    result, captured, routed = ingress(payload, platform, "observation")
    assert result.returncode == 0, result.stderr.decode()
    assert routed
    assert captured is not None
    field = captured["payload"]["toolCall"]["args"] if platform == "antigravity-cli" else captured["payload"]["tool_input"]
    assert field == arguments
    assert captured["payload"]["tool_response"] == {"content": [
        {"type": "text", "id": "synthetic-block-id", "text": ""},
    ]}
    assert "secret" not in captured["raw"]


@pytest.mark.parametrize("platform", ["claude-code", "codex", "antigravity-cli"])
@pytest.mark.parametrize("record", [
    {"id": "safe-id"},
    {"type": "safe-type"},
    {"text": "", "command": "safe command"},
    {"type": "tool_use", "id": "safe-id"},
], ids=["id-record", "type-record", "empty-text-with-safe-command-record", "nonblock-tool-type-record"])
def test_nonblock_response_records_reach_child_without_losing_safe_values(ingress, platform, record):
    response = {"content": [record,
        {"type": "text", "id": "synthetic-block-id", "text": "<private>secret</private>"},
    ]}
    if platform == "antigravity-cli":
        payload = {"conversationId": "agy-session", "workspacePaths": ["/synthetic/project"],
                   "toolCall": {"name": "synthetic-tool", "args": {"command": "<private>secret</private>"}},
                   "error": "", "stepIdx": 15, "tool_response": response}
    else:
        payload = _hook(tool_name="synthetic-tool", tool_input={"command": "<private>secret</private>"},
                        hook_event_name="PostToolUse", tool_response=response)
    result, captured, routed = ingress(payload, platform, "observation")
    assert result.returncode == 0, result.stderr.decode()
    assert routed
    assert captured is not None
    assert captured["payload"]["tool_response"] == {"content": [record,
        {"type": "text", "id": "synthetic-block-id", "text": ""},
    ]}
    assert "secret" not in captured["raw"]
