"""Synthetic wire fixtures prove capture binding, never an externally served model."""
from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import os
import socket
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


@pytest.mark.parametrize("mime,failure", (
    ("text/event-stream", "digest"), ("text/event-stream", "settle"),
    ("text/event-stream", "committed-settle"), ("text/event-stream", "partial"),
    ("application/json", "digest"), ("application/json", "settle"),
    ("application/json", "committed-settle"),
))
def test_native_delivery_failure_emits_only_one_http_response(tmp_path, monkeypatch, mime, failure):
    from shared.lib.fanout.container_broker import _DeliveryBody
    controller = create_lifecycle_controller(tmp_path / "controller")
    ledger = PermitLedger(controller.root / "native-ledger", controller)
    route = _route("codex")
    permit = replace(_routed_permit(route, "/v1/responses"), max_response_bytes=4096)
    permit_id = ledger.register(permit, 1)
    completed = (_sse({"type": "response.completed", "response": _response()})
                 if mime == "text/event-stream" else json.dumps(_response()).encode())
    sent = []

    class Sender:
        def preview(self, method, path, headers, body):
            return PreparedOutbound(route.profile.origin, path, body, MODEL, route.digest)

        def __call__(self, *args):
            sent.append(args)
            def body():
                yield completed
                if failure == "partial":
                    raise RuntimeError("synthetic delivery failure")
            return BrokerResponse(200, {"content-type": mime},
                                  body() if mime == "text/event-stream" else completed)

    if failure == "digest":
        def digest(self):
            raise RuntimeError("synthetic digest failure")
        monkeypatch.setattr(_DeliveryBody, "digest", property(digest))
    elif failure in {"settle", "committed-settle"}:
        original = ledger.settle
        def settle(request_id, digest):
            if failure == "committed-settle":
                original(request_id, digest)
            raise RuntimeError("synthetic settlement failure")
        monkeypatch.setattr(ledger, "settle", settle)
    listener = BrokerListener(Broker(permit, credential="fixture-only", send=Sender()),
        ledger_binding=(ledger, permit_id, 1))
    with listener:
        body = json.dumps({"model": MODEL, "input": []}).encode()
        request = (f"POST /v1/responses HTTP/1.1\r\nHost: {listener.host}:{listener.port}\r\n"
                   f"Content-Length: {len(body)}\r\nContent-Type: application/json\r\n"
                   f"x-fanout-seat: {permit.placeholder_key}\r\n\r\n").encode() + body
        with socket.create_connection((listener.host, listener.port), timeout=3) as connection:
            connection.sendall(request)
            chunks = []
            while piece := connection.recv(4096):
                chunks.append(piece)
            raw = b"".join(chunks)
    assert raw.count(b"HTTP/1.1 ") == 1
    assert raw.startswith(b"HTTP/1.1 200 ")
    assert listener.native_failed is True and listener.native_deliveries == ()
    rows = ledger.request_records(permit_id, 1)
    assert len(rows) == 1
    assert rows[0]["state"] == ("settled" if failure == "committed-settle" else "uncertain")
    with pytest.raises(ValueError, match="fenced"):
        ledger.require_permit(permit_id, 1, permit)
    assert len(sent) == 1


@pytest.mark.parametrize("terminal", (False, True))
def test_complete_http_body_with_incomplete_sse_never_settles_or_proves_model(
    tmp_path, upstream_module, connection_factory, terminal
):
    controller = create_lifecycle_controller(tmp_path / "controller")
    ledger = PermitLedger(controller.root / "native-ledger", controller)
    route = _route("codex")
    permit = replace(_routed_permit(route, "/v1/responses"), max_response_bytes=4096)
    permit_id = ledger.register(permit, 1)
    made, response = connection_factory
    response.headers["content-type"] = "text/event-stream"
    prefix = _sse({"type": "response.created", "response": {
        "id": "resp_fixture", "status": "in_progress"}})
    tail = (_sse({"type": "response.completed", "response": _response()})[:-2]
            if terminal else b'data: [DONE]')
    response.chunks = [prefix, tail]
    response.length = 0
    captured = []
    sender = upstream_module.HostUpstream(permit, route, max_response_bytes=4096,
        deadline_seconds=2, success_sink=captured.append)
    listener = BrokerListener(Broker(permit, credential="fixture-only", send=sender),
        ledger_binding=(ledger, permit_id, 1))
    with listener:
        connection = http.client.HTTPConnection(listener.host, listener.port, timeout=3)
        connection.request("POST", "/v1/responses", json.dumps({"model": MODEL, "input": []}),
            {"content-type": "application/json", "x-fanout-seat": permit.placeholder_key})
        with pytest.raises(http.client.IncompleteRead):
            connection.getresponse().read()
        connection.close()
    assert captured == []
    assert len(made) == 1 and made[0].closed
    assert response.chunks == [] and response.length == 0
    assert listener.native_failed and listener.native_deliveries == ()
    assert ledger.request_records(permit_id, 1)[0]["state"] == "uncertain"
    with pytest.raises(ValueError, match="fenced"):
        ledger.require_permit(permit_id, 1, permit)


