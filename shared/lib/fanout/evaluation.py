"""Maka blind ballots bound to actual native sessions and read-tool evidence."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Mapping, Protocol

from .providers import RoutingTargetRegistry


class JudgeTransportError(ValueError):
    """A judgment transport cannot prove the required isolated invocation."""


_HEX = frozenset("0123456789abcdef")


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


@dataclass(frozen=True, slots=True)
class JudgeRequest:
    """Named final answers; the adapter alone derives the blind A/B presentation."""

    case_id: str
    panel_id: str
    criterion: str
    candidate_answer: str = field(repr=False)
    baseline_answer: str = field(repr=False)
    requested_model: str = ""
    effort: str = ""
    auth_route: str = ""
    order: str = "candidate-first"
    target_id: str = field(default="maka", init=False)

    def __post_init__(self) -> None:
        for label in ("case_id", "criterion", "candidate_answer", "baseline_answer",
                      "requested_model", "effort", "auth_route"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise JudgeTransportError(f"judge {label} must be non-empty text")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as error:
                raise JudgeTransportError(f"judge {label} is not UTF-8 text") from error
        profiles = {
            "interactive": ("api-key-relay", "gpt-6.1-sol", "xhigh"),
        }
        if self.panel_id == "interactive" and self.auth_route == "chatgpt-subscription":
            profiles["interactive"] = ("chatgpt-subscription", "gpt-6.1-sol", "xhigh")
        if self.panel_id not in profiles or (
            self.auth_route, self.requested_model, self.effort
        ) != profiles[self.panel_id]:
            raise JudgeTransportError("judge panel model/auth route is not pinned")
        if self.order not in {"candidate-first", "baseline-first"}:
            raise JudgeTransportError("judge presentation order is invalid")


@dataclass(frozen=True, slots=True)
class PresentedJudgeRequest:
    """Blind transport payload with no candidate label or presentation order."""

    case_id: str
    panel_id: str
    criterion: str
    answer_a: str = field(repr=False)
    answer_b: str = field(repr=False)
    requested_model: str = ""
    effort: str = ""
    auth_route: str = ""
    target_id: str = field(default="maka", init=False)


class NativeJudgeTransport(Protocol):
    """Normal native CLI binding with retained owned session evidence."""

    target_id: str
    native_session_capture: bool

    def judge(self, request: PresentedJudgeRequest) -> Mapping[str, object]: ...


def _presentation(request: JudgeRequest) -> tuple[str, str]:
    if not isinstance(request, JudgeRequest) or request.target_id != "maka":
        raise JudgeTransportError("Maka judge request is invalid")
    return ((request.candidate_answer, request.baseline_answer)
            if request.order == "candidate-first"
            else (request.baseline_answer, request.candidate_answer))


class MakaJudgeAdapter:
    """Bind blind ballots only to actual native session evidence."""

    target_id = "maka"

    def __init__(self, transport: NativeJudgeTransport) -> None:
        if transport.target_id != "maka" or "maka" not in RoutingTargetRegistry.default().target_ids:
            raise JudgeTransportError("quality judgment must use the Maka routing target")
        if getattr(transport, "native_session_capture", False) is not True:
            raise JudgeTransportError("Maka judgment requires native session capture")
        self._transport = transport

    def judge(self, request: JudgeRequest) -> dict[str, object]:
        first, second = _presentation(request)
        presented = PresentedJudgeRequest(
            request.case_id, request.panel_id, request.criterion,
            first, second, request.requested_model, request.effort, request.auth_route,
        )
        try:
            response = self._transport.judge(presented)
        except Exception as error:
            raise JudgeTransportError("Maka judge transport failed") from error
        return validate_native_maka_ballot(request, response)


def native_tool_is_write(name: str, arguments: object = None) -> bool:
    """Reject known mutations in completed evidence, not a pre-execution fence."""
    from .providers import _MCP_WRITE_TOOLS

    if name.rsplit("__", 1)[-1] in {tool for tools in _MCP_WRITE_TOOLS.values() for tool in tools}:
        return True
    if name in {"Write", "Edit", "apply_patch", "patch", "ScheduledTask", "GoalSet", "GoalClear"}:
        return True
    mutation = r"(?:^|__|_)(?:send|post|reply|forward|delete|create|update|edit|write|publish|deploy|restart|merge|push|invite|remove)(?:_|$)"
    if re.search(mutation, name, re.I):
        return True
    return isinstance(arguments, Mapping) and any(
        isinstance(arguments.get(key), str) and re.search(mutation, arguments[key], re.I)
        for key in ("action", "operation", "method", "command_name"))


def validate_native_maka_ballot(request: JudgeRequest, response: Mapping[str, object]) -> dict[str, object]:
    """Bind a native result; unavailable provider telemetry remains unavailable."""
    first, second = _presentation(request)
    required = {"winner", "auth_route", "requested_model", "observed_model", "effort", "session_id",
                "run_id", "turn_id", "invocation_id", "tool_records", "tool_results", "tool_dispatches", "tool_calls", "capture_sha256"}
    if not isinstance(response, Mapping) or set(response) != required:
        raise JudgeTransportError("Maka native structured ballot is incomplete")
    if (response["winner"] not in {"A", "B", "tie", "abstain"}
            or response["auth_route"] != request.auth_route or response["requested_model"] != request.requested_model
            or response["effort"] != request.effort or response["observed_model"] is not None
            or not _digest(response["capture_sha256"]) or type(response["tool_calls"]) is not int
            or not isinstance(response["tool_records"], list)
            or not isinstance(response["tool_results"], list)
            or not isinstance(response["tool_dispatches"], list)
            or len(response["tool_results"]) != len(response["tool_records"])
            or len(response["tool_dispatches"]) != len(response["tool_records"])
            or response["tool_calls"] != len(response["tool_records"])):
        raise JudgeTransportError("Maka native evidence does not match")
    if any(not isinstance(response[name], str) or not response[name] for name in
           ("session_id", "run_id", "turn_id", "invocation_id")):
        raise JudgeTransportError("Maka native identity is unavailable")
    return {"case_id": request.case_id, "panel_id": request.panel_id, "judge": "maka", "family": "openai",
            "order": request.order, "presented_a_sha256": hashlib.sha256(first.encode()).hexdigest(),
            "presented_b_sha256": hashlib.sha256(second.encode()).hexdigest(), **response}
