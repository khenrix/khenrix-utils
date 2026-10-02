"""Bound and sanitize owned hook input before the claude-mem process sees it."""

from __future__ import annotations

import contextlib
import json
import math
import os
import pathlib
import re
import stat
import tempfile
from collections.abc import Iterator
from typing import Any, BinaryIO

MAX_HOOK_BYTES = 1024 * 1024
MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 100_000
MAX_TRANSCRIPT_LINES = 10_000
MAX_TAGS = 10_000
TAG_NAMES = (
    "private", "claude-mem-context", "system_instruction", "system-instruction",
    "persisted-output", "system-reminder",
)
TAG_START = re.compile(r"<\s*(/?)\s*(" + "|".join(TAG_NAMES) + r")(?=[\s/>]|$)", re.I)
JSON_PREFIX = re.compile(r'^\s*(?:\{\s*["}]|\[\s*(?:["{\[\]\-\d]|true\b|false\b|null\b))')
TRANSCRIPT_KEYS = ("transcript_path", "transcriptPath")
IDENTITY_KEYS = (
    "session_id", "sessionId", "id", "conversationId", "turn_id", "tool_use_id",
    "hook_event_name", "cwd", "workspacePaths", "model", "source", "agent_id",
    "agent_type", "tool_name",
)


class IngressRejected(ValueError):
    """Unsafe input; the public diagnostic never includes its contents or path."""


def _reject() -> None:
    raise IngressRejected("memory hook input rejected")


def _visible_ranges(text: str) -> list[tuple[int, int]]:
    output: list[tuple[int, int]] = []
    stack: list[str] = []
    cursor = 0
    count = 0
    for match in TAG_START.finditer(text):
        if match.start() < cursor:
            continue
        count += 1
        if count > MAX_TAGS:
            _reject()
        if not stack:
            output.append((cursor, match.start()))
        end = text.find(">", match.end())
        if end < 0:
            # An unfinished opening tag owns the remaining text.
            if match.group(1):
                _reject()
            return output
        name = match.group(2).lower()
        if match.group(1):
            if not stack or stack[-1] != name:
                _reject()
            stack.pop()
        elif not text[match.end():end].rstrip().endswith("/"):
            stack.append(name)
        cursor = end + 1
    if not stack:
        output.append((cursor, len(text)))
    return output


def _strip_tags(text: str) -> str:
    return "".join(text[start:end] for start, end in _visible_ranges(text))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _reject()
        result[key] = value
    return result


def _parse_json(text: str) -> Any:
    # Check nesting before the decoder allocates a recursive document.
    depth = 0
    quoted = False
    escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                _reject()
        elif character in "]}":
            depth -= 1
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=lambda _: _reject())


def _encoded_json(text: str) -> Any:
    if text.lstrip().startswith(("{", "[", '"')):
        try:
            return _parse_json(text)
        except json.JSONDecodeError:
            # Bracketed prose is ordinary text; malformed encoded payloads
            # cannot bypass redaction by reverting to their raw representation.
            if JSON_PREFIX.match(text) or "\\u003c" in text.lower() or "\\u003e" in text.lower():
                _reject()
    return None


def _sanitize(value: Any, budget: list[int], depth: int = 0) -> Any:
    budget[0] += 1
    if budget[0] > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
        _reject()
    if isinstance(value, str):
        value.encode("utf-8")
        encoded = _encoded_json(value)
        if encoded is not None:
            clean = _sanitize(encoded, budget, depth + 1)
            return value if clean == encoded else json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
        return _strip_tags(value)
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            key.encode("utf-8")
            # Stripping keys can create collisions or change hook identity.
            if _strip_tags(key) != key:
                _reject()
            result[key] = _sanitize(child, budget, depth + 1)
        return result
    if isinstance(value, list):
        return [_sanitize(child, budget, depth + 1) for child in value]
    if isinstance(value, float) and not math.isfinite(value):
        _reject()
    return value


