"""Opt-in choice among admitted descendant descriptors; never launches children."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Mapping, Protocol

from .durable_io import canonical_json
from .providers import ExecutorProfile


def _probability(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


@dataclass(frozen=True, slots=True)
class ChildRoute:
    profile: ExecutorProfile
    description: str

    def __post_init__(self) -> None:
        if (not isinstance(self.profile, ExecutorProfile) or not isinstance(self.description, str)
                or not self.description.strip()):
            raise ValueError("a child route requires an exact profile and description")

    @property
    def route_id(self) -> str:
        return f"{self.profile.binding_key}/{self.profile.digest}"


@dataclass(frozen=True, slots=True)
class ChoiceOption:
    route_id: str
    description: str


@dataclass(frozen=True, slots=True)
class ChoiceRequest:
    task: str
    choices: tuple[ChoiceOption, ...]

    def evidence_bytes(self) -> bytes:
        return canonical_json({"input": self.task, "choices": [
            {"route_id": option.route_id, "description": option.description} for option in self.choices]})


@dataclass(frozen=True, slots=True)
class BackendChoice:
    status: str
    route_id: str | None = None
    confidence: float | None = None
    usage_json: str | None = None


class ChoiceBackend(Protocol):
    backend_id: str

    def choose(self, request: ChoiceRequest) -> BackendChoice: ...


class DecisionTransport(Protocol):
    def __call__(self, method: str, path: str, body: Mapping[str, object]) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class ChildChoicePolicy:
    enabled: bool = False
    admitted_route_ids: frozenset[str] = frozenset()
    usable_route_ids: frozenset[str] = frozenset()
    allow_cross_family: bool = False
    backend_id: str = "openai-decisions/gpt-6-luna"
    minimum_confidence: float = 1.0
    fallback_route_id: str | None = None

    def __post_init__(self) -> None:
        if (type(self.enabled) is not bool or type(self.allow_cross_family) is not bool
                or not _probability(self.minimum_confidence)
                or not isinstance(self.backend_id, str) or not self.backend_id
                or (self.fallback_route_id is not None and (
                    not isinstance(self.fallback_route_id, str) or not self.fallback_route_id))
                or any(not isinstance(ids, frozenset) or any(not isinstance(value, str) or not value for value in ids)
                       for ids in (self.admitted_route_ids, self.usable_route_ids))):
            raise ValueError("invalid child-choice admission policy")


@dataclass(slots=True)
class ChoiceBudget:
    """A local call allocation and evidence/timeout caps, not a cost estimate."""

    remaining_calls: int = 0
    max_evidence_bytes: int = 0
    max_child_seconds: int = 0

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in (
                self.remaining_calls, self.max_evidence_bytes, self.max_child_seconds)):
            raise ValueError("choice budget must contain nonnegative integers")

    def reserve(self, evidence_bytes: int) -> bool:
        if self.remaining_calls < 1 or evidence_bytes > self.max_evidence_bytes:
            return False
        self.remaining_calls -= 1
        return True


@dataclass(frozen=True, slots=True)
class ChildChoiceResult:
    status: str
    reason: str
    eligible_route_ids: tuple[str, ...] = ()
    route_id: str | None = None
    profile_sha256: str | None = None
    backend_id: str | None = None
    confidence: float | None = None
    usage_json: str | None = None
    native_launch_supported: bool = field(default=False, init=False)


def choose_child_route(task: str, parent_profile: ExecutorProfile, routes: tuple[ChildRoute, ...], *,
                       policy: ChildChoicePolicy = ChildChoicePolicy(),
                       backend: ChoiceBackend | None = None, budget: ChoiceBudget | None = None,
                       target_role: str = "child") -> ChildChoiceResult:
    """Select an ID without granting a provider, role, tool, write or launch authority."""
    if not policy.enabled:
        return ChildChoiceResult("disabled", "disabled")
    if target_role != "child" or parent_profile.role != "executor":
        return ChildChoiceResult("blocked", "child-only")
    if not isinstance(task, str) or not task.strip():
        return ChildChoiceResult("blocked", "invalid-task")
    if budget is None or budget.max_child_seconds <= 0:
        return ChildChoiceResult("blocked", "budget")
    if (any(not isinstance(route, ChildRoute) for route in routes)
            or len({route.route_id for route in routes}) != len(routes)):
        return ChildChoiceResult("blocked", "invalid-routes")
    eligible = {}
    for route in routes:
        profile = route.profile
        capabilities = profile.capabilities
        if (route.route_id not in policy.admitted_route_ids or route.route_id not in policy.usable_route_ids
                or (not policy.allow_cross_family and profile.executor_id != parent_profile.executor_id)
                or profile.role != "executor" or profile.execution_class != "read-only"
                or profile.characterized is not True or profile.native_roots is None
                or profile.native_roots != parent_profile.native_roots
                or not all(value is True for value in (capabilities.explicit_resume,
                                                       capabilities.structured_output, capabilities.stdin_prompt))
                or (profile.executor_id == "claude" and capabilities.assigned_session_id is not True)
                or profile.default_timeout > budget.max_child_seconds):
            continue
        eligible[route.route_id] = route
    ids = tuple(eligible)

    def result(status: str, reason: str, route_id: str | None = None,
               answer: BackendChoice | None = None) -> ChildChoiceResult:
        return ChildChoiceResult(status, reason, ids, route_id,
            eligible[route_id].profile.digest if route_id is not None else None,
            getattr(backend, "backend_id", None),
            answer.confidence if answer is not None and _probability(answer.confidence) else None,
            answer.usage_json if answer is not None else None)

    def abstain(reason: str, answer: BackendChoice | None = None) -> ChildChoiceResult:
        fallback = policy.fallback_route_id
        if fallback in eligible:
            return result("fallback", reason, fallback, answer)
        return result("abstained", reason, answer=answer)

    if not eligible:
        return result("blocked", "no-eligible-routes")
    if len(eligible) == 1:
        return result("fixed", "single-eligible-route", ids[0])
    if backend is None:
        return abstain("backend-unavailable")
    if backend.backend_id != policy.backend_id:
        return abstain("backend-mismatch")
    request = ChoiceRequest(task, tuple(ChoiceOption(route.route_id,
        f"{route.description}; model={route.profile.requested_model}; effort={route.profile.requested_effort}")
        for route in eligible.values()))
    if not budget.reserve(len(request.evidence_bytes())):
        return result("blocked", "budget")
    try:
        answer = backend.choose(request)
    except Exception:
        return abstain("backend-error")
    if not isinstance(answer, BackendChoice):
        return abstain("invalid-choice")
    if answer.status == "refused":
        return abstain("refused", answer)
    if (answer.status != "chosen" or not isinstance(answer.route_id, str)
            or answer.route_id not in eligible or not _probability(answer.confidence)):
        return abstain("invalid-choice", answer)
    if answer.confidence < policy.minimum_confidence:
        return abstain("low-confidence", answer)
    return result("chosen", "backend-choice", answer.route_id, answer)


class DecisionsBackend:
    """Documented POST /v1/decisions choice schema; transport/credentials are injected."""

    backend_id = "openai-decisions/gpt-6-luna"

    def __init__(self, transport: DecisionTransport):
        self._transport = transport

    def choose(self, request: ChoiceRequest) -> BackendChoice:
        if not 2 <= len(request.choices) <= 255:
            return BackendChoice("invalid")
        body = {"model": "gpt-6-luna", "input": request.task, "questions": [{
            "type": "choice", "name": "child_route",
            "instructions": "Choose the eligible read-only child route best suited to this task.",
            "choices": [{"value": option.route_id, "description": option.description}
                        for option in request.choices]}]}
        raw = self._transport("POST", "/v1/decisions", body)
        if not isinstance(raw, Mapping) or raw.get("model") != "gpt-6-luna":
            return BackendChoice("invalid")
        usage = raw.get("usage")
        usage_json = canonical_json(usage).decode("utf-8") if isinstance(usage, Mapping) else None
        answers = raw.get("answers")
        if not isinstance(answers, list) or len(answers) != 1:
            return BackendChoice("invalid", usage_json=usage_json)
        answer = answers[0]
        if not isinstance(answer, Mapping) or answer.get("name") != "child_route":
            return BackendChoice("invalid", usage_json=usage_json)
        if answer.get("type") == "refusal":
            return BackendChoice("refused", usage_json=usage_json)
        ids = {option.route_id for option in request.choices}
        probabilities = answer.get("probabilities")
        if (answer.get("type") != "choice" or not isinstance(answer.get("choice"), str)
                or answer["choice"] not in ids or not _probability(answer.get("confidence"))
                or not isinstance(probabilities, list) or len(probabilities) != len(ids)
                or any(not isinstance(item, Mapping) or not isinstance(item.get("value"), str)
                       or item["value"] not in ids or not _probability(item.get("probability"))
                       for item in probabilities)
                or {item["value"] for item in probabilities} != ids
                or not math.isclose(sum(item["probability"] for item in probabilities), 1.0, abs_tol=1e-5)):
            return BackendChoice("invalid", usage_json=usage_json)
        return BackendChoice("chosen", answer["choice"], answer["confidence"], usage_json)
