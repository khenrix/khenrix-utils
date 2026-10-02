"""Synthetic wire fixtures prove capture binding, never an externally served model."""
from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import os
import threading
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from dataclasses import asdict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.lib.fanout.broker import Broker, BrokerListener, BrokerResponse
from shared.lib.fanout.container_broker import PermitLedger
from shared.lib.fanout.container_routes import PreparedOutbound
from shared.lib.fanout.controller import create_lifecycle_controller
from shared.lib.fanout.providers import CodexAdapter
from test_fanout_container_upstream import _route, _routed_permit
from test_fanout_container_upstream import connection_factory, upstream_module  # noqa: F401


MODEL = "gpt-6.1-sol"


def _proof():
    try:
        return importlib.import_module("shared.lib.fanout.canary_proof")
    except ModuleNotFoundError:
        pytest.fail("the scoped Codex canary proof is not implemented")


def _sse(*events):
    return b"".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                    for event in events)


def _response(model=MODEL, **extra):
    return {"id": "resp_fixture", "object": "response", "status": "completed",
            "model": model, "error": None, "incomplete_details": None, **extra}


@pytest.mark.parametrize("mime", ["application/json", "text/event-stream"])
def test_actual_complete_response_model_is_extracted_from_body(mime):
    response = _response()
    raw = (json.dumps(response).encode() if mime == "application/json" else
           _sse({"type": "response.created", "response": {**response, "status": "in_progress"}},
                {"type": "response.completed", "response": response}))
    assert _proof().response_model(raw, mime) == ("resp_fixture", MODEL)


@pytest.mark.parametrize("response", [
    _response(None), _response("gpt-other"), _response(status="incomplete"),
    _response(error={"message": "fixture error"}), _response(incomplete_details={"reason": "max_output_tokens"}),
    {"model": MODEL, "input": []},
])
def test_missing_wrong_error_incomplete_and_requested_only_model_refuse(response):
    proof = _proof()
    with pytest.raises(ValueError):
        proof.response_model(json.dumps(response).encode(), "application/json")


@pytest.mark.parametrize("events", [
    [{"type": "response.created", "response": _response()}],
    [{"type": "response.completed", "response": _response()},
     {"type": "response.completed", "response": _response(id="resp_other")}],
    [{"type": "response.created", "response": _response("gpt-other")},
     {"type": "response.completed", "response": _response()}],
    [{"type": "error", "message": "fixture"},
     {"type": "response.completed", "response": _response()}],
])
def test_all_sse_events_must_describe_one_complete_nonerror_response(events):
    proof = _proof()
    with pytest.raises(ValueError):
        proof.response_model(_sse(*events), "text/event-stream")


def test_partial_created_event_does_not_replace_completed_response_model():
    raw = _sse({"type": "response.created", "response": {"id": "resp_fixture", "status": "in_progress"}},
        {"type": "response.output_text.delta", "delta": "fixture"},
        {"type": "response.completed", "response": _response()})
    assert _proof().response_model(raw, "text/event-stream") == ("resp_fixture", MODEL)


