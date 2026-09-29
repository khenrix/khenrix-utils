"""Maka-only, tool-free blind-ballot boundary for arbitrary quality cases.

This is a transport adapter, not an executor or a Maka CLI command builder.
The live transport must be independently characterized to enforce zero tools;
the adapter refuses missing characterization and checks each result against it.
"""
from __future__ import annotations

import hashlib
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
    tools: tuple[()] = field(default=(), init=False)

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
            "interactive": ("api-key-relay", "gpt-6-sol", "xhigh"),
            "harbor": ("harbor-lab", "gpt-5.6-sol", "xhigh"),
        }
        if self.panel_id not in profiles or (
            self.auth_route, self.requested_model, self.effort
        ) != profiles[self.panel_id]:
            raise JudgeTransportError("judge panel model/auth route is not pinned")
        if self.order not in {"candidate-first", "baseline-first"}:
            raise JudgeTransportError("judge presentation order is invalid")


@dataclass(frozen=True, slots=True)
class PresentedJudgeRequest:
    """Tool-free transport payload with no candidate label or presentation order."""

    case_id: str
    panel_id: str
    criterion: str
    answer_a: str = field(repr=False)
    answer_b: str = field(repr=False)
    requested_model: str = ""
    effort: str = ""
    auth_route: str = ""
    target_id: str = field(default="maka", init=False)
    tools: tuple[()] = field(default=(), init=False)


class ToolFreeMakaTransport(Protocol):
    """Live binding supplied by a separately characterized Maka route."""

    target_id: str
    no_tools_characterization_sha256: str

    def judge(self, request: PresentedJudgeRequest) -> Mapping[str, object]: ...


class MakaJudgeAdapter:
    """Reject uncharacterized transports and retain only a structured ballot."""

    target_id = "maka"

    def __init__(self, transport: ToolFreeMakaTransport) -> None:
        if transport.target_id != "maka" or "maka" not in RoutingTargetRegistry.default().target_ids:
            raise JudgeTransportError("quality judgment must use the Maka routing target")
        if not _digest(transport.no_tools_characterization_sha256):
            raise JudgeTransportError("Maka no-tools transport is not characterized")
        self._transport = transport
        self._boundary_sha256 = transport.no_tools_characterization_sha256

    def judge(self, request: JudgeRequest) -> dict[str, object]:
        if not isinstance(request, JudgeRequest) or request.tools or request.target_id != "maka":
            raise JudgeTransportError("Maka judge request must have no tools")
        first, second = (
            (request.candidate_answer, request.baseline_answer)
            if request.order == "candidate-first"
            else (request.baseline_answer, request.candidate_answer)
        )
        presented = PresentedJudgeRequest(
            request.case_id, request.panel_id, request.criterion,
            first, second, request.requested_model, request.effort, request.auth_route,
        )
        try:
            response = self._transport.judge(presented)
        except Exception as error:
            raise JudgeTransportError("Maka judge transport failed") from error
        if not isinstance(response, Mapping) or set(response) != {
            "winner", "auth_route", "requested_model", "observed_model",
            "effort", "tool_calls", "tool_boundary_sha256",
        }:
            raise JudgeTransportError("Maka judge returned an incomplete structured ballot")
        if response["winner"] not in {"A", "B", "tie", "abstain"}:
            raise JudgeTransportError("Maka judge returned an invalid A/B winner")
        if (response["auth_route"] != request.auth_route
                or response["requested_model"] != request.requested_model
                or response["observed_model"] != request.requested_model
                or response["effort"] != request.effort):
            raise JudgeTransportError("Maka judge model, effort, or auth route was not observed")
        if (type(response["tool_calls"]) is not int or response["tool_calls"] != 0
                or response["tool_boundary_sha256"]
                != self._boundary_sha256
                or self._transport.no_tools_characterization_sha256 != self._boundary_sha256):
            raise JudgeTransportError("Maka judge no-tools boundary was not witnessed")
        return {
            "case_id": request.case_id, "panel_id": request.panel_id,
            "judge": "maka", "family": "openai", "order": request.order,
            "winner": response["winner"], "auth_route": response["auth_route"],
            "requested_model": response["requested_model"],
            "observed_model": response["observed_model"], "effort": response["effort"],
            "tool_boundary_sha256": response["tool_boundary_sha256"],
            "tool_calls": response["tool_calls"],
            "presented_a_sha256": hashlib.sha256(first.encode("utf-8")).hexdigest(),
            "presented_b_sha256": hashlib.sha256(second.encode("utf-8")).hexdigest(),
        }