def _has_text(value: Any, *, response_content: bool = False) -> bool:
    if isinstance(value, str):
        encoded = _encoded_json(value)
        return _has_text(encoded, response_content=response_content) if encoded is not None else bool(value.strip())
    if isinstance(value, dict):
        kind = value.get("type")
        if response_content and "text" in value and (
            kind == "text" or kind is None and value.keys() <= {"type", "text", "citations"}
        ):
            return _has_text(value["text"])
        if response_content and kind == "tool_use" and {"id", "name", "input"} <= value.keys():
            return _has_text(value.get("input"))
        return any(
            _has_text(child, response_content=response_content)
            for key, child in value.items() if not (response_content and kind == "metadata" and key in {"type", "id"})
        )
    if isinstance(value, list):
        return any(_has_text(child, response_content=response_content) for child in value)
    return False


def _read_transcript(path: str) -> bytes:
    source = pathlib.Path(path)
    if ".." in source.parts:
        _reject()
    with contextlib.ExitStack() as cleanup:
        # Resolve every directory through no-follow descriptors. A source must
        # remain the same regular file for the complete bounded read.
        absolute = pathlib.Path(os.path.abspath(source))
        directory = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        cleanup.callback(os.close, directory)
        for part in absolute.parts[1:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            cleanup.callback(os.close, directory)
        name = absolute.name
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        cleanup.callback(os.close, descriptor)
        before = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
                or before.st_size > MAX_TRANSCRIPT_BYTES):
            _reject()
        chunks: list[bytes] = []
        remaining = MAX_TRANSCRIPT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=directory, follow_symlinks=False)
        signatures = {
            (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            for item in (before, after, named_after)
        }
        if len(content) > MAX_TRANSCRIPT_BYTES or len(content) != before.st_size or len(signatures) != 1:
            _reject()
        return content


def _message_text(entry: dict[str, Any], role: str) -> str:
    entry_type = entry.get("type")
    if entry_type is not None and not isinstance(entry_type, str):
        _reject()
    agy_role = {"USER_INPUT": "user", "PLANNER_RESPONSE": "assistant"}.get(entry_type)
    if (agy_role or entry.get("type", entry.get("role"))) != role:
        return ""
    if agy_role:
        content = entry.get("content")
    else:
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"] for block in content if isinstance(block, dict)
            and isinstance(block.get("text"), str) and block.get("type") in (None, "text")
        )
    return ""


def _sanitize_transcript(entry: dict[str, Any], budget: list[int]) -> dict[str, Any]:
    message = entry.get("message")
    container = message if isinstance(message, dict) else entry
    content = container.get("content")
    if isinstance(content, list):
        blocks = [
            (index, block["text"]) for index, block in enumerate(content) if isinstance(block, dict)
            and isinstance(block.get("text"), str) and block.get("type") in (None, "text")
        ]
        joined = "\n".join(text for _, text in blocks)
        if TAG_START.search(joined):
            # Match the parser's joined-text tag state without flattening blocks
            # or losing citations and tool-use identifiers/inputs.
            visible = _visible_ranges(joined)
            normalized_content = list(content)
            position = 0
            range_index = 0
            for index, text in blocks:
                end = position + len(text)
                while range_index < len(visible) and visible[range_index][1] <= position:
                    range_index += 1
                pieces = []
                while range_index < len(visible) and visible[range_index][0] < end:
                    start, stop = visible[range_index]
                    pieces.append(joined[max(position, start):min(end, stop)])
                    if stop > end:
                        break
                    range_index += 1
                normalized_content[index] = {**content[index], "text": "".join(pieces)}
                position = end + 1
            normalized = {**container, "content": normalized_content}
            entry = {**entry, "message": normalized} if container is message else normalized
    return _sanitize(entry, budget)


