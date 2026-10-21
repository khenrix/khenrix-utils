"""Offline descendant-route admission and the documented Decisions wire contract."""
from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "shared/lib/fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_child_choice_contracts", PACKAGE / "__init__.py",
    submodule_search_locations=[str(PACKAGE)])
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)


def module():
    name = f"{SPEC.name}.child_choice"
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise
        pytest.fail("the disabled child-choice seam is not implemented")


def routes():
    choice = module()
    registry = fanout.ProviderRegistry.default()
    profiles = [registry.select(executor, "read-only", tier)
                for executor, tier in (("claude", "standard"), ("claude", "deep"),
                                       ("codex", "standard"), ("agy", "standard"))]
    # Synthetic local characterization only; these are not descendant launch proofs.
    return tuple(choice.ChildRoute(dataclasses.replace(profile, characterized=True, profile_sha256=None),
                                   "Synthetic bounded read-only route") for profile in profiles)


def policy(candidates, **changes):
    choice = module()
    return choice.ChildChoicePolicy(enabled=True,
        admitted_route_ids=frozenset(route.route_id for route in candidates),
        usable_route_ids=frozenset(route.route_id for route in candidates),
        minimum_confidence=0.7, **changes)


def budget(**changes):
    return module().ChoiceBudget(**{"remaining_calls": 1, "max_evidence_bytes": 10000,
                                   "max_child_seconds": 1800, **changes})


class Backend:
    backend_id = "openai-decisions/gpt-6-luna"

    def __init__(self, answer=None, failure=False):
        self.requests = []
        self.answer = answer
        self.failure = failure

    def choose(self, request):
        self.requests.append(request)
        if self.failure:
            raise OSError("transport details must not leak")
        return self.answer or module().BackendChoice("chosen", request.choices[0].route_id, 0.9)


def test_default_policy_cannot_call_backend_or_select_a_route():
    choice = module()
    candidates = routes()
    backend = Backend()
    result = choice.choose_child_route("synthetic task", candidates[0].profile, candidates,
                                       backend=backend, budget=budget())
    assert result.status == "disabled" and result.route_id is None
    assert result.native_launch_supported is False
    assert backend.requests == []


@pytest.mark.parametrize("cross_family, count", [(False, 2), (True, 4)])
def test_only_locally_admitted_eligible_routes_reach_backend(cross_family, count):
    choice = module()
    candidates = routes()
    backend = Backend()
    result = choice.choose_child_route("synthetic task", candidates[0].profile, candidates,
        policy=policy(candidates, allow_cross_family=cross_family), backend=backend, budget=budget())
    assert result.status == "chosen"
    assert result.eligible_route_ids == tuple(route.route_id for route in candidates[:count])
    assert tuple(option.route_id for option in backend.requests[0].choices) == result.eligible_route_ids
    assert result.profile_sha256 == candidates[0].profile.digest
    assert result.native_launch_supported is False


@pytest.mark.parametrize("role", ["executor", "planner", "evaluator", "merger", "owner"])
def test_outer_role_target_cannot_invoke_choice_backend(role):
    choice = module()
    candidates = routes()
    before = [route.profile.to_dict() for route in candidates]
    backend = Backend()
    result = choice.choose_child_route("task", candidates[0].profile, candidates, target_role=role,
        policy=policy(candidates), backend=backend, budget=budget())
    assert result.status == "blocked" and result.reason == "child-only"
    assert backend.requests == []
    assert [route.profile.to_dict() for route in candidates] == before


@pytest.mark.parametrize("invalid", ["unadmitted", "unusable", "capability", "uncharacterized",
                                     "roots", "owner", "write", "timeout"])