@pytest.mark.parametrize("message", ["model rerouted: gpt-6.1-sol -> other (fallback)", "fixture warning"])
def test_codex_native_error_item_remains_failure_after_turn_completed(message):
    stdout = b"\n".join(json.dumps(event).encode() for event in (
        {"type": "thread.started", "thread_id": "fixture-session"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": message}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "fixture answer"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ))
    parsed = CodexAdapter().parse(stdout)
    assert parsed.error == message
    assert parsed.observed_model is None


def test_native_listener_consumes_exact_claim_and_records_full_delivery(tmp_path):
    controller = create_lifecycle_controller(tmp_path / "controller")
    ledger = PermitLedger(controller.root / "native-ledger", controller)
    route = _route("codex")
    permit = replace(_routed_permit(route, "/v1/responses"), max_response_bytes=4096)
    permit_id = ledger.register(permit, 1)
    raw = json.dumps(_response()).encode()

    class Sender:
        def preview(self, method, path, headers, body):
            return PreparedOutbound(route.profile.origin, path, body, MODEL, route.digest)

        def __call__(self, method, path, headers, body):
            return BrokerResponse(200, {"content-type": "application/json"}, raw)

    broker = Broker(permit, credential="fixture-host-only", send=Sender())
    try:
        listener = BrokerListener(broker, ledger_binding=(ledger, permit_id, 1))
    except TypeError:
        pytest.fail("native listener does not use exact ledger reservations")
    with listener:
        connection = http.client.HTTPConnection(listener.host, listener.port, timeout=3)
        connection.request("POST", "/v1/responses", json.dumps({"model": MODEL, "input": []}),
            {"content-type": "application/json", "x-fanout-seat": permit.placeholder_key})
        response = connection.getresponse()
        delivered = response.read()
        connection.close()
        assert response.status == 200
    records = ledger.request_records(permit_id, 1)
    assert len(records) == 1
    assert records[0]["state"] == "settled"
    assert records[0]["response_digest"] == hashlib.sha256(delivered).hexdigest()
    with ledger._locked() as state:
        assert state["send_claims"] == {records[0]["request_id"]: True}
    assert listener.native_deliveries == ({"request_id": records[0]["request_id"],
        "status": 200, "content_type": "application/json", "body_sha256": hashlib.sha256(delivered).hexdigest(),
        "delivery": "native-http-complete"},)
    assert listener.native_failed is False


def test_native_partial_stream_is_uncertain_and_fenced_without_guest_ack(tmp_path):
    controller = create_lifecycle_controller(tmp_path / "controller")
    ledger = PermitLedger(controller.root / "native-ledger", controller)
    route = _route("codex")
    permit = replace(_routed_permit(route, "/v1/responses"), max_response_bytes=4096)
    permit_id = ledger.register(permit, 1)

    class Sender:
        def preview(self, method, path, headers, body):
            return PreparedOutbound(route.profile.origin, path, body, MODEL, route.digest)

        def __call__(self, method, path, headers, body):
            def stream():
                yield b'data: {"type":"response.created","response":{"id":"fixture"}}\n\n'
                raise OSError("fixture interrupted stream")
            return BrokerResponse(200, {"content-type": "text/event-stream"}, stream())

    listener = BrokerListener(Broker(permit, credential="fixture-only", send=Sender()),
        ledger_binding=(ledger, permit_id, 1))
    with listener:
        connection = http.client.HTTPConnection(listener.host, listener.port, timeout=3)
        connection.request("POST", "/v1/responses", json.dumps({"model": MODEL, "input": []}),
            {"content-type": "application/json", "x-fanout-seat": permit.placeholder_key})
        with pytest.raises(http.client.IncompleteRead):
            connection.getresponse().read()
        connection.close()
    assert listener.native_failed is True
    assert listener.native_deliveries == ()
    rows = ledger.request_records(permit_id, 1)
    assert len(rows) == 1 and rows[0]["state"] == "uncertain"
    with pytest.raises(ValueError, match="fenced"):
        ledger.require_permit(permit_id, 1, permit)


def test_context_cannot_replay_a_settled_turn(turn_case):
    case = turn_case
    context = _issue(case)
    request = replace(context.request, canary_capture=context)
    result = case.runtime.run_provider(request, registry=case.registry)
    with pytest.raises(case.runtime.ProviderRequestError, match="consumed"):
        case.runtime.run_provider(request, registry=case.registry)
    assert len(case.made) == 1
    assert _proof().reopen_turn(case.coordinator, case.packet, case.seat, 1, result)["response_model"] == MODEL


def test_resume_requires_original_persisted_first_barrier_and_peer(turn_case):
    case = turn_case
    context = _issue(case)
    result = case.runtime.run_provider(replace(context.request, canary_capture=context), registry=case.registry)
    payload = b"unsigned peer substitution"
    peer = SimpleNamespace(payload=payload, packet_sha256=hashlib.sha256(payload).hexdigest())
    with pytest.raises(case.runtime.ProviderRequestError, match="original.*barrier"):
        _proof().issue_turn(case.coordinator, case.packet, case.seat, 2, case.route, prior=result, peer=peer)
    assert case.loads == ["fixture"]


@pytest.mark.parametrize("second", ["failed", "wrong-model"])
def test_every_request_must_succeed_with_exact_model_for_positive_turn(turn_case, monkeypatch, second):
    from shared.lib.fanout import providers
    from shared.lib.fanout.controller import read_evidence
    case = turn_case
    first_process = providers.run_command

    def mixed_process(command):
        process = first_process(command)
        case.response.status = 500 if second == "failed" else 200
        case.response.chunks = [json.dumps({"error": {"message": "fixture failure", "type": "server_error"}}
            if second == "failed" else _response("other-model")).encode()]
        port_option = next(arg for arg in command.argv if arg.startswith("model_providers.fanout_broker.base_url="))
        marker = next(arg for arg in command.argv if arg.startswith("model_providers.fanout_broker.http_headers="))
        connection = http.client.HTTPConnection("127.0.0.1", int(port_option.split(":")[-1].split("/")[0]), timeout=3)
        connection.request("POST", "/v1/responses", json.dumps({"model": MODEL, "input": []}),
            {"content-type": "application/json", "x-fanout-seat": marker.split('="')[-1].split('"')[0]})
        connection.getresponse().read()
        connection.close()
        return process

    monkeypatch.setattr(providers, "run_command", mixed_process)
    context = _issue(case)
    with pytest.raises((ValueError, case.runtime.ProviderRequestError)):
        case.runtime.run_provider(replace(context.request, canary_capture=context), registry=case.registry)
    assert len(case.made) == 2
    assert not (case.artifacts.root / context.request.artifact_prefix / "codex-canary-proof.json").exists()
    if second == "failed":
        records = context._held["ledger"].request_records(context._held["permit_id"], 1)
        assert sorted(row["state"] for row in records) == ["settled", "uncertain"]
        evidence = [read_evidence(case.controller, "codex-readonly-canary", path.name,
            hashlib.sha256(path.read_bytes()).hexdigest())
            for path in (case.controller.root / "receipts").glob("*.json")
            if json.loads(path.read_bytes())["category"] == "codex-readonly-canary"]
        failures = [item for item in evidence if item["schema_version"] == "fanout-codex-canary-failure-v1"]
        assert len(failures) == 1
        assert failures[0]["failed_or_partial_provider_bytes"] == "unavailable-at-host-callback"
        assert len(failures[0]["complete_wire_captures"]) == 1
        assert len(failures[0]["upstream_failures"]) == 1


@pytest.fixture
def turn_case(tmp_path, monkeypatch, connection_factory):
    from scripts import fanout_smoke as smoke
    from shared.lib.fanout import providers
    from shared.lib.fanout.container_source import release_source_binding
    from shared.lib.fanout import container_credentials
    runtime = smoke.fanout
    root = tmp_path
    repository = smoke._repository(root)
    baseline = runtime.capture_repository_baseline(repository)
    controller = runtime.create_lifecycle_controller(root / "controller")
    artifacts = runtime.ArtifactStore(root / "artifacts")
    registry = runtime.ProviderRegistry.for_installed(smoke.EXECUTORS,
        version_probe=lambda executor: {"claude": "2.1.284", "codex": "0.160.0", "agy": "1.2.13"}[executor])
    packet = smoke._packet(repository, smoke._plan(live=True), run_id=smoke._live_run_id(root))
    inputs = smoke._live_inputs(packet, baseline, registry, smoke._live_source_hash(), release_source_binding())
    route = _route("codex")
    inputs = replace(inputs, skill_manifests={**dict(inputs.skill_manifests), "codex-canary-route": route.digest})
    authority = runtime.LocalAnchorAuthority.bootstrap(root / "authority", run_root=root / "journal", repo_root=repository)
    journal, owner = runtime.RunJournal.create(root / "journal", inputs, anchor_store=authority)
    seats, _ = smoke._seats(root, baseline, controller, artifacts, restore=False)
    seat = next(item for item in seats if item.executor_id == "codex")
    coordinator = smoke._live_coordinator(root, artifacts, journal, owner, controller,
        baseline, registry, smoke._FakeMemory(root, artifacts), runtime.run_provider)
    made, response = connection_factory
    response.chunks = [json.dumps(_response()).encode()]
    loads = []

    class Credential:
        fingerprint = "c" * 64

        def __init__(self, profile):
            pass

        def load(self):
            loads.append("fixture")
            return "fixture-host-only"

    monkeypatch.setattr(container_credentials, "HostCredentialSource", Credential)

    def process(command):
        port_option = next(arg for arg in command.argv if arg.startswith("model_providers.fanout_broker.base_url="))
        port = int(port_option.split(":")[-1].split("/")[0])
        marker = next(arg for arg in command.argv if arg.startswith("model_providers.fanout_broker.http_headers="))
        placeholder = marker.split('="')[-1].split('"')[0]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("POST", "/v1/responses", json.dumps({"model": MODEL, "input": [], "stream": False}),
            {"content-type": "application/json", "x-fanout-seat": placeholder})
        reply = connection.getresponse()
        reply.read()
        connection.close()
        assert reply.status == 200
        session = "fixture-session"
        home = Path(command.environment["HOME"])
        rollout = home / ".codex/sessions/fixture.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": session}}) + "\n")
        stdout = b"\n".join(json.dumps(event).encode() for event in (
            {"type": "thread.started", "thread_id": session},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "fixture answer"}},
            {"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}},
        ))
        return runtime.ProcessResult(runtime.ProcessStatus.EXIT, 0, stdout, b"")

    monkeypatch.setattr(providers, "run_command", process)
    yield SimpleNamespace(smoke=smoke, runtime=runtime, root=root, controller=controller,
        artifacts=artifacts, registry=registry, packet=packet, seat=seat, coordinator=coordinator,
        route=route, loads=loads, response=response, made=made)
    journal.close()
    artifacts.close()


