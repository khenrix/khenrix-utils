"""Bounded projections of native calls, not reasoning or a filesystem sandbox.

Only explicit arguments are inspected. Computed shell paths, aliases, descendants,
managed hooks and implicit service reads are outside this observed-read contract.
"""
from __future__ import annotations

import os
import hashlib
import json
import re
import shlex
from pathlib import Path


MAX_TRACE_BYTES = 64 * 1024 * 1024
MAX_TRACE_RECORDS = 200000


def _user_text(executor, row, expected_prompt=None):
    if executor == "claude" and row.get("type") == "user":
        content = row.get("message", {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list) and content and all(item.get("type") == "text" for item in content):
            return "".join(item["text"] for item in content)
    if executor == "agy" and row.get("type") == "USER_INPUT":
        content = row.get("content")
        if (expected_prompt is not None and isinstance(content, str)
                and content.startswith("<USER_REQUEST>\n")):
            body = content.removeprefix("<USER_REQUEST>\n").removeprefix("/plan ").encode("utf-8")
            boundary = len(expected_prompt)
            # Native transcripts use either no added separator or one LF;
            # the supplied task bytes, including their own final LF, stay exact.
            if body[:boundary] == expected_prompt and body[boundary:].startswith((
                    b"</USER_REQUEST>", b"\n</USER_REQUEST>")):
                return expected_prompt.decode("utf-8")
        return content
    return None


def project_native_trace(executor, raw, *, prompt, session_id, cwd, answer):
    """Project one input-to-terminal interval from an exact native source.

    Codex stdout is already bounded to the launched process's single turn.
    Claude/agy normal transcripts require the exact input and terminal answer.
    Source bytes are hashed, never copied into the projection.
    """
    trace = {"coverage": "incomplete", "executor_id": executor, "session_id": session_id,
             "prompt_sha256": hashlib.sha256(prompt).hexdigest(), "cwd": str(cwd),
             "source_sha256": hashlib.sha256(raw).hexdigest(), "bounds": None, "calls": []}
    try:
        if len(raw) > MAX_TRACE_BYTES:
            raise ValueError("native source exceeds bound")
        lines = raw.splitlines()
        if not lines or len(lines) > MAX_TRACE_RECORDS or not raw.endswith(b"\n"):
            raise ValueError("native source is missing, truncated or exceeds record bound")
        rows = [json.loads(line) for line in lines]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("native record is not an object")
        if executor == "codex":
            if ([row.get("thread_id") for row in rows if row.get("type") == "thread.started"] != [session_id]
                    or sum(row.get("type") == "turn.started" for row in rows) != 1
                    or rows[-1].get("type") != "turn.completed"
                    or sum(row.get("type") == "turn.completed" for row in rows) != 1):
                raise ValueError("native stream has foreign or missing thread/turn bounds")
            start, end = 0, len(rows) - 1
        else:
            starts = [index for index, row in enumerate(rows)
                      if _user_text(executor, row, prompt) == prompt.decode("utf-8")]
            if len(starts) != 1:
                raise ValueError("native transcript lacks unique exact prompt")
            start = starts[0]
            end = len(rows) - 1
            if executor != "claude":
                end = next((index - 1 for index in range(start + 1, len(rows))
                            if _user_text(executor, rows[index]) is not None), end)
        trace["bounds"] = [start, end]
        terminal = executor == "codex"
        pending = {}
        seen = {}
        for index in range(start, end + 1):
            row = rows[index]
            if executor == "claude":
                if row.get("type") == "user":
                    if row.get("isMeta") or row.get("turnCompanion") or row.get("sourceToolUseID") is not None:
                        source = seen.get(row.get("sourceToolUseID"), {})
                        prompt_id = rows[start].get("promptId")
                        if (row.get("isMeta") is not True or row.get("turnCompanion") is not True
                                or not isinstance(prompt_id, str) or not prompt_id
                                or row.get("promptId") != prompt_id or source.get("name") != "Skill"):
                            raise ValueError("unmatched native skill companion")
                    elif index > start and _user_text(executor, row) is not None:
                        trace["bounds"][1] = index - 1
                        break
                if row.get("sessionId") not in {None, session_id}:
                    raise ValueError("foreign native session")
                if row.get("type") in {"assistant", "user"} and row.get("sessionId") != session_id:
                    raise ValueError("native message lacks session identity")
                if row.get("type") not in {"assistant", "user"}:
                    continue
                message = row.get("message", {})
                blocks = message.get("content", [])
                if isinstance(blocks, str):
                    continue
                for block in blocks:
                    if block.get("type") not in {"thinking", "text", "tool_use", "tool_result"}:
                        raise ValueError("unsupported native content block")
                    if block.get("type") == "tool_use":
                        call = _call(block.get("id"), block.get("name"), block.get("input"), "pending")
                        if call["id"] in seen:
                            raise ValueError("duplicate native call identity")
                        seen[call["id"]] = call
                        trace["calls"].append(call)
                        pending[call["id"]] = call
                    elif block.get("type") == "tool_result":
                        call = pending.pop(block.get("tool_use_id"), None)
                        if call is None:
                            raise ValueError("unmatched native tool result")
                        call["status"] = "error" if block.get("is_error") else "completed"
                if message.get("stop_reason") == "end_turn":
                    text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
                    terminal = text.strip() == answer.strip()
            elif executor == "agy":
                if row.get("type") not in {"USER_INPUT", "PLANNER_RESPONSE", "GENERIC", "SYSTEM_MESSAGE"}:
                    raise ValueError("unsupported native transcript record")
                if row.get("type") == "PLANNER_RESPONSE":
                    for offset, call in enumerate(row.get("tool_calls", [])):
                        trace["calls"].append(_call(f"{row['step_index']}:{offset}",
                            call.get("name"), call.get("args"), row.get("status")))
                    if isinstance(row.get("content"), str):
                        terminal = row.get("status") == "DONE" and row["content"].strip() == answer.strip()
            elif executor == "codex":
                kind = row.get("type")
                if kind in {"item.started", "item.updated", "item.completed"}:
                    item = row.get("item", {})
                    item_type = item.get("type")
                    if item_type in {"command_execution", "mcp_tool_call"}:
                        if kind != "item.completed":
                            pending[item.get("id")] = item_type
                            continue
                        pending.pop(item.get("id"), None)
                        args = ({"command": item.get("command")} if item_type == "command_execution"
                                else item.get("arguments"))
                        if item_type == "command_execution" and not isinstance(item.get("command"), str):
                            raise ValueError("native command lacks arguments")
                        name = "shell" if item_type == "command_execution" else f"{item.get('server')}/{item.get('tool')}"
                        trace["calls"].append(_call(item.get("id"), name, args, item.get("status")))
                    elif item_type not in {"agent_message", "reasoning", "todo_list", "error"}:
                        raise ValueError("unsupported native tool item")
                elif kind not in {"thread.started", "turn.started", "turn.completed"}:
                    raise ValueError("unsupported native event")
            else:
                raise ValueError("unsupported executor trace")
        if pending or not terminal:
            raise ValueError("native turn lacks matched completion")
        trace["coverage"] = "complete"
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError) as error:
        trace["detail"] = str(error)[:200]
    return trace