def test_local_authority_capability_and_child_budget_filter_before_backend(invalid):
    choice = module()
    candidates = routes()
    first, second = candidates[:2]
    selected_policy = policy(candidates)
    selected_budget = budget()
    if invalid in ("unadmitted", "unusable"):
        field = "admitted_route_ids" if invalid == "unadmitted" else "usable_route_ids"
        selected_policy = dataclasses.replace(selected_policy, **{field: frozenset({first.route_id})})
    else:
        change = {"characterized": False} if invalid == "uncharacterized" else (
            {"native_roots": None} if invalid == "roots" else
            {"role": "owner"} if invalid == "owner" else
            {"execution_class": "repo-write", "quality_tier": "standard"} if invalid == "write" else
            {"capabilities": dataclasses.replace(second.profile.capabilities, explicit_resume=False)})
        if invalid == "timeout":
            selected_budget = budget(max_child_seconds=900)
        else:
            second = choice.ChildRoute(dataclasses.replace(second.profile, profile_sha256=None, **change), "bad route")
            candidates = (first, second)
            selected_policy = policy(candidates)
    backend = Backend()
    result = choice.choose_child_route("task", first.profile, candidates,
        policy=selected_policy, backend=backend, budget=selected_budget)
    assert result.status == "fixed" and result.route_id == first.route_id
    assert backend.requests == []


@pytest.mark.parametrize("answer, reason", [
    (("refused", None, None), "refused"), (("chosen", "unapproved", 0.99), "invalid-choice"),
    (("chosen", None, 0.99), "invalid-choice"), (("chosen", "first", 0.2), "low-confidence"),
    (("chosen", "first", float("nan")), "invalid-choice"), (("chosen", "first", True), "invalid-choice"),
])
def test_backend_failure_states_are_explicit_and_fallback_never_widens(answer, reason):
    choice = module()
    candidates = routes()
    status, route_id, confidence = answer
    route_id = candidates[0].route_id if route_id == "first" else route_id
    backend = Backend(choice.BackendChoice(status, route_id, confidence))
    selected_policy = policy(candidates, fallback_route_id=candidates[1].route_id)
    result = choice.choose_child_route("task", candidates[0].profile, candidates,
        policy=selected_policy, backend=backend, budget=budget())
    assert (result.status, result.reason, result.route_id) == ("fallback", reason, candidates[1].route_id)
    outside = dataclasses.replace(selected_policy, fallback_route_id=candidates[2].route_id)
    result = choice.choose_child_route("task", candidates[0].profile, candidates,
        policy=outside, backend=backend, budget=budget())
    assert result.route_id is None and result.reason == reason


def test_transport_failure_consumes_one_call_without_retry_or_secret_error_text():
    choice = module()
    candidates = routes()
    backend = Backend(failure=True)
    allocation = budget()
    result = choice.choose_child_route("task", candidates[0].profile, candidates,
        policy=policy(candidates), backend=backend, budget=allocation)
    assert (result.status, result.reason, allocation.remaining_calls) == ("abstained", "backend-error", 0)
    assert "transport details" not in repr(result)
    again = choice.choose_child_route("task", candidates[0].profile, candidates,
        policy=policy(candidates), backend=backend, budget=allocation)
    assert again.reason == "budget" and len(backend.requests) == 1


@pytest.mark.parametrize("overrides", [{"remaining_calls": 0}, {"max_evidence_bytes": 1}, {"max_child_seconds": 0}])
def test_exhausted_budget_cannot_call_backend_or_use_fallback(overrides):
    choice = module()
    candidates = routes()
    backend = Backend()
    result = choice.choose_child_route("task", candidates[0].profile, candidates,
        policy=policy(candidates, fallback_route_id=candidates[0].route_id),
        backend=backend, budget=budget(**overrides))
    assert result.route_id is None and backend.requests == []


def response(route_ids, *, kind="choice", confidence=0.9):
    answer = {"type": kind, "name": "child_route"}
    if kind == "choice":
        answer.update(choice=route_ids[0], confidence=confidence,
                      probabilities=[{"value": value, "probability": probability}
                                     for value, probability in zip(route_ids, [0.75, 0.25])])
    return {"model": "gpt-6-luna", "answers": [answer], "usage": {
        "input_tokens": 42, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        "output_tokens": 0, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 42}}