def _issue(case):
    issue = getattr(_proof(), "issue_turn", None)
    if issue is None:
        pytest.fail("canary launch lacks owner-authenticated exact turn binding")
    return issue(case.coordinator, case.packet, case.seat, 1, case.route)


def test_same_turn_proof_reopens_original_bytes_with_every_external_action_forbidden(turn_case, monkeypatch):
    case = turn_case
    context = _issue(case)
    result = case.runtime.run_provider(replace(context.request, canary_capture=context), registry=case.registry)
    assert result.valid is True
    assert result.observed_model is None
    assert case.loads == ["fixture"]
    raw = result.stdout
    from shared.lib.fanout import providers, container_credentials, container_upstream

    def forbidden(*args, **kwargs):
        raise AssertionError("cold stored proof performed an external action")

    monkeypatch.setattr(providers, "run_command", forbidden)
    monkeypatch.setattr(case.registry, "_version_probe", forbidden)
    monkeypatch.setattr(container_credentials, "HostCredentialSource", forbidden)
    monkeypatch.setattr(container_upstream.HostUpstream, "__call__", forbidden)
    reopened = _proof().reopen_turn(case.coordinator, case.packet, case.seat, 1, result)
    assert reopened["response_model"] == MODEL
    assert reopened["native_observed_model"] is None
    assert result.stdout == raw
    assert len(reopened["wire_records"]) == 1