@pytest.mark.parametrize("value", (b'NaN', b'1e999', b'[' * 64 + b'0' + b']' * 64),
                         ids=("nan", "overflow", "over-depth"))
def test_native_invalid_request_has_no_reservation_preview_or_connection(tmp_path, value):
    controller = create_lifecycle_controller(tmp_path / "controller")
    ledger = PermitLedger(controller.root / "native-ledger", controller)
    route = _route("codex")
    permit = _routed_permit(route, "/v1/responses")
    permit_id = ledger.register(permit, 1)
    external = []
    class Sender:
        def preview(self, *args):
            external.append("preview")
            raise AssertionError("invalid request reached preview")
        def __call__(self, *args):
            external.append("send")
            raise AssertionError("invalid request connected")
    listener = BrokerListener(Broker(permit, credential="fixture-only", send=Sender()),
        ledger_binding=(ledger, permit_id, 1))
    with listener:
        connection = http.client.HTTPConnection(listener.host, listener.port, timeout=3)
        body = b'{"model":"gpt-6.1-sol","max_output_tokens":16,"input":' + value + b'}'
        connection.request("POST", "/v1/responses", body,
            {"content-type": "application/json", "x-fanout-seat": permit.placeholder_key})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.close()
    assert external == [] and ledger.request_records(permit_id, 1) == ()


def test_context_cannot_replay_a_settled_turn(turn_case):
    case = turn_case
    context = _issue(case)
    request = replace(context.request, canary_capture=context)
    result = case.runtime.run_provider(request, registry=case.registry)
    with pytest.raises(case.runtime.ProviderRequestError, match="consumed"):
        case.runtime.run_provider(request, registry=case.registry)
    assert len(case.made) == 1
    assert _proof().reopen_turn(case.coordinator, case.packet, case.seat, 1, result)["response_model"] == MODEL