def _unwrap_agy(text: str) -> str:
    match = re.search(r"<USER_REQUEST>\s*([\s\S]*?)\s*</USER_REQUEST>", text)
    if match:
        return match.group(1).strip()
    return re.sub(r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>[\s\S]*?</\1>", "", text).strip()


def _last_text(original: list[dict[str, Any]], clean: list[dict[str, Any]], role: str, *, agy: bool) -> str:
    for raw, safe in zip(reversed(original), reversed(clean)):
        # Select before stripping. Upstream scans past empty turns, which must
        # not resurrect a previous turn when the actual last text was private.
        if _message_text(raw, role).strip():
            text = _message_text(safe, role)
            return _unwrap_agy(text) if agy and role == "user" else text
    return ""


@contextlib.contextmanager
def prepare_hook(stream: BinaryIO, platform: str, event: str) -> Iterator[bytes | None]:
    """Yield safe JSON and a temporary safe transcript, or skip a private capture."""
    if platform not in {"claude-code", "codex", "antigravity-cli"}:
        _reject()
    try:
        raw = stream.read(MAX_HOOK_BYTES + 1)
        if len(raw) > MAX_HOOK_BYTES:
            _reject()
        document = _parse_json(raw.decode("utf-8"))
        if not isinstance(document, dict):
            _reject()
        for key in ("prompt", "last_assistant_message", *TRANSCRIPT_KEYS):
            if key == "last_assistant_message" and platform == "codex":
                continue
            if key in document and document[key] is not None and not isinstance(document[key], str):
                _reject()
        # Only Codex summaries without inline text need history. Never forward
        # an unused transcript path for the worker to read outside this boundary.
        if platform == "codex" and (
            event != "summarize"
            or isinstance(document.get("last_assistant_message"), str) and document["last_assistant_message"]
        ):
            for key in TRANSCRIPT_KEYS:
                document.pop(key, None)
        budget = [0]
        safe = _sanitize(document, budget)
        if any(document.get(key) != safe.get(key) for key in IDENTITY_KEYS):
            _reject()
        if isinstance(document.get("toolCall"), dict) and document["toolCall"].get("name") != safe["toolCall"].get("name"):
            _reject()
        original_entries: list[dict[str, Any]] = []
        safe_entries: list[dict[str, Any]] = []
        paths = {document[key] for key in TRANSCRIPT_KEYS if document.get(key)}
        if len(paths) > 1:
            _reject()
        if paths:
            path = paths.pop()
            if any(document.get(key) != safe.get(key) for key in TRANSCRIPT_KEYS):
                _reject()
            content = _read_transcript(path).decode("utf-8")
            lines = content.splitlines()
            if len(lines) > MAX_TRANSCRIPT_LINES:
                _reject()
            for line in lines:
                if not line.strip():
                    continue
                if len(line.encode("utf-8")) > MAX_HOOK_BYTES:
                    _reject()
                entry = _parse_json(line)
                if not isinstance(entry, dict):
                    _reject()
                original_entries.append(entry)
                safe_entries.append(_sanitize_transcript(entry, budget))
        agy = platform == "antigravity-cli"
        assistant = _last_text(original_entries, safe_entries, "assistant", agy=agy)
        prompt = safe.get("prompt")
        if agy and prompt is None:
            prompt = _last_text(original_entries, safe_entries, "user", agy=True)
        if event == "session-init":
            capture = prompt
        elif event == "summarize":
            original_assistant = document.get("last_assistant_message")
            capture = (safe["last_assistant_message"]
                       if platform == "codex" and isinstance(original_assistant, str) and original_assistant
                       else assistant)
        elif event == "observation":
            response = safe.get("tool_response")
            response_has_text = _has_text(response, response_content=True)
            if agy and "invocationNum" in safe and not safe.get("toolCall"):
                # The adapter substitutes Completed/User Query for empty turns.
                capture = assistant if _has_text(prompt) else None
            elif agy and isinstance(safe.get("toolCall"), dict):
                capture = _has_text(safe["toolCall"].get("args")) or _has_text(safe.get("error")) or response_has_text
            else:
                capture = _has_text(safe.get("tool_input")) or response_has_text
        elif event == "file-context":
            capture = safe.get("tool_input")
        else:
            capture = True  # Context/lifecycle hooks do not require capture text.
        if capture is not True and not _has_text(capture):
            yield None
            return
        snapshot = "\n".join(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) for entry in safe_entries)
        if len(snapshot.encode("utf-8")) > MAX_TRANSCRIPT_BYTES:
            _reject()
        has_transcript = any(document.get(key) for key in TRANSCRIPT_KEYS)
        temporary_context = (tempfile.TemporaryDirectory(prefix="khenrix-memory-ingress-")
                             if has_transcript else contextlib.nullcontext())
        with temporary_context as temporary:
            if has_transcript:
                os.chmod(temporary, 0o700)
                destination = pathlib.Path(temporary) / "transcript.jsonl"
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(snapshot.encode("utf-8"))
                for key in TRANSCRIPT_KEYS:
                    if document.get(key):
                        safe[key] = str(destination)
            payload = json.dumps(safe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(payload) > MAX_HOOK_BYTES:
                _reject()
            yield payload
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise IngressRejected("memory hook input rejected") from None
