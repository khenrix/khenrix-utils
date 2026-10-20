"""Managed, session-isolated Maka transport for blind interactive ballots."""
from __future__ import annotations

import json
import hashlib
import math
import os
import uuid
from pathlib import Path
from typing import Mapping

from .evaluation import JudgeTransportError, PresentedJudgeRequest, _digest
from .process import ProcessCommand, ProcessStatus, run_command


class NativeMakaTransport:
    """Normal installed CLI with an exactly allocated native journal.

    Explicit native bypass permits normal installed skills, MCP, files and network
    for this trusted same-user evaluator. Instructions and post-run journal write
    rejection do not enforce read-only access or prevent arbitrary shell writes.
    Each call owns one attempt; it never retries a model turn.
    """

    target_id = "maka"
    native_session_capture = True

    def __init__(self, *, launcher: Path, package_root: Path, capture_root: Path,
                 cwd: Path, node: str = "node", connection: str = "keychain-openai",
                 auth_route: str = "api-key-relay", timeout: float = 180) -> None:
        if auth_route not in {"api-key-relay", "chatgpt-subscription"}:
            raise JudgeTransportError("Maka auth route must be explicit")
        if not isinstance(connection, str) or not connection.strip() or "\x00" in connection:
            raise JudgeTransportError("Maka connection must be explicit")
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise JudgeTransportError("Maka timeout must be bounded")
        self.launcher = Path(launcher).resolve(strict=True)
        self.package_root = Path(package_root).resolve(strict=True)
        self.cwd = Path(cwd).resolve(strict=True)
        self.capture_root = Path(capture_root)
        self.node, self.connection, self.auth_route, self.timeout = node, connection, auth_route, timeout

    def _helper(self, spec: dict, attempt: Path, label: str) -> dict:
        result = run_command(ProcessCommand(
            (self.node, str(Path(__file__).with_name("maka_native_session.mjs"))),
            json.dumps(spec).encode(), cwd=self.cwd, timeout=15,
            trusted_native=True, max_output_bytes=4 * 1024 * 1024))
        self._save(attempt / (label + ".json"), result.stdout)
        self._save(attempt / (label + ".stderr.bin"), result.stderr)
        if result.status != ProcessStatus.EXIT or result.returncode != 0 or not result.capture_complete:
            raise JudgeTransportError("Maka native Session capture is unavailable")
        try:
            data = json.loads(result.stdout)
        except (ValueError, UnicodeError) as error:
            raise JudgeTransportError("Maka native Session capture is invalid") from error
        if not isinstance(data, dict):
            raise JudgeTransportError("Maka native Session capture is invalid")
        return data

    @staticmethod
    def _save(path: Path, raw: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    def judge(self, request: PresentedJudgeRequest) -> Mapping[str, object]:
        if (not isinstance(request, PresentedJudgeRequest) or request.panel_id != "interactive"
                or request.auth_route != self.auth_route or request.requested_model != "gpt-6.1-sol"
                or request.effort != "xhigh"):
            raise JudgeTransportError("Native Maka evaluator route is not pinned")
        prompt = ("Evaluate the supplied blind answers using the criterion. Use installed skills and authorized "
                  "read tools when needed. Do not modify shared repositories or services, send messages, "
                  "start subagents, or perform external writes. Return only a JSON object with exactly one "
                  "field, winner, whose value is A, B, tie, or abstain.\n" + json.dumps({
                      "criterion": request.criterion, "answer_a": request.answer_a,
                      "answer_b": request.answer_b}, ensure_ascii=False))
        response = dict(self.evaluate(prompt))
        try:
            answer = json.loads(response.pop("answer"))
        except (ValueError, UnicodeError) as error:
            raise JudgeTransportError("Maka native answer is malformed") from error
        if not isinstance(answer, dict) or set(answer) != {"winner"} or answer["winner"] not in {"A", "B", "tie", "abstain"}:
            raise JudgeTransportError("Maka native answer is malformed")
        return {"winner": answer["winner"], **response}

    def evaluate(self, prompt: str) -> Mapping[str, object]:
        """Run arbitrary permitted analysis through the same owned native path.

        The caller owns the analysis schema. The returned answer is bound to
        actual native read records and carries no inferred served-model identity.
        """
        if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
            raise JudgeTransportError("Maka evaluation prompt must be non-empty text")
        prompt = ("Perform a read-only evaluation with normal installed skills and authorized read tools. "
                  "Do not modify shared repositories or services, send messages, start subagents, "
                  "or perform external writes.\n" + prompt)
        if len(prompt.encode("utf-8")) > 4 * 1024 * 1024:
            raise JudgeTransportError("Maka evaluation prompt exceeds its owned cleanup read bound")
        request = PresentedJudgeRequest("native-analysis", "interactive", "", "", "",
                                        "gpt-6.1-sol", "xhigh", self.auth_route)
        self.capture_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.capture_root.lstat()
        if self.capture_root.is_symlink() or not self.capture_root.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise JudgeTransportError("Maka capture root must be an owned private directory")
        session_id = "fanout-eval-" + uuid.uuid4().hex
        attempt = self.capture_root / session_id
        attempt.mkdir(mode=0o700)
        spec = {"operation": "prepare", "session_id": session_id, "package_root": str(self.package_root),
                "cwd": str(self.cwd), "connection": self.connection, "auth_route": self.auth_route,
                "model": request.requested_model, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
        self._save(attempt / "request.bin", prompt.encode())
        status = "failed"
        launched = cleanup_attempted = False
        cleanup_complete = None
        owned = None
        try:
            owned = self._helper(spec, attempt, "session")
            if owned.get("session_id") != session_id or owned.get("permission_mode") != "bypass":
                raise JudgeTransportError("Maka Session allocation was misbound")
            argv = (str(self.launcher), "run", "-", "--connection", self.connection,
                    "--model", request.requested_model, "--thinking", request.effort,
                    "--resume", session_id, "--yolo", "--timeout", str(self.timeout))
            self._save(attempt / "launch.json", json.dumps({"argv": argv, "session_id": session_id,
                       "auth_route": self.auth_route, "input_sha256": hashlib.sha256(prompt.encode()).hexdigest()}).encode())
            # Native cancellation starts after startup; allow it to stop the Host turn
            # before the supervisor forces local process-group termination.
            launched = True
            result = run_command(ProcessCommand(argv, prompt.encode(), cwd=self.cwd, timeout=self.timeout + 30,
                trusted_native=True, max_output_bytes=4 * 1024 * 1024))
            spec = {**spec, "process_exited": result.status == ProcessStatus.EXIT,
                    "process_returncode": result.returncode}
            self._save(attempt / "stdout.bin", result.stdout)
            self._save(attempt / "stderr.bin", result.stderr)
            if result.status != ProcessStatus.EXIT or result.returncode != 0 or not result.capture_complete:
                cleanup_attempted = True
                cleanup_complete = False
                cleanup_complete = self._cleanup(spec, owned, attempt)
            journal = self._helper({**spec, **owned, "operation": "capture"}, attempt, "journal")
            if result.status != ProcessStatus.EXIT or result.returncode != 0 or not result.capture_complete:
                raise JudgeTransportError("Maka native evaluator did not complete")
            response = _native_response(request, prompt, result.stdout, journal, owned, self.connection, self.cwd)
            response["capture_sha256"] = hashlib.sha256((attempt / "journal.json").read_bytes()).hexdigest()
            status = "completed"
            return response
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise JudgeTransportError("Maka native evaluator evidence is invalid") from error
        finally:
            if launched and status != "completed" and not cleanup_attempted:
                cleanup_attempted = True
                try:
                    cleanup_complete = self._cleanup(spec, owned, attempt)
                except Exception:
                    cleanup_complete = False
            self._save(attempt / "attempt.json", json.dumps({"status": status,
                       "session_id": session_id, "automatic_retries": 0,
                       "cleanup_complete": cleanup_complete}).encode())

    def _cleanup(self, spec: dict, owned: dict, attempt: Path) -> bool:
        cleanup = self._helper({**spec, **owned, "operation": "cleanup"}, attempt, "cleanup")
        if (cleanup.get("session_id") != owned["session_id"] or cleanup.get("permission_mode") != "bypass"
                or cleanup.get("cleanup_complete") is not True):
            raise JudgeTransportError("Maka owned native turn cleanup is unconfirmed")
        return True


def _native_response(request, prompt, stdout, journal, owned, connection, cwd):
    from .evaluation import native_tool_is_write

    if any(journal.get(key) != value for key, value in owned.items()):
        raise JudgeTransportError("Maka native journal ownership changed")
    invocation, records = journal["invocation"], journal["records"]
    identity = {name: invocation[name] for name in ("sessionId", "runId", "turnId", "invocationId")}
    if (identity["sessionId"] != owned["session_id"] or not all(isinstance(v, str) and v for v in identity.values())
            or not isinstance(records, list) or not records
            or any(any(event.get(k) != v for k, v in identity.items()) for event in records)):
        raise JudgeTransportError("Maka native invocation is misbound")
    opening = invocation["opening"]
    if (opening["route"]["modelId"] != request.requested_model
            or opening["route"]["llmConnectionSlug"] != connection
            or opening["configuration"]["cwd"] != str(cwd)
            or opening["configuration"]["permissionMode"] != "bypass"
            or opening["configuration"]["collaborationMode"] != "agent"
            or invocation["terminalEvent"].get("status") != "completed"
            or invocation["terminalEvent"] not in records):
        raise JudgeTransportError("Maka native route or terminal evidence changed")
    content = [event for event in records if isinstance(event.get("content"), dict)]
    if not any(e["role"] == "user" and e["content"].get("text") == prompt for e in content):
        raise JudgeTransportError("Maka native prompt was not observed")
    final = [e["content"]["text"] for e in content if e["role"] == "model"
             and e["content"].get("kind") == "text" and e.get("partial") is False]
    if not final or not final[-1].strip() or final[-1].strip() != stdout.decode("utf-8").strip():
        raise JudgeTransportError("Maka native answer differs from captured stdout")
    tools = [e for e in content if e["content"].get("kind") == "function_call"]
    results = {e["content"].get("id"): e for e in content if e["content"].get("kind") == "function_response"}
    dispatches = [e for e in records if isinstance(e.get("actions"), dict) and "toolDispatch" in e["actions"]]
    dispatched = {e["actions"]["toolDispatch"]["providerToolCallId"]: e for e in dispatches}
    if len(dispatched) != len(dispatches) or len(dispatched) != len(tools):
        raise JudgeTransportError("Maka native tool dispatch evidence is incomplete")
    if any(native_tool_is_write(e["content"]["name"], e["content"].get("args"))
           or e["content"]["id"] not in results
           or e["content"]["id"] not in dispatched
           or dispatched[e["content"]["id"]]["actions"]["toolDispatch"]["toolName"] != e["content"]["name"]
           or results[e["content"]["id"]]["content"].get("name") != e["content"]["name"] for e in tools):
        raise JudgeTransportError("Maka native tools include a write or incomplete result")
    return {"answer": final[-1], "auth_route": request.auth_route,
            "requested_model": request.requested_model, "observed_model": None, "effort": request.effort,
            "session_id": identity["sessionId"], "run_id": identity["runId"], "turn_id": identity["turnId"],
            "invocation_id": identity["invocationId"], "tool_records": tools,
            "tool_results": [results[e["content"]["id"]] for e in tools],
            "tool_dispatches": dispatches, "tool_calls": len(dispatches)}