@pytest.mark.parametrize("changed", ["prompt", "cwd", "profile", "session", "prefix", "write", "v2", "retry"])
def test_canary_context_rejects_request_substitution_before_send(turn_case, changed):
    case = turn_case
    context = _issue(case)
    request = replace(context.request, canary_capture=context)
    changes = {
        "prompt": {"prompt": "substituted"}, "cwd": {"cwd": case.root},
        "profile": {"profile": replace(request.profile, requested_effort="low", profile_sha256=None)},
        "session": {"session_id": "other", "resume": True},
        "prefix": {"artifact_prefix": "other"}, "write": {"execution_class": "repo-write"},
        "v2": {"target_id": "other", "inputs_digest": "a" * 64}, "retry": {"retries": 1},
    }
    with pytest.raises((ValueError, case.runtime.ProviderRequestError)):
        case.runtime.run_provider(replace(request, **changes[changed]), registry=case.registry)
    assert case.loads == []
    assert case.made == []


def test_unsigned_pointer_and_native_byte_substitution_cannot_reopen(turn_case):
    case = turn_case
    context = _issue(case)
    result = case.runtime.run_provider(replace(context.request, canary_capture=context), registry=case.registry)
    changed = case.artifacts.write_bytes("substituted/stdout.bin", result.stdout + b"\n")
    with pytest.raises(ValueError):
        _proof().reopen_turn(case.coordinator, case.packet, case.seat, 1,
            replace(result, stdout_ref=changed, stdout_digest=changed.digest, stdout=result.stdout + b"\n"))
    pointer = case.artifacts.root / context.request.artifact_prefix / "codex-canary-proof.json"
    pointer.write_bytes(case.runtime.canonical_json({"evidence_name": "a" * 64 + ".json", "evidence_digest": "a" * 64}))
    with pytest.raises(ValueError):
        _proof().reopen_turn(case.coordinator, case.packet, case.seat, 1, result)