def test_decisions_adapter_uses_documented_choice_schema_and_injected_transport():
    choice = module()
    candidates = routes()[:2]
    sent = []
    wire = response([route.route_id for route in candidates])
    def transport(method, path, body):
        sent.append((method, path, body))
        return wire
    backend = choice.DecisionsBackend(transport)
    result = choice.choose_child_route("synthetic task", candidates[0].profile, candidates,
        policy=policy(candidates), backend=backend, budget=budget())
    method, path, body = sent[0]
    assert (method, path, body["model"], body["input"]) == (
        "POST", "/v1/decisions", "gpt-6-luna", "synthetic task")
    question, = body["questions"]
    assert (question["type"], question["name"]) == ("choice", "child_route")
    assert [option["value"] for option in question["choices"]] == [route.route_id for route in candidates]
    assert result.status == "chosen" and result.confidence == 0.9
    assert json.loads(result.usage_json) == wire["usage"]


@pytest.mark.parametrize("broken", ["refusal", "wrong-model", "wrong-name", "wrong-type",
                                    "missing-probabilities", "unknown-probability", "nan", "empty", "not-object"])
def test_decisions_response_refusal_and_malformed_shapes_cannot_choose(broken):
    choice = module()
    candidates = routes()[:2]
    wire = response([route.route_id for route in candidates])
    answer = wire["answers"][0]
    if broken == "refusal":
        wire = response([], kind="refusal")
    elif broken == "wrong-model":
        wire["model"] = "unexpected-model"
    elif broken == "wrong-name":
        answer["name"] = "outer_role"
    elif broken == "wrong-type":
        answer["type"] = "score"
    elif broken == "missing-probabilities":
        del answer["probabilities"]
    elif broken == "unknown-probability":
        answer["probabilities"][0]["value"] = "unapproved"
    elif broken == "nan":
        answer["confidence"] = float("nan")
    elif broken == "empty":
        wire["answers"] = []
    else:
        wire = []
    backend = choice.DecisionsBackend(lambda *_: wire)
    result = choice.choose_child_route("task", candidates[0].profile, candidates,
        policy=policy(candidates), backend=backend, budget=budget())
    assert result.route_id is None
    assert result.reason == ("refused" if broken == "refusal" else "invalid-choice")


@pytest.mark.parametrize("changes", [{"fallback_route_id": []}, {"fallback_route_id": ""},
                                     {"minimum_confidence": float("nan")}, {"enabled": "yes"}])
def test_invalid_local_policy_is_rejected_before_it_can_reach_a_backend(changes):
    with pytest.raises(ValueError, match="policy"):
        module().ChildChoicePolicy(**changes)


def test_profile_change_cannot_reuse_an_admitted_route_id():
    choice = module()
    original = routes()[:2]
    changed = choice.ChildRoute(dataclasses.replace(original[1].profile,
        requested_model="unadmitted-model", profile_sha256=None), "different exact model")
    backend = Backend()
    result = choice.choose_child_route("task", original[0].profile, (original[0], changed),
        policy=policy(original), backend=backend, budget=budget())
    assert result.status == "fixed" and result.route_id == original[0].route_id
    assert backend.requests == []


def test_backend_threshold_is_bound_to_the_configured_backend_identity():
    choice = module()
    candidates = routes()
    backend = Backend()
    backend.backend_id = "future-jev-adapter"
    result = choice.choose_child_route("task", candidates[0].profile, candidates,
        policy=policy(candidates), backend=backend, budget=budget())
    assert result.reason == "backend-mismatch" and result.route_id is None
    assert backend.requests == []


def test_backend_neutral_protocol_accepts_an_explicitly_configured_future_adapter():
    choice = module()
    candidates = routes()
    backend = Backend()
    backend.backend_id = "future-jev-adapter"
    result = choice.choose_child_route("task", candidates[0].profile, candidates,
        policy=dataclasses.replace(policy(candidates), backend_id="future-jev-adapter"),
        backend=backend, budget=budget())
    assert result.status == "chosen" and result.backend_id == "future-jev-adapter"


def test_missing_usage_is_unavailable_rather_than_reported_zero():
    choice = module()
    candidates = routes()[:2]
    wire = response([route.route_id for route in candidates])
    del wire["usage"]
    result = choice.choose_child_route("task", candidates[0].profile, candidates,
        policy=policy(candidates), backend=choice.DecisionsBackend(lambda *_: wire), budget=budget())
    assert result.status == "chosen" and result.usage_json is None