@pytest.mark.parametrize("committed", (False, True))
def test_post_delivery_settlement_failure_cannot_issue_native_turn_proof(turn_case, monkeypatch, committed):
    case = turn_case
    original = PermitLedger.settle
    def settle(self, request_id, digest):
        if committed:
            original(self, request_id, digest)
        raise RuntimeError("synthetic settlement failure")
    monkeypatch.setattr(PermitLedger, "settle", settle)
    context = _issue(case)
    request = replace(context.request, canary_capture=context)
    with pytest.raises(case.runtime.ProviderRequestError, match="proof is incomplete"):
        case.runtime.run_provider(request, registry=case.registry)
    records = context._held["ledger"].request_records(context._held["permit_id"], 1)
    assert len(records) == 1
    assert records[0]["state"] == ("settled" if committed else "uncertain")
    assert context._held["listener"].native_failed
    assert context._held["listener"].native_deliveries == ()
    assert not (case.artifacts.root / context.request.artifact_prefix / "codex-canary-proof.json").exists()
    with pytest.raises(case.runtime.ProviderRequestError, match="consumed"):
        case.runtime.run_provider(request, registry=case.registry)
    assert len(case.made) == 1


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
    from shared.lib.fanout import agy_guard
    runtime = smoke.fanout
    root = tmp_path
    auth_home = root / "fixture-auth-home"
    adc = auth_home / ".config/gcloud/application_default_credentials.json"
    adc.parent.mkdir(parents=True)
    adc.write_text("synthetic fixture credential")
    adc.chmod(0o600)
    agy_binary = root / "fixture-agy"
    agy_binary.write_text("#!/bin/sh\nprintf '1.2.13\\n'\n")
    agy_binary.chmod(0o700)
    monkeypatch.setenv("HOME", str(auth_home))
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    monkeypatch.setattr(agy_guard, "_BINARY", agy_binary)
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
    from shared.lib.fanout import memory as memory_module
    executable = root / "memory_exchange.py"
    executable.write_bytes(b"#!/usr/bin/env python3\nraise SystemExit(99)\n")
    executable.chmod(0o700)
    saved = {}
    def memory_transport(command, **kwargs):
        request = json.loads(command.stdin)
        if request["operation"] == "save":
            payload = json.loads(request["text"])["payload"]
            checkpoint = runtime.Checkpoint.create(runtime.CheckpointIdentity.from_dict(payload["identity"]),
                                                   payload["answer"])
            assert (checkpoint.title, checkpoint.project, checkpoint.text) == (
                request["title"], request["project"], request["text"])
            identity = len(saved) + 41
            saved[identity] = checkpoint
            result = {"id": identity, "message": f"Memory saved as observation #{identity}",
                      "project": checkpoint.project, "success": True, "title": checkpoint.title}
        else:
            result = []
            for identity in request["ids"]:
                checkpoint = saved[identity]
                session = f"manual-{checkpoint.project}-claude"
                result.append({"id": identity, "memory_session_id": session,
                    "project": checkpoint.project, "title": checkpoint.title, "narrative": checkpoint.text,
                    "type": "discovery", "subtitle": "Manual memory", "facts": "[]", "concepts": "[]",
                    "files_read": "[]", "files_modified": "[]", "text": None,
                    "content_hash": hashlib.sha256(f"{session}\0{checkpoint.title}\0{checkpoint.text}".encode()).hexdigest()[:16],
                    "created_at": "2026-09-23T00:00:00.000Z", "created_at_epoch": 1795000000000 + identity,
                    "discovery_tokens": 0, "prompt_number": None, "relevance_count": 0,
                    "agent_id": None, "agent_type": None, "generated_by_model": None, "merged_into_project": None,
                    "metadata": None, "origin_device_id": None, "origin_local_id": None,
                    "sync_rev": "1", "synced_at": None})
        return runtime.ProcessResult(runtime.ProcessStatus.EXIT, 0, runtime.canonical_json({
            "ok": True, "operation": request["operation"], "result": result,
            "schema_version": memory_module.CONTROLLER_SCHEMA}), b"")
    monkeypatch.setattr(memory_module, "run_command", memory_transport)
    memory = runtime.MemoryCheckpointExchange(runtime.MemoryController(executable,
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest()), artifacts)
    coordinator = smoke._live_coordinator(root, artifacts, journal, owner, controller,
        baseline, registry, memory, runtime.run_provider)
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
        if rollout.exists():
            with rollout.open("a") as handle:
                handle.write(json.dumps({"type": "event_msg", "payload": {"round": 2}}) + "\n")
        else:
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
        route=route, loads=loads, response=response, made=made, seats=seats)
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
    assert reopened["schema_version"] == "fanout-codex-canary-proof-v2"
    snapshot = case.runtime.ArtifactRef(**reopened["session_state"]["snapshot"])
    original = case.artifacts.read_bytes(snapshot)
    authorization = _proof().read_evidence(case.controller, "codex-readonly-canary",
                                           context.evidence_name, context.evidence_digest)
    mutable = Path(authorization["private_home"]["path"]) / reopened["session_state"]["path"]
    mutable.write_bytes(original + b'{"type":"event_msg","payload":{}}\n')
    assert _proof().reopen_turn(case.coordinator, case.packet, case.seat, 1, result) == reopened
    mutable.unlink()
    assert _proof().reopen_turn(case.coordinator, case.packet, case.seat, 1, result) == reopened
    (case.artifacts.root / snapshot.path).unlink()
    with pytest.raises(ValueError):
        _proof().reopen_turn(case.coordinator, case.packet, case.seat, 1, result)
    (case.artifacts.root / snapshot.path).write_bytes(original + b"tamper")
    with pytest.raises(ValueError):
        _proof().reopen_turn(case.coordinator, case.packet, case.seat, 1, result)