def test_production_synthetic_live_canary_requires_explicit_company_route_before_preflight(turn_case):
    case = turn_case

    def forbidden(*args):
        pytest.fail("preflight ran before the company route was admitted")

    with pytest.raises(case.smoke.SmokeError, match="explicit.*route"):
        case.smoke.prepare_live(case.root / "unadmitted", authorized=True,
            registry=case.registry, preflight=forbidden)


@pytest.mark.parametrize("changed", ["broker_argv", "route_profile", "permit_model", "source", "seat", "round", "body"])
def test_signed_but_misassociated_capture_cannot_satisfy_stored_proof(turn_case, changed):
    from shared.lib.fanout.controller import read_evidence, write_evidence
    case = turn_case
    context = _issue(case)
    result = case.runtime.run_provider(replace(context.request, canary_capture=context), registry=case.registry)
    pointer = json.loads((case.artifacts.root / context.request.artifact_prefix / "codex-canary-proof.json").read_bytes())
    proof = read_evidence(case.controller, "codex-readonly-canary", pointer["evidence_name"], pointer["evidence_digest"])
    authorization = read_evidence(case.controller, "codex-readonly-canary", proof["authorization_name"], proof["authorization_digest"])
    if changed == "broker_argv":
        proof["broker_argv"][0] = "/fixture/different-executable"
    elif changed == "permit_model":
        proof["permit"]["model"] = "other"
    elif changed == "body":
        proof["wire_records"][0]["response_body"] = asdict(case.artifacts.write_json("wrong-response.json", _response(id="resp_other")))
    else:
        if changed == "route_profile":
            authorization["route"]["profile"]["origin"] = "https://other.invalid"
        else:
            authorization["binding"][{"source": "source_sha256", "seat": "seat_id", "round": "round"}[changed]] = "changed"
        name, digest = write_evidence(case.controller, "codex-readonly-canary", authorization)
        proof["authorization_name"], proof["authorization_digest"] = name, digest
    name, digest = write_evidence(case.controller, "codex-readonly-canary", proof)
    path = case.artifacts.root / context.request.artifact_prefix / "codex-canary-proof.json"
    path.write_bytes(case.runtime.canonical_json({"evidence_name": name, "evidence_digest": digest}))
    with pytest.raises(ValueError):
        _proof().reopen_turn(case.coordinator, case.packet, case.seat, 1, result)