def _call(identity, name, arguments, status):
    if (not isinstance(identity, str) or not identity or not isinstance(name, str) or not name
            or not isinstance(arguments, dict) or status not in {"pending", "completed", "failed", "DONE", "ERROR"}
            or "…" in json.dumps(arguments, ensure_ascii=False)):
        raise ValueError("native call has missing or truncated arguments/identity/status")
    return {"id": identity, "name": name, "arguments": arguments, "status": status}


def capture_native_trace(request, result):
    """Read the exact normal native session, never a latest-session heuristic."""
    trace = {"coverage": "unavailable", "executor_id": request.executor_id,
             "session_id": result.session_id, "prompt_sha256": hashlib.sha256(request.prompt_bytes).hexdigest(),
             "cwd": str(request.cwd), "calls": []}
    session = result.session_id
    try:
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session):
            raise ValueError("native session is missing or invalid")
        if request.executor_id == "codex":
            raw = result.stdout
            source = "captured-stdout"
        else:
            if request.executor_id == "claude":
                paths = list((Path.home() / ".claude/projects").glob(f"*/{session}.jsonl"))
            else:
                paths = [Path.home() / ".gemini/antigravity-cli/brain" / session
                         / ".system_generated/logs/transcript_full.jsonl"]
            if len(paths) != 1:
                raise ValueError("native exact-session transcript missing or ambiguous")
            source = str(paths[0])
            with paths[0].open("rb") as handle:
                raw = handle.read(MAX_TRACE_BYTES + 1)
        trace = project_native_trace(request.executor_id, raw, prompt=request.prompt_bytes,
            session_id=session, cwd=request.cwd, answer=result.answer)
        trace["source"] = source
        trace["profile_sha256"] = request.profile.digest if request.profile else None
        trace["cli_version"] = request.profile.cli_version if request.profile else None
        if not result.valid:
            trace["coverage"] = "incomplete"
    except (OSError, ValueError) as error:
        trace["detail"] = str(error)[:200]
    return trace


def read_boundary_violation(trace, *, cwd, forbidden, allowed_trees, allowed_files):
    forbidden = tuple(path.resolve() for path in forbidden)
    trees = tuple(path.resolve() for path in allowed_trees)
    files = {path.resolve() for path in allowed_files}

    def denied(value, base):
        path = Path(os.path.normpath(str(base / value))).resolve()
        if path in files or any(path.is_relative_to(root) for root in trees):
            return False
        return any(path.is_relative_to(root) for root in forbidden)

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, item in value.items():
                if key not in {"description", "toolAction", "toolSummary"}:
                    yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)

    for call in trace.get("calls", ()):
        arguments = call.get("arguments", {})
        base = Path(arguments.get("Cwd", arguments.get("cwd", cwd)))
        if not base.is_absolute():
            base = cwd / base
        if denied(str(base), cwd):
            return call["id"]
        for value in strings(arguments):
            # Tokenization recognizes direct cat/ls/rg paths, including quoted
            # spaces and '..'. It deliberately does not evaluate shell programs.
            try:
                tokens = shlex.split(value, posix=True)
            except ValueError:
                tokens = [value]
            tokens.extend(re.findall(r"(?:/|\.\./)[^\s\"'`;|<>]+", value))
            for token in tokens:
                token = token.rstrip(",)")
                if (token.startswith(("/", "./", "../")) and denied(token, base)):
                    return call["id"]
    return None