def _first_barrier(case):
    agy = next(seat for seat in case.seats if seat.executor_id == "agy")
    guard = case.runtime.issue_agy_readonly_guard(case.controller, agy.workspace_verification,
        case.registry.select("agy", "read-only", "standard"))
    case.seats = tuple(replace(seat, agy_guard=guard) if seat.executor_id == "agy" else seat
                       for seat in case.seats)
    def run(request, *, registry):
        if request.executor_id == "codex":
            context = _issue(case)
            return case.runtime.run_provider(replace(request, canary_capture=context), registry=registry)
        stdout = case.runtime.canonical_json({"fixture": request.executor_id})
        answer = "fixture " + request.executor_id
        prefix = request.artifact_prefix
        return case.runtime.ProviderResult(request.executor_id, True, "ok", None, 1, .01, None,
            request.session_id or "fixture-" + request.executor_id, answer, stdout, b"",
            answer_ref=case.artifacts.write_bytes(prefix + "/answer.txt", answer.encode()),
            stdout_ref=case.artifacts.write_bytes(prefix + "/stdout.bin", stdout),
            stderr_ref=case.artifacts.write_bytes(prefix + "/stderr.bin", b""),
            answer_digest=hashlib.sha256(answer.encode()).hexdigest(),
            stdout_digest=hashlib.sha256(stdout).hexdigest(), stderr_digest=hashlib.sha256(b"").hexdigest(),
            requested_model=request.profile.requested_model)
    case.coordinator.provider_runner = run
    policy = case.runtime.RoundPolicy.from_provider_policy(case.packet.provider_policy)
    first = case.coordinator.execute_round(case.packet, case.seats, policy, round=1)
    assert first.status == "round-complete" and len(first.valid_terminals) == 3
    for terminal in first.valid_terminals:
        checkpoint = case.coordinator._checkpoint_from_barrier(first, terminal)
        receipt = case.coordinator._receipt_for(first, terminal)
        case.coordinator.memory._verify_receipt_artifacts(checkpoint, receipt)
    prior = next(item for item in first.valid_terminals if item.executor_id == "codex")
    peer = case.coordinator.build_peer_packet(case.packet, first, target_seat_id=case.seat.seat_id)
    return first, prior, peer


def test_round2_cold_proof_authenticates_persisted_predecessor_snapshot_and_fingerprint(turn_case, monkeypatch):
    case = turn_case
    first, prior, peer = _first_barrier(case)
    initial = _proof().reopen_turn(case.coordinator, case.packet, case.seat, 1, prior)
    context = _proof().issue_turn(case.coordinator, case.packet, case.seat, 2, case.route, prior=prior, peer=peer)
    authorization = _proof().read_evidence(case.controller, "codex-readonly-canary",
        context.evidence_name, context.evidence_digest)
    mutable = Path(authorization["private_home"]["path"]) / initial["session_state"]["path"]
    original_mutable = mutable.read_bytes()
    mutable.write_bytes(original_mutable + b"changed-before-launch\n")
    with pytest.raises(case.runtime.ProviderRequestError, match="initial session bytes"):
        context.validate(replace(context.request, canary_capture=context))
    mutable.write_bytes(original_mutable)
    case.response.chunks = [json.dumps(_response()).encode()]
    result = case.runtime.run_provider(replace(context.request, canary_capture=context), registry=case.registry)
    from shared.lib.fanout import providers, container_credentials, container_upstream
    def forbidden(*args, **kwargs):
        pytest.fail("cold predecessor proof performed an external action")
    monkeypatch.setattr(providers, "run_command", forbidden)
    monkeypatch.setattr(case.registry, "_version_probe", forbidden)
    monkeypatch.setattr(container_credentials, "HostCredentialSource", forbidden)
    monkeypatch.setattr(container_upstream.HostUpstream, "__call__", forbidden)
    proof = _proof().reopen_turn(case.coordinator, case.packet, case.seat, 2, result, prior=prior, peer=peer)
    original_bytes = case.artifacts.read_bytes(case.runtime.ArtifactRef(**initial["session_state"]["snapshot"]))
    resumed_bytes = case.artifacts.read_bytes(case.runtime.ArtifactRef(**proof["session_state"]["snapshot"]))
    assert resumed_bytes.startswith(original_bytes) and len(resumed_bytes) > len(original_bytes)
    for changed in ("fingerprint", "predecessor", "home", "snapshot", "session", "path", "route", "size", "snapshot-type", "v1"):
        mutated = json.loads(json.dumps(proof))
        if changed == "fingerprint":
            mutated["credential_source_fingerprint"] = "d" * 64
        elif changed == "snapshot":
            mutated["session_state"]["snapshot"] = initial["session_state"]["snapshot"]
        elif changed == "session":
            mutated["native"]["session_id"] = "another-session"
        elif changed == "path":
            mutated["session_state"]["path"] = "../outside.jsonl"
        elif changed == "size":
            mutated["session_state"]["size"] = True
        elif changed == "snapshot-type":
            mutated["session_state"]["snapshot"] = []
        elif changed == "v1":
            mutated["schema_version"] = "fanout-codex-canary-proof-v1"
        else:
            authorization = _proof().read_evidence(case.controller, "codex-readonly-canary",
                proof["authorization_name"], proof["authorization_digest"])
            if changed == "route":
                authorization["route"]["base_url"] = "https://unrelated.invalid"
                authorization["route"]["digest"] = _proof()._sha(case.runtime.canonical_json(
                    {key: value for key, value in authorization["route"].items() if key != "digest"}))
            elif changed == "predecessor":
                authorization["initial_authorization_digest"] = "0" * 64
            else:
                authorization["private_home"]["inode"] += 1
            mutated["authorization_name"], mutated["authorization_digest"] = _proof().write_evidence(
                case.controller, "codex-readonly-canary", authorization)
        name, digest = _proof().write_evidence(case.controller, "codex-readonly-canary", mutated)
        pointer = case.artifacts.root / context.request.artifact_prefix / "codex-canary-proof.json"
        pointer.write_bytes(case.runtime.canonical_json({"evidence_name": name, "evidence_digest": digest}))
        with pytest.raises(ValueError):
            _proof().reopen_turn(case.coordinator, case.packet, case.seat, 2, result, prior=prior, peer=peer)


@pytest.mark.parametrize("kind", ["candidates", "entries", "oversized"])
def test_session_discovery_bounds_all_candidates_and_traversal(tmp_path, monkeypatch, kind):
    home = tmp_path / "home"
    root = home / ".codex/sessions/2026/10/03"
    root.mkdir(parents=True)
    home.chmod(0o700)
    (root / "original.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {"id": "original"}}) + "\n")
    if kind == "candidates":
        for index in range(8):
            (root / f"other-{index}.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {"id": f"other-{index}"}}) + "\n")
    elif kind == "entries":
        for index in range(70):
            (root / f"ordinary-{index}.txt").write_bytes(b"")
    else:
        with (root / "original.jsonl").open("ab") as handle:
            handle.truncate(8 * 1024 * 1024 + 1)
    real_open = os.open
    candidates = []
    def observe(path, *args, **kwargs):
        if str(path).endswith(".jsonl"):
            candidates.append(path)
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(_proof().os, "open", observe)
    with pytest.raises(ValueError):
        _proof()._session_capture(home, "original")
    assert len(candidates) <= 4


def test_session_discovery_accepts_normal_dated_hierarchy_and_checks_all_candidate_bytes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / ".codex/sessions/2026/10/03"
    root.mkdir(parents=True)
    home.chmod(0o700)
    raw = (json.dumps({"type": "session_meta", "payload": {"id": "exact"}}) + "\n").encode()
    (root / "original.jsonl").write_bytes(raw)
    descriptor, captured = _proof()._session_capture(home, "exact")
    assert captured == raw
    assert descriptor == {"path": ".codex/sessions/2026/10/03/original.jsonl",
                          "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
    # Four bounded reads imply the fixed 32 MiB aggregate maximum, including nonmatches.
    for index in range(3):
        (root / f"other-{index}.jsonl").write_bytes(raw.replace(b"exact", b"other"))
    for path in root.iterdir():
        with path.open("ab") as handle:
            handle.truncate(8 * 1024 * 1024)
    read = _proof()._read_session
    sizes = []
    def observe(*args):
        result = read(*args)
        sizes.append(len(result))
        return result
    monkeypatch.setattr(_proof(), "_read_session", observe)
    assert _proof()._session_capture(home, "exact")[1].startswith(raw)
    assert len(sizes) == 4 and sum(sizes) == 32 * 1024 * 1024


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
